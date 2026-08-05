"""Tests for the Windows status_client Case API methods and SSE parsing.

Uses a real daemon (via _start_server) with a temporary service.json so the
client under test hits genuine endpoints.  No Qt dependency — status_client
is pure Python.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from urllib.error import HTTPError
from urllib.request import Request, urlopen

# The windows client is a standalone package that imports app_paths (not daemon).
_WINDOWS_DIR = Path(__file__).resolve().parents[1] / "windows"
if str(_WINDOWS_DIR) not in sys.path:
    sys.path.insert(0, str(_WINDOWS_DIR))

from daemon.server import CodeCCTVServer
from daemon.store import StateStore

from status_client import StatusClient, load_config, parse_sse_envelope, parse_sse_line


def _start_server(store: StateStore) -> tuple[CodeCCTVServer, str, dict]:
    """Start a daemon and write a service.json the client can load."""
    token = secrets.token_hex(16)
    server = CodeCCTVServer(("127.0.0.1", 0), token, store)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{actual_port}", {
        "host": "127.0.0.1", "port": actual_port, "token": token,
    }


class ParseSseEnvelopeTests(unittest.TestCase):
    def test_state_envelope(self) -> None:
        env = parse_sse_envelope(b'data: {"type": "state", "state": {"generated_at": "x"}}\n')
        self.assertEqual(env["type"], "state")
        self.assertIn("state", env)

    def test_case_created_envelope(self) -> None:
        env = parse_sse_envelope(b'data: {"type": "case_created", "case": {"case_id": "c1"}}\n')
        self.assertEqual(env["type"], "case_created")
        self.assertEqual(env["case"]["case_id"], "c1")

    def test_heartbeat_returns_none(self) -> None:
        self.assertIsNone(parse_sse_envelope(b": heartbeat\n"))

    def test_bad_json_returns_none(self) -> None:
        self.assertIsNone(parse_sse_envelope(b"data: not-json\n"))

    def test_parse_sse_line_still_state_only(self) -> None:
        """parse_sse_line remains the state-only fallback (back-compat)."""
        parsed = parse_sse_line(b'data: {"type": "state", "state": {"x": 1}}\n')
        self.assertEqual(parsed[0], "state")
        # Case events yield None (not state)
        self.assertIsNone(parse_sse_line(b'data: {"type": "case_created", "case": {}}\n'))


class StatusClientCaseTests(unittest.TestCase):
    def _client_with_server(self):
        """Context manager: yields (store, server, client) with a temp dir.

        Usage: ``with self._client_with_server() as (store, server, client):``
        """
        @contextlib.contextmanager
        def _manager():
            with tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / "state.sqlite3")
                server, _base_url, config = _start_server(store)
                cfg_path = Path(directory) / "service.json"
                cfg_path.write_text(json.dumps(config), encoding="utf-8")
                client = StatusClient(
                    config_provider=lambda: load_config(str(cfg_path)), enable_stream=False,
                )
                try:
                    yield store, server, client
                finally:
                    server.shutdown()
                    server.server_close()
                    store.close()
        return _manager()

    def _make_plan_case(self, store: StateStore, nonce: str) -> str:
        result = store.create_or_find_case({
            "source_type": "issue", "source_uri": f"sc-{nonce}",
            "client_nonce": f"sc-nonce-{nonce}",
            "raw_content": "KeyError", "repository_ref": "/test",
            "extracted_signals": {
                "exception_type": "KeyError", "message_pattern": "x",
                "key_frames": ["cli.py:1"], "keywords": ["k"], "repository_ref": "/test",
            },
        })
        case_id = result["case_id"]
        store.transition_case(case_id, "TRIAGED")
        store.transition_case(case_id, "DIAGNOSED")
        store.connection.execute(
            "UPDATE cases SET base_commit = 'base-01' WHERE case_id = ?", (case_id,))
        store.connection.commit()
        store.transition_case(case_id, "PLAN_APPROVAL", "approve_plan")
        return case_id

    def test_list_cases(self) -> None:
        with self._client_with_server() as (store, server, client):
            case_id = self._make_plan_case(store, "list")
            cases = client.list_cases()
            self.assertIsNotNone(cases)
            ids = {c["case_id"] for c in cases}
            self.assertIn(case_id, ids)


    def test_list_cases_status_filter(self) -> None:
        with self._client_with_server() as (store, server, client):
            self._make_plan_case(store, "filter")
            cases = client.list_cases(status="PLAN_APPROVAL")
            self.assertTrue(all(c["status"] == "PLAN_APPROVAL" for c in cases))


    def test_get_case(self) -> None:
        with self._client_with_server() as (store, server, client):
            case_id = self._make_plan_case(store, "get")
            case = client.get_case(case_id)
            self.assertEqual(case["status"], "PLAN_APPROVAL")
            self.assertEqual(case["source_count"], 1)


    def test_get_case_evidence(self) -> None:
        with self._client_with_server() as (store, server, client):
            case_id = self._make_plan_case(store, "evidence")
            evidence = client.get_case_evidence(case_id)
            self.assertIn("case", evidence)
            self.assertIn("sources", evidence)
            self.assertIn("agent_runs", evidence)


    def test_approval_grant_and_consume_loop(self) -> None:
        """Issue a grant via the client, consume it, verify the state moves."""
        with self._client_with_server() as (store, server, client):
            case_id = self._make_plan_case(store, "approve")
            grant = client.request_approval_grant(
                case_id, "approve_plan", "base-01", "win-reviewer")
            self.assertIsNotNone(grant)
            self.assertTrue(grant["approval_token"].startswith("at-"))
            case = client.post_case_action(
                case_id, "approve_plan", grant["approval_token"], "base-01", reason="ok")
            self.assertEqual(case["status"], "REPAIRING")


    def test_consume_rejected_without_approval_type(self) -> None:
        """A plain service-token call to /actions grant path is rejected (403)."""
        with self._client_with_server() as (store, server, client):
            case_id = self._make_plan_case(store, "rejectpath")
            grant = client.request_approval_grant(case_id, "approve_plan", "base-01", "r")
            # Simulate a service-token-only call to the grant-consumption path.
            body = json.dumps({
                "action": "approve_plan", "approval_token": grant["approval_token"],
                "target_ref": "base-01",
            }).encode()
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/cases/{case_id}/actions",
                data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "X-Code-CCTV-Token": secrets.token_hex(16)},  # service token, no type
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 403)



if __name__ == "__main__":
    unittest.main()
