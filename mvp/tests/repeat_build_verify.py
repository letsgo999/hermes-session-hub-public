from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


MVP = Path(__file__).resolve().parents[1]
if str(MVP) not in sys.path:
    sys.path.insert(0, str(MVP))

from build import FILES_TO_COPY  # noqa: E402


EVIDENCE_PATH = MVP / "evidence" / "build-repeat-10.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_dist() -> dict[str, Any]:
    dist = MVP / "dist"
    manifest_path = dist / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest["files"]
    listed = {row["path"] for row in rows}
    actual = {
        path.relative_to(dist).as_posix()
        for path in dist.rglob("*")
        if path.is_file() and path != manifest_path
    }
    rows_verified = True
    for row in rows:
        target = dist / row["path"]
        if not target.is_file():
            rows_verified = False
            continue
        data = target.read_bytes()
        if len(data) != row["bytes"] or _sha256_bytes(data) != row["sha256"]:
            rows_verified = False

    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["path"]):
        digest.update(row["path"].encode("utf-8"))
        digest.update(row["sha256"].encode("ascii"))
    source_dist_equality = all(
        (MVP / source_name).read_bytes() == (dist / target_name).read_bytes()
        for source_name, target_name in FILES_TO_COPY
    )
    return {
        "listed": len(listed),
        "actual": len(actual),
        "missing": len(listed - actual),
        "unmanifested": len(actual - listed),
        "rowsVerified": rows_verified,
        "totalBytesVerified": sum(row["bytes"] for row in rows) == manifest["totalBytes"],
        "ledgerSha256Verified": digest.hexdigest() == manifest["sha256"],
        "sourceDistEquality": source_dist_equality,
        "totalBytes": manifest["totalBytes"],
        "ledgerSha256": manifest["sha256"],
        "manifestFileSha256": _sha256_bytes(manifest_path.read_bytes()),
    }


def main() -> int:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for number in range(1, 11):
        completed = subprocess.run(
            [sys.executable, str(MVP / "build.py")],
            cwd=MVP,
            capture_output=True,
            timeout=120,
            check=False,
        )
        try:
            verification = _verify_dist()
        except Exception as error:
            verification = {
                "listed": 0,
                "actual": 0,
                "missing": -1,
                "unmanifested": -1,
                "rowsVerified": False,
                "totalBytesVerified": False,
                "ledgerSha256Verified": False,
                "sourceDistEquality": False,
                "verificationErrorType": type(error).__name__,
            }
        run = {
            "run": number,
            "exit": completed.returncode,
            "stdoutBytes": len(completed.stdout),
            "stderrBytes": len(completed.stderr),
            "stdoutSha256": _sha256_bytes(completed.stdout),
            "stderrSha256": _sha256_bytes(completed.stderr),
            **verification,
        }
        if completed.returncode != 0:
            stderr_text = completed.stderr.decode("utf-8", errors="replace")
            operation_match = re.search(r"RuntimeError: ([A-Za-z0-9_. -]+) failed after \d+ attempts", stderr_text)
            run["failureOperation"] = operation_match.group(1) if operation_match else "unclassified"
        runs.append(run)
        if completed.returncode != 0:
            break

    all_passed = len(runs) == 10 and all(
        run["exit"] == 0
        and run["listed"] == 8
        and run["actual"] == 8
        and run["missing"] == 0
        and run["unmanifested"] == 0
        and run["rowsVerified"] is True
        and run["totalBytesVerified"] is True
        and run["ledgerSha256Verified"] is True
        and run["sourceDistEquality"] is True
        for run in runs
    )
    result = {
        "contract": "session-hub-windows-build-repeat-v1; aggregate-only; no payload paths or contents",
        "platform": platform.system(),
        "requestedRuns": 10,
        "completedRuns": len(runs),
        "allPassed": all_passed,
        "rollbackSnapshotPresent": (MVP / ".dist-previous").is_dir(),
        "runs": runs,
    }
    EVIDENCE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
