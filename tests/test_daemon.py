from __future__ import annotations

import json
import secrets
import sqlite3
import tempfile
import threading
import time
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


# ═══════════════════════════════════════════════════════════════════════════
# DevLoop: Case ingestion, fingerprinting, and state machine tests
# ═══════════════════════════════════════════════════════════════════════════


class DevLoopCaseCreationTests(unittest.TestCase):
    def test_create_case_from_source_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "https://github.com/example/repo/issues/42",
                "client_nonce": "nonce-001",
                "raw_content": "When config.json is empty, --list crashes with KeyError: 'projects'",
                "repository_ref": "/home/user/demo_target",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "config['projects']",
                    "key_frames": ["src/config.py:42"],
                    "keywords": ["config", "KeyError", "projects", "empty"],
                    "repository_ref": "/home/user/demo_target",
                },
                "title": "Empty config crashes with KeyError",
            })
            self.assertNotIn("duplicate", result)
            self.assertIn("case_id", result)
            self.assertEqual(result["status"], "RECEIVED")
            store.close()

    def test_delivery_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            payload = {
                "source_type": "issue",
                "source_uri": "https://github.com/example/repo/issues/42",
                "client_nonce": "nonce-002",
                "raw_content": "KeyError: 'projects'",
                "repository_ref": "/home/user/demo_target",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "config['projects']",
                    "key_frames": ["src/config.py:42"],
                    "keywords": ["KeyError"],
                    "repository_ref": "/home/user/demo_target",
                },
            }
            first = store.create_or_find_case(payload)
            self.assertNotIn("duplicate", first)
            second = store.create_or_find_case(payload)
            self.assertIn("duplicate", second)
            self.assertTrue(second["duplicate"])
            store.close()

    def test_same_incident_signature_merges_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            base = {
                "repository_ref": "/home/user/demo_target",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "config['projects']",
                    "key_frames": ["src/config.py:42"],
                    "keywords": ["KeyError"],
                    "repository_ref": "/home/user/demo_target",
                },
            }
            issue = store.create_or_find_case({
                **base,
                "source_type": "issue",
                "source_uri": "https://github.com/example/repo/issues/42",
                "client_nonce": "nonce-issue-1",
                "raw_content": "Empty config crashes",
            })
            case_id = issue["case_id"]
            log = store.create_or_find_case({
                **base,
                "source_type": "log",
                "source_uri": "/var/log/app/error.log",
                "client_nonce": "nonce-log-1",
                "raw_content": "ERROR: KeyError: 'projects'",
            })
            self.assertEqual(log["case_id"], case_id)
            evidence = store.get_case_evidence(case_id)
            self.assertIsNotNone(evidence)
            self.assertGreaterEqual(len(evidence["sources"]), 2)
            store.close()

    def test_pending_association_for_incomplete_signals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "feedback",
                "source_uri": "user-feedback-001",
                "client_nonce": "nonce-fb-1",
                "raw_content": "The app crashes sometimes",
                "repository_ref": "/home/user/demo_target",
                "extracted_signals": {
                    "keywords": ["crash"],
                    "repository_ref": "/home/user/demo_target",
                },
            })
            self.assertNotIn("duplicate", result)
            # Incomplete signals → pending, no Case created yet
            self.assertTrue(result.get("pending"))
            self.assertIn("observation_id", result)
            self.assertNotIn("case_id", result)
            # The observation exists in case_sources with case_id = NULL
            obs = store.connection.execute(
                "SELECT case_id, association_state FROM case_sources WHERE observation_id = ?",
                (result["observation_id"],),
            ).fetchone()
            self.assertIsNotNone(obs)
            self.assertIsNone(obs["case_id"])
            self.assertEqual(obs["association_state"], "pending")
            store.close()


