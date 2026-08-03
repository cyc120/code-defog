"""Windows-compatibility tests. All run on macOS (and are written to also run
on Windows); they exercise the platform guards, path resolution, timezone
fallback, process detection, and the pure-Python StatusClient against a live
in-process daemon.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "windows"))
sys.path.insert(0, str(ROOT))

import win_support  # noqa: E402


class ZoneInfoFallbackTests(unittest.TestCase):
    def test_load_tz_uses_fixed_offset_when_zoneinfo_missing(self) -> None:
        import update_worklog
        with patch.object(update_worklog, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            tz = update_worklog.load_tz("Asia/Shanghai")
            self.assertEqual(tz.utcoffset(None), timedelta(hours=8))

    def test_load_tz_unknown_zone_uses_utc8_default(self) -> None:
        import update_worklog
        with patch.object(update_worklog, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            tz = update_worklog.load_tz("Mars/Olympus")
            self.assertEqual(tz.utcoffset(None), timedelta(hours=8))

    def test_now_text_with_fallback_is_timestamp(self) -> None:
        import update_worklog
        with patch.object(update_worklog, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            result = update_worklog.now_text("Asia/Shanghai")
            self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


class PathsTests(unittest.TestCase):
    def test_darwin_data_dir_is_historical_location(self) -> None:
        from daemon import paths
        if sys.platform != "darwin":
            self.skipTest("darwin-only assertion")
        expected = Path.home() / "Library" / "Application Support" / "CodeCCTV"
        self.assertEqual(paths.data_dir(), expected)
        self.assertEqual(paths.config_path(), expected / "service.json")
        self.assertEqual(paths.state_path(), expected / "state.sqlite3")

    def test_win32_data_dir_uses_appdata(self) -> None:
        from daemon import paths
        with tempfile.TemporaryDirectory() as tmp, \
             patch("daemon.paths.sys.platform", "win32"), \
             patch.dict(os.environ, {"APPDATA": tmp}, clear=False):
            self.assertEqual(paths.data_dir(), Path(tmp) / "CodeCCTV")

    def test_win32_data_dir_falls_back_to_localappdata(self) -> None:
        from daemon import paths
        with tempfile.TemporaryDirectory() as tmp, \
             patch("daemon.paths.sys.platform", "win32"), \
             patch.dict(os.environ, {"APPDATA": "", "LOCALAPPDATA": tmp}, clear=False):
            self.assertEqual(paths.data_dir(), Path(tmp) / "CodeCCTV")

    def test_config_path_honors_override(self) -> None:
        from daemon import paths
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CODE_CCTV_CONFIG": str(Path(tmp) / "custom.json")}, clear=False):
            self.assertEqual(paths.config_path(), Path(tmp) / "custom.json")


class WinSupportTests(unittest.TestCase):
    def test_chatgpt_running_via_process_table(self) -> None:
        with patch.object(win_support, "_process_running", return_value=True):
            self.assertTrue(win_support.chatgpt_running())

    def test_chatgpt_running_falls_back_to_install_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(win_support, "_process_running", return_value=False), \
             patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
            install_dir = Path(tmp) / "Programs" / "OpenAI"
            install_dir.mkdir(parents=True, exist_ok=True)
            (install_dir / "ChatGPT.exe").write_text("", encoding="utf-8")
            self.assertTrue(win_support.chatgpt_running())

    def test_chatgpt_running_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(win_support, "_process_running", return_value=False), \
             patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
            self.assertFalse(win_support.chatgpt_running())

    def test_task_exists(self) -> None:
        with patch.object(win_support, "_run",
                          return_value=subprocess.CompletedProcess(["schtasks"], 0, "ok", "")):
            self.assertTrue(win_support.task_exists())

    def test_launcher_bat_content_includes_module_and_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(win_support, "data_dir", return_value=Path(tmp)):
            path = win_support.write_launcher_bat(Path("/repo"))
            content = path.read_text(encoding="utf-8")
            self.assertIn("-X", content)
            self.assertIn("utf8", content)
            self.assertIn("-m daemon.serve", content)


class StoreChmodTests(unittest.TestCase):
    def test_win32_no_chmod_crash(self) -> None:
        from daemon.store import StateStore
        with tempfile.TemporaryDirectory() as directory, \
             patch("daemon.store.sys.platform", "win32"):
            store = StateStore(Path(directory) / "state.sqlite3")
            self.assertTrue(store.path.exists())
            store.close()

    def test_posix_chmod_0600(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only assertion")
        from daemon.store import StateStore
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                mode = store.path.stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)
            finally:
                store.close()


class StatusClientTests(unittest.TestCase):
    def _daemon(self):
        """Start an in-process daemon; returns (store, server, thread, url)."""
        import tempfile as _t
        from daemon.server import CodeCCTVServer
        from daemon.store import StateStore
        store = StateStore(Path(_t.mkdtemp()) / "state.sqlite3")
        server = CodeCCTVServer(("127.0.0.1", 0), "test-token", store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return store, server, thread

    def test_content_id_dedup(self) -> None:
        from status_client import content_id
        base = {
            "workspace": "/a", "conversation_id": "t1", "updated_at": "2026-08-03T00:00:00Z",
            "event_count": 3, "status": "监听中", "phase": "x", "focus": "A", "note": "",
            "evidence": "", "event_type": "progress", "active": True,
            "recent_events": [{"id": "e1"}],
        }
        state1 = {"generated_at": "a", "projects": [dict(base)]}
        state2 = {"generated_at": "b", "projects": [dict(base)]}  # only generated_at differs
        changed = {"generated_at": "c", "projects": [dict(base) | {"focus": "B"}]}
        self.assertEqual(content_id(state1), content_id(state2))
        self.assertNotEqual(content_id(state1), content_id(changed))

    def test_parse_sse_line(self) -> None:
        from status_client import parse_sse_line
        self.assertEqual(parse_sse_line(": heartbeat"), None)
        self.assertEqual(parse_sse_line(b"data: {\"type\":\"state\",\"state\":{\"projects\":[]}}"),
                         ("state", {"projects": []}))
        self.assertEqual(parse_sse_line("data: not-json"), None)
        self.assertEqual(parse_sse_line("data: {\"type\":\"other\",\"state\":{}}"), None)

    def test_status_client_receives_state_from_live_daemon(self) -> None:
        store, server, thread = self._daemon()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        from status_client import StatusClient
        client = StatusClient(
            config_provider=lambda: {"host": "127.0.0.1",
                                     "port": server.server_address[1],
                                     "token": "test-token"},
            enable_stream=True,
        )
        received = threading.Event()
        captured: dict = {}

        def on_state(state: dict) -> None:
            captured["state"] = state
            received.set()

        client.on_state = on_state
        client.start()
        try:
            workspace = Path(tempfile.mkdtemp())
            store.ingest({"workspace": str(workspace), "status": "监听中", "focus": "端到端测试"})
            self.assertTrue(received.wait(timeout=5), "StatusClient never received state")
            projects = captured["state"].get("projects", [])
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["focus"], "端到端测试")
        finally:
            client.stop()
            server.shutdown()
            server.server_close()
            store.close()
            thread.join(timeout=1)


def _has_pyside6() -> bool:
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_pyside6(), "PySide6 not installed")
class GuiImportTests(unittest.TestCase):
    def test_gui_modules_import(self) -> None:
        import main_window  # noqa: F401
        import system_tray  # noqa: F401
        import main  # noqa: F401


if __name__ == "__main__":
    unittest.main()
