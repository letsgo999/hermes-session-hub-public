from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from backend.sessionhub import (
    PAGE_SIZE_CHOICES,
    SessionHubBackend,
    SessionHubError,
    classify_session,
    redact_text,
)


SCHEMA_SQL = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (26);
CREATE TABLE sessions(
    id TEXT PRIMARY KEY,
    title TEXT,
    source TEXT,
    chat_type TEXT,
    thread_id TEXT,
    parent_session_id TEXT,
    end_reason TEXT,
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
    active INTEGER NOT NULL DEFAULT 1,
    platform_message_id TEXT,
    display_kind TEXT,
    _compressed_summary INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, session_id UNINDEXED);
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
"""


def add_session(
    conn: sqlite3.Connection,
    *,
    sid: str,
    title: str,
    source: str,
    profile_name: str | None,
    activity: int,
    chat_type: str = "",
    thread_id: str = "",
    messages: list[str] | None = None,
    parent_session_id: str | None = None,
    end_reason: str | None = None,
) -> None:
    rows = messages or []
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, source, chat_type, thread_id, parent_session_id, end_reason, started_at,
            last_activity_at, message_count, profile_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid, title, source, chat_type, thread_id, parent_session_id, end_reason,
            activity - 60, activity, len(rows), profile_name,
        ),
    )
    for index, body in enumerate(rows):
        role = "assistant" if index % 2 else "user"
        conn.execute(
            """INSERT INTO messages(
                   session_id, role, content, timestamp, active, platform_message_id,
                   display_kind, _compressed_summary
               ) VALUES (?, ?, ?, ?, 1, ?, '', 0)""",
            (sid, role, body, activity + index, f"platform-{sid}-{index}" if role == "user" else None),
        )
        conn.execute("INSERT INTO messages_fts(content, session_id) VALUES (?, ?)", (body, sid))
        conn.execute("INSERT INTO messages_fts_trigram(content) VALUES (?)", (body,))


class BackendFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_by_profile: dict[str, Path] = {}
        for profile in ("default", "ops", "review", "builder", "search"):
            db_path = self.root / f"{profile}.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA_SQL)
            self.db_by_profile[profile] = db_path
            conn.close()

        self._seed()
        self.backend = SessionHubBackend(self.db_by_profile)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        default = sqlite3.connect(self.db_by_profile["default"])
        add_session(
            default,
            sid="d-desktop-1",
            title="Quarter planning",
            source="desktop",
            profile_name="stale-label-is-ignored",
            activity=300,
            messages=[
                "First note with alpha@example.com and C:\\secret\\live.db",
                "Second note 010-1234-5678",
                "Third note TOKEN_ABCDEF1234567890",
                "Fourth note should not appear",
            ],
        )
        add_session(
            default,
            sid="d-telegram-dm",
            title="Telegram owner DM",
            source="telegram",
            profile_name=None,
            activity=260,
            chat_type="dm",
            messages=["DM body contains renewal keyword"],
        )
        add_session(
            default,
            sid="d-telegram-group",
            title="Telegram group room",
            source="telegram",
            profile_name=None,
            activity=240,
            chat_type="group",
            messages=["group body"],
        )
        add_session(
            default,
            sid="d-cron-excluded",
            title="Cron should be hidden",
            source="cron",
            profile_name=None,
            activity=999,
            messages=["not visible"],
        )
        default.commit()
        default.close()

        review = sqlite3.connect(self.db_by_profile["review"])
        add_session(
            review,
            sid="r-telegram-topic",
            title="Forum topic case",
            source="telegram",
            profile_name="wrong-history",
            activity=280,
            chat_type="group",
            thread_id="topic-fixture",
            messages=["topic body"],
        )
        add_session(
            review,
            sid="r-desktop-tie-a",
            title="Stable tie A",
            source="desktop",
            profile_name="default",
            activity=250,
            messages=["tie"],
        )
        review.commit()
        review.close()

        builder = sqlite3.connect(self.db_by_profile["builder"])
        add_session(
            builder,
            sid="z-desktop-tie-b",
            title="Stable tie B",
            source="desktop",
            profile_name="review",
            activity=250,
            messages=["tie"],
        )
        for index in range(10):
            add_session(
                builder,
                sid=f"z-extra-{index}",
                title=f"Extra {index}",
                source="desktop",
                profile_name=None,
                activity=200 - index,
                messages=[f"extra body {index}"],
            )
        builder.commit()
        builder.close()

    def test_classifies_only_desktop_and_telegram_sources(self) -> None:
        self.assertEqual(classify_session("desktop", "", False), "desktop")
        self.assertEqual(classify_session("telegram", "dm", False), "telegram_dm")
        self.assertEqual(classify_session("telegram", "group", True), "telegram_topic")
        self.assertEqual(classify_session("telegram", "group", False), "telegram_group")
        self.assertIsNone(classify_session("cron", "", False))
        self.assertIsNone(classify_session("unexpected", "", False))

    def test_title_and_content_search_are_bounded_and_sanitized(self) -> None:
        result = self.backend.list_sessions(query="renewal", page_size=20)
        self.assertEqual([row["title"] for row in result["items"]], ["Telegram owner DM"])
        encoded = json.dumps(result)
        self.assertNotIn("alpha@example.com", encoded)
        self.assertNotIn("C:\\secret", encoded)
        self.assertNotIn("TOKEN_ABCDEF", encoded)

    def test_display_title_prefers_stored_then_lineage_then_first_user_message(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["default"])
        add_session(
            conn,
            sid="compression-parent",
            title="",
            source="desktop",
            profile_name="default",
            activity=1790477548,
            messages=["원래 구간의 사용자 요청"],
            end_reason="compression",
        )
        add_session(
            conn,
            sid="compression-child",
            title="샘플 세션 제목 찾기",
            source="desktop",
            profile_name="default",
            activity=1790477600,
            parent_session_id="compression-parent",
        )
        add_session(
            conn,
            sid="message-title",
            title="",
            source="desktop",
            profile_name="default",
            activity=1790477500,
            messages=["  샘플 파이프라인을 점검해 주세요.  "],
        )
        conn.execute("UPDATE messages SET active=0 WHERE session_id=?", ("message-title",))
        conn.commit()
        conn.close()

        result = self.backend.list_sessions(profiles=["default"], page_size="all")
        titles = {item["storedSessionId"]: item["title"] for item in result["items"]}
        self.assertEqual(titles["compression-child"], "샘플 세션 제목 찾기")
        self.assertEqual(titles["compression-parent"], "샘플 세션 제목 찾기 · 이전 구간")
        self.assertEqual(titles["message-title"], "샘플 파이프라인을 점검해 주세요.")
        self.assertNotIn("Untitled", titles.values())

    def test_empty_session_display_title_is_kst_timestamp_without_guessed_topic(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["default"])
        add_session(
            conn,
            sid="empty-session",
            title="",
            source="desktop",
            profile_name="default",
            activity=1790477548,
        )
        conn.commit()
        conn.close()

        result = self.backend.list_sessions(profiles=["default"], query="", page_size="all")
        item = next(row for row in result["items"] if row["storedSessionId"] == "empty-session")
        self.assertEqual(item["title"], "빈 세션 · 2026-09-27 11:52:28 KST")

    def test_telegram_dm_group_topic_filters(self) -> None:
        dm = self.backend.list_sessions(tab="telegram", telegram_types=["dm"])
        group = self.backend.list_sessions(tab="telegram", telegram_types=["group"])
        topic = self.backend.list_sessions(tab="telegram", telegram_types=["topic"])
        self.assertEqual([row["telegramType"] for row in dm["items"]], ["dm"])
        self.assertEqual([row["telegramType"] for row in group["items"]], ["group"])
        self.assertEqual([row["telegramType"] for row in topic["items"]], ["topic"])

    def test_telegram_tab_returns_user_turn_cards_with_bounded_first_reply(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["default"])
        add_session(
            conn,
            sid="telegram-turns",
            title="one session title",
            source="telegram",
            profile_name=None,
            activity=500,
            chat_type="group",
        )
        long_question = "abcdefghij klmnopqrst uvwxyzABCD EFGHIJKLMN OPQRS"
        rows = [
            ("user", "  abcdefghij klmnopqrst\nuvwxyzABCD EFGHIJKLMN OPQRS  ", 501, "tg-501", "", 0, 1),
            ("assistant", "[SILENT]", 502, None, "", 0, 1),
            ("assistant", "숨김 답변", 502, None, "hidden", 0, 1),
            ("assistant", "실패 답변", 502, None, "failed_turn", 0, 1),
            ("assistant", "[PRIOR CONTEXT internal]", 502, None, "", 0, 1),
            ("assistant", "\n첫 답변 첫 줄\n둘째 줄", 503, None, "", 0, 1),
            ("user", "둘째 질문", 505, "tg-505", "", 0, 1),
            ("assistant", "다음 질문 뒤 답변", 506, None, "", 0, 1),
            ("user", "경계 질문", 507, "tg-507", "", 0, 1),
            ("assistant", "경계 질문 숨김 답변", 508, None, "hidden", 0, 1),
            ("user", "경계 다음 질문", 509, "tg-509", "", 0, 1),
            ("assistant", "경계 다음 질문 답변", 510, None, "", 0, 1),
            ("user", "[CONTEXT COMPACTION synthetic]", 511, "tg-511", "", 0, 1),
            ("user", "내부 steer", 512, "tg-512", "steer", 0, 1),
            ("user", "비활성", 513, "tg-513", "", 0, 0),
            ("user", "요약", 514, "tg-514", "", 1, 1),
            ("user", "응답 없는 질문", 515, "tg-515", "", 0, 1),
        ]
        conn.executemany(
            """INSERT INTO messages(
                   session_id, role, content, timestamp, platform_message_id,
                   display_kind, _compressed_summary, active
               ) VALUES ('telegram-turns', ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        conn.close()

        result = self.backend.list_sessions(tab="telegram", profiles=["default"], page_size=10)
        turns = [row for row in result["items"] if row["storedSessionId"] == "telegram-turns"]
        self.assertEqual(
            [row["platformMessageId"] for row in turns],
            ["tg-515", "tg-509", "tg-507", "tg-505", "tg-501"],
        )
        self.assertEqual(turns[0]["responseState"], "pending")
        self.assertEqual(turns[1]["replyPreview"], "경계 다음 질문 답변")
        self.assertEqual(turns[2]["responseState"], "pending")
        self.assertIsNone(turns[2]["replyPreview"])
        self.assertEqual(turns[3]["replyPreview"], "다음 질문 뒤 답변")
        self.assertEqual(turns[4]["replyPreview"], "첫 답변 첫 줄")
        self.assertEqual(turns[4]["title"], long_question[:40] + "…")
        self.assertEqual(turns[4]["title"][:-1], long_question[:40])
        self.assertEqual(len(turns[4]["title"][:-1]), 40)
        self.assertEqual(turns[4]["questionText"], long_question)
        self.assertEqual(turns[3]["title"], "둘째 질문")
        self.assertFalse(turns[3]["title"].endswith("…"))
        self.assertEqual(turns[4]["messageId"], turns[4]["userMessageId"])
        self.assertEqual(turns[4]["lastActivity"], 501)
        self.assertNotIn("previews", turns[4])

        first = self.backend.list_sessions(tab="telegram", profiles=["default"], page_size=10)
        self.assertEqual(first["items"], sorted(
            first["items"], key=lambda row: (-row["lastActivity"], row["ownerProfile"], -row["messageId"])
        ))

    def test_telegram_turn_pagination_uses_user_timestamp_cursor(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["default"])
        add_session(
            conn,
            sid="telegram-pagination",
            title="pagination fixture",
            source="telegram",
            profile_name=None,
            activity=100,
            chat_type="dm",
        )
        conn.executemany(
            """INSERT INTO messages(
                   session_id, role, content, timestamp, platform_message_id,
                   display_kind, _compressed_summary, active
               ) VALUES ('telegram-pagination', 'user', ?, ?, ?, '', 0, 1)""",
            [(f"page question {index}", 100.0 + index / 10, f"page-{index}") for index in range(12)],
        )
        conn.commit()
        conn.close()

        first = self.backend.list_sessions(tab="telegram", profiles=["default"], page_size=10)
        second = self.backend.list_sessions(
            tab="telegram", profiles=["default"], page_size=10, cursor=first["nextCursor"]
        )
        all_rows = self.backend.list_sessions(tab="telegram", profiles=["default"], page_size="all")
        combined = [(row["ownerProfile"], row["userMessageId"]) for row in first["items"] + second["items"]]
        expected = [(row["ownerProfile"], row["userMessageId"]) for row in all_rows["items"]]
        self.assertEqual(len(first["items"]), 10)
        self.assertIsNotNone(first["nextCursor"])
        self.assertEqual(combined, expected)
        self.assertEqual(len(combined), len(set(combined)))
        self.assertIsNone(second["nextCursor"])

    def test_profile_and_date_filters(self) -> None:
        result = self.backend.list_sessions(profiles=["review"], date_from=270, date_to=290)
        self.assertEqual([(row["ownerProfile"], row["title"]) for row in result["items"]], [("review", "Forum topic case")])

    def test_page_preferences_and_cursor_rendering(self) -> None:
        self.assertEqual(PAGE_SIZE_CHOICES, (10, 20, 50, 100, "all"))
        result = self.backend.list_sessions(page_size=10)
        self.assertEqual(len(result["items"]), 10)
        self.assertTrue(result["nextCursor"])
        next_page = self.backend.list_sessions(page_size=10, cursor=result["nextCursor"])
        self.assertGreaterEqual(len(next_page["items"]), 1)
        self.assertNotEqual(result["items"][0]["storedSessionId"], next_page["items"][0]["storedSessionId"])

    def test_all_preference_continues_past_500_in_bounded_stable_windows(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["default"])
        conn.executemany(
            """
            INSERT INTO sessions(
                id, title, source, chat_type, thread_id, started_at,
                last_activity_at, message_count, profile_name
            ) VALUES (?, 'bounded fixture', 'desktop', '', '', ?, ?, 0, NULL)
            """,
            [(f"overflow-{index:04d}", index, index) for index in range(601)],
        )
        conn.commit()
        conn.close()

        cursor = None
        collected: list[str] = []
        window_sizes: list[int] = []
        total_known: set[int] = set()
        while True:
            result = self.backend.list_sessions(
                tab="desktop",
                profiles=["default"],
                query="bounded fixture",
                page_size="all",
                cursor=cursor,
            )
            current = [row["storedSessionId"] for row in result["items"]]
            collected.extend(current)
            window_sizes.append(len(current))
            total_known.add(result["page"]["totalKnown"])
            cursor = result["nextCursor"]
            if cursor is None:
                break
            self.assertLess(len(window_sizes), 8, "all-mode continuation did not converge")

        expected = [f"overflow-{index:04d}" for index in range(600, -1, -1)]
        self.assertEqual(total_known, {601})
        self.assertEqual(collected, expected)
        self.assertEqual(len(set(collected)), 601)
        self.assertLessEqual(max(window_sizes), 100)
        self.assertEqual(window_sizes, [100, 100, 100, 100, 100, 100, 1])

    def test_recent_preview_is_latest_three_and_redacted(self) -> None:
        result = self.backend.list_sessions(query="Quarter", page_size=20)
        previews = result["items"][0]["previews"]
        self.assertEqual(len(previews), 3)
        self.assertIn("[redacted-token]", " ".join(previews))
        self.assertNotIn("First note", " ".join(previews))

    def test_recent_activity_order_is_stable_across_profiles(self) -> None:
        result = self.backend.list_sessions(page_size="all")
        pairs = [(row["ownerProfile"], row["storedSessionId"]) for row in result["items"]]
        self.assertEqual(pairs[:5], [
            ("default", "d-desktop-1"),
            ("review", "r-telegram-topic"),
            ("default", "d-telegram-dm"),
            ("builder", "z-desktop-tie-b"),
            ("review", "r-desktop-tie-a"),
        ])

    def test_physical_owner_is_authoritative_for_revalidation(self) -> None:
        self.assertEqual(self.backend.revalidate_owner("review", "r-telegram-topic")["ownerProfile"], "review")
        with self.assertRaises(SessionHubError):
            self.backend.revalidate_owner("default", "r-telegram-topic")

    def test_kanban_worker_session_source_is_openable_but_unlisted(self) -> None:
        conn = sqlite3.connect(self.db_by_profile["ops"])
        add_session(conn, sid="20260101_100000_bbbbbb", title="worker run", source="kanban", profile_name="ops", activity=999)
        add_session(conn, sid="20260921_150648_ff0000", title="odd", source="import", profile_name="ops", activity=998)
        conn.commit(); conn.close()
        # D1 (2026-09-22): Kanban worker transcripts have source='kanban'. They must revalidate ...
        self.assertEqual(self.backend.revalidate_owner("ops", "20260101_100000_bbbbbb")["storedSessionId"], "20260101_100000_bbbbbb")
        # ... while unknown sources still fail closed ...
        with self.assertRaises(SessionHubError) as ctx:
            self.backend.revalidate_owner("ops", "20260921_150648_ff0000")
        self.assertEqual(ctx.exception.code, "source_not_allowed")
        # ... and the Session tabs keep listing only desktop/telegram sources.
        listed = {item["storedSessionId"] for item in self.backend.list_sessions(tab="all", profiles=["ops"], date_from=None, page_size=100)["items"]}
        self.assertNotIn("20260101_100000_bbbbbb", listed)

    def test_malformed_or_unknown_session_fails_closed(self) -> None:
        for bad in ("", "../x", "has space", "x" * 129):
            with self.assertRaises(SessionHubError):
                self.backend.revalidate_owner("default", bad)
        with self.assertRaises(SessionHubError):
            self.backend.revalidate_owner("default", "missing-id")

    def test_stale_schema_fails_closed_without_writable_heal(self) -> None:
        stale = sqlite3.connect(self.db_by_profile["search"])
        stale.execute("DELETE FROM schema_version")
        stale.execute("INSERT INTO schema_version(version) VALUES (25)")
        stale.commit()
        stale.close()
        with self.assertRaises(SessionHubError):
            self.backend.list_sessions(profiles=["search"])

    def test_v021_schema_30_is_feature_detected_when_required_columns_exist(self) -> None:
        current = sqlite3.connect(self.db_by_profile["search"])
        current.execute("DELETE FROM schema_version")
        current.execute("INSERT INTO schema_version(version) VALUES (30)")
        current.commit()
        current.close()
        result = self.backend.list_sessions(profiles=["search"])
        self.assertEqual(result["page"]["totalKnown"], 0)

    def test_newer_schema_is_accepted_when_required_columns_exist(self) -> None:
        # 2026-10-13: Hermes bumps schema_version on most updates (0.21.6 -> 31). Newer versions are
        # accepted as long as the required sessions/messages columns are present.
        future = sqlite3.connect(self.db_by_profile["search"])
        future.execute("DELETE FROM schema_version")
        future.execute("INSERT INTO schema_version(version) VALUES (99)")
        future.commit()
        future.close()
        self.backend.list_sessions(profiles=["search"])

    def test_older_than_minimum_schema_still_fails_closed(self) -> None:
        stale = sqlite3.connect(self.db_by_profile["search"])
        stale.execute("DELETE FROM schema_version")
        stale.execute("INSERT INTO schema_version(version) VALUES (25)")
        stale.commit()
        stale.close()
        with self.assertRaises(SessionHubError):
            self.backend.list_sessions(profiles=["search"])

    def test_sql_write_ddl_and_checkpoint_are_rejected(self) -> None:
        with self.backend.read_connection("default") as conn:
            before = self._sha(self.db_by_profile["default"])
            for sql in (
                "INSERT INTO sessions(id) VALUES ('x')",
                "CREATE TABLE should_not_exist(x)",
                "PRAGMA wal_checkpoint(TRUNCATE)",
            ):
                with self.assertRaises(SessionHubError):
                    self.backend.execute_readonly(conn, sql)
            after = self._sha(self.db_by_profile["default"])
        self.assertEqual(before, after)

    def test_fixture_db_integration_and_canary_residue_zero(self) -> None:
        result = self.backend.list_sessions(page_size=20)
        encoded = json.dumps(result)
        self.assertEqual(result["contract"], "sanitized-session-hub-v1")
        self.assertNotIn("d-cron-excluded", encoded)
        self.assertNotIn("live.db", encoded)
        self.assertNotIn("alpha@example.com", encoded)

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