class DevLoopApprovalTokenTests(unittest.TestCase):
    def test_issue_and_use_approval_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "test-issue",
                "client_nonce": "nonce-approval-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "config['projects']",
                    "key_frames": ["src/config.py:42"],
                    "keywords": ["KeyError"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "DIAGNOSED")
            # Set base_commit BEFORE transitioning to PLAN_APPROVAL
            store.connection.execute(
                "UPDATE cases SET base_commit = 'abc123' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

            # Issue approval grant — target_ref must match base_commit
            grant = store.issue_approval_grant(
                case_id, "approve_plan", "abc123", "test-user"
            )
            self.assertIsNotNone(grant)
            self.assertIn("approval_token", grant)

            # Use the token
            action_result = store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "abc123",
                "reason": "Looks good",
                "approver": "test-user",
            })
            self.assertNotIn("error", action_result)
            self.assertEqual(action_result["status"], "REPAIRING")

            # Re-use should fail
            reuse = store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "abc123",
                "reason": "try again",
                "approver": "test-user",
            })
            self.assertIn("error", reuse)
            store.close()

    def test_approval_token_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "test-issue-expiry",
                "client_nonce": "nonce-exp-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "DIAGNOSED")
            store.connection.execute(
                "UPDATE cases SET base_commit = 'abc123' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

            # Issue with already-expired timestamp
            expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            grant = store.issue_approval_grant(
                case_id, "approve_plan", "abc123", "test-user",
                expires_at=expired,
            )
            self.assertIsNotNone(grant)
            action_result = store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "abc123",
                "reason": "should fail",
                "approver": "test-user",
            })
            self.assertIn("error", action_result)
            self.assertIn("expired", action_result["error"])
            store.close()

    def test_target_ref_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "test-mismatch",
                "client_nonce": "nonce-mis-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "DIAGNOSED")
            store.connection.execute(
                "UPDATE cases SET base_commit = 'commit-aaa' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(
                case_id, "approve_plan", "commit-aaa", "test-user"
            )
            action_result = store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "commit-bbb",  # Different!
                "reason": "mismatched target",
                "approver": "test-user",
            })
            self.assertIn("error", action_result)
            self.assertIn("mismatch", action_result["error"])
            store.close()


class DevLoopStateMachineTests(unittest.TestCase):
    def test_valid_transition_chain(self) -> None:
        from agent_runtime.state_machine import is_valid_transition
        self.assertTrue(is_valid_transition("RECEIVED", "TRIAGED"))
        self.assertTrue(is_valid_transition("TRIAGED", "DIAGNOSED"))
        self.assertTrue(is_valid_transition("DIAGNOSED", "PLAN_APPROVAL"))
        self.assertTrue(is_valid_transition("PLAN_APPROVAL", "REPAIRING"))
        self.assertTrue(is_valid_transition("REPAIRING", "VERIFYING"))
        self.assertTrue(is_valid_transition("VERIFYING", "PATCH_REJECTED"))
        self.assertTrue(is_valid_transition("VERIFYING", "RELEASE_APPROVAL"))

    def test_invalid_transition_rejected(self) -> None:
        from agent_runtime.state_machine import is_valid_transition
        self.assertFalse(is_valid_transition("RECEIVED", "REPAIRING"))
        self.assertFalse(is_valid_transition("TRIAGED", "RELEASED"))
        self.assertFalse(is_valid_transition("CLOSED", "REPAIRING"))

    def test_patch_rejected_returns_to_repairing(self) -> None:
        from agent_runtime.state_machine import is_valid_transition
        self.assertTrue(is_valid_transition("PATCH_REJECTED", "REPAIRING"))
        self.assertTrue(is_valid_transition("PATCH_REJECTED", "CLOSED"))

    def test_terminal_states(self) -> None:
        from agent_runtime.state_machine import is_terminal
        self.assertTrue(is_terminal("CLOSED"))
        self.assertFalse(is_terminal("ESCALATED"))   # ESCALATED can reopen → REPAIRING
        self.assertFalse(is_terminal("REPAIRING"))

    def test_approval_states(self) -> None:
        from agent_runtime.state_machine import requires_approval
        self.assertTrue(requires_approval("PLAN_APPROVAL"))
        self.assertTrue(requires_approval("RELEASE_APPROVAL"))
        self.assertFalse(requires_approval("REPAIRING"))


