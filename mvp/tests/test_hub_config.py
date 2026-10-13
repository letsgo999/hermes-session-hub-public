from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _hub_test_env  # noqa: F401

CONFIG_PATH = Path(__file__).resolve().parents[1] / "plugins" / "session-hub" / "dashboard" / "hub_config.py"


def _load_hub_config():
    spec = importlib.util.spec_from_file_location("hub_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HubConfigFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "hermes"
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "state.db").write_bytes(b"x")
        for name in ("writer", "Alpha", "ops"):
            (self.home / "profiles" / name).mkdir()
            (self.home / "profiles" / name / "state.db").write_bytes(b"x")
        (self.home / "profiles" / "empty-folder").mkdir()
        (self.home / "profiles" / "bad name").mkdir()
        (self.home / "profiles" / "bad name" / "state.db").write_bytes(b"x")
        self.config_file = self.home / "session-hub" / "session-hub.config.json"
        self.config_file.parent.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.home),
            "HERMES_SESSION_HUB_CONFIG": str(self.config_file),
        })
        self.env.start()
        os.environ.pop("HERMES_SYNC_ROOT", None)
        self.hub_config = _load_hub_config()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def write(self, payload: dict) -> None:
        self.config_file.write_text(json.dumps(payload), encoding="utf-8")

    def test_without_settings_file_profiles_are_auto_detected_and_no_branches(self) -> None:
        settings = self.hub_config.load_settings()
        self.assertEqual(settings.profiles_mode, "auto")
        self.assertEqual(settings.local_profiles, ("default", "Alpha", "ops", "writer"))
        self.assertTrue(settings.instances_enabled)
        self.assertEqual(settings.remote_instances, {})
        self.assertIsNone(settings.sync_root)
        self.assertIsNone(settings.skills_root)
        self.assertEqual(settings.local_label, "본사")

    def test_profile_folder_given_as_hermes_home_resolves_to_base(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home / "profiles" / "writer")}):
            self.assertEqual(self.hub_config.resolve_hermes_home(), self.home)

    def test_explicit_profiles_and_branches_are_validated(self) -> None:
        self.write({
            "profiles": ["writer", "default", "../escape", "writer", 7],
            "profileLabels": {"writer": "  Writer   bot ", "default": ""},
            "instances": {
                "syncRoot": str(Path(self.tmp.name) / "sync"),
                "remotes": {"A": {"label": "Seoul", "ttlMinutes": 30}, "HQ": {}, "bad/id": {}, "B": {}},
            },
        })
        settings = self.hub_config.load_settings()
        self.assertEqual(settings.local_profiles, ("default", "writer"))
        self.assertEqual(settings.profile_labels["writer"], "Writer bot")
        self.assertNotIn("default", settings.profile_labels)
        self.assertEqual(list(settings.remote_instances), ["A", "B"])
        self.assertEqual(settings.remote_instances["A"], {"label": "Seoul", "ttlSeconds": 1800})
        self.assertEqual(settings.remote_instances["B"]["label"], "B")
        self.assertEqual(settings.remote_profiles, ("A/default", "B/default"))
        self.assertEqual(settings.profile_labels["A/default"], "Seoul (A)")

    def test_instances_can_be_switched_off(self) -> None:
        self.write({"instances": {"enabled": False, "remotes": {"A": {}}, "syncRoot": "D:/x"}})
        settings = self.hub_config.load_settings()
        self.assertFalse(settings.instances_enabled)
        self.assertEqual(settings.remote_instances, {})
        self.assertFalse(settings.public_payload()["instancesEnabled"])

    def test_hostile_values_never_break_loading(self) -> None:
        self.config_file.write_text(
            '{"profiles": ["..", ".", "ok"], "instances": {"remotes": {"A": {"ttlMinutes": Infinity}, "B": {"ttlMinutes": NaN}, "C": {"ttlSeconds": 1e400}}}}',
            encoding="utf-8",
        )
        settings = self.hub_config.load_settings()
        self.assertEqual(settings.local_profiles, ("default", "ok"))
        self.assertEqual(settings.remote_instances["A"]["ttlSeconds"], self.hub_config.DEFAULT_REMOTE_TTL_SECONDS)
        self.assertEqual(settings.remote_instances["C"]["ttlSeconds"], self.hub_config.DEFAULT_REMOTE_TTL_SECONDS)

    def test_base_home_named_profiles_parent_is_not_mistaken_for_a_profile(self) -> None:
        other = Path(self.tmp.name) / "profiles" / "hermes"
        other.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(other)}):
            self.assertEqual(self.hub_config.resolve_hermes_home(), other)

    def test_malformed_settings_file_falls_back_to_defaults(self) -> None:
        self.config_file.write_text("{not json", encoding="utf-8")
        settings = self.hub_config.load_settings()
        self.assertEqual(settings.local_profiles[0], "default")
        self.assertEqual(settings.remote_instances, {})

    def test_public_payload_never_contains_paths(self) -> None:
        self.write({"skillsRoot": "C:/secret/skills", "instances": {"syncRoot": "C:/secret/sync", "remotes": {"A": {}}}})
        payload = json.dumps(self.hub_config.load_settings().public_payload())
        self.assertNotIn("secret", payload)
        self.assertNotIn(str(self.home), payload)


class PluginConfigEndpointFixture(unittest.TestCase):
    def test_config_route_reports_fixture_profiles(self) -> None:
        import asyncio

        import httpx
        from fastapi import FastAPI

        path = Path(__file__).resolve().parents[1] / "plugins" / "session-hub" / "dashboard" / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("session_hub_plugin_api_config_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix="/api/plugins/session-hub")

        async def call() -> dict:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                response = await client.get("/api/plugins/session-hub/config")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                return response.json()

        payload = asyncio.run(call())
        self.assertEqual(payload["contract"], "session-hub-config-v1")
        self.assertEqual(payload["localProfiles"], ["default", "ops", "review", "builder", "search"])
        self.assertEqual(payload["remoteProfiles"], ["X/default", "Y/default", "Z/default"])
        self.assertEqual(payload["profileLabels"]["X/default"], "Branch X (X)")
        self.assertEqual(module.PROFILE_NAMES, ("default", "ops", "review", "builder", "search"))
        with mock.patch.dict(os.environ, {"HERMES_SESSION_HUB_NO_REMOTE": "1"}):
            no_remote = asyncio.run(call())
        self.assertEqual(no_remote["remoteProfiles"], [])
        self.assertFalse(any("/" in key for key in no_remote["profileLabels"]))


if __name__ == "__main__":
    unittest.main()
