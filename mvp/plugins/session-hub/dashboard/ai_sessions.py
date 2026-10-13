"""Codex / Claude desktop session collector and reader for the Session Hub "코덱스·클로드" tab.

The collector only READS local app data and writes one sanitized cache file
(%LOCALAPPDATA%/hermes/session-hub/ai-sessions.json). Per session it keeps a short title,
the first line of the first request, up to three "conclusion" lines from the last assistant
answer, status, folder, model and timestamps. Full transcripts, system prompts, account
fields and credentials are never copied.

Cloud (remote) sessions leave no transcript on this PC, so they write one small summary report
(contract "ai-session-report-v1") into a synced folder at the end of a task. The collector reads that
folder as a fourth source; its location comes from HERMES_SESSION_HUB_REPORTS or "reportsDir" in
%LOCALAPPDATA%/hermes/session-hub/ai-sessions.config.json.

Run standalone (scheduled task):  python ai_sessions.py --collect
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import glob
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

CONTRACT = "ai-sessions-v1"
# Bump when summary extraction changes so cached conclusions are rebuilt.
SUMMARY_VERSION = "2"
SOURCE_LABELS = {
    "codex": "코덱스", "claude-desktop": "클로드 데스크톱", "claude-code": "Claude Code",
    "codex-cloud": "코덱스(원격)", "claude-cloud": "클로드(원격)", "reports": "원격 요약",
}
REPORT_CONTRACT = "ai-session-report-v1"
REPORT_AGENTS = {"codex": "codex-cloud", "claude": "claude-cloud"}
REPORT_STATUSES = {"완료", "진행 중", "중단", "대기", "실패", "알 수 없음"}
REPORT_MAX_BYTES = 64 * 1024
MAX_ITEMS = 300
MAX_AGE_DAYS = 120
TITLE_MAX = 60
LINE_MAX = 120
FIRST_MAX = 140
TAIL_STEPS = (512 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s+|\d+[.)]\s+)")


class AISessionsError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- text helpers
def clean_line(text: Any, limit: int) -> str:
    line = _CONTROL_RE.sub(" ", str(text or ""))
    line = _LINK_RE.sub(r"\1", line)
    line = _MD_PREFIX_RE.sub("", line)
    line = line.replace("**", "").replace("__", "").replace("`", "")
    line = _SECRET_RE.sub("[가림]", line)
    line = _EMAIL_RE.sub("[메일]", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def first_line(text: Any, limit: int) -> str:
    for raw in str(text or "").splitlines():
        if raw.strip().startswith(("# Files mentioned", "## ", "<")) and "My request" not in raw:
            continue
        cleaned = clean_line(raw, limit)
        if cleaned and not re.fullmatch(r"[-=_*#|:\s]+", cleaned):
            return cleaned
    return ""


def request_line(text: Any, limit: int) -> str:
    """Codex wraps attachments as '# Files mentioned ... ## My request:\\n<text>'."""
    raw = str(text or "")
    marker = raw.find("## My request:")
    if marker >= 0:
        raw = raw[marker + len("## My request:"):]
    return first_line(raw, limit)


_TAG_ONLY_RE = re.compile(r"^</?[\w:.-]+[^>]*>(?:[^<]*</[\w:.-]+>)?$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}")


def is_automation_heartbeat(text: Any) -> bool:
    head = str(text or "").lstrip()[:200]
    return head.startswith("<heartbeat") or "<automation_id>" in head


def conclusion_lines(text: Any, count: int = 3) -> list[str]:
    lines: list[str] = []
    in_code = False
    raw_lines = str(text or "").splitlines()
    header_rows = {index - 1 for index, raw in enumerate(raw_lines) if index and _TABLE_SEPARATOR_RE.match(raw.strip())}
    for index, raw in enumerate(raw_lines):
        stripped = raw.strip()
        if index in header_rows or _TAG_ONLY_RE.match(stripped):
            continue
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith("#") or re.fullmatch(r"[-=_*#|:\s]+", stripped):
            continue
        if stripped.startswith("|"):
            stripped = " · ".join(cell.strip() for cell in stripped.strip("|").split("|") if cell.strip())
        cleaned = clean_line(stripped, LINE_MAX)
        if cleaned:
            lines.append(cleaned)
        if len(lines) >= count:
            break
    return lines


def short_folder(path: Any) -> str:
    value = str(path or "").replace("\\\\?\\", "").replace("\\", "/").rstrip("/")
    if value.startswith("//?/"):
        value = value[4:]
    parts = [part for part in value.split("/") if part]
    return "/".join(parts[-2:]) if parts else ""


def iso_from_ms(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number > 10_000_000_000:
        number /= 1000.0
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_mtime(path: Path) -> str:
    try:
        return iso_from_ms(path.stat().st_mtime)
    except OSError:
        return ""


def iter_tail_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSONL records from the end of a (possibly huge) file, newest first."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    seen_from = size
    for step in TAIL_STEPS:
        start = max(0, size - step)
        if start >= seen_from:
            continue
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                chunk = stream.read(seen_from - start)
        except OSError:
            return
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8", errors="ignore"))
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record
        seen_from = start
        if start == 0:
            return


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------- codex
@contextlib.contextmanager
def _sqlite_ro(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _codex_rollout_summary(path: Path) -> tuple[str, list[str]]:
    """Latest turn state plus the most recent completed answer (even while a newer turn runs).

    Codex automations post heartbeat turns into existing threads; those are skipped so the card shows
    the last real answer instead of '<heartbeat>' markup.
    """
    status = ""
    heartbeat_seen = False
    for record in iter_tail_records(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        kind = payload.get("type")
        if record.get("type") != "event_msg" or kind not in ("task_complete", "task_started", "turn_aborted"):
            continue
        if kind == "task_started":
            status = status or "진행 중"
            continue
        if kind == "turn_aborted":
            status = status or "중단"
            continue
        message = payload.get("last_agent_message")
        if is_automation_heartbeat(message):
            heartbeat_seen = True
            status = status or "완료"
            continue
        return (status or "완료"), conclusion_lines(message)
    if heartbeat_seen:
        return (status or "완료"), ["최근 기록은 자동화 점검(heartbeat)뿐이라 표시할 결론이 없습니다."]
    return (status or "알 수 없음"), []


def collect_codex(codex_home: Path, cache: dict[str, dict[str, Any]], cutoff_ms: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    db_files = sorted(codex_home.glob("state_*.sqlite"))
    if not db_files:
        return [], {"ok": False, "count": 0, "error": "codex_state_missing"}
    db = db_files[-1]
    items: list[dict[str, Any]] = []
    with _sqlite_ro(db) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info('threads')")}
        if not {"id", "updated_at_ms", "rollout_path"}.issubset(columns):
            return [], {"ok": False, "count": 0, "error": "codex_schema_unsupported"}
        name_col = "name" if "name" in columns else "NULL"
        source_filter = "AND (thread_source IS NULL OR thread_source = 'user')" if "thread_source" in columns else ""
        rows = conn.execute(
            f"SELECT id, {name_col} AS name, title, first_user_message, cwd, archived, rollout_path, model, "
            f"created_at_ms, updated_at_ms FROM threads WHERE updated_at_ms >= ? {source_filter} "
            "ORDER BY updated_at_ms DESC LIMIT ?",
            (cutoff_ms, MAX_ITEMS),
        ).fetchall()
    for row in rows:
        rollout = Path(str(row["rollout_path"] or "").replace("\\\\?\\", "")) if row["rollout_path"] else None
        key = f"codex:{row['id']}"
        status, conclusion = "알 수 없음", []
        if rollout is not None:
            try:
                stat = rollout.stat()
                stamp = f"{SUMMARY_VERSION}:{stat.st_size}:{int(stat.st_mtime)}"
            except OSError:
                stamp = ""
            cached = cache.get(key)
            if stamp and cached and cached.get("_stamp") == stamp:
                status, conclusion = cached.get("status", status), cached.get("conclusion", [])
            elif stamp:
                status, conclusion = _codex_rollout_summary(rollout)
        else:
            stamp = ""
        items.append({
            "id": key,
            "source": "codex",
            "title": clean_line(row["name"], TITLE_MAX) or request_line(row["title"], TITLE_MAX),
            "firstMessage": request_line(row["first_user_message"] or row["title"], FIRST_MAX),
            "conclusion": conclusion,
            "status": status,
            "folder": short_folder(row["cwd"]),
            "model": clean_line(row["model"], 40),
            "archived": bool(row["archived"]),
            "createdAt": iso_from_ms(row["created_at_ms"]),
            "updatedAt": iso_from_ms(row["updated_at_ms"]),
            "_stamp": stamp,
        })
    return items, {"ok": True, "count": len(items), "error": None}


# ---------------------------------------------------------------- claude transcripts (desktop + code)
def _claude_transcript_summary(path: Path) -> tuple[str, list[str], str]:
    """Return (status, conclusion lines, last timestamp) from the end of a Claude Code JSONL transcript."""
    last_type = None
    last_ts = ""
    for record in iter_tail_records(path):
        kind = record.get("type")
        if kind not in ("user", "assistant") or record.get("isSidechain"):
            continue
        last_ts = last_ts or str(record.get("timestamp") or "")
        if last_type is None:
            last_type = kind
        if kind == "assistant":
            text = message_text((record.get("message") or {}).get("content"))
            if text.strip():
                return ("완료" if last_type == "assistant" else "진행 중"), conclusion_lines(text), last_ts
    return ("진행 중" if last_type == "user" else "알 수 없음"), [], last_ts


def _claude_first_request(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            for index, raw in enumerate(stream):
                if index > 400:
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if record.get("type") == "user" and not record.get("isSidechain"):
                    text = message_text((record.get("message") or {}).get("content"))
                    line = first_line(text, FIRST_MAX)
                    if line:
                        return line
    except OSError:
        pass
    return ""


def collect_claude_desktop(appdata_claude: Path, cache: dict[str, dict[str, Any]], cutoff_ms: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = appdata_claude / "local-agent-mode-sessions"
    if not base.is_dir():
        return [], {"ok": False, "count": 0, "error": "claude_desktop_missing"}
    items: list[dict[str, Any]] = []
    for meta_path in glob.glob(str(base / "*" / "*" / "local_*.json")):
        meta_file = Path(meta_path)
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        last_ms = meta.get("lastActivityAt") or meta.get("createdAt") or 0
        if not isinstance(last_ms, (int, float)) or last_ms < cutoff_ms:
            continue
        session_id = str(meta.get("sessionId") or meta_file.stem)
        key = f"claude-desktop:{session_id}"
        folder_root = meta_file.with_suffix("")
        transcripts = sorted(
            glob.glob(str(folder_root / ".claude" / "projects" / "*" / f"{meta.get('cliSessionId', '*')}.jsonl")),
            key=os.path.getmtime, reverse=True,
        )
        status, conclusion = "알 수 없음", []
        stamp = ""
        if transcripts:
            transcript = Path(transcripts[0])
            stat = transcript.stat()
            stamp = f"{SUMMARY_VERSION}:{stat.st_size}:{int(stat.st_mtime)}"
            cached = cache.get(key)
            if cached and cached.get("_stamp") == stamp:
                status, conclusion = cached.get("status", status), cached.get("conclusion", [])
            else:
                status, conclusion, _ = _claude_transcript_summary(transcript)
        folders = meta.get("userSelectedFolders") if isinstance(meta.get("userSelectedFolders"), list) else []
        items.append({
            "id": key,
            "source": "claude-desktop",
            "title": clean_line(meta.get("title"), TITLE_MAX) or first_line(meta.get("initialMessage"), TITLE_MAX),
            "firstMessage": first_line(meta.get("initialMessage"), FIRST_MAX),
            "conclusion": conclusion,
            "status": status,
            "folder": short_folder(folders[0]) if folders else "",
            "model": clean_line(meta.get("model"), 40),
            "archived": bool(meta.get("isArchived")),
            "createdAt": iso_from_ms(meta.get("createdAt")),
            "updatedAt": iso_from_ms(last_ms),
            "_stamp": stamp,
        })
    return items, {"ok": True, "count": len(items), "error": None}


def collect_claude_code(claude_home: Path, cache: dict[str, dict[str, Any]], cutoff_ms: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = claude_home / "projects"
    if not base.is_dir():
        return [], {"ok": False, "count": 0, "error": "claude_code_missing"}
    items: list[dict[str, Any]] = []
    for transcript_path in glob.glob(str(base / "*" / "*.jsonl")):
        transcript = Path(transcript_path)
        try:
            stat = transcript.stat()
        except OSError:
            continue
        if stat.st_mtime * 1000 < cutoff_ms:
            continue
        key = f"claude-code:{transcript.stem}"
        stamp = f"{SUMMARY_VERSION}:{stat.st_size}:{int(stat.st_mtime)}"
        cached = cache.get(key)
        if cached and cached.get("_stamp") == stamp:
            items.append(dict(cached))
            continue
        status, conclusion, last_ts = _claude_transcript_summary(transcript)
        first = _claude_first_request(transcript)
        items.append({
            "id": key,
            "source": "claude-code",
            "title": clean_line(first, TITLE_MAX),
            "firstMessage": first,
            "conclusion": conclusion,
            "status": status,
            "folder": short_folder(transcript.parent.name.replace("-", "/")),
            "model": "",
            "archived": False,
            "createdAt": "",
            "updatedAt": last_ts or iso_mtime(transcript),
            "_stamp": stamp,
        })
    return items, {"ok": True, "count": len(items), "error": None}


# ---------------------------------------------------------------- remote summary reports (cloud sessions)
def _report_time(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return iso_from_ms(value)
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def collect_reports(reports_dir: Path, cache: dict[str, dict[str, Any]], cutoff_ms: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read cloud-session summary reports. Every field is re-sanitized; unknown fields are dropped."""
    if not reports_dir.is_dir():
        return [], {"ok": False, "count": 0, "error": "reports_dir_missing"}
    items: dict[str, dict[str, Any]] = {}
    skipped = 0
    for report_path in sorted(reports_dir.glob("*.json")):
        try:
            stat = report_path.stat()
            if stat.st_size > REPORT_MAX_BYTES or stat.st_mtime * 1000 < cutoff_ms:
                skipped += stat.st_size > REPORT_MAX_BYTES
                continue
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(report, dict) or report.get("contract") != REPORT_CONTRACT:
            skipped += 1
            continue
        source = REPORT_AGENTS.get(str(report.get("agent") or "").lower())
        session_id = re.sub(r"[^A-Za-z0-9_.:-]", "", str(report.get("sessionId") or ""))[:120]
        if not source or not session_id:
            skipped += 1
            continue
        conclusion_raw = report.get("conclusion")
        if isinstance(conclusion_raw, str):
            conclusion_raw = conclusion_raw.splitlines()
        conclusion = [clean_line(line, LINE_MAX) for line in (conclusion_raw or []) if isinstance(line, str)]
        conclusion = [line for line in conclusion if line][:3]
        status = clean_line(report.get("status"), 12)
        updated = _report_time(report.get("updatedAt")) or iso_mtime(report_path)
        key = f"{source}:{session_id}"
        item = {
            "id": key,
            "source": source,
            "title": clean_line(report.get("title"), TITLE_MAX) or first_line(report.get("request"), TITLE_MAX),
            "firstMessage": first_line(report.get("request"), FIRST_MAX),
            "conclusion": conclusion,
            "status": status if status in REPORT_STATUSES else "알 수 없음",
            "folder": clean_line(report.get("project"), 60),
            "model": clean_line(report.get("model"), 40),
            "archived": bool(report.get("archived")),
            "createdAt": _report_time(report.get("createdAt")),
            "updatedAt": updated,
            "_stamp": f"{SUMMARY_VERSION}:{stat.st_size}:{int(stat.st_mtime)}",
        }
        previous = items.get(key)
        if previous is None or (item["updatedAt"] or "") >= (previous["updatedAt"] or ""):
            items[key] = item
    info: dict[str, Any] = {"ok": True, "count": len(items), "error": None}
    if skipped:
        info["skipped"] = skipped
    return list(items.values()), info