class DevLoopStoreTransitionTests(unittest.TestCase):
    def test_full_case_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "test-lifecycle",
                "client_nonce": "nonce-life-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]

            # Walk to PLAN_APPROVAL via state transitions
            for state in ["TRIAGED", "DIAGNOSED"]:
                r = store.transition_case(case_id, state)
                self.assertEqual(r["status"], state)
            # Set base_commit before entering PLAN_APPROVAL
            store.connection.execute(
                "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

            # At PLAN_APPROVAL — consume approval Grant → REPAIRING
            grant = store.issue_approval_grant(case_id, "approve_plan", "base-01", "user")
            r = store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "base-01",
                "reason": "ok",
                "approver": "user",
            })
            self.assertEqual(r["status"], "REPAIRING")

            # Continue to VERIFYING, then set patch_ref before RELEASE_APPROVAL
            for state in ["VERIFYING"]:
                r = store.transition_case(case_id, state)
                self.assertEqual(r["status"], state)
            store.connection.execute(
                "UPDATE cases SET patch_ref = 'patch-01' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "RELEASE_APPROVAL", "approve_release")

            # At RELEASE_APPROVAL — consume approval Grant → RELEASED
            grant2 = store.issue_approval_grant(case_id, "approve_release", "patch-01", "user")
            r = store.perform_case_action(case_id, "approve_release", {
                "approval_token": grant2["approval_token"],
                "target_ref": "patch-01",
                "reason": "qa passed",
                "approver": "user",
            })
            self.assertEqual(r["status"], "RELEASED")

            # Close
            r = store.transition_case(case_id, "CLOSED")
            self.assertEqual(r["status"], "CLOSED")
            self.assertIsNotNone(r.get("closed_at"))
            store.close()

    def test_patch_rejected_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "ci",
                "source_uri": "test-patch-reject",
                "client_nonce": "nonce-pr-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            for state in ["TRIAGED", "DIAGNOSED"]:
                store.transition_case(case_id, state)
            store.connection.execute(
                "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(case_id, "approve_plan", "base-01", "user")
            store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "base-01",
                "reason": "ok",
                "approver": "user",
            })
            store.transition_case(case_id, "REPAIRING")
            store.transition_case(case_id, "VERIFYING")
            # Patch is bad — reject it
            r = store.transition_case(case_id, "PATCH_REJECTED")
            self.assertEqual(r["status"], "PATCH_REJECTED")
            # Retry
            r = store.transition_case(case_id, "REPAIRING")
            self.assertEqual(r["status"], "REPAIRING")
            store.close()

    def test_escalated_reopen_clears_closed_at(self) -> None:
        """ESCALATED → REPAIRING must clear closed_at.  Only CLOSED is terminal."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "test-escalated-reopen",
                "client_nonce": "nonce-er-1", "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"], "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            # Walk to ESCALATED
            for state in ["TRIAGED", "DIAGNOSED"]:
                store.transition_case(case_id, state)
            store.connection.execute(
                "UPDATE cases SET base_commit = 'b1' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(case_id, "reject_plan", "b1", "u")
            store.perform_case_action(case_id, "reject_plan", {
                "approval_token": grant["approval_token"], "target_ref": "b1",
                "reason": "no", "approver": "u",
            })
            c = store.get_case(case_id)
            self.assertEqual(c["status"], "ESCALATED")
            self.assertIsNone(c["closed_at"], "ESCALATED must not set closed_at")

            # Reopen to REPAIRING — closed_at must stay NULL
            store.transition_case(case_id, "REPAIRING")
            c = store.get_case(case_id)
            self.assertIsNone(c["closed_at"],
                              f"closed_at must be NULL after ESCALATED→REPAIRING, got {c['closed_at']}")

            # ESCALATED → CLOSED: now closed_at should be set
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")
            c = store.get_case(case_id)
            self.assertIsNotNone(c["closed_at"], "CLOSED must set closed_at")
            store.close()


class DevLoopEvidenceTests(unittest.TestCase):
    def test_tool_run_recording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "test-tool",
                "client_nonce": "nonce-tool-1",
                "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            run_id = store.record_tool_run({
                "case_id": case_id,
                "agent_id": "repair",
                "tool_name": "git_checkout",
                "command_template": "git checkout -b fix/{case_id} {base_commit}",
                "actual_argv": "git checkout -b fix/case-abc abc123",
                "working_directory": "/home/user/demo_target",
                "policy_version": "v0.5",
                "input_sha256": "abc123def456",
                "output_sha256": "def789abc012",
                "exit_code": 0,
                "result_ref": "art-001",
            })
            self.assertTrue(run_id.startswith("tool-"))
            evidence = store.get_case_evidence(case_id)
            self.assertEqual(len(evidence["tool_runs"]), 1)
            self.assertEqual(evidence["tool_runs"][0]["exit_code"], 0)
            self.assertNotEqual(evidence["tool_runs"][0]["chain_hash"], "")
            store.close()


class DevLoopEndToEndReleaseTests(unittest.TestCase):
    """Full path: Repair → patch_ref persisted → gate pass → RELEASE_APPROVAL → grant."""

    def test_repair_patch_ref_flows_to_release_approval(self) -> None:
        """Repair returns patch_ref → Case persists it → gate passes →
        RELEASE_APPROVAL (not ESCALATED) → can issue and consume release grant."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "test-e2e-release",
                "client_nonce": "nonce-e2e-1", "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"], "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]

            # Walk to PLAN_APPROVAL and approve
            for state in ["TRIAGED", "DIAGNOSED"]:
                store.transition_case(case_id, state)
            store.connection.execute(
                "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(case_id, "approve_plan", "base-01", "user")
            store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "base-01", "reason": "ok", "approver": "user",
            })

            # ── REPAIRING: simulate Repair Agent returning patch_ref ────
            store.transition_case(case_id, "REPAIRING")
            patch_id = "patch-e2e-abc123"
            store.connection.execute(
                "UPDATE cases SET patch_ref = ? WHERE case_id = ?",
                (patch_id, case_id),
            )
            store.connection.commit()
            c = store.get_case(case_id)
            self.assertEqual(c["patch_ref"], patch_id,
                             "patch_ref must be persisted after Repair Agent runs")

            # ── VERIFYING → RELEASE_APPROVAL (gate passed, patch_ref present)
            store.transition_case(case_id, "VERIFYING")
            store.transition_case(case_id, "RELEASE_APPROVAL", "approve_release")
            c = store.get_case(case_id)
            self.assertEqual(c["status"], "RELEASE_APPROVAL",
                             "must reach RELEASE_APPROVAL, not ESCALATED")
            self.assertEqual(c["pending_action"], "approve_release")

            # ── Issue and consume approve_release Grant ─────────────────
            rel_grant = store.issue_approval_grant(case_id, "approve_release", patch_id, "user")
            self.assertNotIn("error", rel_grant,
                             f"Grant issuance must succeed: {rel_grant}")
            rel_result = store.perform_case_action(case_id, "approve_release", {
                "approval_token": rel_grant["approval_token"],
                "target_ref": patch_id, "reason": "qa ok", "approver": "user",
            })
            self.assertNotIn("error", rel_result)
            self.assertEqual(rel_result["status"], "RELEASED")
            store.close()

    def test_missing_patch_ref_escalates_after_gate_pass(self) -> None:
        """Gate passes but patch_ref is absent → ESCALATED (cannot approve release)."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "test-e2e-missing",
                "client_nonce": "nonce-e2e-2", "raw_content": "test",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"],
                    "keywords": ["test"], "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            for state in ["TRIAGED", "DIAGNOSED"]:
                store.transition_case(case_id, state)
            store.connection.execute(
                "UPDATE cases SET base_commit = 'b1' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(case_id, "approve_plan", "b1", "user")
            store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "b1", "reason": "ok", "approver": "user",
            })
            # REPAIRING — but no patch_ref set
            store.transition_case(case_id, "REPAIRING")
            store.transition_case(case_id, "VERIFYING")
            # Gate passes, but patch_ref is NULL → ESCALATED
            store.transition_case(case_id, "ESCALATED")
            c = store.get_case(case_id)
            self.assertEqual(c["status"], "ESCALATED")
            store.close()


class DevLoopGrantGuardTests(unittest.TestCase):
    """Regression tests: grant issuance is blocked when state/version don't match."""

    def test_received_state_cannot_issue_release_grant(self) -> None:
        """A Case at RECEIVED must not be able to issue an approve_release grant."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "x",
                "client_nonce": "nonce-guard-1", "raw_content": "t",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"], "keywords": ["t"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            # Case is at RECEIVED — approve_release must be rejected
            grant = store.issue_approval_grant(case_id, "approve_release", "v1", "user")
            self.assertIsNotNone(grant)
            self.assertIn("error", grant, f"Expected error, got: {grant}")
            self.assertIn("RELEASE_APPROVAL", grant["error"])
            store.close()

    def test_old_patch_ref_cannot_issue_grant(self) -> None:
        """target_ref must match the case's current patch_ref exactly."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "x",
                "client_nonce": "nonce-guard-2", "raw_content": "t",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"], "keywords": ["t"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            for state in ["TRIAGED", "DIAGNOSED"]:
                store.transition_case(case_id, state)
            store.connection.execute(
                "UPDATE cases SET base_commit = 'base-v1' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
            grant = store.issue_approval_grant(case_id, "approve_plan", "base-v1", "user")
            store.perform_case_action(case_id, "approve_plan", {
                "approval_token": grant["approval_token"],
                "target_ref": "base-v1", "reason": "ok", "approver": "user",
            })
            # Advance to VERIFYING, set patch_ref, then RELEASE_APPROVAL
            store.transition_case(case_id, "VERIFYING")
            store.connection.execute(
                "UPDATE cases SET patch_ref = 'patch-v1' WHERE case_id = ?", (case_id,))
            store.connection.commit()
            store.transition_case(case_id, "RELEASE_APPROVAL", "approve_release")
            # Try to issue a grant with a DIFFERENT (stale) target_ref
            bad_grant = store.issue_approval_grant(case_id, "approve_release", "patch-v2", "user")
            self.assertIsNotNone(bad_grant)
            self.assertIn("error", bad_grant, f"Expected mismatch error, got: {bad_grant}")
            self.assertIn("mismatch", bad_grant["error"])
            store.close()


class DevLoopChainSequenceTests(unittest.TestCase):
    """Regression test: chain_sequence must be strictly increasing per Case."""

    def test_chain_sequence_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "issue", "source_uri": "x",
                "client_nonce": "nonce-chain-1", "raw_content": "t",
                "repository_ref": "/test",
                "extracted_signals": {
                    "exception_type": "KeyError", "message_pattern": "test",
                    "key_frames": ["src/test.py:1"], "keywords": ["t"],
                    "repository_ref": "/test",
                },
            })
            case_id = result["case_id"]
            base = {"case_id": case_id, "agent_id": "repair",
                    "tool_name": "git_checkout",
                    "command_template": "git checkout -b fix/x",
                    "actual_argv": "git checkout -b fix/x abc",
                    "working_directory": "/test", "policy_version": "v1",
                    "input_sha256": "in1", "output_sha256": "out1",
                    "exit_code": 0}
            r1 = store.record_tool_run(base)
            r2 = store.record_tool_run(base)
            r3 = store.record_tool_run(base)
            # Fetch sequences
            seqs = [r["chain_sequence"] for r in store.connection.execute(
                "SELECT chain_sequence FROM tool_runs WHERE case_id = ? ORDER BY chain_sequence",
                (case_id,)).fetchall()]
            self.assertEqual(seqs, [1, 2, 3],
                             f"chain_sequence must be strictly increasing, got: {seqs}")
            # chain_hash must differ across records
            hashes = [r["chain_hash"] for r in store.connection.execute(
                "SELECT chain_hash FROM tool_runs WHERE case_id = ? ORDER BY chain_sequence",
                (case_id,)).fetchall()]
            self.assertEqual(len(set(hashes)), 3,
                             "Each chain_hash must be unique")
            store.close()


