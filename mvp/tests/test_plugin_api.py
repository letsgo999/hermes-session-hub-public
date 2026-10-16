from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from typing import Any
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI

from mvp.tests.test_backend import SCHEMA_SQL, add_session
from mvp.tests.test_kanban_backend import create_board


MVP = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = MVP / "plugins" / "session-hub"


def load_plugin_api():
    path = PLUGIN_ROOT / "dashboard" / "plugin_api.py"
    module_name = "session_hub_plugin_api_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin_api.py is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class UnifiedPackageFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_by_profile: dict[str, Path] = {}
        for index, profile in enumerate(("default", "ops", "review", "builder", "search")):
            path = root / f"{profile}.db"
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA_SQL)
            add_session(
                conn,
                sid=f"{profile}-desktop",
                title=f"Fixture {profile}",
                source="desktop",
                profile_name="ignored",
                activity=100 + index,
                messages=["sanitized fixture preview"],
            )
            conn.commit()
            conn.close()
            self.db_by_profile[profile] = path
        self.kanban = root / "team.db"
        create_board(self.kanban, [
            ("t_api", "API card", "search body", "builder", "running", 7, "worker", 200, 210, None, None, None, None, "default-desktop", "proj-api", None, 220, 4),
        ], runs=[(4, "t_api", "ops", "running", 210, None, None)])
        conn = sqlite3.connect(self.kanban)
        conn.execute("ALTER TABLE task_runs ADD COLUMN metadata TEXT")
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=4",
            (json.dumps({"worker_session_id": "ops-desktop"}),),
        )
        conn.commit()
        conn.close()
        self.hermes_home = root / "hermes"
        skill = self.hermes_home / "skills" / "fixture-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: fixture-skill\ndescription: Use when fixture automation is needed.\ntags: [fixture]\n---\n# Fixture\n",
            encoding="utf-8",
        )
        self.canonical_skills = root / "canonical-skills"
        canonical = self.canonical_skills / "fixture-skill" / "SKILL.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(skill.read_bytes())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_official_unified_package_tree_and_manifests(self) -> None:
        required = {
            "plugin.yaml",
            "__init__.py",
            "dashboard/manifest.json",
            "dashboard/plugin_api.py",
            "dashboard/skills_index.py",
            "dashboard/project_hub.py",
            "dashboard/sessionhub_backend.py",
            "desktop/plugin.js",
        }
        actual = {
            path.relative_to(PLUGIN_ROOT).as_posix()
            for path in PLUGIN_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertTrue(required.issubset(actual))
        plugin_manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        dashboard_manifest = json.loads((PLUGIN_ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_manifest["name"], "session-hub")
        self.assertEqual(plugin_manifest["kind"], "standalone")
        self.assertEqual(dashboard_manifest["name"], "session-hub")
        self.assertEqual(dashboard_manifest["api"], "plugin_api.py")

    async def test_router_mounts_real_namespace_with_no_store_responses(self) -> None:
        module = load_plugin_api()
        module.configure_backend_for_tests(self.db_by_profile)
        module.configure_kanban_for_tests({"team": self.kanban})
        module.configure_skills_for_tests(self.hermes_home, self.canonical_skills, ())
        app = FastAPI()
        app.state.auth_required = False
        app.state.bound_host = "127.0.0.1"
        app.include_router(module.router, prefix="/api/plugins/session-hub")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/api/plugins/session-hub/health")
            sessions = await client.post(
                "/api/plugins/session-hub/sessions",
                json={"tab": "desktop", "profiles": ["review"], "pageSize": 20, "historyScope": "full"},
            )
            cards = await client.post(
                "/api/plugins/session-hub/kanban",
                json={"query": "API", "profiles": ["builder"], "statuses": ["running"], "pageSize": 20, "historyScope": "full"},
            )
            exact = await client.get("/api/plugins/session-hub/kanban/team/t_api")
            missing_card = await client.get("/api/plugins/session-hub/kanban/team/t_missing")
            rejected = await client.post(
                "/api/plugins/session-hub/revalidate",
                json={"ownerProfile": "not-allowed", "storedSessionId": "x"},
            )
            missing = await client.get("/api/plugins/session-hub/not-a-route")
            bad_date = await client.post(
                "/api/plugins/session-hub/sessions",
                json={"tab": "all", "historyScope": "custom", "dateFrom": "abc"},
            )
            skills = await client.post(
                "/api/plugins/session-hub/skills/search",
                json={"query": "fixture auto", "includeArchive": False},
            )
            refreshed_skills = await client.post(
                "/api/plugins/session-hub/skills/refresh",
                json={"query": "fixture", "includeArchive": False},
            )

        self.assertEqual(
            (health.status_code, sessions.status_code, cards.status_code, exact.status_code, missing_card.status_code, rejected.status_code, missing.status_code),
            (200, 200, 200, 200, 400, 400, 404),
        )
        self.assertEqual(sessions.json()["items"][0]["ownerProfile"], "review")
        self.assertEqual(cards.json()["items"][0]["id"], "t_api")
        self.assertEqual((exact.json()["board"], exact.json()["id"]), ("team", "t_api"))
        self.assertEqual(exact.json()["originSession"]["ownerProfile"], "default")
        self.assertEqual(exact.json()["latestWorkerSession"]["ownerProfile"], "ops")
        self.assertTrue(exact.json()["originSession"]["available"])
        self.assertTrue(exact.json()["latestWorkerSession"]["available"])
        self.assertEqual(missing_card.json(), {"ok": False, "error": "unknown_kanban_card"})
        self.assertEqual(rejected.json(), {"ok": False, "error": "bad_profile"})
        self.assertEqual((bad_date.status_code, bad_date.json()), (400, {"ok": False, "error": "bad_date_from"}))
        self.assertEqual(skills.status_code, 200)
        self.assertEqual(skills.json()["items"][0]["title"], "fixture-skill")
        self.assertEqual(skills.json()["items"][0]["canonical"]["status"], "verified")
        self.assertEqual(refreshed_skills.status_code, 200)
        # Blocking sqlite work must run in FastAPI's threadpool, not on the event loop.
        import inspect
        for route in module.router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                self.assertFalse(inspect.iscoroutinefunction(endpoint), getattr(route, "path", endpoint))
        for response in (health, sessions, cards, exact, missing_card, rejected, missing):
            self.assertEqual(response.headers.get("cache-control"), "no-store")

        # Read the plugin router directly: newer FastAPI keeps included routers nested instead of flattening app.routes.
        successful_routes = {
            ("/api/plugins/session-hub" + route.path, tuple(sorted(route.methods or ())))
            for route in module.router.routes
            if "{unmatched_path:path}" not in route.path
        }
        self.assertEqual(successful_routes, {
            ("/api/plugins/session-hub/health", ("GET",)),
            ("/api/plugins/session-hub/config", ("GET",)),
            ("/api/plugins/session-hub/instances", ("GET",)),
            ("/api/plugins/session-hub/sessions", ("POST",)),
            ("/api/plugins/session-hub/kanban", ("POST",)),
            ("/api/plugins/session-hub/external-ai/jobs", ("POST",)),
            ("/api/plugins/session-hub/ai-sessions", ("POST",)),
            ("/api/plugins/session-hub/ai-sessions/refresh", ("POST",)),
            ("/api/plugins/session-hub/skills/search", ("POST",)),
            ("/api/plugins/session-hub/skills/refresh", ("POST",)),
            ("/api/plugins/session-hub/kanban/{board}/{task_id}", ("GET",)),
            ("/api/plugins/session-hub/revalidate", ("POST",)),
            ("/api/plugins/session-hub/projects", ("GET",)),
            ("/api/plugins/session-hub/projects/refresh", ("POST",)),
            ("/api/plugins/session-hub/projects/{project_id}", ("GET",)),
            ("/api/plugins/session-hub/projects/{project_id}/overrides", ("PATCH",)),
            ("/api/plugins/session-hub/registry/export", ("GET",)),
            ("/api/plugins/session-hub/registry/restore", ("POST",)),
        })

    async def test_skills_absolute_paths_fail_closed_for_remote_or_untrusted_requests(self) -> None:
        module = load_plugin_api()
        module.configure_skills_for_tests(self.hermes_home, self.canonical_skills, ())

        local_app = FastAPI()
        local_app.state.auth_required = False
        local_app.state.bound_host = "127.0.0.1"
        local_app.include_router(module.router, prefix="/api/plugins/session-hub")
        local_transport = httpx.ASGITransport(app=local_app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=local_transport, base_url="http://test") as client:
            local = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
        self.assertEqual(local.status_code, 200)
        self.assertTrue(local.json()["items"][0]["runtimeCopies"][0]["absolutePath"].startswith(str(self.hermes_home.resolve())))

        gated_app = FastAPI()
        gated_app.state.auth_required = True
        gated_app.state.bound_host = "127.0.0.1"
        gated_app.include_router(module.router, prefix="/api/plugins/session-hub")
        gated_transport = httpx.ASGITransport(app=gated_app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=gated_transport, base_url="https://remote.example") as client:
            gated = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
        self.assertEqual((gated.status_code, gated.json()), (403, {"ok": False, "error": "local_desktop_required"}))
        self.assertNotIn(str(self.hermes_home.resolve()), gated.text)

        non_loopback_app = FastAPI()
        non_loopback_app.state.auth_required = False
        non_loopback_app.state.bound_host = "127.0.0.1"
        non_loopback_app.include_router(module.router, prefix="/api/plugins/session-hub")
        non_loopback_transport = httpx.ASGITransport(app=non_loopback_app, client=("203.0.113.9", 12345))
        async with httpx.AsyncClient(transport=non_loopback_transport, base_url="http://test") as client:
            non_loopback = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
        self.assertEqual(non_loopback.status_code, 403)
        self.assertNotIn(str(self.hermes_home.resolve()), non_loopback.text)

        public_bind_app = FastAPI()
        public_bind_app.state.auth_required = False
        public_bind_app.state.bound_host = "0.0.0.0"
        public_bind_app.include_router(module.router, prefix="/api/plugins/session-hub")
        public_bind_transport = httpx.ASGITransport(app=public_bind_app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=public_bind_transport, base_url="http://test") as client:
            public_bind = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
        self.assertEqual(public_bind.status_code, 403)
        self.assertNotIn(str(self.hermes_home.resolve()), public_bind.text)

        # Hermes 0.22 gateway-hosted listener: auth_required may be True (dashboard.public_url set),
        # but a Desktop request carries a native owner ticket that Hermes redeems into
        # request.state.native_http_principal, and the raw socket peer is kept in the scope.
        def gateway_app(principal: Any, peer: Any, bound_host: Any = "127.0.0.1") -> FastAPI:
            app = FastAPI()
            app.state.auth_required = True
            if bound_host is not None:
                app.state.bound_host = bound_host

            @app.middleware("http")
            async def stamp_native(request: Any, call_next: Any) -> Any:
                if peer is not None:
                    request.scope["hermes.gateway_socket_peer"] = peer
                if principal is not None:
                    request.state.native_http_principal = principal
                return await call_next(request)

            app.include_router(module.router, prefix="/api/plugins/session-hub")
            return app

        grant = {"profile_id": "default", "purpose": "interactive", "subject": "owner"}
        cases = [
            (grant, ("127.0.0.1", 50000), 200, None),
            (grant, ("::1", 50000, 0, 0), 200, None),
            (None, ("127.0.0.1", 50000), 403, "local_desktop_required"),
            (grant, None, 403, "local_desktop_required"),
            (grant, ("203.0.113.9", 50000), 403, "local_desktop_required"),
            ("forged", ("127.0.0.1", 50000), 403, "local_desktop_required"),
        ]
        cases += [
            (grant, ("127.0.0.1", 50000), 403, "local_desktop_required", "0.0.0.0"),
            (grant, ("127.0.0.1", 50000), 403, "local_desktop_required", None),
        ]
        for principal, peer, expected_status, expected_error, *bound in cases:
            app = gateway_app(principal, peer, *bound)
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                response = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
            self.assertEqual(response.status_code, expected_status, (principal, peer))
            if expected_error:
                self.assertEqual(response.json()["error"], expected_error)
                self.assertNotIn(str(self.hermes_home.resolve()), response.text)

        # plugins.isolation: host replays the request into a bare app; the plugin cannot verify the
        # caller there, so it stays closed but says why.
        hosted_app = FastAPI()
        hosted_app.include_router(module.router)
        hosted_transport = httpx.ASGITransport(app=hosted_app)
        async with httpx.AsyncClient(transport=hosted_transport, base_url="http://plugin-host") as client:
            hosted = await client.post("/skills/search", json={"query": "fixture"})
        self.assertEqual((hosted.status_code, hosted.json()["error"]), (403, "plugin_host_isolation"))
        self.assertNotIn(str(self.hermes_home.resolve()), hosted.text)

        # A spoofed "plugin-host" Host on the real in-process app only changes nothing: still the generic refusal.
        spoof_transport = httpx.ASGITransport(app=gateway_app(None, ("127.0.0.1", 50000)), client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=spoof_transport, base_url="http://plugin-host") as client:
            spoofed = await client.post("/api/plugins/session-hub/skills/search", json={"query": "fixture"})
        self.assertEqual((spoofed.status_code, spoofed.json()["error"]), (403, "local_desktop_required"))



class AISessionsApiFixture(unittest.IsolatedAsyncioTestCase):
    async def test_list_is_read_only_and_refresh_is_local_desktop_only(self) -> None:
        module = load_plugin_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "ai-sessions.json"
            cache.write_text(json.dumps({
                "contract": "ai-sessions-v1", "collectedAt": "2026-10-14T00:00:00Z",
                "sources": {"codex": {"ok": True, "count": 1, "error": None}},
                "items": [{"id": "codex:t1", "source": "codex", "title": "업데이트 오류 진단", "firstMessage": "x",
                           "conclusion": ["완료"], "status": "완료", "folder": "a/b", "model": "", "archived": False,
                           "createdAt": "", "updatedAt": "2026-10-13T00:00:00Z", "_stamp": "1:1"}],
            }, ensure_ascii=False), encoding="utf-8")
            module.configure_ai_sessions_for_tests(cache, {"codex": root / "no-codex"})
            try:
                local_app = FastAPI()
                local_app.state.auth_required = False
                local_app.state.bound_host = "127.0.0.1"
                local_app.include_router(module.router, prefix="/api/plugins/session-hub")
                transport = httpx.ASGITransport(app=local_app, client=("127.0.0.1", 12345))
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    listed = await client.post("/api/plugins/session-hub/ai-sessions", json={"query": "오류", "pageSize": 20})
                    bad = await client.post("/api/plugins/session-hub/ai-sessions", json={"pageSize": 7})
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["items"][0]["title"], "업데이트 오류 진단")
                self.assertNotIn("_stamp", listed.text)
                self.assertEqual((bad.status_code, bad.json()["error"]), (400, "bad_page_size"))

                remote_app = FastAPI()
                remote_app.state.auth_required = True
                remote_app.state.bound_host = "127.0.0.1"
                remote_app.include_router(module.router, prefix="/api/plugins/session-hub")
                remote_transport = httpx.ASGITransport(app=remote_app, client=("127.0.0.1", 12345))
                async with httpx.AsyncClient(transport=remote_transport, base_url="https://remote.example") as client:
                    denied = await client.post("/api/plugins/session-hub/ai-sessions/refresh", json={})
                self.assertEqual((denied.status_code, denied.json()["error"]), (403, "local_desktop_required"))

                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    refreshed = await client.post("/api/plugins/session-hub/ai-sessions/refresh", json={})
                self.assertEqual(refreshed.status_code, 200)

                native_app = FastAPI()
                native_app.state.auth_required = True
                native_app.state.bound_host = "127.0.0.1"

                @native_app.middleware("http")
                async def stamp_native(request: Any, call_next: Any) -> Any:
                    request.scope["hermes.gateway_socket_peer"] = ("127.0.0.1", 50000)
                    request.state.native_http_principal = {"profile_id": "default", "purpose": "native-http"}
                    return await call_next(request)

                native_app.include_router(module.router, prefix="/api/plugins/session-hub")
                native_transport = httpx.ASGITransport(app=native_app, client=("127.0.0.1", 12345))
                async with httpx.AsyncClient(transport=native_transport, base_url="http://127.0.0.1") as client:
                    native_refresh = await client.post("/api/plugins/session-hub/ai-sessions/refresh", json={})
                self.assertEqual(native_refresh.status_code, 200)
                self.assertEqual(refreshed.json()["sources"]["codex"]["error"], "codex_state_missing")
            finally:
                module.configure_ai_sessions_for_tests(None)

if __name__ == "__main__":
    unittest.main()
