"""Tests for the project summary aggregate endpoint and LLM summary module.

Covers:
- StateStore.project_summary() deterministic rollups (empty DB, status/priority/risk,
  agent/tool counts, approval/knowledge/source counts, UTC-day activity timeline).
- daemon.llm_summary: fail-closed without key, JSON parse/normalize, transport error,
  TTL cache behavior (ok/error/unavailable/refresh).
- HTTP GET /api/project/summary: auth, shape, ?refresh=1 passthrough.

No real network is ever touched: the LLM module tests monkeypatch
``_resolve_api_key`` / ``_post_chat``, and the HTTP tests inject a fake
``llm_summary_fn`` into ``CodeDefogServer``.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

from daemon.llm_summary import (
    _extract_json,
    _normalize_summary,
    build_summary_prompt,
    generate_project_summary,
    get_llm_summary,
)
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


def _seed_case(store: StateStore, source_uri: str, nonce: str, repo: str = "/tmp/demo-target",
               **overrides: object) -> str:
    """Create an isolated Case.  A distinct *repo* forces a distinct
    incident_signature so the Case is not merged into another one."""
    payload = {
        "source_type": "issue",
        "source_uri": source_uri,
        "client_nonce": nonce,
        "raw_content": f"KeyError at {nonce}",
        "repository_ref": repo,
        "extracted_signals": {
            "exception_type": "KeyError",
            "message_pattern": f"pattern-{nonce}",
            "key_frames": [f"cli.py:{nonce[-2:]}"],
            "keywords": [nonce],
            "repository_ref": repo,
        },
    }
    payload.update(overrides)
    result = store.create_or_find_case(payload)
    return result["case_id"]


class ProjectSummaryStoreTests(unittest.TestCase):
    def test_empty_db_returns_zero_filled_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            r = store.project_summary()
            self.assertEqual(r["totals"]["cases"], 0)
            self.assertEqual(r["totals"]["active_cases"], 0)
            for key in ("case_counts_by_status", "agent_run_counts", "tool_counts",
                        "approval_counts", "knowledge_counts", "source_counts"):
                self.assertEqual(r[key], [])
            self.assertEqual(len(r["activity_timeline"]), 14)
            self.assertTrue(all(row["cases_updated"] == 0 and row["events"] == 0
                                for row in r["activity_timeline"]))
            store.close()

    def test_case_status_priority_risk_rollups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            a = _seed_case(store, "audit://a", "n-a", repo="/tmp/repo-a")
            b = _seed_case(store, "audit://b", "n-b", repo="/tmp/repo-b")
            c = _seed_case(store, "audit://c", "n-c", repo="/tmp/repo-c", priority="high", risk_level="critical")
            store.transition_case(b, "TRIAGED")
            store.connection.execute(
                "UPDATE cases SET priority = 'high', risk_level = 'critical' WHERE case_id = ?", (c,))
            store.connection.commit()
            r = store.project_summary()
            self.assertEqual(r["totals"]["cases"], 3)
            self.assertGreaterEqual(r["totals"]["active_cases"], 2)
            by_status = {row["status"]: row["count"] for row in r["case_counts_by_status"]}
            self.assertIn("RECEIVED", by_status)
            self.assertIn("TRIAGED", by_status)
            by_priority = {row["priority"]: row["count"] for row in r["case_counts_by_priority"]}
            self.assertEqual(by_priority.get("high"), 1)
            by_risk = {row["risk_level"]: row["count"] for row in r["case_counts_by_risk"]}
            self.assertEqual(by_risk.get("critical"), 1)
            store.close()

    def test_agent_and_tool_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = _seed_case(store, "audit://tools", "n-tools", repo="/tmp/repo-t1")
            case2 = _seed_case(store, "audit://tools2", "n-tools2", repo="/tmp/repo-t2")
            # Explicit agent_runs rows (record_tool_run only writes tool_runs).
            for i, cid in enumerate((case_id, case2)):
                store.connection.execute(
                    "INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at) "
                    "VALUES (?, ?, ?, 'completed', 'tr', '2026-08-01T00:00:00Z')",
                    (f"run-{i}", cid, "verification"))
            # Two tool runs: one exit 0, one exit 1 (different cases keep the chain simple)
            store.record_tool_run({
                "case_id": case_id, "agent_id": "verification", "tool_name": "quality_gate",
                "command_template": "python quality_gate.py {repo}", "actual_argv": "python quality_gate.py /tmp",
                "working_directory": "/tmp", "policy_version": "v0.5",
                "input_sha256": "a" * 64, "output_sha256": "b" * 64, "exit_code": 0,
                "result_ref": "art-1",
            })
            store.record_tool_run({
                "case_id": case2, "agent_id": "verification", "tool_name": "quality_gate",
                "command_template": "python quality_gate.py {repo}", "actual_argv": "python quality_gate.py /tmp2",
                "working_directory": "/tmp", "policy_version": "v0.5",
                "input_sha256": "c" * 64, "output_sha256": "d" * 64, "exit_code": 1,
                "result_ref": "art-2",
            })
            store.connection.commit()
            r = store.project_summary()
            self.assertEqual(r["totals"]["tool_runs"], 2)
            tool = {row["tool_name"]: row for row in r["tool_counts"]}["quality_gate"]
            self.assertEqual(tool["count"], 2)
            self.assertEqual(tool["exit_zero"], 1)
            self.assertEqual(r["totals"]["agent_runs"], 2)
            agent = {row["agent_id"]: row for row in r["agent_run_counts"]}["verification"]
            self.assertEqual(agent["count"], 2)
            store.close()

    def test_approval_knowledge_source_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = _seed_case(store, "audit://gov", "n-gov")
            store.connection.execute(
                "INSERT INTO approvals (approval_id, case_id, grant_id, action, decision, "
                "approver, reason, target_ref, token_hash, expires_at, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ap-1", case_id, "g-1", "approve_plan", "approved", "reviewer", "",
                 "base", "abc", "2026-12-31", "2026-08-01"),
            )
            store.connection.execute(
                "INSERT INTO knowledge_records (record_id, case_id, status, content_ref, "
                "reuse_tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("kr-1", case_id, "pending_review", "art#0", "[]", "2026-08-01"),
            )
            store.connection.commit()
            r = store.project_summary()
            self.assertEqual(r["totals"]["approvals"], 1)
            self.assertEqual(r["approval_counts"][0]["decision"], "approved")
            self.assertEqual(r["totals"]["knowledge_records"], 1)
            self.assertEqual(r["knowledge_counts"][0]["status"], "pending_review")
            self.assertGreaterEqual(r["totals"]["sources"], 1)
            store.close()

    def test_project_summary_workspace_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            repo_a = Path(directory) / "repo-a"
            repo_b = Path(directory) / "repo-b"
            repo_a.mkdir(); repo_b.mkdir()
            a = _seed_case(store, "audit://wa", "n-wa", repo=str(repo_a))
            _seed_case(store, "audit://wb", "n-wb", repo=str(repo_b))
            # Give repo-a one extra artifact so totals differ.
            store.record_artifact(a, "patch_metadata", "patch.json", b"abc")
            scoped = store.project_summary(workspace=str(repo_a.resolve()))
            self.assertEqual(scoped["totals"]["cases"], 1)
            self.assertGreaterEqual(scoped["totals"]["artifacts"], 1)
            by_status = {row["status"]: row["count"] for row in scoped["case_counts_by_status"]}
            self.assertEqual(sum(by_status.values()), 1)
            # repo-b case must not leak into repo-a timeline counts
            self.assertEqual(scoped["totals"]["sources"], 1)
            global_s = store.project_summary()
            self.assertEqual(global_s["totals"]["cases"], 2)
            store.close()

    def test_project_summary_no_workspace_is_global(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            _seed_case(store, "audit://ga", "n-ga", repo="/tmp/repo-a")
            _seed_case(store, "audit://gb", "n-gb", repo="/tmp/repo-b")
            r = store.project_summary()
            self.assertEqual(r["totals"]["cases"], 2)
            self.assertEqual(len(r["activity_timeline"]), 14)
            store.close()


class ProjectSummaryLLMTests(unittest.TestCase):
    def test_fail_closed_without_key(self) -> None:
        orig = generate_project_summary.__globals__["_resolve_api_key"]
        try:
            from daemon import llm_summary
            llm_summary._resolve_api_key = lambda: ""
            result = generate_project_summary({"totals": {"cases": 1}})
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("DEEPSEEK_API_KEY", result["reason"])
        finally:
            generate_project_summary.__globals__["_resolve_api_key"] = orig

    def test_empty_stats_short_circuits_prompt(self) -> None:
        prompt = build_summary_prompt({"totals": {"cases": 0}}, "")
        self.assertIn("暂无 Case", prompt)

    def test_ok_parses_and_normalizes(self) -> None:
        from daemon import llm_summary
        orig_key, orig_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"
            llm_summary._post_chat = lambda key, prompt: json.dumps({
                "overall_status": "  一切正常  ",
                "top_priorities": ["P0 修复", "P1 优化"],
                "progress_by_phase": [{"phase": "受控闭环", "progress": 80, "status": "进行中"}],
                "division_of_labor": [{"agent": "repair", "activity": "应用补丁", "share": 40}],
                "risks": ["待确认项"],
                "next_steps": ["提交"],
            })
            result = generate_project_summary({"totals": {"cases": 1}})
            self.assertEqual(result["status"], "ok")
            s = result["summary"]
            self.assertEqual(s["overall_status"], "一切正常")
            self.assertEqual(s["top_priorities"][0], "P0 修复")
            self.assertEqual(s["progress_by_phase"][0]["progress"], 80)
            self.assertEqual(s["division_of_labor"][0]["agent"], "repair")
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = orig_key, orig_post

    def test_missing_fields_coerced(self) -> None:
        from daemon import llm_summary
        orig_key, orig_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"
            llm_summary._post_chat = lambda key, prompt: json.dumps({"overall_status": 42})
            result = generate_project_summary({"totals": {"cases": 1}})
            self.assertEqual(result["status"], "ok")
            s = result["summary"]
            self.assertEqual(s["overall_status"], "未知")  # int -> "未知"
            self.assertEqual(s["top_priorities"], [])
            self.assertEqual(s["risks"], [])
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = orig_key, orig_post

    def test_malformed_json_is_error(self) -> None:
        from daemon import llm_summary
        orig_key, orig_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"
            llm_summary._post_chat = lambda key, prompt: "not json at all"
            result = generate_project_summary({"totals": {"cases": 1}})
            self.assertEqual(result["status"], "error")
            self.assertIn("JSON", result["reason"])
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = orig_key, orig_post

    def test_transport_error_is_error(self) -> None:
        from daemon import llm_summary
        orig_key, orig_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"

            def boom(key, prompt):
                raise urllib.error.URLError("connection refused")
            llm_summary._post_chat = boom
            result = generate_project_summary({"totals": {"cases": 1}})
            self.assertEqual(result["status"], "error")
            self.assertIn("失败", result["reason"])
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = orig_key, orig_post

    def test_extract_json_fence_and_bare(self) -> None:
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```')["a"], 1)
        self.assertEqual(_extract_json('leading prose\n{"a": 2}')["a"], 2)
        self.assertIsNone(_extract_json("no json here"))

    def test_normalize_summary_coerces(self) -> None:
        n = _normalize_summary({"overall_status": "ok", "top_priorities": "not a list",
                                "division_of_labor": [{"agent": "x"}]})
        self.assertEqual(n["top_priorities"], [])
        self.assertEqual(n["division_of_labor"][0]["agent"], "x")
        self.assertEqual(n["division_of_labor"][0]["share"], 0)

    def test_get_llm_summary_cache(self) -> None:
        from daemon import llm_summary
        calls = {"n": 0}

        def fake(stats, worklog_text=None):
            calls["n"] += 1
            return {"status": "ok", "summary": {"overall_status": "x"}}
        orig = llm_summary.generate_project_summary
        llm_summary.generate_project_summary = fake
        try:
            cache = {}
            get_llm_summary({"totals": {"cases": 1}}, cache=cache)
            get_llm_summary({"totals": {"cases": 1}}, cache=cache)
            self.assertEqual(calls["n"], 1)  # cached
            get_llm_summary({"totals": {"cases": 1}}, refresh=True, cache=cache)
            self.assertEqual(calls["n"], 2)  # bypass
        finally:
            llm_summary.generate_project_summary = orig

    def test_get_llm_summary_cache_keyed_by_project(self) -> None:
        from daemon import llm_summary
        calls = {"n": 0}

        def fake(stats, worklog_text=None):
            calls["n"] += 1
            return {"status": "ok", "summary": {"overall_status": f"x{calls['n']}"}}
        orig = llm_summary.generate_project_summary
        llm_summary.generate_project_summary = fake
        try:
            cache = {}
            r_a1 = get_llm_summary({"totals": {"cases": 1}}, cache=cache, key="repo-a")
            r_a2 = get_llm_summary({"totals": {"cases": 1}}, cache=cache, key="repo-a")
            self.assertEqual(calls["n"], 1)  # same key cached
            r_b1 = get_llm_summary({"totals": {"cases": 2}}, cache=cache, key="repo-b")
            self.assertEqual(calls["n"], 2)  # different key regenerates
            self.assertEqual(r_a1, r_a2)  # identical cached result
            self.assertNotEqual(r_a1["summary"], r_b1["summary"])
        finally:
            llm_summary.generate_project_summary = orig


class ProjectSummaryEndpointTests(unittest.TestCase):
    def _start_server(self, store: StateStore, llm_fn=None) -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        server = CodeCCTVServer(
            ("127.0.0.1", 0), token, store,
            llm_summary_fn=llm_fn or (lambda stats, refresh=False, cache=None, key="default": {"status": "ok", "summary": {"overall_status": "test"}}),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    def test_requires_service_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, _ = self._start_server(store)
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urlopen(Request(f"{base_url}/api/project/summary"), timeout=3)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_summary_endpoint_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            _seed_case(store, "audit://http", "n-http")
            server, base_url, token = self._start_server(store)
            try:
                req = Request(f"{base_url}/api/project/summary",
                              headers={"X-Code-CCTV-Token": token})
                with urlopen(req, timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertTrue(body["ok"])
                self.assertIn("generated_at", body)
                self.assertIn("stats", body)
                self.assertIn("llm", body)
                self.assertEqual(body["stats"]["totals"]["cases"], 1)
                self.assertEqual(body["llm"]["status"], "ok")
                self.assertEqual(body["llm"]["summary"]["overall_status"], "test")
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_refresh_param_passed_to_summarizer(self) -> None:
        seen = {"refresh": None}
        def recording_fn(stats, refresh=False, cache=None, key="default"):
            seen["refresh"] = refresh
            return {"status": "ok", "summary": {"overall_status": "t"}}
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = self._start_server(store, llm_fn=recording_fn)
            try:
                req = Request(f"{base_url}/api/project/summary",
                              headers={"X-Code-CCTV-Token": token})
                urlopen(req, timeout=3)
                self.assertFalse(seen["refresh"])
                req2 = Request(f"{base_url}/api/project/summary?refresh=1",
                               headers={"X-Code-CCTV-Token": token})
                urlopen(req2, timeout=3)
                self.assertTrue(seen["refresh"])
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_workspace_param_scopes_summary_and_key(self) -> None:
        import urllib.parse

        seen = {"key": None, "cases": None}
        def recording_fn(stats, refresh=False, cache=None, key="default"):
            seen["key"] = key
            seen["cases"] = stats.get("totals", {}).get("cases", 0)
            return {"status": "ok", "summary": {"overall_status": "t"}}
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            repo_a = Path(directory) / "repo-a"
            repo_b = Path(directory) / "repo-b"
            repo_a.mkdir(); repo_b.mkdir()
            _seed_case(store, "audit://repo-a", "n-a", repo=str(repo_a))
            _seed_case(store, "audit://repo-b", "n-b", repo=str(repo_b))
            server, base_url, token = self._start_server(store, llm_fn=recording_fn)
            try:
                ws = str(repo_a.resolve())
                req = Request(f"{base_url}/api/project/summary?workspace={urllib.parse.quote(ws)}",
                              headers={"X-Code-CCTV-Token": token})
                with urlopen(req, timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertEqual(body["stats"]["totals"]["cases"], 1)  # scoped to repo-a
                self.assertEqual(seen["key"], ws)
                self.assertEqual(seen["cases"], 1)
            finally:
                server.shutdown(); server.server_close(); store.close()


if __name__ == "__main__":
    unittest.main()