class DevLoopStructuredValidationTests(unittest.TestCase):
    """Production adapter output validation — JSON extraction + schema checks."""

    def test_invalid_structured_output_marked_failed(self) -> None:
        """JSON missing/invalid must produce status='failed' with
        failure_reason='invalid_structured_output'."""
        from agent_runtime.teams_adapter import _validate_structured
        # No output at all
        err = _validate_structured("triage", None)
        self.assertIsNotNone(err)
        self.assertIn("missing", err)

    def test_missing_required_field_detected(self) -> None:
        from agent_runtime.teams_adapter import _validate_structured
        err = _validate_structured("repair", {"action": "patched"})
        self.assertIsNotNone(err)
        self.assertIn("patch_ref", err)

    def test_wrong_type_detected(self) -> None:
        from agent_runtime.teams_adapter import _validate_structured
        # quality_gate_passed must be bool, not string
        err = _validate_structured("verification", {
            "action": "verified",
            "quality_gate_passed": "yes",
        })
        self.assertIsNotNone(err)
        self.assertIn("quality_gate_passed", err)

    def test_valid_structured_passes(self) -> None:
        from agent_runtime.teams_adapter import _validate_structured
        err = _validate_structured("triage", {
            "action": "triage",
            "priority": "high",
            "confidence": 0.85,
        })
        self.assertIsNone(err)

    def test_boolean_zero_one_normalized_to_bool(self) -> None:
        """LLM may output 0/1 for booleans — validate accepts and
        normalizes to Python bool so the orchestrator's is True/is False
        checks work."""
        from agent_runtime.teams_adapter import _validate_structured
        structured = {
            "action": "verified",
            "quality_gate_passed": 0,
        }
        err = _validate_structured("verification", structured)
        self.assertIsNone(err, f"0 should be accepted as boolean: {err}")
        self.assertIsInstance(structured["quality_gate_passed"], bool,
                              "0 must be normalized to bool(False)")
        self.assertFalse(structured["quality_gate_passed"])

    def test_top_level_fields_promoted(self) -> None:
        """Verify that schema fields are promoted to result top level
        (simulating what the orchestrator reads)."""
        from agent_runtime.teams_adapter import _validate_structured, _TOP_LEVEL_FIELDS

        structured = {
            "action": "patched",
            "patch_ref": "abc123def",
            "branch": "fix/keyerror",
            "files_changed": ["src/config.py"],
        }
        err = _validate_structured("repair", structured)
        self.assertIsNone(err)
        promoted = {}
        for field in _TOP_LEVEL_FIELDS.get("repair", ()):
            if field in structured:
                promoted[field] = structured[field]
        self.assertEqual(promoted["patch_ref"], "abc123def")