# ---------------------------------------------------------------- collect + cache
def _hermes_home() -> Path | None:
    """Same rule as hub_config.resolve_hermes_home (HERMES_HOME, else %LOCALAPPDATA%\\hermes)."""
    value = os.environ.get("HERMES_HOME")
    if value:
        home = Path(value).expanduser()
        base = home.parent.parent
        if home.parent.name.casefold() == "profiles" and ((base / "config.yaml").is_file() or (base / "state.db").is_file()):
            return base
        return home
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "hermes" if local else None


def default_cache_path() -> Path:
    override = os.environ.get("HERMES_SESSION_HUB_AI_SESSIONS")
    if override:
        return Path(override)
    home = _hermes_home()
    if home is None:
        raise AISessionsError("cache_unavailable")
    return home / "session-hub" / "ai-sessions.json"


def default_config_path() -> Path | None:
    home = _hermes_home()
    return home / "session-hub" / "ai-sessions.config.json" if home else None


def reports_dir_from_config(config_path: Path | None = None) -> Path | None:
    override = os.environ.get("HERMES_SESSION_HUB_REPORTS")
    if override:
        return Path(override)
    config_path = config_path or default_config_path()
    if config_path is None:
        return None
    config = _load_cache(config_path)
    value = config.get("reportsDir") if isinstance(config, dict) else None
    return Path(value) if isinstance(value, str) and value.strip() else None


