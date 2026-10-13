from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote


def _load_hub_config_module():
    import importlib.util
    import sys

    path = Path(__file__).with_name("hub_config.py").resolve()
    module_name = "hermes_session_hub_config"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Session Hub config module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


hub_config = _load_hub_config_module()

# Local ("HQ") profiles live on this PC and are auto-detected from the Hermes home folder
# (default + profiles/<name>/state.db) unless session-hub.config.json lists them explicitly.
# Optional branch instances ship a trimmed copy of their own default-profile state.db + kanban.db
# to <syncRoot>/snapshots/<ID>/ (tools/session_snapshot.py, contract v1); each has a freshness TTL.
# These module globals are (re)bound by apply_profile_settings(); read them at call time.
LOCAL_INSTANCE_ID = "HQ"
LOCAL_PROFILE_NAMES: tuple[str, ...] = ("default",)
PROFILE_NAMES: tuple[str, ...] = LOCAL_PROFILE_NAMES  # backwards-compatible alias
REMOTE_INSTANCES: dict[str, dict[str, Any]] = {}
REMOTE_PROFILE_NAMES: tuple[str, ...] = ()
ALL_PROFILE_NAMES: tuple[str, ...] = LOCAL_PROFILE_NAMES
LOCAL_INSTANCE_LABEL = hub_config.DEFAULT_LOCAL_LABEL


def apply_profile_settings(
    local_profiles: Iterable[str],
    remote_instances: Mapping[str, Mapping[str, Any]] | None = None,
    local_label: str | None = None,
) -> None:
    """Rebind the profile allowlists (call once at load, or from tests)."""
    global LOCAL_PROFILE_NAMES, PROFILE_NAMES, REMOTE_INSTANCES, REMOTE_PROFILE_NAMES, ALL_PROFILE_NAMES, LOCAL_INSTANCE_LABEL
    names = tuple(dict.fromkeys(str(name) for name in local_profiles)) or ("default",)
    LOCAL_PROFILE_NAMES = names
    PROFILE_NAMES = names
    REMOTE_INSTANCES = {str(key): dict(value) for key, value in (remote_instances or {}).items()}
    REMOTE_PROFILE_NAMES = tuple(f"{instance}/default" for instance in REMOTE_INSTANCES)
    ALL_PROFILE_NAMES = LOCAL_PROFILE_NAMES + REMOTE_PROFILE_NAMES
    LOCAL_INSTANCE_LABEL = local_label or hub_config.DEFAULT_LOCAL_LABEL


def apply_hub_settings(settings: Any) -> None:
    apply_profile_settings(settings.local_profiles, settings.remote_instances, settings.local_label)


apply_hub_settings(hub_config.current_settings())
SNAPSHOT_CONTRACT = "session-hub-branch-snapshot-v1"
SNAPSHOT_STATE_FILE = "state_snapshot.sqlite3"
SNAPSHOT_KANBAN_FILE = "kanban_snapshot.sqlite3"
SNAPSHOT_MANIFEST_FILE = "manifest.json"
INSTANCE_STATES = ("local", "fresh", "stale", "unknown", "damaged", "unsupported")
ALLOWED_SOURCES = ("desktop", "telegram")
KANBAN_STATUSES = (
    "triage", "todo", "ready", "running", "review", "review-pending",
    "blocked", "done", "archived", "cancelled",
)
PAGE_SIZE_CHOICES = (10, 20, 50, 100, "all")
DEFAULT_PAGE_SIZE = 20
ALL_WINDOW_ROWS = 100
MIN_SCHEMA_VERSION = 26
# Highest state.db schema_version verified against this plugin (Hermes 0.21.6~0.21.7 = 31, 2026-10-13).
# Newer versions are NOT rejected any more: Hermes bumps schema_version on most updates, so a hard upper
# bound broke the hub on every upgrade (stale_schema). Newer schemas are accepted when the required
# columns are still present (see _validate_schema) and a one-time warning is logged.
MAX_SCHEMA_VERSION = 31
SUPPORTED_SCHEMA_VERSIONS = tuple(range(MIN_SCHEMA_VERSION, MAX_SCHEMA_VERSION + 1))  # verified range
_LOG = logging.getLogger(__name__)
_WARNED_NEWER_SCHEMAS: set[int] = set()


def schema_version_supported(version: Any) -> bool:
    """Accept any int schema_version >= MIN_SCHEMA_VERSION (forward-compatible)."""
    if isinstance(version, bool) or not isinstance(version, int) or version < MIN_SCHEMA_VERSION:
        return False
    if version > MAX_SCHEMA_VERSION and version not in _WARNED_NEWER_SCHEMAS:
        _WARNED_NEWER_SCHEMAS.add(version)
        _LOG.warning(
            "session-hub: state.db schema_version %s is newer than verified %s; "
            "continuing with column checks", version, MAX_SCHEMA_VERSION,
        )
    return True
MAX_PREVIEW_CHARS = 160
MAX_QUERY_CHARS = 120
MAX_DISPLAY_TITLE_CHARS = 120
TELEGRAM_TITLE_CHARS = 40
KST = timezone(timedelta(hours=9), name="KST")
PREVIOUS_SEGMENT_SUFFIX = " · 이전 구간"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
BOARD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
WRITE_AUTHORIZER_ACTIONS = frozenset({
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
})
READ_AUTHORIZER_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", -999),
})
READONLY_PRAGMA_ALLOWLIST = frozenset({"query_only", "busy_timeout", "table_info"})


class SessionHubError(RuntimeError):
    """A sanitized, fail-closed error suitable for UI display."""

    def __init__(self, code: str, message: str = "Session Hub request rejected") -> None:
        super().__init__(message)
        self.code = code


# Sessions the Desktop may open in place. Kanban worker runs record their transcript with
# source='kanban' (and sub-agent runs with 'subagent'); those are not listed in the Session
# tabs, but an exact Kanban-card link must still be able to open them (D1, 2026-09-22).
OPENABLE_SOURCES = frozenset({"desktop", "telegram", "kanban", "subagent", "cron"})


def is_openable_source(source: str | None) -> bool:
    return (source or "").strip().lower() in OPENABLE_SOURCES


def profile_instance(profile: str) -> str:
    """'A/default' -> 'A'; any local profile -> 'HQ'."""
    if profile in REMOTE_PROFILE_NAMES:
        return profile.split("/", 1)[0]
    return LOCAL_INSTANCE_ID


def is_remote_profile(profile: str) -> bool:
    return profile in REMOTE_PROFILE_NAMES


