"""Regression coverage for the read-only project assistant.

The assistant is intentionally scoped to one monitored project.  These tests
keep its transport offline and verify that it cannot become an execution or
data-exfiltration path.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from daemon import llm_summary
from daemon.llm_summary import (
    build_project_assistant_prompt,
    generate_project_assistant_reply,
)
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


def _project(workspace: str) -> dict[str, object]:
    return {
        "workspace": workspace,
        "name": "demo-project",
        "kind": "git",
        "status": "watching",
        "base_commit": "abc123",
        "last_seen": "2026-08-10T00:00:00Z",
        "last_error": "private project error must not leave the daemon",
        # These extra fields must never be included in an assistant prompt.
        "git_remote": "https://token@example.invalid/private.git",
        "watcher_config": {"secret": "do-not-send"},
        "canonical_ref": "private-ref",
    }


def _stats() -> dict[str, object]:
    return {
        "totals": {"cases": 2, "active_cases": 1, "tool_runs": 3},
        "case_counts_by_status": [{"status": "VERIFYING", "count": 1}],
    }


def _drive() -> dict[str, object]:
    return {
        "status": "complete",
        "started_at": "2026-08-10T01:00:00Z",
        "finished_at": "2026-08-10T01:01:00Z",
        "duration_s": 60.0,
        "error": "private drive error must not leave the daemon",
        "browse": {
            "file_count": 12,
            "total_size": 456,
            "language_stats": {"Python": 12},
            "markers": ["TODO"],
            "symbol_total": 24,
            "git": {
                "is_git": True,
                "branch": "main",
                "head": "abc123",
                "dirty_count": 1,
                "remote": "https://token@example.invalid/private.git",
            },
            "test": {
                "detected": True,
                "ran": True,
                "passed": True,
                "kind": "pytest",
                "output": "test output must not leave the daemon",
            },
            "static_scan": {
                "todo_count": 1,
                "fixme_count": 0,
                "error_handling_gaps": [{
                    "file": "daemon/server.py",
                    "line": 99,
                    "kind": "bare-except",
                    "snippet": "private source code must not leave the daemon",
                }],
            },
        },
    }


class ProjectAssistantLLMTests(unittest.TestCase):
    def test_without_key_is_unavailable_without_network_request(self) -> None:
        original_key, original_post = llm_summary._resolve_api_key, llm_summary._post_chat
        called = {"post": False}
        try:
            llm_summary._resolve_api_key = lambda: ""

            def forbidden_post(*args: object, **kwargs: object) -> str:
                called["post"] = True
                raise AssertionError("assistant must fail closed before network access")

            llm_summary._post_chat = forbidden_post
            reply = generate_project_assistant_reply("项目有什么风险？", _project("/tmp/demo"), _stats(), _drive())
            self.assertEqual(reply["status"], "unavailable")
            self.assertIn("DEEPSEEK_API_KEY", reply["reason"])
            self.assertFalse(called["post"])
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = original_key, original_post

    def test_prompt_excludes_remote_source_and_full_test_output(self) -> None:
        prompt = build_project_assistant_prompt("现在进度如何？", _project("/tmp/demo"), _stats(), _drive())
        self.assertIn('"file_count": 12', prompt)
        self.assertIn('"line": 99', prompt)
        self.assertNotIn("token@example.invalid", prompt)
        self.assertNotIn("do-not-send", prompt)
        self.assertNotIn("private-ref", prompt)
        self.assertNotIn("private project error", prompt)
        self.assertNotIn("private drive error", prompt)
        self.assertNotIn("test output must not leave", prompt)
        self.assertNotIn("private source code", prompt)

    def test_normalizes_reply_and_only_allows_known_source_labels(self) -> None:
        original_key, original_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"
            llm_summary._post_chat = lambda *args, **kwargs: json.dumps({
                "answer": "  当前有 2 个 Case。  ",
                "follow_ups": ["下一步？", 99, "风险？", "额外问题"],
                "sources": ["外部网页", "Case 聚合统计", "项目监控记录", "远程仓库"],
            })
            reply = generate_project_assistant_reply("现在进度如何？", _project("/tmp/demo"), _stats(), _drive())
            self.assertEqual(reply["status"], "ok")
            self.assertEqual(reply["answer"], "当前有 2 个 Case。")
            self.assertEqual(reply["follow_ups"], ["下一步？", "风险？"])
            self.assertEqual(reply["sources"], ["Case 聚合统计", "项目监控记录"])
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = original_key, original_post


class ProjectAssistantEndpointTests(unittest.TestCase):
    def _start_server(
        self, store: StateStore, assistant_fn: object | None = None,
    ) -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        server = CodeCCTVServer(
            ("127.0.0.1", 0), token, store,
            llm_chat_fn=assistant_fn,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    @staticmethod
    def _request(base_url: str, workspace: str, token: str | None, question: object) -> Request:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Code-CCTV-Token"] = token
        return Request(
            f"{base_url}/api/projects/{quote(workspace, safe='')}/assistant",
            data=json.dumps({"question": question}).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def test_endpoint_enforces_auth_project_and_question_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            workspace = Path(directory) / "project"
            workspace.mkdir()
            registered = store.register_monitored_project({"workspace": str(workspace), "kind": "process"})
            server, base_url, token = self._start_server(store)
            try:
                with self.assertRaises(HTTPError) as unauthenticated:
                    urlopen(self._request(base_url, registered["workspace"], None, "进度？"), timeout=3)
                self.assertEqual(unauthenticated.exception.code, 401)

                with self.assertRaises(HTTPError) as unknown_project:
                    urlopen(self._request(base_url, str(Path(directory) / "missing"), token, "进度？"), timeout=3)
                self.assertEqual(unknown_project.exception.code, 404)

                with self.assertRaises(HTTPError) as blank_question:
                    urlopen(self._request(base_url, registered["workspace"], token, "   "), timeout=3)
                self.assertEqual(blank_question.exception.code, 400)

                with self.assertRaises(HTTPError) as long_question:
                    urlopen(self._request(base_url, registered["workspace"], token, "x" * 1001), timeout=3)
                self.assertEqual(long_question.exception.code, 400)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_endpoint_uses_injected_read_only_reply_and_preserves_unavailable(self) -> None:
        seen: dict[str, object] = {}

        def fake_assistant(question: str, project: dict, stats: dict, drive: dict | None) -> dict:
            seen.update({"question": question, "project": project, "stats": stats, "drive": drive})
            return {
                "status": "ok",
                "answer": "当前没有阻塞。",
                "follow_ups": ["还需要什么？"],
                "sources": ["项目监控记录"],
            }

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            workspace = Path(directory) / "project"
            workspace.mkdir()
            registered = store.register_monitored_project({"workspace": str(workspace), "kind": "process"})
            server, base_url, token = self._start_server(store, fake_assistant)
            try:
                with urlopen(self._request(base_url, registered["workspace"], token, "项目进度？"), timeout=3) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["assistant"]["answer"], "当前没有阻塞。")
                self.assertEqual(seen["question"], "项目进度？")
                self.assertEqual(seen["project"]["workspace"], registered["workspace"])
                self.assertEqual(seen["stats"]["totals"]["cases"], 0)

                server.llm_chat_fn = lambda *args: {
                    "status": "unavailable", "reason": "DEEPSEEK_API_KEY 未配置；项目助手不可用。",
                }
                with urlopen(self._request(base_url, registered["workspace"], token, "还有风险吗？"), timeout=3) as response:
                    unavailable = json.loads(response.read())
                self.assertFalse(unavailable["ok"])
                self.assertEqual(unavailable["assistant"]["status"], "unavailable")
            finally:
                server.shutdown(); server.server_close(); store.close()


class ProjectAssistantConsoleTests(unittest.TestCase):
    def test_console_has_bounded_project_assistant_drawer(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="assistant-open-btn"', console)
        self.assertIn('id="assistant-drawer"', console)
        self.assertIn('maxlength="1000"', console)
        self.assertIn('/assistant`', console)
        self.assertIn('data-lucide="bot-message-square"', console)


if __name__ == "__main__":
    unittest.main()
