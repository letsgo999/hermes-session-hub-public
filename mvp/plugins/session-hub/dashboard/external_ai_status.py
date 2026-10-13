from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ExternalAIStatusError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


TITLE_MAX_CHARS = 48
LINE_MAX_CHARS = 110
READ_LIMIT_BYTES = 4096
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKDOWN_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s+|\d+[.)]\s+)")
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

STATUS_LABELS = {
    "RECEIVED": "접수", "TRIAGED": "분류됨", "SANITIZED": "정제됨", "ASSIGNED": "배정됨",
    "RUNNING": "실행 중", "CANDIDATE_READY": "후보 준비", "REVIEWING": "검증 중",
    "APPROVAL_REQUESTED": "승인 대기", "APPROVED": "승인됨", "PROMOTED": "반영 완료",
    "COMPLETED": "완료", "DONE": "완료", "REJECTED": "반려", "CHANGES_REQUESTED": "수정 요청",
    "FAILED": "실패", "CANCELLED": "취소", "EXPIRED": "만료",
}
STATUS_SENTENCES = {
    "APPROVAL_REQUESTED": "격리 후보를 만들고 검증까지 마쳐 승인을 기다리는 중입니다.",
    "FAILED": "실행 중 실패해 결과 후보가 확정되지 않았습니다.",
    "RUNNING": "외부 AI가 작업을 실행하는 중입니다.",
    "ASSIGNED": "실행기에 배정되어 시작을 기다리는 중입니다.",
    "CANDIDATE_READY": "결과 후보가 만들어져 검증을 앞두고 있습니다.",
    "REVIEWING": "결과 후보를 검증하는 중입니다.",
    "APPROVED": "승인되어 정본 반영을 기다리는 중입니다.",
    "PROMOTED": "승인 후 정본에 반영까지 끝났습니다.",
    "COMPLETED": "작업이 끝났습니다.",
    "DONE": "작업이 끝났습니다.",
    "REJECTED": "반려되어 반영하지 않았습니다.",
    "CHANGES_REQUESTED": "수정 요청을 받아 재작업이 필요합니다.",
    "CANCELLED": "작업이 취소되었습니다.",
}
VERIFICATION_LABELS = {"pass": "통과", "passed": "통과", "fail": "실패", "failed": "실패", "pending": "대기", "skipped": "생략"}


