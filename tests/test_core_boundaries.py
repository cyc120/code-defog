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
from _helpers import seed_case, start_server
from agent_runtime.harness import DevLoopHarness
from agent_runtime.orchestrator import Orchestrator


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

    def test_case_intake_delegates_to_orchestrator_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            orchestrator = _RecordingOrchestrator()
            server, base_url, token = start_server(store, orchestrator=orchestrator, approval_secret="human-test-key")
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
            server, base_url, token = start_server(store, orchestrator=orchestrator, approval_secret="human-test-key")
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
            server, base_url, token = start_server(store, approval_secret=approval_key)
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


class DeterministicReleaseGateTests(unittest.TestCase):
    """RELEASE_APPROVAL must never be driven by a model's self-assessment.

    A gate 'passed' verdict is trusted only when the case carries a
    Store-persisted sandbox_ref; an LLM-claimed patch_ref must never
    anchor a release grant."""

    def _store_with_case(self, case_id: str = "case-gate-1"):
        directory = tempfile.TemporaryDirectory()
        store = StateStore(Path(directory.name) / "state.sqlite3")
        now = "2026-08-01T00:00:00Z"
        store.connection.execute(
            "INSERT INTO cases (case_id, status, created_at, updated_at) VALUES (?, 'REPAIRING', ?, ?)",
            (case_id, now, now),
        )
        store.connection.commit()
        return directory, store

    class _GateExecutor:
        """Executor that answers VERIFYING with an LLM-claimed pass."""

        mode = "mock"

        def __init__(self, verdict: dict) -> None:
            self.verdict = verdict

        def dispatch_task(self, case_id: str, state: str, context: dict) -> dict:
            if state == "VERIFYING":
                return dict(self.verdict)
            return {"status": "completed", "action": state.lower()}

        def dispatch_review_task(self, *args: object, **kwargs: object) -> dict:
            return {"status": "completed"}

    def test_gate_pass_without_sandbox_ref_never_reaches_release_approval(self) -> None:
        directory, store = self._store_with_case()
        try:
            executor = self._GateExecutor({
                "status": "completed",
                "quality_gate_passed": True,
                "patch_ref": "model-fabricated-patch",
            })
            orchestrator = Orchestrator(store, DevLoopHarness(executor))
            result = orchestrator.advance("case-gate-1", "VERIFYING")
            self.assertEqual(result["status"], "VERIFYING",
                             "LLM-asserted pass without sandbox must not release")
            self.assertIsNone(store.get_case("case-gate-1")["patch_ref"],
                              "LLM-claimed patch_ref must not be persisted")
        finally:
            store.close(); directory.cleanup()

    def test_close_case_and_retry_repair_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            now = "2026-08-01T00:00:00Z"
            for case_id, status in (("case-close-1", "PATCH_REJECTED"),
                                    ("case-retry-1", "PATCH_REJECTED"),
                                    ("case-retry-2", "ESCALATED")):
                store.connection.execute(
                    "INSERT INTO cases (case_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (case_id, status, now, now),
                )
            store.connection.commit()
            token = secrets.token_hex(16)
            server = CodeCCTVServer((
                "127.0.0.1", 0,), token, store, approval_secret="human-test-key")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                # close_case: PATCH_REJECTED -> CLOSED
                status, body = _post(
                    f"{base_url}/api/cases/case-close-1/actions", token,
                    {"action": "close_case", "reason": "won't fix", "approver": "reviewer"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["case"]["status"], "CLOSED")
                # Closed cases cannot be closed twice.
                status, body = _post(
                    f"{base_url}/api/cases/case-close-1/actions", token,
                    {"action": "close_case"},
                )
                self.assertEqual(status, 409)
                # retry_repair: PATCH_REJECTED -> REPAIRING
                status, body = _post(
                    f"{base_url}/api/cases/case-retry-1/actions", token,
                    {"action": "retry_repair", "approver": "reviewer"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["case"]["status"], "REPAIRING")
                # retry_repair from ESCALATED is also valid.
                status, body = _post(
                    f"{base_url}/api/cases/case-retry-2/actions", token,
                    {"action": "retry_repair"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["case"]["status"], "REPAIRING")
                # Unknown actions still rejected.
                status, _ = _post(
                    f"{base_url}/api/cases/case-retry-1/actions", token,
                    {"action": "bogus"},
                )
                self.assertEqual(status, 400)
            finally:
                server.shutdown(); server.server_close(); store.close()
                thread.join(timeout=1)

    def test_recover_interrupted_escalates_active_cases(self) -> None:
        directory = tempfile.TemporaryDirectory()
        store = StateStore(Path(directory.name) / "state.sqlite3")
        now = "2026-08-01T00:00:00Z"
        for case_id, status in (("case-act-1", "REPAIRING"), ("case-act-2", "TRIAGED"),
                                ("case-wait-1", "PLAN_APPROVAL"), ("case-done-1", "CLOSED")):
            store.connection.execute(
                "INSERT INTO cases (case_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (case_id, status, now, now),
            )
        store.connection.commit()
        store.begin_agent_run("case-act-1", "repair", "trace-1")
        try:
            executor = _CompletingTeams()
            orchestrator = Orchestrator(store, DevLoopHarness(executor))
            recovered = orchestrator.recover_interrupted()
            self.assertEqual(sorted(recovered), ["case-act-1", "case-act-2"])
            self.assertEqual(store.get_case("case-act-1")["status"], "ESCALATED")
            self.assertEqual(store.get_case("case-act-2")["status"], "ESCALATED")
            # Approval-waiting and terminal cases are untouched.
            self.assertEqual(store.get_case("case-wait-1")["status"], "PLAN_APPROVAL")
            self.assertEqual(store.get_case("case-done-1")["status"], "CLOSED")
            # The interrupted agent run is marked failed.
            evidence = store.get_case_evidence("case-act-1")
            self.assertEqual(evidence["agent_runs"][0]["status"], "failed")
        finally:
            store.close(); directory.cleanup()

    def test_repair_without_trusted_mode_cannot_persist_sandbox(self) -> None:
        directory, store = self._store_with_case()
        try:
            executor = self._GateExecutor({
                "status": "completed",
                "patch_ref": "llm-patch",
                "sandbox_repository_ref": "/tmp/attacker-sandbox",
            })
            orchestrator = Orchestrator(store, DevLoopHarness(executor))
            result = orchestrator.advance("case-gate-1", "REPAIRING")
            case = store.get_case("case-gate-1")
            self.assertEqual(result["status"], "ESCALATED")
            self.assertIsNone(case["patch_ref"], "untrusted patch_ref must not persist")
            self.assertIsNone(case["sandbox_ref"], "untrusted sandbox_ref must not persist")
        finally:
            store.close(); directory.cleanup()


if __name__ == "__main__":
    unittest.main()
