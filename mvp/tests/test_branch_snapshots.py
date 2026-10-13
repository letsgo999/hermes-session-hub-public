"""Branch snapshot integration: collector output -> SnapshotCache -> Session/Kanban backends.

The collector (tools/session_snapshot.py) is executed for real against a fixture state.db so that the
contract between the branch side and the HQ reader is tested end to end, not with a hand-made manifest.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from mvp.tests.test_backend import SCHEMA_SQL, add_session
from mvp.tests.test_kanban_backend import create_board
from mvp.tests.test_plugin_api import load_plugin_api

MVP = Path(__file__).resolve().parents[1]
REPO = MVP.parent
COLLECTOR = REPO / "tools" / "session_snapshot.py"


def load_collector():
    name = "session_snapshot_collector_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def load_backend():
    name = "session_hub_branch_backend_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MVP / "backend" / "sessionhub.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def make_state_db(path: Path, prefix: str, now: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    add_session(conn, sid=f"{prefix}-recent", title=f"{prefix} recent work", source="desktop", profile_name=None,
                activity=now - 3600, messages=["branch user note", "branch assistant reply", "branch third"])
    add_session(conn, sid=f"{prefix}-old", title=f"{prefix} old work", source="telegram", chat_type="dm",
                profile_name=None, activity=now - 45 * 86400, messages=["too old to copy"])
    conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES (?, 'tool', 'SECRET tool output', ?, 1)",
        (f"{prefix}-recent", now - 3500),
    )
    conn.commit()
    conn.close()


def make_hermes_home(root: Path, prefix: str, now: int) -> Path:
    home = root / f"home-{prefix}"
    (home / "kanban" / "boards" / "current").mkdir(parents=True)
    make_state_db(home / "state.db", prefix, now)
    create_board(home / "kanban" / "boards" / "current" / "kanban.db", [
        (f"{prefix}_t1", f"{prefix} branch card", "branch body", "default", "running", 5, "worker", now - 500, now - 400,
         None, None, None, None, f"{prefix}-recent", None, None, now - 300, None),
        (f"{prefix}_t2", f"{prefix} unassigned", "", None, "todo", 1, "worker", now - 900, None, None, None, None, None,
         None, None, None, None, None),
    ])
    return home


def run_collector(collector, *, device: str, home: Path, sync_root: Path, extra: tuple[str, ...] = ()) -> dict:
    argv = sys.argv
    sys.argv = ["session_snapshot.py", "--device", device, "--hermes-home", str(home), "--sync-root", str(sync_root), *extra]
    try:
        code = collector.main()
    finally:
        sys.argv = argv
    assert code == 0, code
    return json.loads((sync_root / "snapshots" / device / "manifest.json").read_text(encoding="utf-8"))


class BranchSnapshotFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = int(time.time())
        self.module = load_backend()
        self.collector = load_collector()
        self.sync_root = self.root / "SyncFolder"
        self.cache_dir = self.root / "cache"
        self.local: dict[str, Path] = {}
        for profile in self.module.LOCAL_PROFILE_NAMES:
            path = self.root / f"{profile}.db"
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA_SQL)
            add_session(conn, sid=f"{profile}-hq", title=f"HQ {profile}", source="desktop", profile_name=None,
                        activity=self.now - 7200, messages=["hq note"])
            conn.commit()
            conn.close()
            self.local[profile] = path
        self.k_home = make_hermes_home(self.root, "k", self.now)
        self.k_manifest = run_collector(self.collector, device="X", home=self.k_home, sync_root=self.sync_root)
        self.snapshot_dirs = {instance: self.sync_root / "snapshots" / instance for instance in ("X", "Y", "Z")}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cache(self, **kwargs):
        return self.module.SnapshotCache(self.snapshot_dirs, self.cache_dir, **kwargs)

    def backend(self, **kwargs):
        return self.module.SessionHubBackend(self.local, self.cache(**kwargs))

    # --- collector contract -------------------------------------------------------------------

    def test_collector_trims_messages_and_never_copies_tool_rows(self) -> None:
        self.assertEqual(self.k_manifest["contract"], self.module.SNAPSHOT_CONTRACT)
        self.assertEqual(self.k_manifest["state"]["sessions"], 2)
        self.assertEqual(self.k_manifest["state"]["messages"], 3)  # recent user/assistant only; old + tool rows dropped
        self.assertEqual(self.k_manifest["kanban"]["counts"]["tasks"], 2)
        snapshot = self.sync_root / "snapshots" / "X" / self.module.SNAPSHOT_STATE_FILE
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), self.k_manifest["state"]["sha256"])
        snapshot_db = sqlite3.connect(snapshot)
        try:
            rows = snapshot_db.execute("SELECT DISTINCT role FROM messages ORDER BY role").fetchall()
        finally:
            snapshot_db.close()
        self.assertEqual([r[0] for r in rows], ["assistant", "user"])
        # Source databases are untouched (the branch's state.db keeps its tool row and old session).
        source_db = sqlite3.connect(self.k_home / "state.db")
        try:
            self.assertEqual(source_db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0], 1)
        finally:
            source_db.close()

    def test_collector_skips_rewrite_when_source_is_unchanged(self) -> None:
        second = run_collector(self.collector, device="X", home=self.k_home, sync_root=self.sync_root)
        self.assertEqual(second["state"]["sha256"], self.k_manifest["state"]["sha256"])
        self.assertEqual(second["exported_at"], self.k_manifest["exported_at"])
        self.assertGreaterEqual(second["checked_at"], self.k_manifest["checked_at"])

    # --- SnapshotCache freshness ---------------------------------------------------------------

    def test_fresh_stale_unknown_states_follow_manifest_and_ttl(self) -> None:
        statuses = {s["id"]: s for s in self.cache().statuses()}
        self.assertEqual(statuses["X"]["state"], "fresh")
        self.assertEqual(statuses["X"]["label"], "Branch X")
        self.assertEqual(statuses["X"]["sessions"], 2)
        self.assertLessEqual(statuses["X"]["ageSeconds"], 120)
        self.assertEqual((statuses["Y"]["state"], statuses["Y"]["reason"]), ("unknown", "no_snapshot"))
        self.assertEqual((statuses["Z"]["state"], statuses["Z"]["reason"]), ("unknown", "no_snapshot"))
        later = self.cache(now=lambda: self.now + self.module.REMOTE_INSTANCES["X"]["ttlSeconds"] + 60)
        self.assertEqual((later.status("X")["state"], later.status("X")["reason"]), ("stale", "ttl_exceeded"))

    def test_sha_mismatch_keeps_previous_verified_copy_and_reports_stale(self) -> None:
        first = self.cache()
        verified = first.state_db("X")
        self.assertIsNotNone(verified)
        self.assertTrue(str(verified).startswith(str(self.cache_dir)))
        # Drive delivers a new manifest before the new database file: sha no longer matches.
        manifest_path = self.sync_root / "snapshots" / "X" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = self.cache()
        self.assertEqual(second.state_db("X"), verified)
        self.assertEqual((second.status("X")["state"], second.status("X")["reason"]), ("stale", "sync_in_progress"))
        # Without any previous copy the instance is damaged and not read at all.
        fresh_cache = self.module.SnapshotCache(self.snapshot_dirs, self.root / "empty-cache")
        self.assertIsNone(fresh_cache.state_db("X"))
        self.assertEqual(fresh_cache.status("X")["state"], "damaged")

    def test_unsupported_schema_and_foreign_contract_are_isolated(self) -> None:
        manifest_path = self.sync_root / "snapshots" / "X" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"]["schema_version"] = 25
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status = self.cache().status("X")
        self.assertEqual((status["state"], status["reason"]), ("unsupported", "schema_25"))
        manifest["state"]["schema_version"] = 99  # newer than verified is accepted (column checks still apply)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertNotEqual(self.cache().status("X")["state"], "unsupported")
        manifest["contract"] = "something-else"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.cache().status("X")["reason"], "contract_mismatch")
        with self.assertRaises(self.module.SessionHubError):
            self.module.SnapshotCache({"Q": self.root}, None)

    # --- SessionHubBackend --------------------------------------------------------------------

    def test_sessions_merge_branch_rows_and_isolate_missing_instances(self) -> None:
        result = self.backend().list_sessions(tab="all", page_size=50)
        by_id = {item["storedSessionId"]: item for item in result["items"]}
        self.assertIn("k-recent", by_id)
        self.assertEqual(by_id["k-recent"]["ownerProfile"], "X/default")
        self.assertEqual((by_id["k-recent"]["instance"], by_id["k-recent"]["remote"]), ("X", True))
        self.assertEqual(by_id["k-recent"]["previews"], ["branch third", "branch assistant reply", "branch user note"])
        self.assertEqual((by_id["default-hq"]["instance"], by_id["default-hq"]["remote"]), ("HQ", False))
        self.assertIn("k-old", by_id)  # listed (sessions table is complete) even though its messages were trimmed
        self.assertEqual(by_id["k-old"]["previews"], [])
        self.assertEqual({s["profile"] for s in result["skippedProfiles"]}, {"Y/default", "Z/default"})
        self.assertEqual({s["error"] for s in result["skippedProfiles"]}, {"snapshot_unavailable"})
        states = {s["id"]: s["state"] for s in result["instances"]}
        self.assertEqual(states, {"HQ": "local", "X": "fresh", "Y": "unknown", "Z": "unknown"})

    def test_profile_filter_and_cursor_accept_remote_keys(self) -> None:
        backend = self.backend()
        only_k = backend.list_sessions(tab="all", profiles=["X/default"], page_size=10)
        self.assertEqual({item["ownerProfile"] for item in only_k["items"]}, {"X/default"})
        first = backend.list_sessions(tab="all", page_size=10, profiles=["default", "X/default"])
        self.assertIsNone(first["nextCursor"])
        paged = backend.list_sessions(tab="all", page_size=10, profiles=["default", "X/default"], cursor=None)
        self.assertEqual(len(paged["items"]), 3)
        encoded = self.module._encode_cursor(paged["items"][0])
        self.assertEqual(self.module._decode_cursor(encoded)[1], paged["items"][0]["ownerProfile"])
        with self.assertRaises(self.module.SessionHubError) as ctx:
            backend.list_sessions(tab="all", profiles=["Q/default"])
        self.assertEqual(ctx.exception.code, "bad_profile")

    def test_one_stale_local_profile_is_skipped_but_all_failing_fails_closed(self) -> None:
        stale = sqlite3.connect(self.local["review"])
        stale.execute("DELETE FROM schema_version")
        stale.execute("INSERT INTO schema_version(version) VALUES (25)")
        stale.commit()
        stale.close()
        result = self.backend().list_sessions(tab="all")
        self.assertIn({"profile": "review", "instance": "HQ", "error": "stale_schema"}, result["skippedProfiles"])
        telegram = self.backend().list_sessions(tab="telegram")
        self.assertIn("review", [row["profile"] for row in telegram["skippedProfiles"]])
        with self.assertRaises(self.module.SessionHubError) as ctx:
            self.backend().list_sessions(tab="all", profiles=["review"])
        self.assertEqual(ctx.exception.code, "stale_schema")

    def test_remote_rows_cannot_be_opened_and_snapshot_is_write_protected(self) -> None:
        backend = self.backend()
        with self.assertRaises(self.module.SessionHubError) as ctx:
            backend.revalidate_owner("X/default", "k-recent")
        self.assertEqual(ctx.exception.code, "remote_instance")
        probe = backend.measure_authorizer_write_denial("X/default")
        self.assertTrue(probe["writeProbeRejected"])
        # A 0.4.x client that omits profiles still gets HQ rows plus every configured branch.
        without_snapshots = self.module.SessionHubBackend(self.local)
        self.assertEqual(without_snapshots.available_profiles(), self.module.LOCAL_PROFILE_NAMES)
        self.assertEqual([s["id"] for s in without_snapshots.instance_status()], ["HQ"])
        skipped = without_snapshots.list_sessions(tab="all", profiles=["default", "X/default"])
        self.assertEqual(skipped["skippedProfiles"], [{"profile": "X/default", "instance": "X", "error": "instance_not_configured"}])

    # --- KanbanHubBackend ---------------------------------------------------------------------

    def test_branch_kanban_board_is_published_as_instance_board(self) -> None:
        local_board = self.root / "team.db"
        create_board(local_board, [
            ("hq_t1", "HQ card", "", "builder", "running", 5, "worker", self.now - 100, None, None, None, None, None, None, None, None, None, None),
        ])
        backend = self.module.KanbanHubBackend({"team": local_board}, self.local, self.cache())
        result = backend.list_cards(page_size=50)
        by_id = {card["id"]: card for card in result["items"]}
        self.assertEqual(set(by_id), {"hq_t1", "k_t1", "k_t2"})
        self.assertEqual((by_id["k_t1"]["board"], by_id["k_t1"]["instance"], by_id["k_t1"]["remote"]), ("X", "X", True))
        self.assertEqual(by_id["k_t1"]["ownerProfile"], "X/default")
        self.assertEqual(by_id["k_t1"]["sourceInstance"], "X")
        self.assertIsNone(by_id["k_t2"]["ownerProfile"])
        self.assertEqual((by_id["hq_t1"]["instance"], by_id["hq_t1"]["remote"]), ("HQ", False))
        self.assertEqual({s["id"]: s["state"] for s in result["instances"]}["X"], "fresh")
        only_k = backend.list_cards(assignees=["X/default"], page_size=50)
        self.assertEqual([card["id"] for card in only_k["items"]], ["k_t1"])
        legacy_all = backend.list_cards(assignees=list(self.module.LOCAL_PROFILE_NAMES), page_size=50)
        self.assertEqual(len(legacy_all["items"]), 3)
        detail = backend.get_card("X", "k_t1")
        self.assertEqual(detail["originSession"]["state"], "remote")
        self.assertFalse(detail["originSession"]["available"])

    def test_damaged_branch_board_is_skipped_not_fatal(self) -> None:
        local_board = self.root / "team.db"
        create_board(local_board, [
            ("hq_t1", "HQ card", "", "builder", "running", 5, "worker", self.now - 100, None, None, None, None, None, None, None, None, None, None),
        ])
        cache = self.cache()
        cached_kanban = cache.kanban_db("X")
        assert cached_kanban is not None
        cached_kanban.write_bytes(b"not a database at all")
        backend = self.module.KanbanHubBackend({"team": local_board}, self.local, cache)
        result = backend.list_cards(page_size=50)
        self.assertEqual([card["id"] for card in result["items"]], ["hq_t1"])
        self.assertEqual(result["skippedRemoteBoards"], [{"board": "X", "instance": "X", "error": "kanban_read_failed"}])
        self.assertEqual(result["skippedBoards"], 1)
        # No local board at all but branch boards present is still a usable Kanban tab.
        clean_cache = self.module.SnapshotCache(self.snapshot_dirs, self.root / "clean-cache")
        fresh = self.module.KanbanHubBackend({}, self.local, clean_cache)
        self.assertEqual({card["board"] for card in fresh.list_cards(page_size=50)["items"]}, {"X"})


class BranchSnapshotApiFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = int(time.time())
        collector = load_collector()
        self.local: dict[str, Path] = {}
        for profile in ("default", "ops", "review", "builder", "search"):
            path = self.root / f"{profile}.db"
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA_SQL)
            add_session(conn, sid=f"{profile}-hq", title=f"HQ {profile}", source="desktop", profile_name=None,
                        activity=self.now - 7200, messages=["hq note"])
            conn.commit()
            conn.close()
            self.local[profile] = path
        self.sync_root = self.root / "SyncFolder"
        for device in ("X", "Z"):
            run_collector(collector, device=device, home=make_hermes_home(self.root, device.lower(), self.now), sync_root=self.sync_root)
        self.kanban = self.root / "team.db"
        create_board(self.kanban, [
            ("hq_t1", "HQ card", "", "builder", "running", 5, "worker", self.now - 100, None, None, None, None, None, None, None, None, None, None),
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_api_exposes_instances_and_accepts_eight_profiles(self) -> None:
        module = load_plugin_api()
        module.configure_backend_for_tests(self.local)
        module.configure_kanban_for_tests({"team": self.kanban})
        module.configure_snapshots_for_tests(
            {instance: self.sync_root / "snapshots" / instance for instance in ("X", "Y", "Z")}, self.root / "cache"
        )
        app = FastAPI()
        app.include_router(module.router, prefix="/api/plugins/session-hub")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/api/plugins/session-hub/health")
            instances = await client.get("/api/plugins/session-hub/instances")
            sessions = await client.post(
                "/api/plugins/session-hub/sessions",
                json={"tab": "all", "profiles": list(module.ALL_PROFILE_NAMES), "pageSize": 50, "historyScope": "30d"},
            )
            legacy = await client.post(
                "/api/plugins/session-hub/sessions",
                json={"tab": "all", "profiles": list(module.PROFILE_NAMES), "pageSize": 50, "historyScope": "30d"},
            )
            cards = await client.post(
                "/api/plugins/session-hub/kanban",
                json={"profiles": list(module.ALL_PROFILE_NAMES), "pageSize": 50, "historyScope": "full"},
            )
            remote_card = await client.get("/api/plugins/session-hub/kanban/Z/z_t1")
            revalidate = await client.post(
                "/api/plugins/session-hub/revalidate", json={"ownerProfile": "X/default", "storedSessionId": "x-recent"}
            )
        module.configure_snapshots_for_tests(None)
        self.assertEqual(health.json()["allProfiles"], list(module.ALL_PROFILE_NAMES))
        states = {item["id"]: item["state"] for item in instances.json()["instances"]}
        self.assertEqual(states, {"HQ": "local", "X": "fresh", "Y": "unknown", "Z": "fresh"})
        owners = {item["ownerProfile"] for item in sessions.json()["items"]}
        self.assertEqual(owners, {*module.PROFILE_NAMES, "X/default", "Z/default"})
        self.assertEqual(sessions.json()["skippedProfiles"], [{"profile": "Y/default", "instance": "Y", "error": "snapshot_unavailable"}])
        self.assertEqual({item["ownerProfile"] for item in legacy.json()["items"]}, set(module.PROFILE_NAMES))
        self.assertEqual({card["board"] for card in cards.json()["items"]}, {"team", "X", "Z"})
        self.assertEqual((remote_card.status_code, remote_card.json()["instance"], remote_card.json()["remote"]), (200, "Z", True))
        self.assertEqual((revalidate.status_code, revalidate.json()), (400, {"ok": False, "error": "remote_instance"}))


if __name__ == "__main__":
    unittest.main()
