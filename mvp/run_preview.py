from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from backend.api import serve_loopback
from backend.sessionhub import PROFILE_NAMES


SCHEMA_SQL = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (26);
CREATE TABLE sessions(
    id TEXT PRIMARY KEY,
    title TEXT,
    source TEXT,
    chat_type TEXT,
    thread_id TEXT,
    started_at INTEGER,
    last_activity_at INTEGER,
    message_count INTEGER,
    profile_name TEXT
);
CREATE TABLE messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    timestamp INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, session_id UNINDEXED);
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
"""


def build_fixture(root: Path) -> dict[str, Path]:
    dbs = {}
    for index, profile in enumerate(PROFILE_NAMES):
        db = root / f"{profile}.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA_SQL)
        for source in ("desktop", "telegram"):
            sid = f"{profile}-{source}"
            chat_type = "dm" if source == "telegram" else ""
            conn.execute(
                """
                INSERT INTO sessions(
                    id, title, source, chat_type, thread_id, started_at,
                    last_activity_at, message_count, profile_name
                ) VALUES (?, ?, ?, ?, '', ?, ?, 1, NULL)
                """,
                (sid, f"{profile} {source}", source, chat_type, 100 + index, 200 + index),
            )
            conn.execute(
                "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES (?, 'assistant', ?, ?, 1)",
                (sid, f"{source} sanitized preview", 201 + index),
            )
        conn.commit()
        conn.close()
        dbs[profile] = db
    return dbs


def resolve_preview_port(value: str | None) -> int:
    if value is None:
        return 8787
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("SESSION_HUB_PREVIEW_PORT must be an integer from 0 to 65535")
    port = int(value)
    if not 0 <= port <= 65535:
        raise ValueError("SESSION_HUB_PREVIEW_PORT must be an integer from 0 to 65535")
    return port


def main() -> None:
    preview_path = Path(__file__).resolve().parent / "evidence" / "ui-harness.html"
    if not preview_path.is_file():
        raise FileNotFoundError("actual component preview is missing; run npm run preview:render")
    document = preview_path.read_text(encoding="utf-8")
    temp = tempfile.TemporaryDirectory()
    dbs = build_fixture(Path(temp.name))
    requested_port = resolve_preview_port(os.environ.get("SESSION_HUB_PREVIEW_PORT"))
    server = serve_loopback(dbs, port=requested_port, preview_html=document)
    preview_url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Session Hub actual-component preview: {preview_url}", flush=True)
    print(f"Fixture API health: {preview_url}health", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        temp.cleanup()


if __name__ == "__main__":
    main()
