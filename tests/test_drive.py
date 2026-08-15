"""Tests for the automated project drive (browse + test probe + static scan +
LLM summary) and its HTTP endpoints.  Zero network: LLM calls are monkeypatched,
test probes use fake commands, and everything runs in temp dirs.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.harness import DevLoopHarness
from agent_runtime.orchestrator import Orchestrator
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter
from daemon.drive import browse_project, detect_test_command, run_drive, run_test_probe, scan_static
from daemon.llm_summary import build_drive_prompt, generate_drive_summary
from _helpers import start_server
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


def _make_repo(base: Path, name: str = "proj") -> Path:
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (repo / "README.md").write_text("# proj\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)
    return repo


class BrowseProjectTests(unittest.TestCase):
    def test_browse_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            b = browse_project(str(repo))
            self.assertGreaterEqual(b["file_count"], 2)
            self.assertIn("py", b["language_stats"])
            self.assertTrue(b["git"]["is_git"])
            self.assertTrue(b["git"]["branch"])
            self.assertTrue(b["git"]["head"])
            self.assertIn("README.md", b["markers"])

    def test_browse_can_skip_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            b = browse_project(str(repo), include_git=False)
            self.assertTrue(b["git"]["skipped"])
            self.assertFalse(b["git"]["is_git"])

    def test_browse_non_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            b = browse_project(directory)
            self.assertFalse(b["git"]["is_git"])
            self.assertEqual(b["git"]["remote"], "")

    def test_detect_test_command_pytest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            cmd = detect_test_command(d)
            self.assertTrue(cmd["detected"])
            self.assertEqual(cmd["kind"], "pytest")

    def test_detect_test_command_npm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "package.json").write_text(
                '{"scripts": {"test": "jest"}}', encoding="utf-8")
            cmd = detect_test_command(d)
            self.assertTrue(cmd["detected"])
            self.assertEqual(cmd["kind"], "npm")

    def test_detect_test_command_from_tests_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "tests").mkdir()
            (d / "tests" / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
            cmd = detect_test_command(d)
            self.assertTrue(cmd["detected"])
            self.assertEqual(cmd["kind"], "pytest")
            self.assertEqual(cmd["detail"], "tests 目录")
            self.assertTrue(cmd["command"].startswith(sys.executable))

    def test_detect_test_command_unittest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "tests").mkdir()
            (d / "tests" / "test_sample.py").write_text(
                "import unittest\n\nclass Sample(unittest.TestCase): pass\n", encoding="utf-8")
            cmd = detect_test_command(d)
            self.assertEqual(cmd["kind"], "unittest")
            self.assertIn("-m unittest discover -s tests", cmd["command"])

    def test_detect_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(detect_test_command(Path(directory))["detected"])


class TestProbeTests(unittest.TestCase):
    def test_probe_passing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            r = run_test_probe(directory, {"detected": True, "command": "true"}, timeout=5)
            self.assertTrue(r["ran"])
            self.assertTrue(r["passed"])

    def test_probe_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            r = run_test_probe(directory, {"detected": True, "command": "false"}, timeout=5)
            self.assertTrue(r["ran"])
            self.assertFalse(r["passed"])

    def test_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            r = run_test_probe(directory, {"detected": True, "command": "sleep 30"},
                               timeout=1)
            self.assertTrue(r["timed_out"])
            self.assertFalse(r["passed"])

    def test_probe_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            r = run_test_probe(directory, {"detected": True, "command": "definitely-not-a-cmd-xyz"},
                               timeout=2)
            self.assertTrue(r["ran"])
            self.assertFalse(r["passed"])

    def test_probe_not_detected(self) -> None:
        r = run_test_probe("/tmp", {"detected": False}, timeout=1)
        self.assertFalse(r["ran"])

    @mock.patch("daemon.drive.importlib.util.find_spec", return_value=None)
    def test_probe_missing_pytest_is_an_environment_observation(self, _find_spec) -> None:
        with tempfile.TemporaryDirectory() as directory:
            r = run_test_probe(directory, {
                "detected": True, "kind": "pytest", "command": "python -m pytest -q",
            }, timeout=1)
            self.assertFalse(r["ran"])
            self.assertTrue(r["execution_error"])
            self.assertTrue(r["runner_unavailable"])


class StaticScanTests(unittest.TestCase):
    def test_scan_finds_todo_and_bare_except(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "code.py").write_text(
                "def f():\n    # TODO fix this\n    try:\n        pass\n    except:\n        pass\n",
                encoding="utf-8")
            s = scan_static(directory)
            self.assertGreaterEqual(s["todo_count"], 1)
            self.assertTrue(any(g["kind"] == "python_bare_except" for g in s["error_handling_gaps"]))


class DriveSummaryTests(unittest.TestCase):
    def test_fail_closed_without_key(self) -> None:
        from daemon import llm_summary
        orig = llm_summary._resolve_api_key
        try:
            llm_summary._resolve_api_key = lambda: ""
            r = generate_drive_summary("/tmp/x", {"file_count": 1}, {"totals": {"cases": 0}})
            self.assertEqual(r["status"], "unavailable")
        finally:
            llm_summary._resolve_api_key = orig

    def test_drive_prompt_no_zero_case_shortcut(self) -> None:
        prompt = build_drive_prompt({"file_count": 5, "language_stats": {"py": 3}},
                                    {"totals": {"cases": 0}})
        self.assertNotIn("暂无 Case", prompt)
        self.assertIn("浏览报告", prompt)

    def test_drive_summary_ok(self) -> None:
        from daemon import llm_summary
        orig_key, orig_post = llm_summary._resolve_api_key, llm_summary._post_chat
        try:
            llm_summary._resolve_api_key = lambda: "sk-test"
            llm_summary._post_chat = lambda key, prompt, timeout=30, system_prompt=None: json.dumps({
                "overall_status": "项目健康",
                "top_priorities": ["P0 无"],
                "progress_by_phase": [{"phase": "browse", "progress": 100, "status": "已验证"}],
                "division_of_labor": [{"agent": "browse", "activity": "浏览", "share": 100}],
                "risks": [],
                "next_steps": [],
            })
            r = generate_drive_summary("/tmp/x", {"file_count": 1}, {"totals": {"cases": 0}})
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["summary"]["overall_status"], "项目健康")
        finally:
            llm_summary._resolve_api_key, llm_summary._post_chat = orig_key, orig_post


class DriveRunStoreTests(unittest.TestCase):
    def test_drive_run_crud(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            run_id = store.begin_drive_run(directory)
            self.assertEqual(store.get_latest_drive_run(directory)["status"], "running")
            store.finish_drive_run(run_id, "complete", 1.5, {"file_count": 3},
                                   {"status": "ok", "summary": {"overall_status": "x"}}, None)
            r = store.get_latest_drive_run(directory)
            self.assertEqual(r["status"], "complete")
            self.assertEqual(r["duration_s"], 1.5)
            self.assertEqual(r["browse"]["file_count"], 3)
            self.assertEqual(r["llm"]["summary"]["overall_status"], "x")
            store.close()

    @staticmethod
    def _browse_with_test(workspace: str) -> dict:
        return {
            "workspace": workspace,
            "file_count": 1,
            "language_stats": {"py": 1},
            "symbol_total": 1,
            "git": {"is_git": True, "branch": "main", "remote": "", "head": "abc", "dirty_count": 0, "recent_commits": []},
            "test": {"detected": True, "kind": "pytest", "command": "python -m pytest -q", "detail": "pytest 配置"},
            "static_scan": {"todo_count": 0, "fixme_count": 0, "error_handling_gaps": [], "scanned_files": 0},
        }

    @staticmethod
    def _static_scan(_: str) -> dict:
        return {"todo_count": 1, "fixme_count": 0, "error_handling_gaps": [{"file": "x.py", "line": 1}], "scanned_files": 1}

    def test_failed_test_is_promoted_and_dispatched_through_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            store = StateStore(Path(directory) / "state.sqlite3")
            orchestrator = Orchestrator(store, DevLoopHarness(AgentScopeExecutionAdapter(store)))
            result = run_drive(
                store, str(repo),
                browse_fn=self._browse_with_test,
                test_fn=lambda *_: {"detected": True, "ran": True, "timed_out": False, "passed": False, "exit_code": 2, "output_summary": "1 failed"},
                scan_fn=self._static_scan,
                llm_fn=lambda *_: {"status": "unavailable"},
                case_intake=orchestrator.on_source_received,
            )

            promotion = result["browse"]["case_promotion"]
            self.assertEqual(promotion["status"], "linked")
            case = store.get_case(promotion["case_id"])
            self.assertIsNotNone(case)
            self.assertEqual(case["status"], "PLAN_APPROVAL")
            evidence = store.get_case_evidence(promotion["case_id"])
            self.assertEqual(evidence["sources"][0]["source_type"], "self_test")
            signals = json.loads(evidence["sources"][0]["extracted_signals_json"])
            self.assertEqual(signals["exception_type"], "SelfTestFailure")
            self.assertNotIn("1 failed", json.dumps(signals, ensure_ascii=False))
            runs = [json.loads(run["output_ref"]) for run in evidence["agent_runs"]]
            self.assertEqual([run["harness_agent_id"] for run in runs], ["triage", "diagnosis"])

            repeated = run_drive(
                store, str(repo),
                browse_fn=self._browse_with_test,
                test_fn=lambda *_: {"detected": True, "ran": True, "timed_out": False, "passed": False, "exit_code": 2, "output_summary": "1 failed"},
                scan_fn=self._static_scan,
                llm_fn=lambda *_: {"status": "unavailable"},
                case_intake=orchestrator.on_source_received,
            )
            self.assertEqual(repeated["browse"]["case_promotion"]["case_id"], promotion["case_id"])
            self.assertEqual(store.project_summary(workspace=str(repo))["totals"]["cases"], 1)
            self.assertEqual(len(store.get_case_evidence(promotion["case_id"])["sources"]), 2)
            store.close()

    def test_timeout_is_promoted_but_pass_and_launch_errors_are_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            store = StateStore(Path(directory) / "state.sqlite3")
            orchestrator = Orchestrator(store, DevLoopHarness(AgentScopeExecutionAdapter(store)))
            common = {
                "browse_fn": self._browse_with_test,
                "scan_fn": self._static_scan,
                "llm_fn": lambda *_: {"status": "unavailable"},
                "case_intake": orchestrator.on_source_received,
            }
            timeout = run_drive(
                store, str(repo),
                test_fn=lambda *_: {"detected": True, "ran": True, "timed_out": True, "passed": False, "exit_code": None},
                **common,
            )
            self.assertEqual(timeout["browse"]["case_promotion"]["outcome"], "timeout")
            self.assertEqual(store.project_summary(workspace=str(repo))["totals"]["cases"], 1)

            for probe in (
                {"detected": True, "ran": True, "timed_out": False, "passed": True, "exit_code": 0},
                {"detected": True, "ran": True, "timed_out": False, "passed": False, "execution_error": True, "exit_code": None},
            ):
                result = run_drive(store, str(repo), test_fn=lambda *_args, value=probe: value, **common)
                self.assertFalse(result["browse"]["case_promotion"]["triggered"])
            self.assertEqual(store.project_summary(workspace=str(repo))["totals"]["cases"], 1)
            store.close()

    def test_review_run_persists_harness_owned_project_agent_and_parallel_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            store = StateStore(Path(directory) / "state.sqlite3")
            harness = DevLoopHarness(AgentScopeExecutionAdapter(store))
            result = run_drive(
                store, str(repo),
                browse_fn=self._browse_with_test,
                test_fn=lambda *_: {"detected": True, "ran": True, "timed_out": False,
                                    "passed": True, "exit_code": 0},
                scan_fn=self._static_scan,
                llm_fn=lambda *_: {"status": "unavailable"},
                harness=harness,
                scope={"mode": "full", "components": {"tests": True, "static": True}},
            )

            review = store.get_review_run(result["run_id"])
            self.assertIsNotNone(review)
            self.assertEqual(review["status"], "complete")
            self.assertEqual([task["task_key"] for task in review["tasks"]], [
                "prepare", "project_review", "test_probe", "static_scan", "summary", "case_handling",
            ])
            tasks = {task["task_key"]: task for task in review["tasks"]}
            self.assertTrue(all(task["status"] == "complete" for task in tasks.values()))
            agent_output = tasks["project_review"]["output"]
            self.assertEqual(agent_output["harness_id"], harness.harness_id)
            self.assertEqual(agent_output["harness_agent_id"], "project_review")
            self.assertEqual(agent_output["runtime_kind"], "local_deterministic_review_agent")
            self.assertTrue(agent_output["read_only"])
            self.assertEqual(review["linked_case_ids"], [])
            self.assertNotIn("repair", [task.get("agent_id") for task in review["tasks"]])
            self.assertNotIn("verification", [task.get("agent_id") for task in review["tasks"]])
            store.close()

    def test_review_scope_skips_disabled_checks_without_creating_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            store = StateStore(Path(directory) / "state.sqlite3")
            result = run_drive(
                store, str(repo),
                browse_fn=self._browse_with_test,
                test_fn=lambda *_: self.fail("disabled test probe should not run"),
                scan_fn=lambda *_: self.fail("disabled static scan should not run"),
                llm_fn=lambda *_: {"status": "unavailable"},
                scope={"mode": "fast", "components": {"tests": False, "static": False, "git": False}},
            )
            review = store.get_review_run(result["run_id"])
            tasks = {task["task_key"]: task for task in review["tasks"]}
            self.assertEqual(tasks["test_probe"]["status"], "skipped")
            self.assertEqual(tasks["static_scan"]["status"], "skipped")
            self.assertFalse(result["browse"]["case_promotion"]["triggered"])
            self.assertEqual(review["scope"]["mode"], "fast")
            store.close()

    def test_parallel_scan_exception_is_persisted_without_stalling_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            store = StateStore(Path(directory) / "state.sqlite3")
            result = run_drive(
                store, str(repo),
                browse_fn=self._browse_with_test,
                test_fn=lambda *_: {"detected": True, "ran": True, "timed_out": False,
                                    "passed": True, "exit_code": 0},
                scan_fn=lambda *_: (_ for _ in ()).throw(RuntimeError("scan failed")),
                llm_fn=lambda *_: {"status": "unavailable"},
            )
            review = store.get_review_run(result["run_id"])
            tasks = {task["task_key"]: task for task in review["tasks"]}
            self.assertEqual(review["status"], "complete")
            self.assertEqual(tasks["test_probe"]["status"], "complete")
            self.assertEqual(tasks["static_scan"]["status"], "error")
            self.assertIn("RuntimeError", tasks["static_scan"]["failure_reason"])
            self.assertEqual(tasks["summary"]["status"], "complete")
            self.assertFalse(result["browse"]["case_promotion"]["triggered"])
            store.close()


class DriveEndpointTests(unittest.TestCase):
    def _start_server(self, store, runner=None, inject_stub=True):
        drive_runner = runner
        if drive_runner is None and inject_stub:
            drive_runner = (
                lambda store, workspace, run_id=None, publish=None: {"status": "complete"}
            )
        return start_server(store, drive_runner=drive_runner)

    def _post_drive(self, base, token, workspace):
        enc = workspace.replace("/", "%2F")
        return Request(f"{base}/api/projects/{enc}/drive",
                       data=b"{}", method="POST",
                       headers={"X-Code-CCTV-Token": token})

    def test_post_requires_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            server, base, _ = self._start_server(store)
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urlopen(self._post_drive(base, "", directory), timeout=3)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_drive_unregistered_workspace_forbidden(self) -> None:
        """The built-in driver must refuse unregistered directories: it reads
        the tree, executes tests and may ship content to an LLM provider."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            # No drive_runner injection → built-in path, registration enforced.
            server, base, token = self._start_server(store, inject_stub=False)
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urlopen(self._post_drive(base, token, directory), timeout=3)
                self.assertEqual(ctx.exception.code, 403)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_drive_registered_workspace_accepted(self) -> None:
        """A registered monitored project may be driven on the built-in path."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            store.register_monitored_project({
                "workspace": directory, "kind": "process", "name": "tmp-project",
            })
            server, base, token = self._start_server(store, inject_stub=False)
            try:
                with urlopen(self._post_drive(base, token, directory), timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertEqual(resp.status, 202)
                self.assertTrue(body["ok"])
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_drive_returns_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            server, base, token = self._start_server(store)
            try:
                with urlopen(self._post_drive(base, token, directory), timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertEqual(resp.status, 202)
                self.assertTrue(body["ok"])
                self.assertEqual(body["run"]["status"], "running")
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_get_drive_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            store.finish_drive_run(store.begin_drive_run(directory), "complete", 2.0,
                                   {"file_count": 1}, None, None)
            server, base, token = self._start_server(store)
            try:
                req = Request(f"{base}/api/projects/{directory.replace('/', '%2F')}/drive",
                              headers={"X-Code-CCTV-Token": token})
                with urlopen(req, timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["run"]["status"], "complete")
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_get_review_history_returns_first_class_review_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            run_id = store.begin_review_run(directory, {"mode": "full"}, [{
                "task_key": "prepare", "title": "准备", "stage": "prepare", "order": 1,
            }])
            store.finish_review_run(run_id, "complete", 0.1, {"file_count": 1}, None, [], None)
            server, base, token = self._start_server(store)
            try:
                req = Request(
                    f"{base}/api/projects/{directory.replace('/', '%2F')}/reviews",
                    headers={"X-Code-CCTV-Token": token},
                )
                with urlopen(req, timeout=3) as resp:
                    body = json.loads(resp.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["count"], 1)
                self.assertEqual(body["runs"][0]["run_id"], run_id)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_begin_review_run_if_idle_blocks_concurrent_start(self) -> None:
        """A second idle-guarded start while one is running must return None."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            first = store.begin_review_run_if_idle(directory, {"mode": "full"}, [{
                "task_key": "prepare", "title": "准备", "stage": "prepare", "order": 1,
            }])
            self.assertIsNotNone(first)
            # A running Review Run exists → the guard refuses a second one.
            self.assertIsNone(store.begin_review_run_if_idle(directory))
            # After finishing, a new run can start.
            store.finish_review_run(first, "complete", 0.1, None, None, [], None)
            second = store.begin_review_run_if_idle(directory)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            store.close()


if __name__ == "__main__":
    unittest.main()
