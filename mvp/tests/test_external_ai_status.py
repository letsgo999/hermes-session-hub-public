from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).parents[1] / "plugins" / "session-hub" / "dashboard" / "external_ai_status.py"


def load_module():
    spec = importlib.util.spec_from_file_location("external_ai_status_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ExternalAIStatusTests(unittest.TestCase):
    def test_listing_returns_sanitized_title_and_summary_only(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = root / "EA-20260923-ABCDEF12"
            for folder in ("request", "output", "review", "audit", "approval"):
                (job / folder).mkdir(parents=True)
            (job / "request" / "request.md").write_text(
                "# 샘플 작업의 첫 줄 제목을 확인한다.\n두 번째 줄은 반환되지 않음\n",
                encoding="utf-8",
            )
            (job / "output" / "result.md").write_text("isolated hotfix candidate\n", encoding="utf-8")
            (job / "review" / "verification.md").write_text("19/19 tests PASS token=abc123secret\n", encoding="utf-8")
            (job / "audit" / "audit.jsonl").write_text('{"event":"audit body must not be returned"}\n', encoding="utf-8")
            (job / "approval" / "request.txt").write_text(
                "승인 nonce: deadbeefnonce\n승인 만료: 2000-01-01T00:00:00Z\n", encoding="utf-8"
            )
            (job / "job.yaml").write_text(json.dumps({
                "job_id": job.name,
                "kind": "wiki_draft",
                "status": "APPROVAL_REQUESTED",
                "node": "HQ",
                "executor": "claude",
                "risk": "L2",
                "target_scope": ["wiki/a.md", "wiki/b.md"],
                "created_at": "2026-09-23T00:00:00Z",
                "updated_at": "2026-09-23T00:00:00Z",
                "metrics": {"tokens": 42, "cost_usd": 0.1, "attempts": 1},
                "approval": {"state": "requested", "nonce": "deadbeefnonce"},
                "verification": {"state": "pass"},
                "artifact_sha256": "a" * 64,
            }), encoding="utf-8")
            result = module.ExternalAIStatusReader(root).list_jobs(query="샘플", page_size=10)
            self.assertTrue(result["readonly"])
            item = result["items"][0]
            self.assertEqual(item["id"], job.name)
            self.assertTrue(item["title"].startswith("샘플 작업의 첫 줄"))
            self.assertLessEqual(len(item["title"]), module.TITLE_MAX_CHARS)
            self.assertEqual(len(item["summaryLines"]), 3)
            self.assertIn("승인 기한", item["summaryLines"][0])
            self.assertIn("wiki/a.md 외 1개", item["summaryLines"][1])
            self.assertIn("19/19 tests PASS", item["summaryLines"][2])
            encoded = json.dumps(result, ensure_ascii=False)
            for forbidden in ("두 번째 줄", "audit body", "deadbeefnonce", "abc123secret", "request.md"):
                self.assertNotIn(forbidden, encoded)

    def test_failed_job_points_to_later_rerun(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for job_id, status, created in (
                ("EA-20260924-AAAAAAA1", "FAILED", "2026-09-24T08:09:11Z"),
                ("EA-20260924-BBBBBBB2", "APPROVAL_REQUESTED", "2026-09-24T08:10:33Z"),
            ):
                job = root / job_id
                (job / "request").mkdir(parents=True)
                (job / "request" / "request.md").write_text("같은 요청\n", encoding="utf-8")
                (job / "job.yaml").write_text(json.dumps({
                    "job_id": job_id, "kind": "code_patch", "status": status, "created_at": created,
                    "updated_at": created, "verification": {"state": "pending"},
                }), encoding="utf-8")
            items = {item["id"]: item for item in module.ExternalAIStatusReader(root).list_jobs(page_size=10)["items"]}
            self.assertEqual(items["EA-20260924-AAAAAAA1"]["retriedBy"], "EA-20260924-BBBBBBB2")
            self.assertIn("다시 실행", items["EA-20260924-AAAAAAA1"]["summaryLines"][2])
            self.assertEqual(items["EA-20260924-BBBBBBB2"]["retriedBy"], "")

if __name__ == "__main__":
    unittest.main()
