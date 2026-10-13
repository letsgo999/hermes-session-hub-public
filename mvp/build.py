from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any


FILES_TO_COPY = (
    ("plugins/session-hub/plugin.yaml", "plugins/session-hub/plugin.yaml"),
    ("plugins/session-hub/__init__.py", "plugins/session-hub/__init__.py"),
    ("plugins/session-hub/dashboard/manifest.json", "plugins/session-hub/dashboard/manifest.json"),
    ("plugins/session-hub/dashboard/plugin_api.py", "plugins/session-hub/dashboard/plugin_api.py"),
    ("plugins/session-hub/dashboard/skills_index.py", "plugins/session-hub/dashboard/skills_index.py"),
    ("plugins/session-hub/dashboard/external_ai_status.py", "plugins/session-hub/dashboard/external_ai_status.py"),
    ("plugins/session-hub/dashboard/ai_sessions.py", "plugins/session-hub/dashboard/ai_sessions.py"),
    ("plugins/session-hub/dashboard/project_hub.py", "plugins/session-hub/dashboard/project_hub.py"),
    ("plugins/session-hub/dashboard/sessionhub_backend.py", "plugins/session-hub/dashboard/sessionhub_backend.py"),
    ("plugins/session-hub/dashboard/hub_config.py", "plugins/session-hub/dashboard/hub_config.py"),
    ("plugins/session-hub/desktop/plugin.js", "plugins/session-hub/desktop/plugin.js"),
    ("INSTALLABLE_README.md", "README.md"),
    ("installer/install.ps1", "install.ps1"),
    ("installer/uninstall.ps1", "uninstall.ps1"),
    ("installer/session-hub.config.example.json", "session-hub.config.example.json"),
)

RETRY_DELAYS_SECONDS = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8)
_TEST_FAULT_ATTEMPTS: dict[str, int] = {}


def build_dist(mvp_root: Path) -> dict[str, Any]:
    root = mvp_root.resolve(strict=True)
    dist = root / "dist"
    staging = root / ".dist-staging"
    _validate_required_sources(root)
    _recover_interrupted_build(root, dist)
    staging.mkdir(parents=False, exist_ok=False)
    try:
        manifest = _build_candidate(root, staging)
        _verify_complete_dist(staging, manifest)
        _publish_candidate(root, dist, staging, manifest)
        return manifest
    except BaseException:
        if staging.exists():
            _remove_tree_with_retry(staging)
        raise


