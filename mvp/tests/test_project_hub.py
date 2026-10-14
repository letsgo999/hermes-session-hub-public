from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.project_hub import ProjectHub, ProjectHubError, derive_projects

TEST_PROJECTS = [
    {"id": "alpha-site", "name": "AlphaSite", "goal": "Alpha site rebuild", "aliases": ["alpha site"]},
    {"id": "beta-reports", "name": "BetaReports", "goal": "Weekly beta reports", "aliases": ["beta reports"]},
    {"id": "gamma-automation", "name": "GammaAutomation", "goal": "Gamma automation runs", "aliases": ["gamma automation"]},
]


KANBAN_SCHEMA = """
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
 priority INTEGER, created_at INTEGER, started_at INTEGER, completed_at INTEGER,
 workspace_path TEXT, result TEXT, session_id TEXT, project_id TEXT
);
CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER);
CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_attachments(id INTEGER PRIMARY KEY, task_id TEXT, filename TEXT, path TEXT);
"""


class ProjectHubFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.kanban = self.root / "kanban.db"
        conn = sqlite3.connect(self.kanban)
        conn.executescript(KANBAN_SCHEMA)
        rows = [
            ("t_orig01", "Alpha site original", "first implementation", "builder", "done", 7, 10, 11, 12, str(self.root / "AlphaSite"), "superseded", "sess-original", None),
            ("t_recov01", "[복구] Alpha site", "recovery replacement for t_orig01", "builder", "running", 11, 20, 21, None, str(self.root / "AlphaSite"), None, "sess-recovery", None),
            ("t_knowledge", "BetaReports batch", "deterministic path", "search", "blocked", 5, 30, 31, None, str(self.root / "BetaReports"), None, None, None),
            ("t_shadow", "Gamma automation", "deterministic path", "default", "ready", 4, 40, None, None, str(self.root / "GammaAutomation"), None, None, None),
            ("t_generic", "site report copy", "common words only", "default", "todo", 1, 45, None, None, None, None, None, None),
            ("t_ambiguous", "Alpha site vs beta reports", "mentions several pilots", "default", "todo", 1, 50, None, None, None, None, None, None),
        ]
        conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", ("t_orig01", "t_recov01"))
        conn.commit()
        conn.close()
        self.before = hashlib.sha256(self.kanban.read_bytes()).hexdigest()
        self.registry = self.root / "project-hub.sqlite3"
        self.hub = ProjectHub(self.registry, self.kanban, projects=TEST_PROJECTS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_discovers_three_projects_and_separates_ambiguous_candidate(self) -> None:
        result = self.hub.refresh()
        self.assertEqual([p["id"] for p in result["projects"]], [
            "alpha-site", "beta-reports", "gamma-automation"
        ])
        self.assertEqual(result["sourceReadonly"], True)
        self.assertEqual([c["sourceId"] for c in result["unclassified"]], ["t_ambiguous"])
        links = {(link["projectId"], link["sourceId"]) for link in result["links"]}
        self.assertIn(("alpha-site", "t_recov01"), links)
        self.assertIn(("beta-reports", "t_knowledge"), links)
        self.assertEqual(hashlib.sha256(self.kanban.read_bytes()).hexdigest(), self.before)

    def test_groups_original_and_recovery_as_one_logical_work_item(self) -> None:
        self.hub.refresh()
        detail = self.hub.project_detail("alpha-site")
        groups = [set(item["memberIds"]) for item in detail["logicalWork"]]
        self.assertIn({"t_orig01", "t_recov01"}, groups)
        self.assertTrue(any(task["id"] == "t_recov01" and task["sessionId"] == "sess-recovery" for task in detail["tasks"]))

    def test_override_is_audited_exported_and_rollback_restores_value(self) -> None:
        self.hub.refresh()
        baseline = self.hub.export_snapshot()
        updated = self.hub.update_override("alpha-site", {
            "priority": 99, "nextAction": "Run local acceptance", "ownerProfile": "builder"
        })
        self.assertEqual(updated["effective"]["priority"], 99)
        self.assertEqual(updated["effective"]["nextAction"], "Run local acceptance")
        after = self.hub.export_snapshot()
        self.assertGreater(len(after["auditEvents"]), len(baseline["auditEvents"]))
        restored = self.hub.restore_snapshot(baseline)
        self.assertTrue(restored["ok"])
        detail = self.hub.project_detail("alpha-site")
        self.assertNotEqual(detail["effective"]["priority"], 99)

    def test_invalid_registry_update_is_rejected(self) -> None:
        self.hub.refresh()
        with self.assertRaises(ProjectHubError):
            self.hub.update_override("alpha-site", {"delete": True})

    def test_restore_rejects_injected_column_names(self) -> None:
        self.hub.refresh()
        snapshot = self.hub.export_snapshot()
        before = self.hub.export_snapshot()["tables"]["audit_events"]
        injected = "event_type,project_id,payload_json,created_at) SELECT name,NULL,COALESCE(sql,1),? FROM sqlite_master --"
        snapshot["tables"]["audit_events"] = [{injected: 1}]
        with self.assertRaises(ProjectHubError):
            self.hub.restore_snapshot(snapshot)
        self.assertEqual(self.hub.export_snapshot()["tables"]["audit_events"], before)

    def test_restore_rejects_malformed_rows_without_partial_write(self) -> None:
        self.hub.refresh()
        snapshot = self.hub.export_snapshot()
        snapshot["tables"]["projects"] = ["not-a-row"]
        with self.assertRaises(ProjectHubError):
            self.hub.restore_snapshot(snapshot)
        self.assertEqual(len(self.hub.portfolio()["projects"]), 3)

    def test_bad_override_values_are_rejected_and_reads_stay_healthy(self) -> None:
        self.hub.refresh()
        for change in ({"priority": "high"}, {"priority": True}, {"dueAt": "tomorrow"}, {"ownerProfile": "stranger"}, {"pinned": "yes"}, {"nextAction": "x" * 501}):
            with self.assertRaises(ProjectHubError):
                self.hub.update_override("alpha-site", change)
        self.assertEqual(len(self.hub.portfolio()["projects"]), 3)

    def test_legacy_invalid_override_row_is_ignored_on_read(self) -> None:
        self.hub.refresh()
        conn = sqlite3.connect(self.hub.registry_path)
        conn.execute("INSERT OR REPLACE INTO project_overrides(project_id,field,value_json,updated_at) VALUES ('alpha-site','priority','\"high\"',1)")
        conn.commit(); conn.close()
        portfolio = self.hub.portfolio()
        self.assertTrue(all(isinstance(row["priority"], int) for row in portfolio["actions"]))

    def test_without_configured_projects_kanban_folders_become_projects(self) -> None:
        hub = ProjectHub(self.root / "auto.sqlite3", self.kanban)
        result = hub.refresh()
        ids = [p["id"] for p in result["projects"]]
        self.assertEqual(sorted(ids), ["alphasite", "betareports", "gammaautomation"])
        self.assertEqual(hub.portfolio()["contract"], "project-hub-v1")
        self.assertEqual(derive_projects([{"project_id": "../", "workspace_path": "C:\\"}]), ())

    def test_empty_kanban_gives_an_empty_portfolio_not_an_error(self) -> None:
        empty = self.root / "empty-kanban.db"
        conn = sqlite3.connect(empty)
        conn.executescript(KANBAN_SCHEMA)
        conn.close()
        hub = ProjectHub(self.root / "empty-registry.sqlite3", empty)
        self.assertEqual(hub.portfolio()["projects"], [])
        refreshed = hub.refresh()
        self.assertEqual((refreshed["contract"], refreshed["projects"], refreshed["actions"]), ("project-hub-v1", [], []))

    def test_installable_project_hub_is_byte_identical_to_canonical(self) -> None:
        mvp = Path(__file__).resolve().parents[1]
        import backend.project_hub as development

        installable = mvp / "plugins" / "session-hub" / "dashboard" / "project_hub.py"
        # backend/project_hub.py is an alias that executes the installable file itself.
        self.assertEqual(Path(development.__file__).resolve(), installable.resolve())
        self.assertLess(len((mvp / "backend" / "project_hub.py").read_text(encoding="utf-8")), 800)

    def test_common_words_do_not_make_unclassified_candidates(self) -> None:
        result = self.hub.refresh()
        self.assertNotIn("t_generic", [c["sourceId"] for c in result["unclassified"]])

    def test_refresh_drops_links_for_tasks_that_moved_project(self) -> None:
        self.hub.refresh()
        conn = sqlite3.connect(self.kanban)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id='t_knowledge'", (str(self.root / "GammaAutomation"),))
        conn.commit(); conn.close()
        result = self.hub.refresh()
        links = {(link["projectId"], link["sourceId"]) for link in result["links"]}
        self.assertIn(("gamma-automation", "t_knowledge"), links)
        self.assertNotIn(("beta-reports", "t_knowledge"), links)

    def test_source_path_with_hash_opens_the_right_file(self) -> None:
        odd = self.root / "board#1"
        odd.mkdir()
        target = odd / "kanban.db"
        target.write_bytes(self.kanban.read_bytes())
        hub = ProjectHub(self.root / "odd-registry.sqlite3", target)
        result = hub.refresh()
        self.assertEqual(len(result["projects"]), 3)
        self.assertTrue(result["links"])


if __name__ == "__main__":
    unittest.main()
