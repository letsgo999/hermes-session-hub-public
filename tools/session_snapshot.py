#!/usr/bin/env python3
"""Branch session snapshot collector for Hermes Session Hub (optional multi-PC setup).

Runs on a branch PC. Reads that PC's Hermes ``state.db`` and ``kanban.db`` in
SQLite read-only mode and writes a *trimmed* copy plus a manifest into a shared
sync folder (``<sync root>/snapshots/<DEVICE>/``), where the main PC's Session Hub
reads it. The sync folder is any folder both PCs see (a cloud-drive folder, a NAS share).

Copy policy: ``sessions`` fully, ``messages`` only for role in (user, assistant),
active=1, last 30 days. ``tool`` rows (command output, file contents) are never
copied. ``kanban.db`` is copied fully (small).

Standard library only — branch PCs do not have psutil. Never writes to the
source databases. Skips the copy when nothing changed since the last run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SNAPSHOT_CONTRACT = "session-hub-branch-snapshot-v1"
STATE_NAME = "state_snapshot.sqlite3"
KANBAN_NAME = "kanban_snapshot.sqlite3"
MANIFEST_NAME = "manifest.json"
MESSAGE_ROLES = ("user", "assistant")
KANBAN_TABLES = ("tasks", "task_links", "task_runs", "task_comments", "task_attachments", "task_events", "boards")


def ro_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hermes_home() -> Path:
    explicit = os.environ.get("HERMES_HOME")
    if explicit:
        home = Path(explicit).expanduser()
        base = home.parent.parent
        if home.parent.name.casefold() == "profiles" and ((base / "config.yaml").is_file() or (base / "state.db").is_file()):
            return base
        return home
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def find_kanban(home: Path) -> Path | None:
    explicit = os.environ.get("HERMES_KANBAN_DB")
    candidates = [Path(explicit)] if explicit else []
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        candidates.extend(sorted(p / "kanban.db" for p in boards.iterdir() if p.is_dir()))
    candidates.append(home / "kanban.db")
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            return c
    return None


def source_fingerprint(state: Path, kanban: Path | None) -> dict:
    fp: dict = {}
    conn = sqlite3.connect(ro_uri(state), uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        fp["state_data_version"] = conn.execute("PRAGMA data_version").fetchone()[0]
        fp["state_max_activity"] = conn.execute("SELECT MAX(last_activity_at) FROM sessions").fetchone()[0]
        fp["state_session_count"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()
    fp["state_mtime"] = int(state.stat().st_mtime)
    fp["kanban_mtime"] = int(kanban.stat().st_mtime) if kanban else None
    return fp


def build_state_snapshot(src: Path, dst: Path, days: int, max_chars: int = 0) -> dict:
    if dst.exists():
        dst.unlink()
    cutoff = time.time() - days * 86400
    # The output connection must be URI-enabled, otherwise ATTACH treats the
    # read-only "file:...?mode=ro" string as a literal file name (Windows failure).
    out = sqlite3.connect(f"file:{quote(dst.resolve().as_posix(), safe='/:')}", uri=True)
    try:
        out.execute("ATTACH DATABASE ? AS src", (ro_uri(src),))
        # Schema version table: Session Hub validates MAX(version) against its supported range.
        out.execute("CREATE TABLE schema_version AS SELECT * FROM src.schema_version")
        out.execute("CREATE TABLE sessions AS SELECT * FROM src.sessions")
        cols = [r[1] for r in out.execute("PRAGMA src.table_info('messages')")]
        has_active = "active" in cols
        has_role = "role" in cols
        where = ["timestamp >= ?"]
        params: list = [cutoff]
        if has_role:
            where.append("role IN (%s)" % ",".join("?" for _ in MESSAGE_ROLES))
            params.extend(MESSAGE_ROLES)
        if has_active:
            where.append("active = 1")
        select_cols = ", ".join(
            f"substr(content, 1, {int(max_chars)}) AS content" if (c == "content" and max_chars > 0) else c for c in cols
        )
        out.execute(f"CREATE TABLE messages AS SELECT {select_cols} FROM src.messages WHERE {' AND '.join(where)}", params)
        out.execute("CREATE INDEX idx_sessions_id ON sessions(id)")
        out.execute("CREATE INDEX idx_sessions_activity ON sessions(last_activity_at)")
        out.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
        counts = {
            "schema_version": out.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
            "sessions": out.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "messages": out.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "messages_source_total": out.execute("SELECT COUNT(*) FROM src.messages").fetchone()[0],
            "message_roles": MESSAGE_ROLES if has_role else ("all",),
            "message_days": days,
            "max_content_chars": max_chars,
        }
        out.commit()
        out.execute("DETACH DATABASE src")
        out.execute("VACUUM")
    finally:
        out.close()
    return counts


def build_kanban_snapshot(src: Path, dst: Path) -> dict:
    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(ro_uri(src), uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        out = sqlite3.connect(dst)
        try:
            conn.backup(out)  # consistent single-file copy even while WAL is active
        finally:
            out.close()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KANBAN_TABLES if t in tables}
    finally:
        conn.close()
    return counts


def atomic_replace(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)


def sync_root_candidates(explicit: str | None) -> list[Path]:
    """Where the shared sync folder may live: --sync-root, then HERMES_SYNC_ROOT."""
    out: list[Path] = []
    env = os.environ.get("HERMES_SYNC_ROOT")
    if explicit:
        out.append(Path(explicit))
    if env:
        out.append(Path(env))
    return out


def resolve_sync_root(explicit: str | None) -> tuple[Path | None, list[str]]:
    if explicit:
        return Path(explicit), [explicit]  # an explicit path is authoritative (tests, unusual layouts)
    tried: list[str] = []
    for candidate in sync_root_candidates(None):
        tried.append(str(candidate))
        try:
            if candidate.is_dir():
                return candidate, tried
        except OSError:
            continue
    return None, tried




def _emit(out_dir: Path | None, payload: dict) -> None:
    """Print the result line and append it to <out>/run.log (the ticket asks for the log's last line)."""
    line = json.dumps(payload, ensure_ascii=False)
    print(line)
    if out_dir is not None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / "run.log", "a", encoding="utf-8") as handle:
                handle.write(datetime.now(timezone.utc).isoformat() + " " + line + "\n")
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", required=True, help="branch ID as listed in the main PC's session-hub.config.json (e.g. A)")
    ap.add_argument("--nickname", default="", help="display name stored in the manifest (default: the device ID)")
    ap.add_argument("--hermes-home", default=None, help="default: %%LOCALAPPDATA%%\\hermes")
    ap.add_argument("--out", default=None, help="default: <sync root>/snapshots/<device>/")
    ap.add_argument("--sync-root", default=None, help="shared sync folder (default: HERMES_SYNC_ROOT env)")
    # run_snapshot.cmd passes no tuning flags; a branch can set these environment variables instead
    # (e.g. `set HERMES_SNAPSHOT_MAX_CHARS=4000` before re-running when the copy exceeds 100 MB).
    env_int = lambda name, default: int(os.environ.get(name) or default)  # noqa: E731
    ap.add_argument("--days", type=int, default=env_int("HERMES_SNAPSHOT_DAYS", 30), help="messages window in days (default 30)")
    ap.add_argument("--max-content-chars", type=int, default=env_int("HERMES_SNAPSHOT_MAX_CHARS", 0),
                    help="truncate each copied message body to N chars (0 = keep whole; HQ-sized profiles: 4000)")
    ap.add_argument("--force", action="store_true", default=bool(os.environ.get("HERMES_SNAPSHOT_FORCE")),
                    help="rebuild even when the source is unchanged")
    args = ap.parse_args()
    # Keep the ID exactly as configured on the main PC (IDs are matched case-sensitively there).
    device = args.device.strip()
    if not device or not all(ch.isalnum() or ch in "_-" for ch in device) or len(device) > 16:
        _emit(None, {"status": "ERROR", "reason": "bad_device", "device": device})
        return 2
    nickname = args.nickname.strip()
    if not nickname or not all(ord(ch) < 0x80 or "\uac00" <= ch <= "\ud7a3" for ch in nickname):
        # Empty, or mojibake from a cmd.exe code page mismatch: fall back to the device ID.
        nickname = device

    home = Path(args.hermes_home) if args.hermes_home else hermes_home()
    state = home / "state.db"
    if not state.is_file():
        _emit(None, {"status": "ERROR", "reason": "state_db_missing", "path": str(state)})
        return 2
    kanban = find_kanban(home)
    if args.out:
        out_dir = Path(args.out)
    else:
        sync_root, tried = resolve_sync_root(args.sync_root)
        if sync_root is None:
            _emit(None, {"status": "ERROR", "reason": "sync_root_missing", "device": device,
                         "hint": "set HERMES_SYNC_ROOT or pass --sync-root (a folder shared with the main PC)",
                         "tried": tried})
            return 3
        out_dir = sync_root / "snapshots" / device
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / MANIFEST_NAME

    fingerprint = source_fingerprint(state, kanban)
    previous = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    if previous and not args.force and previous.get("source_fingerprint") == fingerprint and (out_dir / STATE_NAME).is_file():
        # Nothing changed; only refresh the heartbeat so the main PC can tell "collector alive" from "stale".
        previous["checked_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(previous, ensure_ascii=False, indent=1), encoding="utf-8")
        _emit(out_dir, {"status": "UNCHANGED", "device": device, "checked_at": previous["checked_at"]})
        return 0

    started = time.time()
    tmp_state = out_dir / (STATE_NAME + ".tmp")
    state_counts = build_state_snapshot(state, tmp_state, args.days, args.max_content_chars)
    kanban_counts = None
    tmp_kanban = out_dir / (KANBAN_NAME + ".tmp")
    if kanban is not None:
        kanban_counts = build_kanban_snapshot(kanban, tmp_kanban)

    atomic_replace(tmp_state, out_dir / STATE_NAME)
    if kanban is not None:
        atomic_replace(tmp_kanban, out_dir / KANBAN_NAME)

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "contract": SNAPSHOT_CONTRACT,
        "device": device,
        "nickname": nickname,
        "host": os.environ.get("COMPUTERNAME", ""),
        "hermes_home": str(home),
        "exported_at": now,
        "checked_at": now,
        "elapsed_seconds": round(time.time() - started, 2),
        "state": {
            "file": STATE_NAME,
            "bytes": (out_dir / STATE_NAME).stat().st_size,
            "sha256": sha256(out_dir / STATE_NAME),
            **state_counts,
        },
        "kanban": None if kanban is None else {
            "file": KANBAN_NAME,
            "source": str(kanban),
            "bytes": (out_dir / KANBAN_NAME).stat().st_size,
            "sha256": sha256(out_dir / KANBAN_NAME),
            "counts": kanban_counts,
        },
        "source_fingerprint": fingerprint,
        "privacy": "messages limited to role in (user, assistant), active=1, last %d days; no tool output; no .env/auth" % args.days,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    _emit(out_dir, {"status": "PASS", "device": device, "nickname": nickname, "out": str(out_dir),
                    "state_bytes": manifest["state"]["bytes"], "sessions": state_counts["sessions"],
                    "messages": state_counts["messages"], "schema_version": state_counts["schema_version"],
                    "kanban_bytes": manifest["kanban"]["bytes"] if manifest["kanban"] else None,
                    "elapsed_seconds": manifest["elapsed_seconds"], "hermes_home": str(home)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