def default_sources() -> dict[str, Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    sources = {
        "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex"),
        "claude-desktop": appdata / "Claude",
        "claude-code": home / ".claude",
    }
    reports = reports_dir_from_config()
    if reports is not None:
        sources["reports"] = reports
    return sources


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def collect(cache_path: Path | None = None, sources: dict[str, Path] | None = None, *, now: float | None = None) -> dict[str, Any]:
    cache_path = cache_path or default_cache_path()
    sources = sources or default_sources()
    now = time.time() if now is None else now
    cutoff_ms = (now - MAX_AGE_DAYS * 86400) * 1000
    previous = {item["id"]: item for item in _load_cache(cache_path).get("items", []) if isinstance(item, dict) and "id" in item}
    collectors = {
        "codex": collect_codex, "claude-desktop": collect_claude_desktop, "claude-code": collect_claude_code,
        "reports": collect_reports,
    }
    items: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    for name, fn in collectors.items():
        root = sources.get(name)
        if root is None:
            continue
        try:
            found, info = fn(Path(root), previous, cutoff_ms)
        except (OSError, sqlite3.Error) as exc:
            found, info = [], {"ok": False, "count": 0, "error": type(exc).__name__}
        items.extend(found)
        status[name] = info
    items.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    payload = {
        "contract": CONTRACT,
        "collectedAt": datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": status,
        "items": items[:MAX_ITEMS],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ai-sessions-", suffix=".json", dir=str(cache_path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(tmp, cache_path)
    return payload


class AISessionsReader:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = Path(cache_path)

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
            raise AISessionsError("bad_cursor") from exc
        if value < 0:
            raise AISessionsError("bad_cursor")
        return value

    def list_sessions(self, *, query: str = "", sources: list[str] | None = None, include_archived: bool = False,
                      page_size: int | str = 20, cursor: str | None = None) -> dict[str, Any]:
        if page_size == "all":
            limit = 100
        else:
            try:
                limit = int(page_size)
            except (TypeError, ValueError) as exc:
                raise AISessionsError("bad_page_size") from exc
            if limit not in {10, 20, 50, 100}:
                raise AISessionsError("bad_page_size")
        data = _load_cache(self.cache_path)
        items = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in data.get("items", []) if isinstance(item, dict)
        ]
        wanted = {str(value) for value in sources or [] if str(value)}
        if wanted:
            def matches(source: str) -> bool:
                return (
                    source in wanted
                    or ("claude" in wanted and source.startswith("claude"))
                    or ("codex" in wanted and source.startswith("codex"))
                    or ("remote" in wanted and source.endswith("-cloud"))
                )
            items = [item for item in items if matches(str(item.get("source", "")))]
        if not include_archived:
            items = [item for item in items if not item.get("archived")]
        needle = str(query or "").strip().casefold()
        if needle:
            items = [item for item in items if needle in " ".join(
                [str(item.get("title", "")), str(item.get("firstMessage", "")), str(item.get("folder", ""))]
                + [str(line) for line in item.get("conclusion", [])]
            ).casefold()]
        offset = self._offset(cursor)
        page = items[offset: offset + limit]
        next_offset = offset + len(page)
        return {
            "contract": CONTRACT,
            "readonly": True,
            "collectedAt": data.get("collectedAt"),
            "sources": data.get("sources", {}),
            "items": page,
            "page": {"returned": len(page), "totalKnown": len(items)},
            "nextCursor": self._cursor(next_offset) if next_offset < len(items) else None,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Codex/Claude desktop sessions for Session Hub")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--hermes-home", help="Hermes home folder (default: HERMES_HOME, then %%LOCALAPPDATA%%\\hermes)")
    args = parser.parse_args(argv)
    if args.hermes_home:
        os.environ["HERMES_HOME"] = args.hermes_home
    if not args.collect:
        parser.print_help()
        return 2
    result = collect(Path(args.output) if args.output else None)
    print(json.dumps({"collectedAt": result["collectedAt"], "sources": result["sources"], "items": len(result["items"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