def _parse_iso8601(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotCache:
    """Local, verified copies of branch snapshots.

    Drive's sync client may hold or half-write ``state_snapshot.sqlite3`` while HQ reads it, so the
    snapshot is copied to a local cache directory first and only swapped in once its sha256 matches
    the manifest. A mismatch keeps the previous verified copy and reports ``damaged``; a snapshot
    older than the instance TTL reports ``stale``; nothing on disk reports ``unknown``. Never fakes
    an empty instance as "0 sessions" (decision 2026-09-21).
    """

    def __init__(self, snapshot_dirs: Mapping[str, str | Path], cache_dir: str | Path | None, *, now: Any = None) -> None:
        unknown = set(snapshot_dirs) - set(REMOTE_INSTANCES)
        if unknown:
            raise SessionHubError("bad_instance")
        self._dirs = {instance: Path(path) for instance, path in snapshot_dirs.items()}
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._now = now or time.time
        self._status: dict[str, dict[str, Any]] = {}
        self._paths: dict[str, dict[str, Path | None]] = {}

    @property
    def instances(self) -> tuple[str, ...]:
        return tuple(self._dirs)

    def state_db(self, instance: str) -> Path | None:
        self._ensure(instance)
        return self._paths[instance]["state"]

    def kanban_db(self, instance: str) -> Path | None:
        self._ensure(instance)
        return self._paths[instance]["kanban"]

    def status(self, instance: str) -> dict[str, Any]:
        self._ensure(instance)
        return dict(self._status[instance])

    def statuses(self) -> list[dict[str, Any]]:
        return [self.status(instance) for instance in self._dirs]

    def _ensure(self, instance: str) -> None:
        if instance in self._status:
            return
        if instance not in self._dirs:
            raise SessionHubError("bad_instance")
        self._status[instance], self._paths[instance] = self._resolve(instance)

    def _base_status(self, instance: str, state: str, reason: str | None) -> dict[str, Any]:
        meta = REMOTE_INSTANCES[instance]
        return {
            "id": instance, "profile": f"{instance}/default", "label": meta["label"], "kind": "remote",
            "state": state, "reason": reason, "exportedAt": None, "checkedAt": None, "ageSeconds": None,
            "ttlSeconds": meta["ttlSeconds"], "sessions": None, "messages": None, "schemaVersion": None,
        }

    def _resolve(self, instance: str) -> tuple[dict[str, Any], dict[str, Path | None]]:
        folder = self._dirs[instance]
        none: dict[str, Path | None] = {"state": None, "kanban": None}
        manifest_path = folder / SNAPSHOT_MANIFEST_FILE
        if not manifest_path.is_file():
            return self._base_status(instance, "unknown", "no_snapshot"), none
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._base_status(instance, "damaged", "manifest_unreadable"), none
        if not isinstance(manifest, dict) or manifest.get("contract") != SNAPSHOT_CONTRACT:
            return self._base_status(instance, "unsupported", "contract_mismatch"), none
        status = self._base_status(instance, "unknown", None)
        state_meta = manifest.get("state") if isinstance(manifest.get("state"), dict) else {}
        kanban_meta = manifest.get("kanban") if isinstance(manifest.get("kanban"), dict) else None
        exported_at = _parse_iso8601(manifest.get("exported_at"))
        checked_at = _parse_iso8601(manifest.get("checked_at")) or exported_at
        status.update({
            "exportedAt": exported_at, "checkedAt": checked_at,
            "sessions": state_meta.get("sessions") if isinstance(state_meta.get("sessions"), int) else None,
            "messages": state_meta.get("messages") if isinstance(state_meta.get("messages"), int) else None,
            "schemaVersion": state_meta.get("schema_version") if isinstance(state_meta.get("schema_version"), int) else None,
        })
        schema_version = status["schemaVersion"]
        if schema_version is not None and not schema_version_supported(schema_version):
            status.update(state="unsupported", reason="schema_%s" % schema_version)
            return status, none
        state_path, state_verified = self._verified_copy(
            instance, folder, state_meta.get("file") or SNAPSHOT_STATE_FILE, state_meta.get("sha256"), "state"
        )
        if state_path is None:
            status.update(state="damaged", reason="state_sha_mismatch")
            return status, none
        kanban_path = None
        if kanban_meta:
            kanban_path, _ = self._verified_copy(
                instance, folder, kanban_meta.get("file") or SNAPSHOT_KANBAN_FILE, kanban_meta.get("sha256"), "kanban"
            )
        reference = checked_at or exported_at
        if reference is None:
            status.update(state="unknown", reason="no_timestamp")
        elif not state_verified:
            # The manifest is newer than the file Drive has delivered so far: serve the previous verified copy.
            status.update(state="stale", reason="sync_in_progress")
        else:
            age = max(0, int(self._now()) - reference)
            status["ageSeconds"] = age
            fresh = age <= status["ttlSeconds"]
            status.update(state="fresh" if fresh else "stale", reason=None if fresh else "ttl_exceeded")
        return status, {"state": state_path, "kanban": kanban_path}

    def _verified_copy(
        self, instance: str, folder: Path, file_name: str, expected_sha: Any, kind: str
    ) -> tuple[Path | None, bool]:
        """Return (usable path, verified). ``verified`` is False when only an older cached copy is usable."""
        if not isinstance(file_name, str) or "/" in file_name or "\\" in file_name or not file_name:
            return None, False
        source = folder / file_name
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            return None, False
        if self._cache_dir is None:
            # No cache directory configured: verify in place (tests, or a local sync root).
            try:
                ok = source.is_file() and _sha256_file(source) == expected_sha
            except OSError:
                ok = False
            return (source, True) if ok else (None, False)
        target_dir = self._cache_dir / instance
        target = target_dir / f"{kind}.sqlite3"
        marker = target_dir / f"{kind}.sha256"
        try:
            if target.is_file() and marker.is_file() and marker.read_text(encoding="ascii").strip() == expected_sha:
                return target, True
        except OSError:
            pass
        previous = (target, False) if target.is_file() else (None, False)
        try:
            if not source.is_file():
                return previous
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = target_dir / f"{kind}.sqlite3.tmp"
            shutil.copyfile(source, tmp)
            if _sha256_file(tmp) != expected_sha:
                tmp.unlink(missing_ok=True)
                return previous
            os.replace(tmp, target)
            marker.write_text(expected_sha, encoding="ascii")
            return target, True
        except OSError:
            return previous


def classify_session(source: str | None, chat_type: str | None, has_thread: bool) -> str | None:
    normalized_source = (source or "").strip().lower()
    normalized_chat = (chat_type or "").strip().lower()
    if normalized_source == "desktop":
        return "desktop"
    if normalized_source == "telegram":
        if normalized_chat == "dm":
            return "telegram_dm"
        if has_thread:
            return "telegram_topic"
        return "telegram_group"
    return None


_REDACTION_PATTERNS = (
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "[redacted-email]"),
    (re.compile(r"\b(?:\+?\d[\d -]{8,}\d)\b"), "[redacted-phone]"),
    (re.compile(r"\b(?:TOKEN|SECRET|KEY|PWD|PASSWORD)[A-Z0-9_:=.-]{6,}\b", re.I), "[redacted-token]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[redacted-token]"),
    (re.compile(r"\b[A-Za-z]:\\[^\s]+"), "[redacted-path]"),
    (re.compile(r"/(?:Users|home|var|tmp|mnt|Volumes)/[^\s]+"), "[redacted-path]"),
)

_INTERNAL_DISPLAY_KINDS = frozenset({
    "failed_turn", "hidden", "internal", "internal-notice", "internal_notice", "steer",
})
_SYNTHETIC_MESSAGE_PREFIXES = (
    "[CONTEXT COMPACTION", "[PRIOR CONTEXT", "[SYSTEM CONTEXT", "[INTERNAL CONTEXT",
)


def _redacted_normalized_text(value: str | None) -> str:
    text = " ".join((value or "").split())
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _is_synthetic_message(value: str | None) -> bool:
    normalized = " ".join((value or "").split()).upper()
    return any(normalized.startswith(prefix) for prefix in _SYNTHETIC_MESSAGE_PREFIXES)


def redact_text(value: str | None, *, limit: int = MAX_PREVIEW_CHARS) -> str:
    text = _redacted_normalized_text(value)
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _encode_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "lastActivity": item["lastActivity"],
            "ownerProfile": item["ownerProfile"],
            "storedSessionId": item["storedSessionId"],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        last_activity = payload["lastActivity"]
        owner_profile = payload["ownerProfile"]
        stored_session_id = payload["storedSessionId"]
    except Exception as exc:  # noqa: BLE001 - sanitize malformed cursor details
        raise SessionHubError("bad_cursor") from exc
    if payload.get("v") != 1:
        raise SessionHubError("bad_cursor")
    if isinstance(last_activity, bool) or not isinstance(last_activity, int):
        raise SessionHubError("bad_cursor")
    if owner_profile not in ALL_PROFILE_NAMES:
        raise SessionHubError("bad_cursor")
    if not isinstance(stored_session_id, str) or not (1 <= len(stored_session_id) <= 1024):
        raise SessionHubError("bad_cursor")
    return last_activity, owner_profile, stored_session_id


