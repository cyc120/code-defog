"""Regression tests for the DevLoop entry and human-approval boundaries."""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from daemon.server import CodeCCTVServer
from daemon.store import StateStore


def _post(url: str, token: str, payload: dict, *, approval_key: str = "",
          token_type: str = "") -> tuple[int, dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Code-CCTV-Token": token,
    }
    if approval_key:
        headers["X-Code-CCTV-Approval-Key"] = approval_key
    if token_type:
        headers["X-Code-CCTV-Token-Type"] = token_type
    request = Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def on_source_received(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"case_id": "case-through-orchestrator", "status": "TRIAGED"}


class _CompletingTeams:
    """Minimal execution adapter for verifying the intake response snapshot."""

    def dispatch_task(self, case_id: str, state: str, context: dict) -> dict:
        return {"status": "completed", "action": state.lower()}


class CoreBoundaryTests(unittest.TestCase):
    def _start_server(self, store: StateStore, *, orchestrator: object | None = None,
                      approval_key: str = "human-test-key") -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        server = CodeCCTVServer(
            ("127.0.0.1", 0), token, store, orchestrator,
            approval_secret=approval_key,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    def test_case_intake_delegates_to_orchestrator_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            orchestrator = _RecordingOrchestrator()
            server, base_url, token = self._start_server(store, orchestrator=orchestrator)
            try:
                status, body = _post(
                    f"{base_url}/api/cases", token,
                    {"source_type": "issue", "source_uri": "audit://intake"},
                )
                self.assertEqual(status, 201)
                self.assertEqual(body["case"]["status"], "TRIAGED")
                self.assertEqual(orchestrator.payloads[0]["source_uri"], "audit://intake")
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_case_intake_returns_the_latest_orchestrated_state(self) -> None:
        from agent_runtime.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            orchestrator = Orchestrator(store, _CompletingTeams())
            server, base_url, token = self._start_server(store, orchestrator=orchestrator)
            try:
                status, body = _post(
                    f"{base_url}/api/cases", token,
                    {
                        "source_type": "issue",
                        "source_uri": "audit://fresh-state",
                        "client_nonce": "fresh-state",
                        "raw_content": "KeyError: missing projects",
                        "repository_ref": "/tmp/demo-target",
                        "extracted_signals": {
                            "exception_type": "KeyError",
                            "message_pattern": "missing projects",
                            "key_frames": ["cli.py:25"],
                        },
                    },
                )
                self.assertEqual(status, 201)
                self.assertEqual(body["case"]["status"], "PLAN_APPROVAL")
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_service_token_cannot_issue_its_own_approval_grant(self) -> None:
        approval_key = "human-test-key"
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = self._start_server(store, approval_key=approval_key)
            try:
                case = store.create_or_find_case({
                    "source_type": "issue",
                    "source_uri": "audit://approval",
                    "client_nonce": "audit-human-boundary",
                    "raw_content": "KeyError projects",
                    "repository_ref": "/tmp/demo-target",
                    "extracted_signals": {
                        "exception_type": "KeyError",
                        "message_pattern": "projects missing",
                        "key_frames": ["cli.py:25"],
                    },
                })
                case_id = case["case_id"]
                store.transition_case(case_id, "TRIAGED")
                store.transition_case(case_id, "DIAGNOSED")
                store.connection.execute(
                    "UPDATE cases SET base_commit = ? WHERE case_id = ?", ("audit-base", case_id)
                )
                store.connection.commit()
                store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")

                body = {"action": "approve_plan", "target_ref": "audit-base", "approver": "reviewer"}
                status, rejected = _post(
                    f"{base_url}/api/cases/{case_id}/approval-grant", token, body
                )
                self.assertEqual(status, 403)
                self.assertIn("human approval key", rejected["error"])

                status, granted = _post(
                    f"{base_url}/api/cases/{case_id}/approval-grant", token, body,
                    approval_key=approval_key,
                )
                self.assertEqual(status, 200)
                approval_token = granted["grant"]["approval_token"]
                status, consumed = _post(
                    f"{base_url}/api/cases/{case_id}/actions", approval_token,
                    {
                        "action": "approve_plan",
                        "approval_token": approval_token,
                        "target_ref": "audit-base",
                    },
                    token_type="approval",
                )
                self.assertEqual(status, 200)
                self.assertEqual(consumed["case"]["status"], "REPAIRING")
            finally:
                server.shutdown()
                server.server_close()
                store.close()


if __name__ == "__main__":
    unittest.main()