def _clean_line(text: str, limit: int) -> str:
    line = _CONTROL_RE.sub(" ", text)
    line = _MARKDOWN_PREFIX_RE.sub("", line)
    line = _SECRET_RE.sub("[가림]", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _first_line(path: Path, job_dir: Path, limit: int) -> str:
    """First meaningful line of a small text file inside this job folder (read-only, size-capped)."""
    try:
        resolved = path.resolve()
        if job_dir not in resolved.parents or not resolved.is_file():
            return ""
        with resolved.open("rb") as stream:
            raw = stream.read(READ_LIMIT_BYTES)
    except OSError:
        return ""
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        cleaned = _clean_line(line, limit)
        if cleaned and not re.fullmatch(r"[-=_*#\s]+", cleaned):
            return cleaned
    return ""


def _approval_expiry(job_dir: Path) -> str:
    """Expiry timestamp only (never the nonce or approval text)."""
    target = job_dir / "approval" / "target.json"
    try:
        resolved = target.resolve()
        if job_dir in resolved.parents and resolved.is_file():
            value = json.loads(resolved.read_text(encoding="utf-8")).get("expires_at")
            if isinstance(value, str):
                return value
    except (OSError, ValueError):
        pass
    request = job_dir / "approval" / "request.txt"
    try:
        resolved = request.resolve()
        if job_dir in resolved.parents and resolved.is_file():
            with resolved.open("rb") as stream:
                text = stream.read(READ_LIMIT_BYTES).decode("utf-8", errors="ignore")
            match = re.search(r"승인 만료:\s*(\S+)", text)
            if match:
                return match.group(1)
    except OSError:
        pass
    return ""


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _kst_text(value: datetime) -> str:
    from datetime import timedelta
    return (value.astimezone(timezone(timedelta(hours=9)))).strftime("%Y-%m-%d %H:%M")


def _summary_lines(item: dict[str, Any], *, result: str, verification: str, expiry: str, now: datetime) -> list[str]:
    status = item["status"]
    first = STATUS_SENTENCES.get(status, f"현재 상태: {STATUS_LABELS.get(status, status)}")
    if status == "APPROVAL_REQUESTED":
        expires = _parse_time(expiry)
        if expires and expires < now:
            first = f"검증까지 마쳤지만 승인 기한({_kst_text(expires)})이 지나 승인되지 않은 채 남아 있습니다."
    scope = item.get("targetScope") or []
    target = f"대상: {scope[0]}" + (f" 외 {len(scope) - 1}개" if len(scope) > 1 else "") if scope else "대상: 지정 없음"
    second = target + (f" · 결과: {result}" if result else "")
    verification_state = VERIFICATION_LABELS.get(item["verificationState"], item["verificationState"])
    third = f"검증: {verification}" if verification else f"검증: {verification_state}"
    if item.get("retriedBy"):
        third += f" · 같은 요청을 {item['retriedBy']}로 다시 실행"
    elif status == "APPROVAL_REQUESTED" and not first.startswith("검증까지 마쳤지만"):
        third += " · 다음: 승인/반려/수정요청 결정 필요"
    return [_clean_line(first, LINE_MAX_CHARS), _clean_line(second, LINE_MAX_CHARS), _clean_line(third, LINE_MAX_CHARS)]


class ExternalAIStatusReader:
    """Read the per-job metadata plus a short, sanitized title/summary.

    Only the first line of request/request.md, output/result.md and review/verification.md and the
    approval expiry timestamp are read (size-capped, secrets masked). Audit logs, approval nonces,
    candidate files and full request/result bodies are never returned.
    """

    def __init__(self, jobs_root: str | Path) -> None:
        self.jobs_root = Path(jobs_root).resolve()

    @staticmethod
    def _cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")

    @staticmethod
    def _offset(cursor: Any) -> int:
        if cursor in (None, ""):
            return 0
        try:
            raw = str(cursor)
            raw += "=" * (-len(raw) % 4)
            value = int(base64.urlsafe_b64decode(raw.encode("ascii")).decode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ExternalAIStatusError("bad_cursor") from exc
        if value < 0:
            raise ExternalAIStatusError("bad_cursor")
        return value

    def _items(self) -> list[dict[str, Any]]:
        if not self.jobs_root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        extras: dict[str, dict[str, str]] = {}
        for path in self.jobs_root.glob("EA-*/job.yaml"):
            resolved = path.resolve()
            if self.jobs_root not in resolved.parents:
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(data.get("job_id", ""))
            if path.parent.name != job_id or not job_id.startswith("EA-"):
                continue
            job_dir = resolved.parent
            metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
            approval = data.get("approval") if isinstance(data.get("approval"), dict) else {}
            verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
            scope = data.get("target_scope") if isinstance(data.get("target_scope"), list) else []
            request_line = _first_line(job_dir / "request" / "request.md", job_dir, 400)
            items.append({
                "id": job_id,
                "title": _clean_line(request_line, TITLE_MAX_CHARS).rstrip(".") if request_line else "",
                "kind": str(data.get("kind", "unknown")),
                "status": str(data.get("status", "unknown")),
                "node": str(data.get("node", "unknown")),
                "executor": str(data.get("executor", "unknown")),
                "risk": str(data.get("risk", "unknown")),
                "createdAt": str(data.get("created_at", "")),
                "updatedAt": str(data.get("updated_at", "")),
                "tokens": metrics.get("tokens"),
                "costUsd": metrics.get("cost_usd"),
                "attempts": metrics.get("attempts", 0),
                "approvalState": str(approval.get("state", "unknown")),
                "verificationState": str(verification.get("state", "unknown")),
                "artifactSha256": str(data.get("artifact_sha256", "")),
                "targetScope": [_clean_line(str(value), 80) for value in scope[:20]],
                "retriedBy": "",
                "summaryLines": [],
                "_request": request_line,
            })
            extras[job_id] = {
                "result": _first_line(job_dir / "output" / "result.md", job_dir, 60),
                "verification": _first_line(job_dir / "review" / "verification.md", job_dir, 80),
                "expiry": _approval_expiry(job_dir),
            }
        # A later job with the same request text is treated as a re-run of an earlier one.
        by_request: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if item["_request"]:
                by_request.setdefault(item["_request"], []).append(item)
        for group in by_request.values():
            group.sort(key=lambda entry: (entry["createdAt"], entry["id"]))
            for earlier, later in zip(group, group[1:]):
                if earlier["status"] in {"FAILED", "CANCELLED", "REJECTED", "CHANGES_REQUESTED"}:
                    earlier["retriedBy"] = later["id"]
        now = datetime.now(timezone.utc)
        for item in items:
            extra = extras[item["id"]]
            item["summaryLines"] = _summary_lines(
                item, result=extra["result"], verification=extra["verification"], expiry=extra["expiry"], now=now
            )
            item.pop("_request", None)
        return sorted(items, key=lambda item: (item["updatedAt"], item["id"]), reverse=True)

    def list_jobs(
        self,
        *,
        query: str = "",
        statuses: list[str] | None = None,
        page_size: int | str = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if page_size == "all":
            limit = 100
        else:
            try:
                limit = int(page_size)
            except (TypeError, ValueError) as exc:
                raise ExternalAIStatusError("bad_page_size") from exc
            if limit not in {10, 20, 50, 100}:
                raise ExternalAIStatusError("bad_page_size")
        needle = query.strip().casefold()
        allowed = {str(value) for value in statuses or [] if str(value)}
        items = self._items()
        if needle:
            items = [item for item in items if needle in " ".join(str(value) for value in item.values()).casefold()]
        if allowed:
            items = [item for item in items if item["status"] in allowed]
        offset = self._offset(cursor)
        page = items[offset: offset + limit]
        next_offset = offset + len(page)
        return {
            "contract": "external-ai-status-v1",
            "readonly": True,
            "items": page,
            "page": {"returned": len(page), "totalKnown": len(items)},
            "nextCursor": self._cursor(next_offset) if next_offset < len(items) else None,
        }