def _build_candidate(root: Path, staging: Path) -> dict[str, Any]:
    copied: list[dict[str, Any]] = []
    for source_name, target_name in FILES_TO_COPY:
        source = (root / source_name).resolve(strict=True)
        target = staging / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(_file_record(staging, target))

    copied.sort(key=lambda row: row["path"])
    total = sum(item["bytes"] for item in copied)
    digest = _ledger_digest(copied)
    manifest = {
        "contract": "session-hub-dist-v2",
        "files": copied,
        "totalBytes": total,
        "sha256": digest,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _publish_candidate(root: Path, dist: Path, staging: Path, manifest: dict[str, Any]) -> None:
    previous = root / ".dist-previous"
    had_dist = dist.exists()
    if had_dist:
        _validate_dist_target(root, dist)
        if _candidate_matches_dist(staging, dist, manifest):
            _remove_tree_with_retry(staging)
            return
        _replace_with_retry(dist, previous, operation="backup")
    try:
        _replace_with_retry(staging, dist, operation="promote")
        _verify_complete_dist(dist, manifest)
    except BaseException:
        if had_dist and previous.exists():
            if dist.exists():
                _replace_with_retry(dist, staging, operation="quarantine")
            _replace_with_retry(previous, dist, operation="rollback")
        raise


def _candidate_matches_dist(staging: Path, dist: Path, manifest: dict[str, Any]) -> bool:
    try:
        _verify_complete_dist(dist, manifest)
        return (dist / "manifest.json").read_bytes() == (staging / "manifest.json").read_bytes()
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return False


def _validate_required_sources(root: Path) -> None:
    missing = []
    for source_name, target_name in FILES_TO_COPY:
        source = root / source_name
        target = Path(target_name)
        if target.is_absolute() or ".." in target.parts:
            raise RuntimeError(f"unsafe dist target: {target_name}")
        if not source.is_file():
            missing.append(source_name)
        elif not source.resolve().is_relative_to(root):
            raise RuntimeError(f"source escaped project: {source_name}")
    if missing:
        raise FileNotFoundError("missing required build source(s): " + ", ".join(sorted(missing)))


def _validate_dist_target(root: Path, dist: Path) -> None:
    if dist.is_symlink() or not dist.is_dir():
        raise RuntimeError("dist must be a real project directory")
    resolved = dist.resolve()
    if resolved.parent != root or resolved.name != "dist":
        raise RuntimeError("dist target escaped project")


def _recover_interrupted_build(root: Path, dist: Path) -> None:
    staging = root / ".dist-staging"
    previous = root / ".dist-previous"
    if not dist.exists() and previous.is_dir():
        _replace_with_retry(previous, dist, operation="recover")
    if dist.exists() and previous.exists():
        _remove_tree_with_retry(previous)
    if staging.exists():
        _remove_tree_with_retry(staging)


def _remove_tree_with_retry(path: Path) -> None:
    _retry_windows(lambda: _remove_tree_once(path), operation=f"cleanup {path.name}")


def _remove_tree_once(path: Path) -> None:
    _inject_test_fault("cleanup")
    shutil.rmtree(path, onerror=_remove_readonly)


def _replace_with_retry(source: Path, target: Path, *, operation: str) -> None:
    _retry_windows(lambda: _replace_once(source, target, operation), operation=operation)


def _replace_once(source: Path, target: Path, operation: str) -> None:
    _inject_test_fault(operation)
    os.replace(source, target)


def _retry_windows(action, *, operation: str) -> None:
    last_error: OSError | None = None
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        if delay:
            time.sleep(delay)
        try:
            action()
            if attempt > 1:
                print(f"{operation} recovered after {attempt} attempts", file=sys.stderr)
            return
        except OSError as error:
            if not _is_transient_windows_error(error):
                raise
            last_error = error
    assert last_error is not None
    raise RuntimeError(f"{operation} failed after {len(RETRY_DELAYS_SECONDS)} attempts: {last_error}") from last_error


def _is_transient_windows_error(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {32, 33, 145} or error.errno in {
        errno.EACCES,
        errno.EBUSY,
        errno.ENOTEMPTY,
        errno.EPERM,
        145,
    }


def _inject_test_fault(point: str) -> None:
    specification = os.environ.get("SESSION_HUB_BUILD_TEST_FAULT", "")
    fault_point, separator, count_text = specification.partition(":")
    if fault_point != point:
        return
    if separator:
        fail_count = int(count_text)
        attempts = _TEST_FAULT_ATTEMPTS.get(point, 0)
        if attempts >= fail_count:
            return
        _TEST_FAULT_ATTEMPTS[point] = attempts + 1
    error = OSError(errno.ENOTEMPTY, f"injected {point} failure")
    error.winerror = 145
    raise error


def _verify_complete_dist(dist: Path, manifest: dict[str, Any]) -> None:
    listed = {item["path"] for item in manifest["files"]}
    actual = {
        path.relative_to(dist).as_posix()
        for path in dist.rglob("*")
        if path.is_file() and path != dist / "manifest.json"
    }
    if actual != listed:
        raise RuntimeError("dist file set does not exactly match manifest")
    for item in manifest["files"]:
        target = dist / item["path"]
        current = _file_record(dist, target)
        if current != item:
            raise RuntimeError(f"dist verification failed: {item['path']}")
    if sum(item["bytes"] for item in manifest["files"]) != manifest["totalBytes"]:
        raise RuntimeError("dist totalBytes does not match manifest rows")
    if _ledger_digest(sorted(manifest["files"], key=lambda row: row["path"])) != manifest["sha256"]:
        raise RuntimeError("dist ledger SHA-256 does not match manifest rows")


def _ledger_digest(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in rows:
        digest.update(item["path"].encode("utf-8"))
        digest.update(item["sha256"].encode("ascii"))
    return digest.hexdigest()


def _remove_readonly(function, path: str, _error_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def write_evidence_manifest(evidence_dir: Path) -> dict[str, Any]:
    root = evidence_dir.resolve()
    files = [
        _file_record(root, path)
        for path in sorted(root.glob("*"))
        if path.is_file() and path.name != "manifest.json"
    ]
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(item["sha256"].encode("ascii"))
    manifest = {"contract": "session-hub-evidence-v2", "files": files, "sha256": digest.hexdigest()}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _file_record(root: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


if __name__ == "__main__":
    print(json.dumps(build_dist(Path(__file__).resolve().parent), ensure_ascii=False, indent=2))