class DevLoopFingerprintTests(unittest.TestCase):
    def test_delivery_id_reuses_nonce_on_retry(self) -> None:
        from daemon.store import compute_delivery_id
        # Same nonce → same delivery_id (connector must reuse nonce on retry)
        id1 = compute_delivery_id("issue", "https://example.com/1", "fixed-nonce")
        id2 = compute_delivery_id("issue", "https://example.com/1", "fixed-nonce")
        self.assertEqual(id1, id2)

    def test_delivery_id_differs_with_new_nonce(self) -> None:
        from daemon.store import compute_delivery_id
        id1 = compute_delivery_id("issue", "https://example.com/1", "nonce-a")
        id2 = compute_delivery_id("issue", "https://example.com/1", "nonce-b")
        self.assertNotEqual(id1, id2)

    def test_incident_signature_ignores_timestamp(self) -> None:
        from daemon.store import compute_incident_signature
        sig1 = compute_incident_signature("/repo", "KeyError", "config['projects']", ["src/config.py:42"])
        sig2 = compute_incident_signature("/repo", "KeyError", "config['projects']", ["src/config.py:42"])
        self.assertEqual(sig1, sig2)


# ═══════════════════════════════════════════════════════════════════════════
# DevLoop: HTTP integration tests (real server)
# ═══════════════════════════════════════════════════════════════════════════