def _encode_telegram_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "kind": "telegram-turn",
            "lastActivity": item["lastActivity"],
            "ownerProfile": item["ownerProfile"],
            "userMessageId": item["userMessageId"],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_telegram_cursor(cursor: str | None) -> tuple[int | float, str, int] | None:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        last_activity = payload["lastActivity"]
        owner_profile = payload["ownerProfile"]
        user_message_id = payload["userMessageId"]
    except Exception as exc:  # noqa: BLE001 - sanitize malformed cursor details
        raise SessionHubError("bad_cursor") from exc
    if payload.get("v") != 1 or payload.get("kind") != "telegram-turn":
        raise SessionHubError("bad_cursor")
    if (
        isinstance(last_activity, bool)
        or not isinstance(last_activity, (int, float))
        or not math.isfinite(last_activity)
    ):
        raise SessionHubError("bad_cursor")
    if owner_profile not in ALL_PROFILE_NAMES:
        raise SessionHubError("bad_cursor")
    if isinstance(user_message_id, bool) or not isinstance(user_message_id, int) or user_message_id < 1:
        raise SessionHubError("bad_cursor")
    return last_activity, owner_profile, user_message_id


def _encode_kanban_cursor(item: dict[str, Any]) -> str:
    payload = json.dumps(
        {"v": 1, "updatedAt": item["updatedAt"], "board": item["board"], "id": item["id"]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_kanban_cursor(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        updated_at = payload["updatedAt"]
        board = payload["board"]
        task_id = payload["id"]
    except Exception as exc:  # noqa: BLE001 - malformed input is sanitized
        raise SessionHubError("bad_cursor") from exc
    if payload.get("v") != 1 or isinstance(updated_at, bool) or not isinstance(updated_at, int):
        raise SessionHubError("bad_cursor")
    if not isinstance(board, str) or not BOARD_NAME_RE.fullmatch(board):
        raise SessionHubError("bad_cursor")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise SessionHubError("bad_cursor")
    return updated_at, board, task_id


def _sqlite_ro_uri(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise SessionHubError("db_unavailable")
    return f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"


def _authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None) -> int:
    if action in READ_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in READONLY_PRAGMA_ALLOWLIST:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# Request-level errors always fail the whole request.
_REQUEST_ERRORS = frozenset({"bad_profile", "bad_tab", "bad_telegram_type", "bad_page_size", "bad_cursor"})
# A local profile may be skipped (and reported in skippedProfiles) only for "this database is not
# usable" errors. Anything that suggests the read-only guard failed keeps failing closed.
_SKIPPABLE_LOCAL_ERRORS = frozenset({"db_unavailable", "stale_schema", "unexpected_schema", "db_read_failed", "telegram_turns_unsupported"})


def _skippable_profile_error(profile: str, exc: SessionHubError) -> bool:
    if exc.code in _REQUEST_ERRORS:
        return False
    return is_remote_profile(profile) or exc.code in _SKIPPABLE_LOCAL_ERRORS


def _raise_if_every_local_profile_failed(selected: list[str], skipped: list[dict[str, str]]) -> None:
    """If no selected local profile could be read at all, fail with the first local error."""
    local = [profile for profile in selected if not is_remote_profile(profile)]
    failed = [row for row in skipped if not is_remote_profile(row["profile"])]
    if local and len(failed) == len(local):
        raise SessionHubError(failed[0]["error"])


class SessionHubBackend:
    def __init__(
        self,
        db_by_profile: Mapping[str, str | Path],
        snapshots: SnapshotCache | None = None,
    ) -> None:
        missing = set(LOCAL_PROFILE_NAMES) - set(db_by_profile)
        extra = set(db_by_profile) - set(LOCAL_PROFILE_NAMES)
        if missing or extra:
            raise SessionHubError("bad_profile_allowlist")
        self._db_by_profile = {profile: Path(path) for profile, path in db_by_profile.items()}
        self._snapshots = snapshots

    @property
    def snapshots(self) -> SnapshotCache | None:
        return self._snapshots

    def available_profiles(self) -> tuple[str, ...]:
        """Local profiles plus every configured remote instance profile (even when its snapshot is missing)."""
        remote = tuple(f"{instance}/default" for instance in (self._snapshots.instances if self._snapshots else ()))
        return LOCAL_PROFILE_NAMES + remote

    def instance_status(self) -> list[dict[str, Any]]:
        local = {
            "id": LOCAL_INSTANCE_ID, "profile": None, "label": LOCAL_INSTANCE_LABEL, "kind": "local", "state": "local",
            "reason": None, "exportedAt": None, "checkedAt": None, "ageSeconds": 0, "ttlSeconds": 0,
            "sessions": None, "messages": None, "schemaVersion": None,
        }
        return [local] + (self._snapshots.statuses() if self._snapshots else [])

    def _profile_db_path(self, profile: str) -> Path:
        if is_remote_profile(profile):
            instance = profile_instance(profile)
            if self._snapshots is None or instance not in self._snapshots.instances:
                raise SessionHubError("instance_not_configured")
            path = self._snapshots.state_db(instance)
            if path is None:
                raise SessionHubError("snapshot_unavailable")
            return path
        return self._db_by_profile[profile]

    @contextlib.contextmanager
    def read_connection(self, profile: str) -> Iterator[sqlite3.Connection]:
        self._require_profile(profile)
        uri = _sqlite_ro_uri(self._profile_db_path(profile))
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            conn.set_authorizer(_authorizer)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=3000")
            self._validate_schema(conn)
            yield conn
        except SessionHubError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SessionHubError("db_read_failed") from exc
        finally:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass

    def execute_readonly(
        self,
        conn: sqlite3.Connection,
        sql: str,
        params: Iterable[Any] = (),
    ) -> list[sqlite3.Row]:
        stripped = sql.strip()
        first = stripped.split(None, 1)[0].upper() if stripped else ""
        lower = stripped.lower()
        if first not in {"SELECT", "PRAGMA", "WITH"}:
            raise SessionHubError("sql_not_allowed")
        if "wal_checkpoint" in lower or "journal_mode" in lower and "=" in lower:
            raise SessionHubError("sql_not_allowed")
        try:
            return list(conn.execute(stripped, tuple(params)))
        except sqlite3.DatabaseError as exc:
            raise SessionHubError("db_read_failed") from exc

    def measure_authorizer_write_denial(self, profile: str) -> dict[str, Any]:
        """Measure a rejected write attempt without exposing database content."""
        evidence = {"writeAttempts": 0, "writeAllowed": 0}

        def measured_authorizer(
            action: int,
            arg1: str | None,
            arg2: str | None,
            db: str | None,
            source: str | None,
        ) -> int:
            if action in WRITE_AUTHORIZER_ACTIONS:
                evidence["writeAttempts"] += 1
            return _authorizer(action, arg1, arg2, db, source)

        rejected = False
        with self.read_connection(profile) as conn:
            conn.set_authorizer(measured_authorizer)
            try:
                conn.execute("CREATE TABLE __session_hub_write_probe__(value INTEGER)")
            except sqlite3.DatabaseError:
                rejected = True
        if not rejected or evidence["writeAttempts"] < 1:
            raise SessionHubError("readonly_probe_failed")
        return {
            "authorizerWriteAttemptsObserved": evidence["writeAttempts"],
            "authorizerWriteOpsAllowed": evidence["writeAllowed"],
            "writeProbeRejected": rejected,
        }

    def list_sessions(
        self,
        *,
        tab: str = "all",
        query: str = "",
        profiles: list[str] | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        telegram_types: list[str] | None = None,
        page_size: int | str = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        selected_profiles = list(profiles or self.available_profiles())
        for profile in selected_profiles:
            self._require_profile(profile)
        limit = self._normalize_page_size(page_size)
        query = self._normalize_query(query)
        if tab == "telegram":
            return self._list_telegram_turns(
                selected_profiles=selected_profiles,
                query=query,
                date_from=date_from,
                date_to=date_to,
                telegram_types=telegram_types,
                page_size=page_size,
                limit=limit,
                cursor=cursor,
                started=started,
            )
        after = _decode_cursor(cursor)
        candidates: list[dict[str, Any]] = []
        total_known = 0
        skipped: list[dict[str, str]] = []
        for profile in selected_profiles:
            try:
                with self.read_connection(profile) as conn:
                    total_known += self._count_profile_rows(
                        conn, tab, query, date_from, date_to, telegram_types
                    )
                    candidates.extend(
                        self._fetch_profile_rows(
                            conn,
                            profile,
                            tab,
                            query,
                            date_from,
                            date_to,
                            telegram_types,
                            after=after,
                            limit=limit + 1,
                        )
                    )
            except SessionHubError as exc:
                # A branch snapshot, or one stale/unused local profile, must not take the whole list down.
                if not _skippable_profile_error(profile, exc):
                    raise
                skipped.append({"profile": profile, "instance": profile_instance(profile), "error": exc.code})
        _raise_if_every_local_profile_failed(selected_profiles, skipped)
        candidates.sort(key=lambda row: (-row["lastActivity"], row["ownerProfile"], row["storedSessionId"]))
        page = candidates[:limit]
        next_cursor = _encode_cursor(page[-1]) if len(candidates) > limit and page else None
        return {
            "contract": "sanitized-session-hub-v1",
            "items": page,
            "page": {
                "defaultSize": DEFAULT_PAGE_SIZE,
                "size": page_size,
                "windowLimit": limit,
                "returned": len(page),
                "totalKnown": total_known,
            },
            "nextCursor": next_cursor,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "instances": self.instance_status(),
            "skippedProfiles": skipped,
        }

    def _list_telegram_turns(
        self,
        *,
        selected_profiles: list[str],
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
        page_size: int | str,
        limit: int,
        cursor: str | None,
        started: float,
    ) -> dict[str, Any]:
        after = _decode_telegram_cursor(cursor)
        candidates: list[dict[str, Any]] = []
        total_known = 0
        skipped: list[dict[str, str]] = []
        for profile in selected_profiles:
            try:
                with self.read_connection(profile) as conn:
                    total_known += self._count_telegram_turns(
                        conn, query, date_from, date_to, telegram_types
                    )
                    candidates.extend(
                        self._fetch_telegram_turns(
                            conn,
                            profile,
                            query,
                            date_from,
                            date_to,
                            telegram_types,
                            after=after,
                            limit=limit + 1,
                        )
                    )
            except SessionHubError as exc:
                if not _skippable_profile_error(profile, exc):
                    raise
                skipped.append({"profile": profile, "instance": profile_instance(profile), "error": exc.code})
        _raise_if_every_local_profile_failed(selected_profiles, skipped)
        candidates.sort(key=lambda row: (-row["lastActivity"], row["ownerProfile"], -row["userMessageId"]))
        page = candidates[:limit]
        next_cursor = _encode_telegram_cursor(page[-1]) if len(candidates) > limit and page else None
        return {
            "contract": "sanitized-session-hub-v1",
            "items": page,
            "page": {
                "defaultSize": DEFAULT_PAGE_SIZE,
                "size": page_size,
                "windowLimit": limit,
                "returned": len(page),
                "totalKnown": total_known,
            },
            "nextCursor": next_cursor,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "instances": self.instance_status(),
            "skippedProfiles": skipped,
        }

    def _count_telegram_turns(
        self,
        conn: sqlite3.Connection,
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
    ) -> int:
        self._require_telegram_turn_columns(conn)
        where, params = self._telegram_turn_filter_sql(query, date_from, date_to, telegram_types)
        rows = self.execute_readonly(
            conn,
            f"SELECT COUNT(*) AS total FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {where}",
            params,
        )
        return int(rows[0]["total"])

    def _fetch_telegram_turns(
        self,
        conn: sqlite3.Connection,
        profile: str,
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
        *,
        after: tuple[int | float, str, int] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self._require_telegram_turn_columns(conn)
        filters, params = self._telegram_turn_filter_sql(query, date_from, date_to, telegram_types)
        if after is not None:
            last_activity, last_profile, last_message_id = after
            if profile < last_profile:
                filters += " AND m.timestamp < ?"
                params.append(last_activity)
            elif profile > last_profile:
                filters += " AND m.timestamp <= ?"
                params.append(last_activity)
            else:
                filters += " AND (m.timestamp < ? OR (m.timestamp = ? AND m.id < ?))"
                params.extend([last_activity, last_activity, last_message_id])
        params.append(limit)
        rows = self.execute_readonly(
            conn,
            f"""
            SELECT m.id AS message_id, m.content AS question_content,
                   m.timestamp AS user_timestamp, m.platform_message_id,
                   s.id AS session_id, COALESCE(s.chat_type, '') AS chat_type,
                   CASE WHEN s.thread_id IS NOT NULL AND TRIM(s.thread_id) <> '' THEN 1 ELSE 0 END AS has_thread,
                   COALESCE(s.message_count, 0) AS message_count
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {filters}
            ORDER BY m.timestamp DESC, m.id DESC
            LIMIT ?
            """,
            params,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            bucket = classify_session("telegram", row["chat_type"], bool(row["has_thread"]))
            question_text = _redacted_normalized_text(row["question_content"])
            title = question_text[:TELEGRAM_TITLE_CHARS]
            if len(question_text) > TELEGRAM_TITLE_CHARS:
                title = title.rstrip() + "…"
            reply = self._first_visible_assistant_reply(
                conn,
                str(row["session_id"]),
                int(row["message_id"]),
                row["user_timestamp"],
            )
            timestamp = row["user_timestamp"]
            out.append({
                "ownerProfile": profile,
                "instance": profile_instance(profile),
                "remote": is_remote_profile(profile),
                "storedSessionId": row["session_id"],
                "title": title,
                "questionText": question_text,
                "source": "telegram",
                "telegramType": self._telegram_type(bucket or "telegram_group"),
                "lastActivity": timestamp,
                "timestamp": timestamp,
                "userTimestamp": timestamp,
                "messageCount": int(row["message_count"]),
                "messageId": int(row["message_id"]),
                "userMessageId": int(row["message_id"]),
                "platformMessageId": row["platform_message_id"],
                "replyPreview": reply,
                "responseState": "answered" if reply else "pending",
            })
        return out

    @staticmethod
    def _telegram_turn_filter_sql(
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
    ) -> tuple[str, list[Any]]:
        filters = [
            "s.source = 'telegram'",
            "m.active = 1",
            "m.role = 'user'",
            "m.platform_message_id IS NOT NULL",
            "COALESCE(m._compressed_summary, 0) = 0",
            "TRIM(COALESCE(m.content, '')) <> ''",
            "LOWER(TRIM(COALESCE(m.display_kind, ''))) NOT IN ('failed_turn','hidden','internal','internal-notice','internal_notice','steer')",
            "UPPER(TRIM(COALESCE(m.content, ''))) NOT LIKE '[CONTEXT COMPACTION%'",
            "UPPER(TRIM(COALESCE(m.content, ''))) NOT LIKE '[PRIOR CONTEXT%'",
            "UPPER(TRIM(COALESCE(m.content, ''))) NOT LIKE '[SYSTEM CONTEXT%'",
            "UPPER(TRIM(COALESCE(m.content, ''))) NOT LIKE '[INTERNAL CONTEXT%'",
        ]
        params: list[Any] = []
        if date_from is not None:
            filters.append("m.timestamp >= ?")
            params.append(int(date_from))
        if date_to is not None:
            filters.append("m.timestamp <= ?")
            params.append(int(date_to))
        if query:
            like = f"%{query}%"
            filters.append("(m.content LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\')")
            params.extend([like, like])
        requested_telegram = set(telegram_types or [])
        if not requested_telegram.issubset({"dm", "group", "topic"}):
            raise SessionHubError("bad_telegram_type")
        if requested_telegram:
            is_dm = "LOWER(TRIM(COALESCE(s.chat_type, ''))) = 'dm'"
            has_thread = "s.thread_id IS NOT NULL AND TRIM(s.thread_id) <> ''"
            conditions: list[str] = []
            if "dm" in requested_telegram:
                conditions.append(f"({is_dm})")
            if "topic" in requested_telegram:
                conditions.append(f"(NOT ({is_dm}) AND ({has_thread}))")
            if "group" in requested_telegram:
                conditions.append(f"(NOT ({is_dm}) AND NOT ({has_thread}))")
            filters.append("(" + " OR ".join(conditions) + ")")
        return " AND ".join(filters), params

    def _require_telegram_turn_columns(self, conn: sqlite3.Connection) -> None:
        required = {
            "id", "session_id", "role", "content", "timestamp", "active",
            "platform_message_id", "display_kind", "_compressed_summary",
        }
        actual = {row["name"] for row in self.execute_readonly(conn, "PRAGMA table_info('messages')")}
        if not required.issubset(actual):
            raise SessionHubError("telegram_turns_unsupported")

    def _first_visible_assistant_reply(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        user_message_id: int,
        user_timestamp: int | float,
    ) -> str | None:
        rows = self.execute_readonly(
            conn,
            """
            SELECT id, role, content, timestamp, active, platform_message_id,
                   display_kind, _compressed_summary
            FROM messages
            WHERE session_id = ?
              AND (timestamp > ? OR (timestamp = ? AND id > ?))
            ORDER BY timestamp ASC, id ASC
            """,
            (session_id, user_timestamp, user_timestamp, user_message_id),
        )
        for row in rows:
            if self._is_eligible_telegram_user_row(row):
                break
            if int(row["active"] or 0) != 1 or str(row["role"] or "").lower() != "assistant":
                continue
            if int(row["_compressed_summary"] or 0) != 0:
                continue
            if str(row["display_kind"] or "").strip().lower() in _INTERNAL_DISPLAY_KINDS:
                continue
            if _is_synthetic_message(row["content"]):
                continue
            normalized = " ".join(str(row["content"] or "").split()).upper()
            if normalized.startswith("[SILENT]"):
                continue
            for line in str(row["content"] or "").splitlines():
                preview = redact_text(line)
                if preview:
                    return preview
        return None

    @staticmethod
    def _is_eligible_telegram_user_row(row: sqlite3.Row) -> bool:
        return (
            int(row["active"] or 0) == 1
            and str(row["role"] or "").lower() == "user"
            and row["platform_message_id"] is not None
            and int(row["_compressed_summary"] or 0) == 0
            and str(row["display_kind"] or "").strip().lower() not in _INTERNAL_DISPLAY_KINDS
            and bool(" ".join(str(row["content"] or "").split()))
            and not _is_synthetic_message(row["content"])
        )

    def revalidate_owner(self, profile: str, stored_session_id: str) -> dict[str, str]:
        self._require_profile(profile)
        if is_remote_profile(profile):
            # Desktop SDK openSession only reaches this PC's profiles; branch rows are display-only.
            raise SessionHubError("remote_instance")
        if not SESSION_ID_RE.fullmatch(stored_session_id or ""):
            raise SessionHubError("bad_session_id")
        with self.read_connection(profile) as conn:
            rows = self.execute_readonly(
                conn,
                """
                SELECT id, source, COALESCE(chat_type, '') AS chat_type,
                       CASE WHEN thread_id IS NOT NULL AND TRIM(thread_id) <> '' THEN 1 ELSE 0 END AS has_thread
                FROM sessions
                WHERE id = ?
                LIMIT 1
                """,
                (stored_session_id,),
            )
        if not rows:
            raise SessionHubError("unknown_session")
        row = rows[0]
        if not is_openable_source(row["source"]):
            raise SessionHubError("source_not_allowed")
        return {"ownerProfile": profile, "storedSessionId": stored_session_id}

    def _count_profile_rows(
        self,
        conn: sqlite3.Connection,
        tab: str,
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
    ) -> int:
        where, params = self._profile_filter_sql(tab, query, date_from, date_to, telegram_types)
        rows = self.execute_readonly(
            conn,
            f"SELECT COUNT(*) AS total FROM sessions s WHERE {where}",
            params,
        )
        return int(rows[0]["total"])

    def _fetch_profile_rows(
        self,
        conn: sqlite3.Connection,
        profile: str,
        tab: str,
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
        *,
        after: tuple[int, str, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        filters, params = self._profile_filter_sql(tab, query, date_from, date_to, telegram_types)
        session_columns = {
            row["name"] for row in self.execute_readonly(conn, "PRAGMA table_info('sessions')")
        }
        parent_select = "s.parent_session_id" if "parent_session_id" in session_columns else "NULL"
        end_reason_select = "s.end_reason" if "end_reason" in session_columns else "NULL"
        supports_lineage = {"parent_session_id", "end_reason"}.issubset(session_columns)
        activity = "COALESCE(s.last_activity_at, s.started_at, 0)"
        if after is not None:
            last_activity, last_profile, last_session_id = after
            if profile < last_profile:
                filters += f" AND {activity} < ?"
                params.append(last_activity)
            elif profile > last_profile:
                filters += f" AND {activity} <= ?"
                params.append(last_activity)
            else:
                filters += f" AND ({activity} < ? OR ({activity} = ? AND s.id > ?))"
                params.extend([last_activity, last_activity, last_session_id])
        params.append(limit)
        rows = self.execute_readonly(
            conn,
            f"""
            SELECT s.id, COALESCE(s.title, '') AS title, s.source,
                   COALESCE(s.chat_type, '') AS chat_type,
                   CASE WHEN s.thread_id IS NOT NULL AND TRIM(s.thread_id) <> '' THEN 1 ELSE 0 END AS has_thread,
                   {parent_select} AS parent_session_id,
                   {end_reason_select} AS end_reason,
                   {activity} AS last_activity,
                   COALESCE(s.message_count, 0) AS message_count
            FROM sessions s
            WHERE {filters}
            ORDER BY {activity} DESC, s.id ASC
            LIMIT ?
            """,
            params,
        )
        out = []
        for row in rows:
            bucket = classify_session(row["source"], row["chat_type"], bool(row["has_thread"]))
            if bucket is None:
                continue
            out.append(
                {
                    "ownerProfile": profile,
                    "instance": profile_instance(profile),
                    "remote": is_remote_profile(profile),
                    "storedSessionId": row["id"],
                    "title": self._display_title(conn, row, supports_lineage=supports_lineage),
                    "source": "desktop" if bucket == "desktop" else "telegram",
                    "telegramType": self._telegram_type(bucket),
                    "lastActivity": int(row["last_activity"]),
                    "messageCount": int(row["message_count"]),
                    "previews": self._fetch_previews(conn, row["id"]),
                }
            )
        return out

    def _display_title(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        supports_lineage: bool,
    ) -> str:
        stored = redact_text(row["title"], limit=MAX_DISPLAY_TITLE_CHARS)
        if stored:
            return stored

        if supports_lineage and str(row["end_reason"] or "").strip().lower() == "compression":
            descendant = self._first_descendant_title(conn, str(row["id"]))
            if descendant:
                return descendant + PREVIOUS_SEGMENT_SUFFIX

        message_rows = self.execute_readonly(
            conn,
            """
            SELECT content
            FROM messages
            WHERE session_id = ?
              AND role = 'user'
              AND TRIM(COALESCE(content, '')) <> ''
            ORDER BY timestamp ASC, id ASC
            LIMIT 1
            """,
            (row["id"],),
        )
        if message_rows:
            excerpt = redact_text(message_rows[0]["content"], limit=MAX_DISPLAY_TITLE_CHARS)
            if excerpt:
                return excerpt

        try:
            activity = datetime.fromtimestamp(float(row["last_activity"]), tz=timezone.utc).astimezone(KST)
            return f"빈 세션 · {activity:%Y-%m-%d %H:%M:%S} KST"
        except (OverflowError, TypeError, ValueError, OSError):
            return "빈 세션"

    def _first_descendant_title(self, conn: sqlite3.Connection, session_id: str) -> str:
        rows = self.execute_readonly(
            conn,
            """
            WITH RECURSIVE descendants(id, title, started_at, depth, path) AS (
              SELECT id, title, started_at, 1, ',' || id || ','
              FROM sessions
              WHERE parent_session_id = ?
              UNION ALL
              SELECT child.id, child.title, child.started_at, descendants.depth + 1,
                     descendants.path || child.id || ','
              FROM sessions child
              JOIN descendants ON child.parent_session_id = descendants.id
              WHERE descendants.depth < 32
                AND INSTR(descendants.path, ',' || child.id || ',') = 0
            )
            SELECT title
            FROM descendants
            WHERE TRIM(COALESCE(title, '')) <> ''
            ORDER BY depth ASC, started_at ASC, id ASC
            LIMIT 1
            """,
            (session_id,),
        )
        if not rows:
            return ""
        return redact_text(
            rows[0]["title"],
            limit=MAX_DISPLAY_TITLE_CHARS - len(PREVIOUS_SEGMENT_SUFFIX),
        )

    @staticmethod
    def _profile_filter_sql(
        tab: str,
        query: str,
        date_from: int | None,
        date_to: int | None,
        telegram_types: list[str] | None,
    ) -> tuple[str, list[Any]]:
        filters = ["s.source IN ('desktop', 'telegram')"]
        params: list[Any] = []
        if tab == "desktop":
            filters.append("s.source = 'desktop'")
        elif tab == "telegram":
            filters.append("s.source = 'telegram'")
        elif tab != "all":
            raise SessionHubError("bad_tab")
        if date_from is not None:
            filters.append("COALESCE(s.last_activity_at, s.started_at, 0) >= ?")
            params.append(int(date_from))
        if date_to is not None:
            filters.append("COALESCE(s.last_activity_at, s.started_at, 0) <= ?")
            params.append(int(date_to))
        if query:
            like = f"%{query}%"
            filters.append(
                """
                (
                  s.title LIKE ? ESCAPE '\\'
                  OR EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.session_id = s.id
                      AND m.active = 1
                      AND m.content LIKE ? ESCAPE '\\'
                    LIMIT 1
                  )
                )
                """
            )
            params.extend([like, like])
        requested_telegram = set(telegram_types or [])
        if not requested_telegram.issubset({"dm", "group", "topic"}):
            raise SessionHubError("bad_telegram_type")
        if requested_telegram:
            is_dm = "LOWER(TRIM(COALESCE(s.chat_type, ''))) = 'dm'"
            has_thread = "s.thread_id IS NOT NULL AND TRIM(s.thread_id) <> ''"
            conditions = []
            if "dm" in requested_telegram:
                conditions.append(f"({is_dm})")
            if "topic" in requested_telegram:
                conditions.append(f"(NOT ({is_dm}) AND ({has_thread}))")
            if "group" in requested_telegram:
                conditions.append(f"(NOT ({is_dm}) AND NOT ({has_thread}))")
            filters.append("(s.source = 'telegram' AND (" + " OR ".join(conditions) + "))")
        return " AND ".join(filters), params

    def _fetch_previews(self, conn: sqlite3.Connection, session_id: str) -> list[str]:
        rows = self.execute_readonly(
            conn,
            """
            SELECT content
            FROM messages
            WHERE session_id = ?
              AND active = 1
              AND role IN ('user', 'assistant')
            ORDER BY timestamp DESC, id DESC
            LIMIT 3
            """,
            (session_id,),
        )
        return [redact_text(row["content"]) for row in rows if redact_text(row["content"])]

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        version_rows = self.execute_readonly(conn, "SELECT MAX(version) AS version FROM schema_version")
        version = version_rows[0]["version"] if version_rows else None
        if not schema_version_supported(version):
            raise SessionHubError("stale_schema")
        required = {
            "sessions": {"id", "title", "source", "chat_type", "thread_id", "started_at", "last_activity_at", "message_count"},
            "messages": {"id", "session_id", "role", "content", "timestamp", "active"},
        }
        for table, columns in required.items():
            actual = {row["name"] for row in self.execute_readonly(conn, f"PRAGMA table_info('{table}')")}
            if not columns.issubset(actual):
                raise SessionHubError("unexpected_schema")
        query_only = self.execute_readonly(conn, "PRAGMA query_only")
        if not query_only or query_only[0][0] != 1:
            raise SessionHubError("readonly_not_enforced")

    @staticmethod
    def _normalize_page_size(page_size: int | str) -> int:
        if page_size == "all":
            return ALL_WINDOW_ROWS
        if page_size not in (10, 20, 50, 100):
            raise SessionHubError("bad_page_size")
        return int(page_size)

    @staticmethod
    def _normalize_query(query: str) -> str:
        cleaned = (query or "").strip()[:MAX_QUERY_CHARS]
        return cleaned.replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _telegram_type(bucket: str) -> str | None:
        if bucket == "telegram_dm":
            return "dm"
        if bucket == "telegram_topic":
            return "topic"
        if bucket == "telegram_group":
            return "group"
        return None

    @staticmethod
    def _require_profile(profile: str) -> None:
        if profile not in ALL_PROFILE_NAMES:
            raise SessionHubError("bad_profile")


class KanbanHubBackend:
    """Read-only, multi-board Kanban card adapter for the Session Hub UI."""

    def __init__(
        self,
        db_by_board: Mapping[str, str | Path],
        db_by_profile: Mapping[str, str | Path] | None = None,
        snapshots: SnapshotCache | None = None,
    ) -> None:
        if not db_by_board and snapshots is None:
            raise SessionHubError("kanban_db_unavailable")
        normalized: dict[str, Path] = {}
        skipped: list[str] = []
        for board, path in (db_by_board or {}).items():
            if not isinstance(board, str) or not BOARD_NAME_RE.fullmatch(board):
                # One oddly named board folder (spaces, Korean, ...) must not take down the whole Kanban tab.
                skipped.append(str(board))
                continue
            normalized[board] = Path(path)
        # Branch boards: the branch PC's own kanban.db copy, published as board "<ID>".
        remote_boards: dict[str, str] = {}
        self._snapshots = snapshots
        if snapshots is not None:
            for instance in snapshots.instances:
                if instance in normalized:
                    skipped.append(f"{instance} (remote board shadowed by local board)")
                    continue
                path = snapshots.kanban_db(instance)
                if path is None:
                    continue  # reported through instance_status(); never faked as an empty board
                normalized[instance] = path
                remote_boards[instance] = instance
        if not normalized:
            raise SessionHubError("bad_board" if db_by_board else "kanban_db_unavailable")
        self._db_by_board = normalized
        self._remote_boards = remote_boards
        self.skipped_boards = tuple(skipped)
        profile_paths = dict(db_by_profile or {})
        if any(profile not in LOCAL_PROFILE_NAMES for profile in profile_paths):
            raise SessionHubError("bad_profile_allowlist")
        self._db_by_profile = {profile: Path(path) for profile, path in profile_paths.items()}

    def instance_status(self) -> list[dict[str, Any]]:
        local = {
            "id": LOCAL_INSTANCE_ID, "profile": None, "label": LOCAL_INSTANCE_LABEL, "kind": "local", "state": "local",
            "reason": None, "exportedAt": None, "checkedAt": None, "ageSeconds": 0, "ttlSeconds": 0,
            "sessions": None, "messages": None, "schemaVersion": None,
        }
        return [local] + (self._snapshots.statuses() if self._snapshots else [])

    def board_instance(self, board: str) -> str:
        return self._remote_boards.get(board, LOCAL_INSTANCE_ID)

    @contextlib.contextmanager
    def read_connection(self, board: str) -> Iterator[sqlite3.Connection]:
        self._require_board(board)
        uri = _sqlite_ro_uri(self._db_by_board[board])
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            conn.set_authorizer(_authorizer)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=3000")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info('tasks')")}
            if not {"id", "title", "status", "created_at"}.issubset(columns):
                raise SessionHubError("kanban_schema_unsupported")
            yield conn
        except SessionHubError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SessionHubError("kanban_read_failed") from exc
        finally:
            if conn is not None:
                conn.close()

    def list_cards(
        self,
        *,
        query: str = "",
        assignees: list[str] | None = None,
        statuses: list[str] | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        page_size: int | str = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        selected_assignees = list(assignees or ALL_PROFILE_NAMES)
        if any(profile not in ALL_PROFILE_NAMES for profile in selected_assignees):
            raise SessionHubError("bad_profile")
        selected_statuses = list(statuses or [])
        if any(status not in KANBAN_STATUSES for status in selected_statuses):
            raise SessionHubError("bad_kanban_status")
        normalized_query = " ".join((query or "").strip().split())[:MAX_QUERY_CHARS].casefold()
        limit = SessionHubBackend._normalize_page_size(page_size)
        after = _decode_kanban_cursor(cursor)

        matching: list[dict[str, Any]] = []
        skipped_remote: list[dict[str, str]] = []
        for board in sorted(self._db_by_board):
            try:
                matching.extend(self._read_board_cards(board, include_session_detail=False))
            except SessionHubError as exc:
                if board not in self._remote_boards:
                    raise
                # One damaged branch board copy must not hide HQ boards.
                skipped_remote.append({"board": board, "instance": board, "error": exc.code})
        # Selecting every profile (8 keys, or the 5 HQ keys from a 0.4.x client) means "no assignee
        # filter": keep unassigned or non-HQ cards visible too.
        selected_set = set(selected_assignees)
        all_assignees = selected_set >= set(ALL_PROFILE_NAMES) or selected_set == set(LOCAL_PROFILE_NAMES)
        matching = [
            card for card in matching
            if (all_assignees or card["ownerProfile"] in selected_set)
            and (not selected_statuses or card["status"] in selected_statuses)
            and (date_from is None or card["updatedAt"] >= int(date_from))
            and (date_to is None or card["updatedAt"] <= int(date_to))
            and (
                not normalized_query
                or normalized_query in card.pop("_searchText").casefold()
            )
        ]
        for card in matching:
            card.pop("_searchText", None)
        matching.sort(key=self._sort_key)
        total_known = len(matching)
        if after is not None:
            after_key = (-after[0], after[1], after[2])
            matching = [card for card in matching if self._sort_key(card) > after_key]
        page = matching[:limit]
        next_cursor = _encode_kanban_cursor(page[-1]) if len(matching) > limit and page else None
        return {
            "contract": "sanitized-kanban-hub-v1",
            "items": page,
            "page": {
                "defaultSize": DEFAULT_PAGE_SIZE,
                "size": page_size,
                "windowLimit": limit,
                "returned": len(page),
                "totalKnown": total_known,
            },
            "nextCursor": next_cursor,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "sourceReadonly": True,
            "skippedBoards": len(self.skipped_boards) + len(skipped_remote),
            "skippedRemoteBoards": skipped_remote,
            "instances": self.instance_status(),
        }

    def get_card(self, board: str, task_id: str) -> dict[str, Any]:
        self._require_board(board)
        if not TASK_ID_RE.fullmatch(task_id or ""):
            raise SessionHubError("bad_task_id")
        card = next(
            (row for row in self._read_board_cards(board, include_session_detail=True, only_task_id=task_id) if row["id"] == task_id),
            None,
        )
        if card is None:
            raise SessionHubError("unknown_kanban_card")
        card.pop("_searchText", None)
        return card

    def _read_board_cards(
        self, board: str, *, include_session_detail: bool, only_task_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self.read_connection(board) as conn:
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info('tasks')")}
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            wanted = (
                "id", "title", "body", "assignee", "status", "priority", "created_by",
                "created_at", "started_at", "completed_at", "due_at", "project_id",
                "session_id", "block_kind", "last_heartbeat_at", "current_run_id", "source_instance",
            )
            select = [name if name in task_columns else f"NULL AS {name}" for name in wanted]
            task_rows = list(conn.execute(f"SELECT {','.join(select)} FROM tasks"))
            parents: dict[str, list[str]] = {}
            children: dict[str, list[str]] = {}
            if "task_links" in tables:
                link_columns = {row["name"] for row in conn.execute("PRAGMA table_info('task_links')")}
                if {"parent_id", "child_id"}.issubset(link_columns):
                    for link in conn.execute("SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"):
                        parents.setdefault(str(link["child_id"]), []).append(str(link["parent_id"]))
                        children.setdefault(str(link["parent_id"]), []).append(str(link["child_id"]))
            run_by_id: dict[int, sqlite3.Row] = {}
            latest_running: dict[str, sqlite3.Row] = {}
            latest_by_task: dict[str, sqlite3.Row] = {}
            if "task_runs" in tables:
                run_columns = {row["name"] for row in conn.execute("PRAGMA table_info('task_runs')")}
                if {"id", "task_id", "status", "started_at"}.issubset(run_columns):
                    profile_expr = "profile" if "profile" in run_columns else "NULL AS profile"
                    ended_expr = "ended_at" if "ended_at" in run_columns else "NULL AS ended_at"
                    outcome_expr = "outcome" if "outcome" in run_columns else "NULL AS outcome"
                    summary_expr = "summary" if "summary" in run_columns else "NULL AS summary"
                    metadata_expr = "metadata" if "metadata" in run_columns else "NULL AS metadata"
                    for run in conn.execute(
                        f"SELECT id,task_id,{profile_expr},status,started_at,{ended_expr},{outcome_expr},{summary_expr},{metadata_expr} "
                        "FROM task_runs ORDER BY started_at,id"
                    ):
                        run_by_id[int(run["id"])] = run
                        latest_by_task[str(run["task_id"])] = run
                        if run["status"] == "running":
                            latest_running[str(run["task_id"])] = run

        cards: list[dict[str, Any]] = []
        board_instance = self.board_instance(board)
        remote_board = board_instance != LOCAL_INSTANCE_ID
        for row in task_rows:
            task_id = str(row["id"])
            if only_task_id is not None and task_id != only_task_id:
                # Card detail needs one card only; skip per-task session probes for the rest of the board.
                continue
            assignee = str(row["assignee"] or "")
            if remote_board:
                # A branch board's "default" assignee is that branch's own profile ("<ID>/default").
                owner_profile = f"{board_instance}/{assignee}" if assignee == "default" else None
            else:
                owner_profile = assignee or None
            updated_at = max(
                int(row["created_at"] or 0), int(row["started_at"] or 0),
                int(row["completed_at"] or 0), int(row["last_heartbeat_at"] or 0),
            )
            current_run = None
            run = None
            if row["current_run_id"] is not None:
                run = run_by_id.get(int(row["current_run_id"]))
                if run is not None and str(run["task_id"]) != task_id:
                    run = None
            if run is None:
                run = latest_running.get(task_id)
            if run is not None:
                current_run = {
                    "id": int(run["id"]), "profile": run["profile"],
                    "status": run["status"], "startedAt": int(run["started_at"]),
                }
                updated_at = max(updated_at, current_run["startedAt"])
            session_id = str(row["session_id"] or "")
            source_instance = str(row["source_instance"] or "") or None
            if remote_board and not source_instance:
                source_instance = board_instance
            latest_run_row = latest_by_task.get(task_id)
            latest_run = self._run_detail(latest_run_row) if latest_run_row is not None else None
            if latest_run is not None:
                updated_at = max(updated_at, latest_run["startedAt"])
            title = redact_text(str(row["title"] or ""), limit=120) or task_id
            card = {
                "kind": "kanban", "id": task_id, "title": title, "board": board,
                "instance": board_instance, "remote": remote_board,
                "status": str(row["status"] or "unknown"), "assignee": assignee or None,
                "ownerProfile": owner_profile, "priority": int(row["priority"] or 0),
                "createdBy": redact_text(str(row["created_by"] or ""), limit=80) or None,
                "createdAt": int(row["created_at"] or 0),
                "startedAt": int(row["started_at"]) if row["started_at"] is not None else None,
                "completedAt": int(row["completed_at"]) if row["completed_at"] is not None else None,
                "updatedAt": updated_at,
                "dueAt": int(row["due_at"]) if row["due_at"] is not None else None,
                "projectId": str(row["project_id"]) if row["project_id"] else None,
                "parentIds": parents.get(task_id, []), "childIds": children.get(task_id, []),
                "currentRun": current_run, "latestRun": latest_run,
                "blocked": row["status"] == "blocked",
                "blockKind": str(row["block_kind"]) if row["block_kind"] else None,
                "sourceInstance": source_instance,
                "_searchText": " ".join((task_id, str(row["title"] or ""), str(row["body"] or ""), str(row["project_id"] or ""))),
            }
            if include_session_detail:
                card["body"] = redact_text(str(row["body"] or ""), limit=4000)
                card["originSession"] = self._origin_session_link(session_id, source_instance)
                card["latestWorkerSession"] = self._worker_session_link(latest_run_row, source_instance)
            cards.append(card)
        return cards

    @staticmethod
    def _run_detail(run: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(run["id"]),
            "profile": str(run["profile"]) if run["profile"] else None,
            "status": str(run["status"]),
            "startedAt": int(run["started_at"]),
            "endedAt": int(run["ended_at"]) if run["ended_at"] is not None else None,
            "outcome": str(run["outcome"]) if run["outcome"] else None,
            "summary": redact_text(str(run["summary"] or ""), limit=500) or None,
        }

    @staticmethod
    def _is_remote_source(source_instance: str | None) -> bool:
        # Any instance tag other than this PC's own ("HQ") marks a task created on another PC.
        value = (source_instance or "").strip()
        return bool(value) and value.upper() not in {LOCAL_INSTANCE_ID, "LOCAL"}

    @staticmethod
    def _empty_link(kind: str, state: str, reason: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "state": state,
            "available": False,
            "ownerProfile": None,
            "storedSessionId": None,
            "reason": reason,
        }

    def _origin_session_link(self, session_id: str, source_instance: str | None) -> dict[str, Any]:
        if self._is_remote_source(source_instance):
            return self._empty_link("origin", "remote", f"Origin session belongs to remote source {source_instance}.")
        if not session_id:
            return self._empty_link("origin", "absent", "No origin session is recorded for this task.")
        if not SESSION_ID_RE.fullmatch(session_id):
            return self._empty_link("origin", "damaged", "Origin session identifier is damaged or unsupported.")
        owners = [profile for profile in PROFILE_NAMES if profile in self._db_by_profile and self._session_exists(profile, session_id)]
        if len(owners) == 1 and not is_openable_source(self._session_source(owners[0], session_id)):
            return {
                **self._empty_link("origin", "not_openable", "Origin session source cannot be opened in Desktop."),
                "ownerProfile": owners[0], "storedSessionId": session_id,
            }
        if len(owners) == 1:
            return {
                "kind": "origin", "state": "available", "available": True,
                "ownerProfile": owners[0], "storedSessionId": session_id, "reason": None,
            }
        if len(owners) > 1:
            return self._empty_link("origin", "ambiguous", "Origin session exists in more than one local profile.")
        return self._empty_link("origin", "stale", "Origin session is not available in any permitted local profile.")

    def _worker_session_link(self, run: sqlite3.Row | None, source_instance: str | None) -> dict[str, Any]:
        if self._is_remote_source(source_instance):
            return self._empty_link("worker", "remote", f"Worker session belongs to remote source {source_instance}.")
        if run is None:
            return self._empty_link("worker", "absent", "No worker execution session is recorded.")
        profile = str(run["profile"] or "")
        try:
            metadata = json.loads(str(run["metadata"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {**self._empty_link("worker", "damaged", "Latest run metadata is damaged."), "runId": int(run["id"])}
        session_id = metadata.get("worker_session_id") if isinstance(metadata, dict) else None
        if not session_id:
            return {**self._empty_link("worker", "absent", "Latest run has no worker execution session."), "runId": int(run["id"])}
        if profile not in PROFILE_NAMES or not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            return {**self._empty_link("worker", "damaged", "Worker session profile or identifier is damaged."), "runId": int(run["id"])}
        source = self._session_source(profile, session_id) if profile in self._db_by_profile else None
        if source is not None and is_openable_source(source):
            return {
                "kind": "worker", "state": "available", "available": True,
                "ownerProfile": profile, "storedSessionId": session_id, "reason": None,
                "runId": int(run["id"]),
            }
        if source is not None:
            return {
                **self._empty_link("worker", "not_openable", f"Worker session source '{source}' cannot be opened in Desktop."),
                "ownerProfile": profile, "storedSessionId": session_id, "runId": int(run["id"]),
            }
        return {
            **self._empty_link("worker", "stale", "Worker session is not available under the exact run profile."),
            "ownerProfile": profile,
            "storedSessionId": session_id,
            "runId": int(run["id"]),
        }

    def _session_exists(self, profile: str, session_id: str) -> bool:
        return self._session_source(profile, session_id) is not None

    def _session_source(self, profile: str, session_id: str) -> str | None:
        """Return the stored session's source ('' when unknown) or None when it does not exist."""
        path = self._db_by_profile[profile]
        uri = _sqlite_ro_uri(path)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            conn.set_authorizer(_authorizer)
            conn.execute("PRAGMA query_only=ON")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info('sessions')")}
            if "id" not in columns:
                return None
            select = "id, source" if "source" in columns else "id, '' AS source"
            row = conn.execute(f"SELECT {select} FROM sessions WHERE id=? LIMIT 1", (session_id,)).fetchone()
            return None if row is None else str(row["source"] or "")
        except (OSError, sqlite3.DatabaseError, SessionHubError):
            return None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _sort_key(card: dict[str, Any]) -> tuple[int, str, str]:
        return -int(card["updatedAt"]), str(card["board"]), str(card["id"])

    def _require_board(self, board: str) -> None:
        if not isinstance(board, str) or not BOARD_NAME_RE.fullmatch(board) or board not in self._db_by_board:
            raise SessionHubError("bad_board")
