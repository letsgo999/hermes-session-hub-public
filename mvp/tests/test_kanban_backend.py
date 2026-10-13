from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


MVP = Path(__file__).resolve().parents[1]
BACKEND_PATH = MVP / "plugins" / "session-hub" / "dashboard" / "sessionhub_backend.py"

KANBAN_SCHEMA = """
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
 priority INTEGER, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
 completed_at INTEGER, due_at INTEGER, workspace_path TEXT, result TEXT, session_id TEXT,
 project_id TEXT, block_kind TEXT, last_heartbeat_at INTEGER, current_run_id INTEGER
);
CREATE TABLE task_runs(
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL,
 started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT
);
CREATE TABLE task_links(parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
"""


def load_backend_module():
    name = "session_hub_kanban_backend_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, BACKEND_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("backend module is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def create_board(path: Path, rows: list[tuple], runs: list[tuple] = (), links: list[tuple] = ()) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(KANBAN_SCHEMA)
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)", runs)
    conn.executemany("INSERT INTO task_links VALUES (?,?)", links)
    conn.commit()
    conn.close()


def create_session_store(path: Path, session_ids: tuple[str, ...] = (), sources: dict[str, str] | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
    conn.executemany("INSERT INTO sessions(id, source) VALUES (?, ?)", ((value, (sources or {}).get(value, "desktop")) for value in session_ids))
    conn.commit()
    conn.close()


class KanbanHubBackendFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_backend_module()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.default = root / "default.db"
        self.team = root / "team.db"
        create_board(self.default, [
            ("t_old", "Old card", "archive", "default", "done", 1, "worker", 100, 110, 120, None, None, "done", None, None, None, None, None),
            ("t_active", "Needle implementation", "contact owner@example.com TOKEN_ABCDEF123456", "builder", "running", 9, "worker", 200, 210, None, 500, None, None, "s-origin", "proj-a", None, 230, 7),
        ], runs=[(7, "t_active", "ops", "running", 210, None, None)])
        team_rows = [
            ("t_blocked", "Blocked card", "needle in body", "review", "blocked", 8, "worker", 300, 310, None, 600, None, None, None, "proj-b", "needs_input", 320, 11),
            ("t_ready", "Ready card", "other", "search", "ready", 3, "worker", 400, None, None, None, None, None, None, None, None, None, None),
        ]
        team_rows.extend(
            (f"t_extra_{index}", f"Extra {index}", "paging", "builder", "ready", 1, "worker", 500 + index, None, None, None, None, None, None, None, None, None, None)
            for index in range(9)
        )
        create_board(self.team, team_rows, runs=[(11, "t_blocked", "review", "running", 310, None, None)], links=[("t_blocked", "t_ready")])
        for path in (self.default, self.team):
            conn = sqlite3.connect(path)
            conn.execute("ALTER TABLE tasks ADD COLUMN source_instance TEXT")
            conn.execute("ALTER TABLE task_runs ADD COLUMN metadata TEXT")
            conn.commit()
            conn.close()
        conn = sqlite3.connect(self.default)
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=7",
            (json.dumps({"worker_session_id": "s-worker"}),),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.team)
        conn.execute("UPDATE tasks SET source_instance='X', session_id='s-origin' WHERE id='t_ready'")
        conn.commit()
        conn.close()
        self.paths = {"default": self.default, "team": self.team}
        self.profile_paths = {}
        for profile in ("default", "ops", "review", "builder", "search"):
            path = root / f"profile-{profile}.db"
            ids = ("s-origin",) if profile == "default" else ("s-worker", "s-worker-odd") if profile == "ops" else ()
            # Real worker transcripts are stored with source='kanban' (D1 regression).
            create_session_store(path, ids, {"s-worker": "kanban", "s-worker-odd": "import"})
            self.profile_paths[profile] = path
        all_paths = {f"board:{name}": path for name, path in self.paths.items()}
        all_paths.update({f"profile:{name}": path for name, path in self.profile_paths.items()})
        self.before = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in all_paths.items()}
        self.backend = self.module.KanbanHubBackend(self.paths, self.profile_paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_search_date_assignee_status_and_board_metadata(self) -> None:
        result = self.backend.list_cards(
            query="needle", assignees=["review"], statuses=["blocked"],
            date_from=250, date_to=350, page_size=20,
        )
        self.assertEqual(result["contract"], "sanitized-kanban-hub-v1")
        self.assertEqual(result["page"]["totalKnown"], 1)
        self.assertEqual([card["id"] for card in result["items"]], ["t_blocked"])
        card = result["items"][0]
        self.assertEqual(card["board"], "team")
        self.assertEqual(card["assignee"], "review")
        self.assertEqual(card["dueAt"], 600)
        self.assertEqual(card["projectId"], "proj-b")
        self.assertEqual(card["childIds"], ["t_ready"])
        self.assertEqual(card["currentRun"], {"id": 11, "profile": "review", "status": "running", "startedAt": 310})
        self.assertTrue(card["blocked"])
        self.assertEqual(card["blockKind"], "needs_input")

    def test_paging_is_stable_across_boards_without_duplicates(self) -> None:
        first = self.backend.list_cards(page_size=10)
        second = self.backend.list_cards(page_size=10, cursor=first["nextCursor"])
        ids = [(row["board"], row["id"]) for row in first["items"] + second["items"]]
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(second["items"]), 3)
        self.assertEqual(len(ids), 13)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIsNone(second["nextCursor"])

    def test_exact_card_detail_never_substitutes_another_board_or_id(self) -> None:
        card = self.backend.get_card("default", "t_active")
        self.assertEqual((card["board"], card["id"]), ("default", "t_active"))
        self.assertEqual(card["body"], "contact [redacted-email] [redacted-token]")
        self.assertEqual(card["originSession"], {
            "kind": "origin", "state": "available", "available": True,
            "ownerProfile": "default", "storedSessionId": "s-origin", "reason": None,
        })
        self.assertEqual(card["latestWorkerSession"], {
            "kind": "worker", "state": "available", "available": True,
            "ownerProfile": "ops", "storedSessionId": "s-worker", "reason": None,
            "runId": 7,
        })
        self.assertEqual(card["latestRun"]["id"], 7)
        with self.assertRaises(self.module.SessionHubError) as unknown:
            self.backend.get_card("team", "t_active")
        self.assertEqual(unknown.exception.code, "unknown_kanban_card")

    def test_missing_and_remote_sessions_are_explicit_and_never_substituted(self) -> None:
        missing = self.backend.get_card("default", "t_old")
        self.assertEqual(missing["originSession"]["state"], "absent")
        self.assertFalse(missing["originSession"]["available"])
        self.assertEqual(missing["latestWorkerSession"]["state"], "absent")

        remote = self.backend.get_card("team", "t_ready")
        self.assertEqual(remote["sourceInstance"], "X")
        self.assertEqual(remote["originSession"]["state"], "remote")
        self.assertFalse(remote["originSession"]["available"])
        self.assertIn("remote", remote["originSession"]["reason"].lower())

    def test_card_detail_probes_sessions_for_the_requested_card_only(self) -> None:
        calls: list[tuple[str, str]] = []
        original = self.backend._session_exists

        def counting(profile: str, session_id: str) -> bool:
            calls.append((profile, session_id))
            return original(profile, session_id)

        self.backend._session_exists = counting  # type: ignore[method-assign]
        self.backend.get_card("default", "t_active")
        # one card: at most one origin probe per profile plus one worker probe (previously every card on the board)
        self.assertLessEqual(len(calls), len(self.backend._db_by_profile) + 1)

    def test_unassigned_cards_show_when_all_profiles_selected(self) -> None:
        conn = sqlite3.connect(self.team)
        conn.execute("UPDATE tasks SET assignee=NULL WHERE id='t_extra_0'")
        conn.execute("UPDATE tasks SET assignee='outsider' WHERE id='t_extra_1'")
        conn.commit(); conn.close()
        ids = {card["id"] for card in self.backend.list_cards(query="", page_size=100, date_from=None)["items"]}
        self.assertTrue({"t_extra_0", "t_extra_1"}.issubset(ids))
        narrowed = {card["id"] for card in self.backend.list_cards(query="", assignees=["builder"], page_size=100, date_from=None)["items"]}
        self.assertNotIn("t_extra_0", narrowed)

    def test_badly_named_board_is_skipped_not_fatal(self) -> None:
        backend = self.module.KanbanHubBackend({**self.paths, "보드 이름": self.team}, self.profile_paths)
        result = backend.list_cards(query="", page_size=100, date_from=None)
        self.assertEqual(result["skippedBoards"], 1)
        self.assertTrue(result["items"])

    def test_worker_link_marks_unopenable_source_instead_of_available(self) -> None:
        conn = sqlite3.connect(self.default)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=7", (json.dumps({"worker_session_id": "s-worker-odd"}),))
        conn.commit(); conn.close()
        link = self.backend.get_card("default", "t_active")["latestWorkerSession"]
        self.assertEqual((link["state"], link["available"], link["ownerProfile"], link["storedSessionId"]), ("not_openable", False, "ops", "s-worker-odd"))
        # restore for the byte-identical DB test
        conn = sqlite3.connect(self.default)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=7", (json.dumps({"worker_session_id": "s-worker"}),))
        conn.commit(); conn.close()

    def test_invalid_filters_and_board_fail_closed(self) -> None:
        with self.assertRaises(self.module.SessionHubError):
            self.backend.list_cards(assignees=["remote-profile"])
        with self.assertRaises(self.module.SessionHubError):
            self.backend.list_cards(statuses=["not-a-status"])
        with self.assertRaises(self.module.SessionHubError):
            self.backend.get_card("../default", "t_active")

    def test_all_source_databases_remain_byte_identical(self) -> None:
        self.backend.list_cards(page_size="all")
        self.backend.get_card("team", "t_blocked")
        all_paths = {f"board:{name}": path for name, path in self.paths.items()}
        all_paths.update({f"profile:{name}": path for name, path in self.profile_paths.items()})
        after = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in all_paths.items()}
        self.assertEqual(after, self.before)


if __name__ == "__main__":
    unittest.main()
