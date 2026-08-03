from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from daemon.server import CodeCCTVServer
from daemon.store import StateStore


class StateStoreTests(unittest.TestCase):
    def test_ingest_builds_global_project_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            state = store.ingest(
                {
                    "workspace": str(workspace),
                    "event_type": "progress",
                    "source": "test",
                    "status": "验证中",
                    "phase": "测试",
                    "focus": "检查服务",
                    "note": "只保存摘要",
                    "files": ["src/main.py"],
                }
            )

            self.assertEqual(state["summary"]["total_projects"], 1)
            self.assertEqual(state["summary"]["active_projects"], 1)
            self.assertEqual(state["projects"][0]["name"], "demo")
            self.assertEqual(state["projects"][0]["recent_events"][0]["files"], ["src/main.py"])
            store.close()

    def test_same_workspace_keeps_conversations_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "shared"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            for conversation_id, focus in (("thread-a", "会话 A"), ("thread-b", "会话 B")):
                store.ingest(
                    {
                        "workspace": str(workspace),
                        "workspace_name": "shared",
                        "conversation_id": conversation_id,
                        "status": "监听中",
                        "focus": focus,
                    }
                )

            state = store.state()
            self.assertEqual(state["summary"]["total_projects"], 2)
            self.assertEqual(
                {project["conversation_id"] for project in state["projects"]},
                {"thread-a", "thread-b"},
            )
            self.assertEqual(
                {project["recent_events"][0]["conversation_id"] for project in state["projects"]},
                {"thread-a", "thread-b"},
            )
            store.close()

    def test_legacy_workspace_schema_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            workspace = Path(directory) / "legacy"
            workspace.mkdir()
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE projects (
                    workspace TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    note TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE events (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    note TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    files_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(workspace), "legacy", "完成", "", "旧状态", "", "", "progress", "2026-01-01T00:00:00Z", 1),
            )
            connection.commit()
            connection.close()

            store = StateStore(database)
            state = store.state()
            self.assertEqual(state["summary"]["total_projects"], 1)
            self.assertEqual(state["projects"][0]["conversation_id"], "default")
            self.assertEqual(state["projects"][0]["name"], "legacy")
            store.close()

    def test_watching_status_stays_active_after_event_timeout(self) -> None:
        self.assertTrue(StateStore.is_watching("监听中"))
        self.assertTrue(StateStore.is_watching("Watching"))
        self.assertFalse(StateStore.is_watching("完成"))

    def test_recent_events_limited_to_eight_per_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "burst"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            base = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
            for index in range(24):
                store.ingest(
                    {
                        "workspace": str(workspace),
                        "workspace_name": "burst",
                        "conversation_id": "thread-a" if index % 2 == 0 else "thread-b",
                        "status": "监听中",
                        "phase": "步骤",
                        "focus": f"会话 {index % 2 + 1} 事件 {index}",
                        "timestamp": (base + timedelta(seconds=index))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )

            state = store.state()
            by_session = {
                project["conversation_id"]: project["recent_events"] for project in state["projects"]
            }
            self.assertEqual(len(by_session["thread-a"]), 8)
            self.assertEqual(len(by_session["thread-b"]), 8)
            for session_events in by_session.values():
                numbers = [
                    int(event["focus"].rsplit("事件 ", 1)[1]) for event in session_events
                ]
                self.assertEqual(numbers, sorted(numbers, reverse=True))
            store.close()

    def test_retention_prune_is_throttled_and_keeps_bounded_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "history"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3", retention=100)
            for index in range(150):
                store.ingest(
                    {
                        "workspace": str(workspace),
                        "workspace_name": "history",
                        "status": "监听中",
                        "focus": f"事件 {index}",
                    }
                )

            connection = sqlite3.connect(store.path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 100)
            state = store.state()
            self.assertEqual(state["projects"][0]["event_count"], 150)
            self.assertEqual(len(state["projects"][0]["recent_events"]), 8)
            self.assertEqual(store._ingests_since_prune, 0)
            store.close()

    def test_retention_prune_does_not_evict_idle_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "mix"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3", retention=100)
            base = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
            for index in range(150):
                store.ingest(
                    {
                        "workspace": str(workspace),
                        "conversation_id": "busy",
                        "status": "监听中",
                        "focus": f"busy {index}",
                        "timestamp": (base + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
                    }
                )
            for index in range(5):
                store.ingest(
                    {
                        "workspace": str(workspace),
                        "conversation_id": "idle",
                        "status": "监听中",
                        "focus": f"idle {index}",
                        "timestamp": (base + timedelta(seconds=1000 + index)).isoformat().replace("+00:00", "Z"),
                    }
                )

            connection = sqlite3.connect(store.path)
            try:
                counts = dict(
                    connection.execute(
                        "SELECT conversation_id, COUNT(*) FROM events GROUP BY conversation_id"
                    ).fetchall()
                )
            finally:
                connection.close()
            # The busy session may be pruned down to `retention`, but the idle
            # session's events must never be evicted by another session.
            self.assertEqual(counts.get("idle"), 5)
            self.assertLess(counts.get("busy", 0), 150)
            store.close()

    def test_is_active_uses_120s_window(self) -> None:
        now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(StateStore.is_active("2026-08-03T11:59:00Z", now))  # 60s ago
        self.assertTrue(StateStore.is_active("2026-08-03T12:00:00Z", now))
        self.assertFalse(StateStore.is_active("2026-08-03T11:57:00Z", now))  # 180s ago
        self.assertFalse(StateStore.is_active("not-a-date", now))

    def test_migration_adds_conversation_name_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            workspace = Path(directory) / "named"
            workspace.mkdir()
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE projects (
                    workspace TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    note TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workspace, conversation_id)
                );
                CREATE TABLE events (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    note TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    files_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO projects (workspace, conversation_id, name, status, phase, focus, note, evidence, event_type, updated_at, event_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(workspace), "t1", "named", "完成", "", "", "", "", "progress", "2026-01-01T00:00:00Z", 1),
            )
            connection.commit()
            connection.close()

            store = StateStore(database)
            try:
                columns = {
                    row[1] for row in store.connection.execute("PRAGMA table_info(projects)").fetchall()
                }
                self.assertIn("conversation_name", columns)
                state = store.state()
                self.assertEqual(state["projects"][0]["conversation_id"], "t1")
                self.assertEqual(state["projects"][0]["conversation_name"], "")
            finally:
                store.close()

    def test_management_info_reports_store_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "info-demo"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            store.ingest({"workspace": str(workspace), "status": "监听中", "focus": "A"})

            info = store.info()
            self.assertEqual(info["total_sessions"], 1)
            self.assertEqual(info["total_events"], 1)
            self.assertEqual(info["retention"], 2000)
            self.assertEqual(Path(info["state_path"]), store.path)
            self.assertGreaterEqual(info["db_bytes"], 0)
            store.close()


class ServerTests(unittest.TestCase):
    def test_http_api_authenticates_and_ingests_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server = CodeCCTVServer(("127.0.0.1", 0), "test-token", store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                request = Request(f"{base_url}/api/state")
                with self.assertRaises(HTTPError) as error:
                    try:
                        urlopen(request, timeout=1)
                    except HTTPError as raised:
                        raised.close()
                        raise
                self.assertEqual(error.exception.code, 401)

                payload = json.dumps({"workspace": directory, "status": "侦察中"}).encode()
                request = Request(
                    f"{base_url}/api/events",
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/json", "X-Code-CCTV-Token": "test-token"},
                )
                with urlopen(request, timeout=1) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["state"]["summary"]["total_projects"], 1)
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=1)

    def test_management_clear_session_and_clear_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace_a = Path(directory) / "a"
            workspace_a.mkdir()
            workspace_b = Path(directory) / "b"
            workspace_b.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            server = CodeCCTVServer(("127.0.0.1", 0), "test-token", store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            headers = {"X-Code-CCTV-Token": "test-token"}
            try:
                def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                    request = Request(
                        base_url + path,
                        data=json.dumps(payload).encode(),
                        method="POST",
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    with urlopen(request, timeout=1) as response:
                        return json.loads(response.read())

                store.ingest({"workspace": str(workspace_a), "status": "监听中"})
                store.ingest({"workspace": str(workspace_b), "status": "监听中"})

                request = Request(f"{base_url}/api/management/info", headers=headers)
                with urlopen(request, timeout=1) as response:
                    info = json.loads(response.read())
                self.assertEqual(info["total_sessions"], 2)
                self.assertEqual(info["total_events"], 2)
                self.assertEqual(info["port"], server.server_address[1])

                body = post(
                    "/api/management/session/clear",
                    {"workspace": str(workspace_a), "conversation_id": "default"},
                )
                self.assertTrue(body["ok"])
                self.assertEqual(body["state"]["summary"]["total_projects"], 1)

                body = post("/api/management/clear-all", {})
                self.assertTrue(body["ok"])
                self.assertEqual(body["state"]["summary"]["total_projects"], 0)
                self.assertEqual(body["state"]["summary"]["event_count"], 0)
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=1)


    def test_api_events_missing_workspace_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server = CodeCCTVServer(("127.0.0.1", 0), "test-token", store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                request = Request(
                    f"{base_url}/api/events",
                    data=json.dumps({"status": "侦察中"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "X-Code-CCTV-Token": "test-token"},
                )
                try:
                    urlopen(request, timeout=1)
                    self.fail("expected HTTPError")
                except HTTPError as error:
                    try:
                        self.assertEqual(error.code, 400)
                        body = json.loads(error.read())
                        self.assertIn("workspace", body["error"])
                    finally:
                        error.close()
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=1)

    def test_stream_endpoint_pushes_initial_state_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "stream-ws"
            workspace.mkdir()
            store = StateStore(Path(directory) / "state.sqlite3")
            server = CodeCCTVServer(("127.0.0.1", 0), "test-token", store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                store.ingest({"workspace": str(workspace), "status": "监听中", "focus": "初始"})
                connected = threading.Event()
                received: list[str] = []
                stream_thread = threading.Thread(
                    target=self._read_stream, args=(base_url, connected, received), daemon=True
                )
                stream_thread.start()
                self.assertTrue(connected.wait(timeout=3), "SSE initial state frame never arrived")
                request = Request(
                    f"{base_url}/api/events",
                    data=json.dumps({"workspace": str(workspace), "status": "监听中", "focus": "更新"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "X-Code-CCTV-Token": "test-token"},
                )
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                stream_thread.join(timeout=3)
                self.assertFalse(stream_thread.is_alive(), "stream thread should finish after two frames")
                joined = "\n".join(received)
                self.assertIn("初始", joined)
                self.assertIn("更新", joined)
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=1)

    @staticmethod
    def _read_stream(base_url: str, connected: threading.Event, received: list[str]) -> None:
        request = Request(f"{base_url}/api/stream", headers={"X-Code-CCTV-Token": "test-token"})
        with urlopen(request, timeout=5) as response:
            frames = 0
            while frames < 2:
                line = response.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    received.append(line.decode("utf-8"))
                    frames += 1
                    connected.set()


if __name__ == "__main__":
    unittest.main()
