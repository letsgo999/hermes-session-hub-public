from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from tests.test_project_hub import KANBAN_SCHEMA

MVP = Path(__file__).resolve().parents[1]
PLUGIN_API = MVP / "plugins" / "session-hub" / "dashboard" / "plugin_api.py"


def load_api():
    name = "session_hub_project_api_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_API)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class ProjectApiFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.kanban = self.root / "kanban.db"
        conn = sqlite3.connect(self.kanban)
        conn.executescript(KANBAN_SCHEMA)
        for index, (task_id, name) in enumerate((
            ("t_hub", "AlphaSite"),
            ("t_knowledge", "BetaReports"),
            ("t_shadow", "GammaAutomation"),
        )):
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, name, "fixture", "builder", "running", index + 1, index + 1, None, None, str(self.root / name), None, f"session-{index}", None),
            )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_project_routes_refresh_detail_override_export_and_restore(self) -> None:
        module = load_api()
        registry = self.root / "registry.sqlite3"
        module.configure_project_hub_for_tests(registry, self.kanban)
        app = FastAPI()
        app.include_router(module.router, prefix="/api/plugins/session-hub")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            refreshed = await client.post("/api/plugins/session-hub/projects/refresh")
            portfolio = await client.get("/api/plugins/session-hub/projects")
            detail = await client.get("/api/plugins/session-hub/projects/alpha-site")
            updated = await client.patch("/api/plugins/session-hub/projects/alpha-site/overrides", json={"priority": 42, "nextAction": "Open source"})
            exported = await client.get("/api/plugins/session-hub/registry/export")
            restored = await client.post("/api/plugins/session-hub/registry/restore", json=exported.json())

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual([p["id"] for p in portfolio.json()["projects"]], [
            "alpha-site", "beta-reports", "gamma-automation"
        ])
        self.assertEqual(detail.json()["tasks"][0]["id"], "t_hub")
        self.assertEqual(updated.json()["effective"]["priority"], 42)
        self.assertEqual(exported.json()["contract"], "project-hub-registry-snapshot-v1")
        self.assertEqual(restored.json()["ok"], True)
        self.assertTrue(registry.is_file())


if __name__ == "__main__":
    unittest.main()
