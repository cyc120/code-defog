"""Regression coverage for the local DevLoop Harness boundary."""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.harness import AGENT_TASKS, DevLoopHarness
from agent_runtime.orchestrator import Orchestrator
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


class RecordingExecutor:
    mode = "mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def dispatch_task(self, case_id: str, state: str, context: dict) -> dict:
        self.calls.append((case_id, state, context))
        return {
            "status": "completed",
            # An executor cannot forge the Harness dispatch identity.
            "harness_id": "untrusted-executor-value",
        }


class HarnessTests(unittest.TestCase):
    def test_manifest_exposes_the_four_explicit_business_tasks(self) -> None:
        executor = RecordingExecutor()
        harness = DevLoopHarness(executor)

        manifest = harness.describe()

        self.assertEqual(manifest["id"], "devloop-local-harness-v1")
        self.assertEqual(manifest["kind"], "local")
        self.assertEqual(manifest["agent_count"], 4)
        self.assertEqual(
            [(task.state, task.agent_id) for task in AGENT_TASKS],
            [("TRIAGED", "triage"), ("DIAGNOSED", "diagnosis"),
             ("REPAIRING", "repair"), ("VERIFYING", "verification")],
        )
        self.assertIn("不持有", str(manifest["approval_boundary"]))

    def test_dispatch_issues_authoritative_metadata_to_the_executor(self) -> None:
        executor = RecordingExecutor()
        harness = DevLoopHarness(executor)

        result = harness.dispatch("case-a", "TRIAGED", {"case_id": "case-a"})

        self.assertEqual(len(executor.calls), 1)
        _, state, dispatched = executor.calls[0]
        self.assertEqual(state, "TRIAGED")
        self.assertEqual(dispatched["harness_agent_id"], "triage")
        self.assertEqual(dispatched["harness_task_state"], "TRIAGED")
        self.assertTrue(dispatched["harness_task_id"].startswith("htask-"))
        self.assertEqual(result["harness_id"], harness.harness_id)
        self.assertEqual(result["harness_task_id"], dispatched["harness_task_id"])

    def test_approval_or_mismatched_context_never_reaches_an_executor(self) -> None:
        executor = RecordingExecutor()
        harness = DevLoopHarness(executor)

        approval = harness.dispatch("case-a", "PLAN_APPROVAL", {"case_id": "case-a"})
        mismatch = harness.dispatch("case-a", "TRIAGED", {"case_id": "case-b"})

        self.assertEqual(executor.calls, [])
        self.assertEqual(approval["status"], "failed")
        self.assertIn("no business-Agent task", approval["failure_reason"])
        self.assertEqual(mismatch["status"], "failed")
        self.assertIn("does not match", mismatch["failure_reason"])

    def test_orchestrator_persists_harness_metadata_in_mock_agent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            created = store.create_or_find_case({
                "source_type": "issue",
                "source_uri": "harness://audit",
                "client_nonce": "harness-audit",
                "raw_content": "KeyError: projects",
                "repository_ref": "/tmp/harness-repo",
                "extracted_signals": {
                    "exception_type": "KeyError",
                    "message_pattern": "projects",
                    "key_frames": ["cli.py:1"],
                    "keywords": ["projects"],
                },
            })
            case_id = created["case_id"]
            harness = DevLoopHarness(AgentScopeExecutionAdapter(store))

            result = Orchestrator(store, harness).advance(case_id, "TRIAGED")

            self.assertEqual(result["status"], "PLAN_APPROVAL")
            evidence = store.get_case_evidence(case_id)
            outputs = [json.loads(run["output_ref"]) for run in evidence["agent_runs"]]
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(output["harness_id"] == harness.harness_id for output in outputs))
            self.assertTrue(all(output["harness_task_id"].startswith("htask-") for output in outputs))
            store.close()


class HarnessEndpointTests(unittest.TestCase):
    def _start(self, store: StateStore) -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        harness = DevLoopHarness(RecordingExecutor())
        server = CodeCCTVServer(("127.0.0.1", 0), token, store, harness=harness)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    def test_endpoint_requires_service_token_and_returns_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = self._start(store)
            try:
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(f"{base_url}/api/harness", timeout=3)
                self.assertEqual(rejected.exception.code, 401)

                request = Request(
                    f"{base_url}/api/harness",
                    headers={"X-Code-CCTV-Token": token},
                )
                with urlopen(request, timeout=3) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["harness"]["agent_count"], 4)
                self.assertEqual(body["harness"]["tasks"][0]["agent_id"], "triage")
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_console_reads_harness_manifest_and_names_the_control_surface(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Harness 调度", console)
        self.assertIn('api("/api/harness")', console)
        self.assertIn("Harness 显式派发 Agent 任务", console)


if __name__ == "__main__":
    unittest.main()
