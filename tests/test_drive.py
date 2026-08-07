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
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.drive import browse_project, detect_test_command, run_test_probe, scan_static
from daemon.llm_summary import build_drive_prompt, generate_drive_summary
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


class DriveEndpointTests(unittest.TestCase):
    def _start_server(self, store, runner=None):
        token = secrets.token_hex(16)
        server = CodeCCTVServer(
            ("127.0.0.1", 0), token, store, drive_runner=runner or (
                lambda store, workspace, run_id=None, publish=None: {"status": "complete"}
            ))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

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


if __name__ == "__main__":
    unittest.main()
