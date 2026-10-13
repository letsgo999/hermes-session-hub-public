from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from backend.api import handle_api_request, render_static_preview, serve_loopback
from backend.compatibility import generate_compatibility_matrix
from backend.smoke import _hash_assessment, run_aggregate_smoke
from build import build_dist, write_evidence_manifest
from mvp.tests.test_backend import SCHEMA_SQL, add_session
from run_preview import resolve_preview_port


class OpsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_by_profile: dict[str, Path] = {}
        for profile in ("default", "ops", "review", "builder", "search"):
            db_path = self.root / f"{profile}.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA_SQL)
            add_session(
                conn,
                sid=f"{profile}-desktop",
                title=f"{profile} desktop",
                source="desktop",
                profile_name="ignored",
                activity=100,
                messages=[f"{profile} preview with local@example.com"],
            )
            add_session(
                conn,
                sid=f"{profile}-telegram",
                title=f"{profile} telegram",
                source="telegram",
                chat_type="dm",
                profile_name=None,
                activity=90,
                messages=[f"{profile} dm"],
            )
            conn.commit()
            conn.close()
            self.db_by_profile[profile] = db_path

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_compatibility_matrix_selects_strict_ro_fallback(self) -> None:
        matrix = generate_compatibility_matrix()
        self.assertEqual(matrix["hermesDesktopVersion"], "0.21.0")
        self.assertEqual(matrix["supportedSchemaVersions"], [26, 27, 28, 29, 30])
        self.assertEqual(matrix["liveSchemaVersion"], 30)
        self.assertEqual(matrix["selectedDataPath"], "strict_ro_fallback")
        self.assertIn("feature-detected", matrix["fallbackReason"])
        self.assertEqual(matrix["capabilities"]["routePage"], "pass")
        self.assertEqual(matrix["capabilities"]["sessionRpcSearchMessages"], "insufficient")

    def test_api_health_and_sessions_are_sanitized(self) -> None:
        health_status, health = handle_api_request(self.db_by_profile, "GET", "/health", None)
        sessions_status, sessions = handle_api_request(
            self.db_by_profile,
            "POST",
            "/sessions",
            {"tab": "telegram", "telegramTypes": ["dm"], "pageSize": 20},
        )
        self.assertEqual(health_status, 200)
        self.assertEqual(health["ok"], True)
        self.assertEqual(sessions_status, 200)
        self.assertEqual(len(sessions["items"]), 5)
        self.assertNotIn("local@example.com", json.dumps(sessions))

    def test_static_preview_contains_ui_readback_without_canary(self) -> None:
        _, sessions = handle_api_request(self.db_by_profile, "POST", "/sessions", {"pageSize": 20})
        html = render_static_preview(sessions)
        self.assertIn("Session Hub", html)
        self.assertIn("Desktop", html)
        self.assertIn("Telegram", html)
        self.assertNotIn("local@example.com", html)
        self.assertNotIn("storedSessionId", html)

    def test_preview_server_root_renders_ui_and_no_store(self) -> None:
        document = "<!doctype html><title>Session Hub</title><main><label>Search title or content<input></label></main>"
        server = serve_loopback(self.db_by_profile, port=0, preview_html=document)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                self.assertIn("text/html", response.headers.get("Content-Type", ""))
                self.assertIn("Session Hub", body)
                self.assertIn("Search title or content", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_preview_port_supports_safe_ephemeral_override(self) -> None:
        self.assertEqual(resolve_preview_port(None), 8787)
        self.assertEqual(resolve_preview_port("0"), 0)
        self.assertEqual(resolve_preview_port("18787"), 18787)
        for invalid in ("", "-1", "65536", "not-a-port"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    resolve_preview_port(invalid)

    def test_hash_assessment_distinguishes_live_observation_from_fixture_proof(self) -> None:
        self.assertEqual(_hash_assessment(is_live_default=True, observed_drift=False), "unchanged_during_live_observation_window")
        self.assertEqual(_hash_assessment(is_live_default=True, observed_drift=True), "inconclusive_external_live_db_drift")
        self.assertEqual(_hash_assessment(is_live_default=False, observed_drift=False), "unchanged_during_isolated_fixture_smoke")
        self.assertEqual(_hash_assessment(is_live_default=False, observed_drift=True), "unexpected_injected_fixture_drift")

    def test_time_compressed_refresh_stress_preserves_db_hashes(self) -> None:
        result = run_aggregate_smoke(self.db_by_profile, iterations=25, include_items=False)
        self.assertEqual(result["iterations"], 25)
        self.assertEqual(result["startSha256"], result["endSha256"])
        self.assertNotIn("writeDelta", result)
        self.assertEqual(result["hashAssessment"], "unchanged_during_isolated_fixture_smoke")
        self.assertGreaterEqual(result["writeControlEvidence"]["authorizerWriteAttemptsObserved"], 1)
        self.assertEqual(result["writeControlEvidence"]["authorizerWriteOpsAllowed"], 0)
        self.assertEqual(result["writeControlEvidence"]["writeProbeRejected"], True)
        self.assertEqual(result["contract"], "aggregate-only; no raw ids, titles, messages, user/chat ids, or paths")

    def test_development_and_installable_backend_sources_match(self) -> None:
        import backend.sessionhub as development

        installable = Path.cwd() / "mvp" / "plugins" / "session-hub" / "dashboard" / "sessionhub_backend.py"
        # The development module is an alias that executes the installable file itself.
        self.assertEqual(Path(development.__file__).resolve(), installable.resolve())
        alias = (Path.cwd() / "mvp" / "backend" / "sessionhub.py").read_text(encoding="utf-8")
        self.assertIn("sessionhub_backend.py", alias)
        self.assertLess(len(alias), 1200)

    def test_legacy_plugin_prototype_is_inert_and_points_to_canonical_source(self) -> None:
        legacy = (Path.cwd() / "mvp" / "plugin" / "plugin.js").read_text(encoding="utf-8")
        self.assertNotIn("host.openSession", legacy)
        self.assertNotIn("ctx.registerMany", legacy)
        self.assertIn("plugins/session-hub/desktop/plugin.js", legacy)

    def test_build_dist_and_evidence_manifest_have_hashes(self) -> None:
        evidence_dir = self.root / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "green.log").write_text("exit 0\npass 1\n", encoding="utf-8")
        manifest = write_evidence_manifest(evidence_dir)
        self.assertEqual(manifest["files"][0]["sha256"], self._sha(evidence_dir / "green.log"))
        build = build_dist(Path.cwd() / "mvp")
        self.assertGreater(build["totalBytes"], 0)
        self.assertTrue((Path.cwd() / "mvp" / "dist" / "plugins" / "session-hub" / "desktop" / "plugin.js").is_file())
        self.assertTrue((Path.cwd() / "mvp" / "dist" / "plugins" / "session-hub" / "dashboard" / "external_ai_status.py").is_file())
        self.assertRegex(build["sha256"], r"^[a-f0-9]{64}$")

    def test_build_publishes_contractually_opaque_kanban_drawer_surface(self) -> None:
        build_dist(Path.cwd() / "mvp")
        source = (Path.cwd() / "mvp" / "plugins" / "session-hub" / "desktop" / "plugin.js").read_text(encoding="utf-8")
        packaged = (Path.cwd() / "mvp" / "dist" / "plugins" / "session-hub" / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for plugin in (source, packaged):
            self.assertIn("bg-(--theme-elevated-seed)", plugin)
            self.assertIn("var(--theme-elevated-seed, Canvas)", plugin)
            self.assertIn("isolate", plugin)
            self.assertIn("opacity-100", plugin)
            self.assertNotIn("bg-(--ui-bg-primary)", plugin)
            self.assertNotIn("bg-(--ui-bg-elevated)", plugin)
            self.assertNotIn("var(--ui-bg-primary", plugin)
            self.assertNotIn("var(--ui-bg-elevated", plugin)

    def test_build_cleans_stale_files_and_manifest_equals_complete_dist_set(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "candidate"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        stale = candidate / "dist" / "stale-canary.js"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")

        manifest = build_dist(candidate)
        self.assertFalse(stale.exists())
        listed = {row["path"] for row in manifest["files"]}
        actual = {
            path.relative_to(candidate / "dist").as_posix()
            for path in (candidate / "dist").rglob("*")
            if path.is_file() and path != candidate / "dist" / "manifest.json"
        }
        self.assertEqual(actual, listed)
        self.assertNotIn("stale-canary.js", listed)

    def test_build_rejects_any_missing_required_source(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "missing-source"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        (candidate / "plugins" / "session-hub" / "desktop" / "plugin.js").unlink()
        with self.assertRaises(FileNotFoundError):
            build_dist(candidate)

    def test_build_cleanup_failure_exits_nonzero_and_preserves_existing_dist(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "cleanup-failure"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        build_dist(candidate)
        before = self._tree_snapshot(candidate / "dist")
        orphan = candidate / ".dist-staging"
        orphan.mkdir()
        (orphan / "cleanup-canary.txt").write_text("must not affect dist", encoding="utf-8")

        env = os.environ.copy()
        env["SESSION_HUB_BUILD_TEST_FAULT"] = "cleanup"
        completed = subprocess.run(
            [sys.executable, str(candidate / "build.py")],
            cwd=candidate,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("injected cleanup failure", completed.stdout + completed.stderr)
        self.assertIn("failed after 6 attempts", completed.stdout + completed.stderr)
        self.assertEqual(self._tree_snapshot(candidate / "dist"), before)

    def test_build_transient_cleanup_winerror_retries_then_succeeds(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "cleanup-retry"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        build_dist(candidate)
        orphan = candidate / ".dist-staging"
        orphan.mkdir()
        (orphan / "cleanup-canary.txt").write_text("retry me", encoding="utf-8")

        env = os.environ.copy()
        env["SESSION_HUB_BUILD_TEST_FAULT"] = "cleanup:2"
        completed = subprocess.run(
            [sys.executable, str(candidate / "build.py")],
            cwd=candidate,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("cleanup .dist-staging recovered after 3 attempts", completed.stdout + completed.stderr)
        self.assertFalse(orphan.exists())

    def test_build_promotion_failure_exits_nonzero_and_rolls_back_existing_dist(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "promotion-failure"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        build_dist(candidate)
        before = self._tree_snapshot(candidate / "dist")
        with (candidate / "INSTALLABLE_README.md").open("a", encoding="utf-8") as stream:
            stream.write("\nchanged candidate must not be published\n")

        env = os.environ.copy()
        env["SESSION_HUB_BUILD_TEST_FAULT"] = "promote"
        completed = subprocess.run(
            [sys.executable, str(candidate / "build.py")],
            cwd=candidate,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("injected promote failure", completed.stdout + completed.stderr)
        self.assertIn("failed after 6 attempts", completed.stdout + completed.stderr)
        self.assertEqual(self._tree_snapshot(candidate / "dist"), before)

    def test_identical_verified_candidate_reuses_dist_without_backup_rename(self) -> None:
        source = Path.cwd() / "mvp"
        candidate = self.root / "identical-candidate"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("dist", ".dist-*", "evidence", "__pycache__"))
        build_dist(candidate)
        before = self._tree_snapshot(candidate / "dist")

        env = os.environ.copy()
        env["SESSION_HUB_BUILD_TEST_FAULT"] = "backup"
        completed = subprocess.run(
            [sys.executable, str(candidate / "build.py")],
            cwd=candidate,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self._tree_snapshot(candidate / "dist"), before)
        self.assertFalse((candidate / ".dist-staging").exists())
        self.assertFalse((candidate / ".dist-previous").exists())

    def test_readme_and_package_expose_one_command_run_and_build(self) -> None:
        readme = (Path.cwd() / "mvp" / "README.md").read_text(encoding="utf-8")
        package = json.loads((Path.cwd() / "mvp" / "package.json").read_text(encoding="utf-8"))
        self.assertIn("npm run preview", readme)
        self.assertIn("npm run build", readme)
        self.assertIn("preview:render", package["scripts"]["preview"])
        self.assertEqual(package["scripts"]["build"], "python build.py")
        self.assertTrue((Path.cwd() / "mvp" / "run_preview.py").is_file())
        build_dist(Path.cwd() / "mvp")
        dist_readme = (Path.cwd() / "mvp" / "dist" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("npm run preview", dist_readme)
        self.assertNotIn("npm run build", dist_readme)

    def test_package_lock_is_committed_and_dependency_free(self) -> None:
        lock_path = Path.cwd() / "mvp" / "package-lock.json"
        self.assertTrue(lock_path.is_file())
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(lock["lockfileVersion"], 3)
        self.assertEqual(lock["packages"][""]["name"], "hermes-session-hub-mvp")
        self.assertNotIn("dependencies", lock["packages"][""])
        self.assertNotIn("devDependencies", lock["packages"][""])

    @staticmethod
    def _sha(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def _tree_snapshot(cls, root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): cls._sha(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
