from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "plugins" / "session-hub" / "dashboard" / "skills_index.py"


def load_module():
    name = "session_hub_skills_index_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("skills_index.py is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_skill(path: Path, *, name: str, description: str, body: str = "", tags: list[str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    text = ["---", f"name: {name}", f'description: "{description}"']
    if tags:
        text.append("tags: [" + ", ".join(tags) + "]")
    text.extend(["---", "", f"# {name}", "", body])
    target = path / "SKILL.md"
    target.write_text("\n".join(text) + "\n", encoding="utf-8")
    return target


class SkillsIndexFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "hermes"
        self.canonical = self.root / "drive" / "skills"
        self.archive = self.root / "drive" / "backup"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def index(self):
        return self.module.SkillsIndex(
            hermes_home=self.home,
            canonical_root=self.canonical,
            archive_roots=(self.archive,),
        )

    def test_groups_active_profiles_and_only_verifies_canonical_by_hash(self) -> None:
        default_path = write_skill(
            self.home / "skills" / "automation" / "exact-skill",
            name="exact-skill",
            description="Use when exact phrase automation is requested.",
            body="## Trigger\nRun for safe reports.",
            tags=["automation", "reports"],
        )
        devel_path = write_skill(
            self.home / "profiles" / "builder" / "skills" / "automation" / "exact-skill",
            name="exact-skill",
            description="Use when exact phrase automation is requested.",
            body="## Trigger\nRun for safe reports.",
            tags=["automation", "reports"],
        )
        canonical_path = write_skill(
            self.canonical / "automation" / "exact-skill",
            name="exact-skill",
            description="Use when exact phrase automation is requested.",
            body="## Trigger\nRun for safe reports.",
            tags=["automation", "reports"],
        )
        write_skill(
            self.canonical / "candidate-only",
            name="candidate-only",
            description="A same-name-free Drive candidate without provenance.",
        )

        payload = self.index().search("")
        by_name = {item["title"]: item for item in payload["items"]}
        exact = by_name["exact-skill"]
        self.assertEqual(exact["deployedProfiles"], ["default", "builder"])
        self.assertEqual(
            {copy["absolutePath"] for copy in exact["runtimeCopies"]},
            {str(default_path.resolve()), str(devel_path.resolve())},
        )
        self.assertEqual(exact["canonical"]["status"], "verified")
        self.assertEqual(exact["canonical"]["absolutePath"], str(canonical_path.resolve()))
        self.assertEqual(exact["canonical"]["evidence"], "sha256_match_active")
        self.assertNotIn("candidate-only", by_name)
        candidate = self.index().search("candidate-only")["items"][0]
        self.assertEqual(candidate["canonical"]["status"], "unverified")
        self.assertEqual(candidate["canonical"]["evidence"], "candidate_only")

    def test_archive_is_opt_in_and_badged_without_becoming_active(self) -> None:
        write_skill(
            self.home / "skills" / "live",
            name="live",
            description="Use when production-safe live lookup is needed.",
        )
        archived = write_skill(
            self.archive / "retired",
            name="retired",
            description="Use when old migration notes are needed.",
        )

        active_only = self.index().search("")
        self.assertEqual([item["title"] for item in active_only["items"]], ["live"])

        including_archive = self.index().search("", include_archive=True)
        by_name = {item["title"]: item for item in including_archive["items"]}
        self.assertEqual(by_name["retired"]["statuses"], ["backup"])
        self.assertEqual(by_name["retired"]["runtimeCopies"], [])
        self.assertEqual(by_name["retired"]["otherCopies"][0]["absolutePath"], str(archived.resolve()))

    def test_search_uses_and_tokens_quoted_phrases_partial_match_and_relevance(self) -> None:
        write_skill(
            self.home / "skills" / "best",
            name="automation-exact",
            description="Use when exact phrase reporting is required.",
            body="Secondary body text.",
            tags=["workflow"],
        )
        write_skill(
            self.home / "skills" / "body-only",
            name="body-only",
            description="Generic helper.",
            body="The exact phrase appears here with automation details.",
        )
        write_skill(
            self.home / "skills" / "missing-token",
            name="unrelated",
            description="Contains exact phrase but not the other word.",
        )

        result = self.index().search('AUTO "exact phrase"')
        self.assertEqual([item["title"] for item in result["items"]], ["automation-exact", "body-only"])
        self.assertGreater(result["items"][0]["score"], result["items"][1]["score"])
        self.assertTrue(result["items"][0]["matchReason"])
        self.assertIn("exact phrase", result["items"][0]["snippet"].lower())

    def test_cache_refresh_and_source_failures_are_explicit(self) -> None:
        index = self.index()
        first = index.search("")
        write_skill(self.home / "skills" / "late", name="late", description="Added after cache creation.")
        cached = index.search("")
        refreshed = index.search("", force_refresh=True)

        self.assertEqual(first["items"], cached["items"])
        self.assertEqual([item["title"] for item in refreshed["items"]], ["late"])
        self.assertEqual(first["refreshedAt"], cached["refreshedAt"])
        statuses = {source["kind"]: source["status"] for source in first["sources"]}
        self.assertEqual(statuses["canonical"], "missing")

    def test_oversized_skill_is_skipped_and_reported_as_partial_source(self) -> None:
        write_skill(
            self.home / "skills" / "too-large",
            name="too-large",
            description="A skill that exceeds the configured test limit.",
            body="x" * 500,
        )
        index = self.module.SkillsIndex(
            hermes_home=self.home,
            canonical_root=self.canonical,
            archive_roots=(),
            max_file_bytes=100,
        )

        payload = index.search("")

        self.assertEqual(payload["items"], [])
        default_source = next(source for source in payload["sources"] if source["profile"] == "default")
        self.assertEqual(default_source["status"], "partial")
        self.assertEqual(default_source["issues"], ["skill_file_too_large"])


if __name__ == "__main__":
    unittest.main()
