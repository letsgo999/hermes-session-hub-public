from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

MODULE = Path(__file__).parents[1] / "plugins" / "session-hub" / "dashboard" / "ai_sessions.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ai_sessions_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")


class AISessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.now_ms = int(self.now * 1000)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_codex(self) -> Path:
        home = self.root / "codex"
        home.mkdir()
        rollout = home / "sessions" / "rollout-a.jsonl"
        write_jsonl(rollout, [
            {"type": "session_meta", "payload": {"id": "t1"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message":
                "## 결과\n원인을 찾아 고쳤습니다.\n- 커밋: [abc](https://x) api_key=sk-abcdefghijklmnopqrstuvwxyz\n```\ncode\n```\n세 번째 줄\n네 번째 줄"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message":
                "<heartbeat>\n<automation_id>c-30</automation_id>\n<decision>DONT_NOTIFY</decision>\n</heartbeat>"}},
        ])
        conn = sqlite3.connect(home / "state_5.sqlite")
        conn.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, first_user_message TEXT, cwd TEXT, archived INT,"
            " model TEXT, created_at_ms INT, updated_at_ms INT, name TEXT, thread_source TEXT)"
        )
        conn.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("t1", str(rollout), "긴 첫 질문 원문", "# Files mentioned by the user:\n\n## My request:\n빌드 오류 진단해줘",
             "\\\\?\\C:\\Users\\me\\Documents\\Codex\\2026-01-05\\sample-task", 0, "gpt-5", self.now_ms - 5000, self.now_ms - 1000,
             "빌드 스크립트 오류 진단", "user"),
            ("t2", str(home / "missing.jsonl"), "보관된 작업", "보관된 작업", "C:\\x", 1, "gpt-5", self.now_ms, self.now_ms - 2000, "", "user"),
            ("t3", "", "서브에이전트", "서브에이전트", "C:\\x", 0, "gpt-5", self.now_ms, self.now_ms - 3000, "sub", "subagent"),
            ("old", "", "오래된 작업", "오래된 작업", "C:\\x", 0, "gpt-5", 0, 1000, "old", "user"),
        ])
        conn.commit()
        conn.close()
        return home

    def make_claude_desktop(self) -> Path:
        appdata = self.root / "Claude"
        org = appdata / "local-agent-mode-sessions" / "acct" / "org"
        org.mkdir(parents=True)
        meta = {
            "sessionId": "local_abc", "cliSessionId": "cli-1", "title": "문서 양식 정리",
            "initialMessage": "양식을 정리해줘.", "createdAt": self.now_ms - 9000, "lastActivityAt": self.now_ms - 500,
            "model": "claude-model-x", "isArchived": False, "userSelectedFolders": ["D:\\Work\\Deck"],
            "emailAddress": "someone@example.com", "systemPrompt": "SECRET SYSTEM PROMPT",
        }
        (org / "local_abc.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        write_jsonl(org / "local_abc" / ".claude" / "projects" / "proj" / "cli-1.jsonl", [
            {"type": "user", "message": {"content": "양식 고쳐줘"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "정리를 마쳤습니다 (v2).\n변경 세 가지"}]}},
        ])
        return appdata

    def make_claude_code(self) -> Path:
        home = self.root / "dot-claude"
        write_jsonl(home / "projects" / "D--Work-Deck" / "sess-1.jsonl", [
            {"type": "user", "message": {"content": "보고서 초안을 마무리해줘"}, "timestamp": "2026-10-10T00:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Production is complete.\nQA PASS 12/12"}]},
             "timestamp": "2026-10-10T00:05:00Z"},
            {"type": "user", "message": {"content": "하나 더"}, "timestamp": "2026-10-10T00:06:00Z"},
        ])
        return home

    def test_collect_all_sources_and_read(self) -> None:
        cache = self.root / "cache" / "ai-sessions.json"
        payload = self.module.collect(cache, {
            "codex": self.make_codex(), "claude-desktop": self.make_claude_desktop(), "claude-code": self.make_claude_code(),
        }, now=self.now)
        self.assertEqual(payload["sources"]["codex"]["count"], 2)  # subagent + too-old excluded
        items = {item["id"]: item for item in payload["items"]}
        codex = items["codex:t1"]
        self.assertEqual(codex["title"], "빌드 스크립트 오류 진단")
        self.assertEqual(codex["firstMessage"], "빌드 오류 진단해줘")
        self.assertEqual(codex["status"], "완료")
        self.assertEqual(codex["conclusion"][0], "원인을 찾아 고쳤습니다.")
        self.assertIn("커밋: abc [가림]", codex["conclusion"])
        self.assertEqual(len(codex["conclusion"]), 3)
        self.assertEqual(codex["folder"], "2026-01-05/sample-task")
        desktop = items["claude-desktop:local_abc"]
        self.assertEqual(desktop["title"], "문서 양식 정리")
        self.assertEqual(desktop["status"], "완료")
        self.assertEqual(desktop["conclusion"][0], "정리를 마쳤습니다 (v2).")
        code = items["claude-code:sess-1"]
        self.assertEqual(code["title"], "보고서 초안을 마무리해줘")
        self.assertEqual(code["status"], "진행 중")
        self.assertEqual(code["conclusion"][0], "Production is complete.")
        encoded = cache.read_text(encoding="utf-8")
        for forbidden in ("sk-abcdefghijklmnopqrstuvwxyz", "someone@example.com", "SECRET SYSTEM PROMPT", "code\\n"):
            self.assertNotIn(forbidden, encoded)

        reader = self.module.AISessionsReader(cache)
        listed = reader.list_sessions(page_size=10)
        self.assertEqual(listed["page"]["totalKnown"], 3)  # archived codex thread hidden by default
        self.assertTrue(all(not key.startswith("_") for item in listed["items"] for key in item))
        self.assertEqual(reader.list_sessions(sources=["claude"], page_size=10)["page"]["totalKnown"], 2)
        self.assertEqual(reader.list_sessions(sources=["codex"], include_archived=True, page_size=10)["page"]["totalKnown"], 2)
        self.assertEqual(reader.list_sessions(query="보고서", page_size=10)["items"][0]["id"], "claude-code:sess-1")
        with self.assertRaises(self.module.AISessionsError):
            reader.list_sessions(page_size=7)

    def make_reports(self) -> Path:
        folder = self.root / "SyncFolder" / "ai-session-reports"
        folder.mkdir(parents=True)
        base = {"contract": "ai-session-report-v1", "agent": "claude", "sessionId": "session_01ABC",
                "title": "원격 요약 예시", "request": "원격 작업 요약을 보여줘",
                "conclusion": ["요약 파일 출처를 추가했습니다.", "token=abc123 가려져야 함", "세 번째", "네 번째"],
                "status": "완료", "project": "SampleProject", "model": "claude-opus-5-5",
                "createdAt": "2026-10-13T21:00:00Z", "updatedAt": "2026-10-13T22:00:00+00:00",
                "transcript": "FULL TRANSCRIPT MUST NOT BE COPIED"}
        (folder / "claude-session_01ABC.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        codex = dict(base, agent="codex", sessionId="task_9", title="문서 초안", status="이상한상태", conclusion="첫 줄\n둘째 줄")
        (folder / "codex-task_9.json").write_text("\ufeff" + json.dumps(codex, ensure_ascii=False), encoding="utf-8")
        (folder / "bad-contract.json").write_text(json.dumps(dict(base, contract="other")), encoding="utf-8")
        (folder / "broken.json").write_text("{not json", encoding="utf-8")
        (folder / "huge.json").write_text(json.dumps(dict(base, sessionId="huge", pad="x" * 70000)), encoding="utf-8")
        return folder

    def test_remote_reports_are_sanitized_and_filterable(self) -> None:
        cache = self.root / "ai.json"
        payload = self.module.collect(cache, {"codex": self.make_codex(), "reports": self.make_reports()}, now=self.now)
        self.assertEqual(payload["sources"]["reports"]["count"], 2)
        self.assertEqual(payload["sources"]["reports"]["skipped"], 3)
        items = {item["id"]: item for item in payload["items"]}
        claude = items["claude-cloud:session_01ABC"]
        self.assertEqual(claude["source"], "claude-cloud")
        self.assertEqual(claude["title"], "원격 요약 예시")
        self.assertEqual(claude["firstMessage"], "원격 작업 요약을 보여줘")
        self.assertEqual(claude["conclusion"], ["요약 파일 출처를 추가했습니다.", "[가림] 가려져야 함", "세 번째"])
        self.assertEqual(claude["updatedAt"], "2026-10-13T22:00:00Z")
        self.assertEqual(claude["folder"], "SampleProject")
        codex = items["codex-cloud:task_9"]
        self.assertEqual(codex["status"], "알 수 없음")
        self.assertEqual(codex["conclusion"], ["첫 줄", "둘째 줄"])
        self.assertNotIn("FULL TRANSCRIPT", cache.read_text(encoding="utf-8"))
        reader = self.module.AISessionsReader(cache)
        self.assertEqual(reader.list_sessions(sources=["remote"], page_size=10)["page"]["totalKnown"], 2)
        self.assertEqual(reader.list_sessions(sources=["claude"], page_size=10)["page"]["totalKnown"], 1)
        self.assertEqual({i["id"] for i in reader.list_sessions(sources=["codex"], page_size=10)["items"]},
                         {"codex:t1", "codex-cloud:task_9"})

    def test_reports_dir_comes_from_env_or_config(self) -> None:
        import os
        from unittest import mock
        config = self.root / "ai-sessions.config.json"
        config.write_text("\ufeff" + json.dumps({"reportsDir": "G:/Sync/ai-session-reports"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_SESSION_HUB_REPORTS", None)
            self.assertEqual(self.module.reports_dir_from_config(config), Path("G:/Sync/ai-session-reports"))
            self.assertIsNone(self.module.reports_dir_from_config(self.root / "missing.json"))
            os.environ["HERMES_SESSION_HUB_REPORTS"] = str(self.root / "env")
            self.assertEqual(self.module.reports_dir_from_config(config), self.root / "env")

    def test_conclusion_skips_tags_and_table_headers(self) -> None:
        lines = self.module.conclusion_lines("<decision>NOTIFY</decision>\n| 역할 | 도구 |\n|---|---|\n| 담당 | 도구A |\n결론입니다")
        self.assertEqual(lines, ["담당 · 도구A", "결론입니다"])

    def test_unchanged_files_reuse_cached_summary(self) -> None:
        cache = self.root / "ai.json"
        codex = self.make_codex()
        self.module.collect(cache, {"codex": codex}, now=self.now)
        data = json.loads(cache.read_text(encoding="utf-8"))
        for item in data["items"]:
            if item["id"] == "codex:t1":
                item["conclusion"] = ["캐시에서 재사용"]
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = self.module.collect(cache, {"codex": codex}, now=self.now)
        self.assertEqual({item["id"]: item for item in again["items"]}["codex:t1"]["conclusion"], ["캐시에서 재사용"])

    def test_missing_sources_are_reported_not_fatal(self) -> None:
        payload = self.module.collect(self.root / "ai.json", {
            "codex": self.root / "nope", "claude-desktop": self.root / "nope2", "claude-code": self.root / "nope3",
        }, now=self.now)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["sources"]["codex"]["error"], "codex_state_missing")
        self.assertFalse(payload["sources"]["claude-code"]["ok"])
        missing = self.module.collect(self.root / "ai2.json", {"reports": self.root / "nope4"}, now=self.now)
        self.assertEqual(missing["sources"]["reports"]["error"], "reports_dir_missing")


if __name__ == "__main__":
    unittest.main()