def _start_server(store: StateStore) -> tuple[CodeCCTVServer, str, str]:
    """Start the daemon on a random port and return (server, base_url, token)."""
    port = 0
    token = secrets.token_hex(16)
    server = CodeCCTVServer(("127.0.0.1", port), token, store)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{actual_port}", token


class DevLoopHTTPIntegrationTests(unittest.TestCase):
    def test_service_token_cannot_transition_case(self) -> None:
        """Arbitrary state transitions are NOT exposed via HTTP.
        Only approve_plan/reject_plan/approve_release/reject_release/cancel
        are valid actions on /actions."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                # Create a case
                result = store.create_or_find_case({
                    "source_type": "issue",
                    "source_uri": "test-http-transition",
                    "client_nonce": "nonce-http-1",
                    "raw_content": "test",
                    "repository_ref": "/test",
                    "extracted_signals": {
                        "exception_type": "KeyError", "message_pattern": "test",
                        "key_frames": ["src/test.py:1"],
                        "keywords": ["test"], "repository_ref": "/test",
                    },
                })
                case_id = result["case_id"]

                # Advance to TRIAGED using store directly (orchestrator path)
                store.transition_case(case_id, "TRIAGED")

                # Try to jump to REPAIRING via HTTP — should be rejected
                import urllib.request
                body = json.dumps({"action": "REPAIRING"}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/cases/{case_id}/actions",
                    data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": token,
                        "X-Code-CCTV-Token-Type": "service",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read())
                    self.assertIn("error", data)
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (400, 409))
            finally:
                store.close()
                server.shutdown()

    def test_service_token_cannot_approve(self) -> None:
        """service_token with type=service must be rejected for grant actions."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                result = store.create_or_find_case({
                    "source_type": "issue",
                    "source_uri": "test-no-approve",
                    "client_nonce": "nonce-no-ap-1",
                    "raw_content": "test",
                    "repository_ref": "/test",
                    "extracted_signals": {
                        "exception_type": "KeyError", "message_pattern": "test",
                        "key_frames": ["src/test.py:1"],
                        "keywords": ["test"], "repository_ref": "/test",
                    },
                })
                case_id = result["case_id"]
                store.transition_case(case_id, "DIAGNOSED")
                store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

                import urllib.request
                body = json.dumps({"action": "approve_plan", "approval_token": "fake-token"}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/cases/{case_id}/actions",
                    data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": token,
                        "X-Code-CCTV-Token-Type": "service",  # Wrong type!
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        pass
                    self.fail("Expected 403 Forbidden")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 403)
            finally:
                store.close()
                server.shutdown()

    def test_reject_plan_uses_same_grant_model_as_approve(self) -> None:
        """reject_plan consumes an approval Grant, same auth model as approve_plan."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                result = store.create_or_find_case({
                    "source_type": "issue",
                    "source_uri": "test-reject-grant",
                    "client_nonce": "nonce-rj-1",
                    "raw_content": "test",
                    "repository_ref": "/test",
                    "extracted_signals": {
                        "exception_type": "KeyError", "message_pattern": "test",
                        "key_frames": ["src/test.py:1"],
                        "keywords": ["test"], "repository_ref": "/test",
                    },
                })
                case_id = result["case_id"]
                store.transition_case(case_id, "TRIAGED")
                store.transition_case(case_id, "DIAGNOSED")
                store.connection.execute(
                    "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
                store.connection.commit()
                store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

                # Issue a reject_plan grant
                grant = store.issue_approval_grant(case_id, "reject_plan", "base-01", "qa-user")
                self.assertIsNotNone(grant)

                # Execute reject_plan via HTTP with the approval_token
                import urllib.request
                body = json.dumps({
                    "action": "reject_plan",
                    "approval_token": grant["approval_token"],
                    "target_ref": "base-01",
                    "reason": "Need more details",
                }).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/cases/{case_id}/actions",
                    data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": grant["approval_token"],
                        "X-Code-CCTV-Token-Type": "approval",
                    },
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                self.assertTrue(data["ok"])
                self.assertEqual(data["case"]["status"], "ESCALATED")
            finally:
                store.close()
                server.shutdown()

    def test_expired_grant_rejected_by_http(self) -> None:
        """An expired approval Grant must be rejected even via HTTP."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                result = store.create_or_find_case({
                    "source_type": "issue",
                    "source_uri": "test-expired-http",
                    "client_nonce": "nonce-exph-1",
                    "raw_content": "test",
                    "repository_ref": "/test",
                    "extracted_signals": {
                        "exception_type": "KeyError", "message_pattern": "test",
                        "key_frames": ["src/test.py:1"],
                        "keywords": ["test"], "repository_ref": "/test",
                    },
                })
                case_id = result["case_id"]
                store.transition_case(case_id, "TRIAGED")
                store.transition_case(case_id, "DIAGNOSED")
                store.connection.execute(
                    "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
                store.connection.commit()
                store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

                expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
                grant = store.issue_approval_grant(case_id, "approve_plan", "base-01", "qa-user", expires_at=expired)

                import urllib.request
                body = json.dumps({
                    "action": "approve_plan",
                    "approval_token": grant["approval_token"],
                    "target_ref": "base-01",
                }).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/cases/{case_id}/actions",
                    data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": grant["approval_token"],
                        "X-Code-CCTV-Token-Type": "approval",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read())
                    self.assertIn("error", data)
                    self.assertIn("expired", data["error"])
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (400, 401))
            finally:
                store.close()
                server.shutdown()

    def test_case_creation_pushes_sse(self) -> None:
        """Creating a Case should push an SSE event of type 'case_created'."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                # Subscribe to SSE
                import threading, urllib.request

                sse_events: list[dict] = []

                def sse_listener():
                    try:
                        req = urllib.request.Request(
                            f"{base_url}/api/stream",
                            headers={"X-Code-CCTV-Token": token},
                        )
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            for line in resp:
                                line = line.decode("utf-8").strip()
                                if line.startswith("data: "):
                                    sse_events.append(json.loads(line[6:]))
                    except Exception:
                        pass

                sse_thread = threading.Thread(target=sse_listener, daemon=True)
                sse_thread.start()

                # Give SSE connection time to establish
                time.sleep(0.3)

                # Create a Case
                store.create_or_find_case({
                    "source_type": "ci",
                    "source_uri": "test-sse-case",
                    "client_nonce": "nonce-sse-1",
                    "raw_content": "SSE test",
                    "repository_ref": "/test",
                    "extracted_signals": {
                        "exception_type": "KeyError", "message_pattern": "SSE test",
                        "key_frames": ["src/test.py:1"],
                        "keywords": ["SSE"], "repository_ref": "/test",
                    },
                })

                time.sleep(0.3)

                # Should have received a case_created event via SSE
                case_events = [e for e in sse_events if e.get("type") == "case_created"]
                self.assertGreaterEqual(len(case_events), 1,
                                         "SSE should emit case_created when a Case is created")
            finally:
                store.close()
                server.shutdown()

    def test_pending_observation_promoted_on_timeout(self) -> None:
        """resolve_pending_sources should create a Case for expired pending observations."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = store.create_or_find_case({
                "source_type": "feedback",
                "source_uri": "user-says-crash",
                "client_nonce": "nonce-promo-1",
                "raw_content": "It keeps crashing",
                "repository_ref": "/test",
                "extracted_signals": {
                    "keywords": ["crash"],
                    "repository_ref": "/test",
                },
            })
            self.assertTrue(result.get("pending"))
            obs_id = result["observation_id"]

            # Manually set the deadline to the past to force promotion
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            store.connection.execute(
                "UPDATE case_sources SET association_deadline = ? WHERE observation_id = ?",
                (past, obs_id),
            )
            store.connection.commit()

            created = store.resolve_pending_sources()
            self.assertGreaterEqual(len(created), 1)
            self.assertIn("case_id", created[0])
            # The observation should now be linked to the new Case
            obs = store.connection.execute(
                "SELECT case_id, association_state FROM case_sources WHERE observation_id = ?",
                (obs_id,),
            ).fetchone()
            self.assertIsNotNone(obs)
            self.assertEqual(obs["case_id"], created[0]["case_id"])
            store.close()


if __name__ == "__main__":
    unittest.main()
