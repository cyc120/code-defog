"""Tests for the P4 retrospective / knowledge-sink module.

Covers the pure retrospective skills (retrospective/skills.py), the
knowledge_records store methods, the generate_retrospective orchestration,
the terminal-state trigger hook, and the HTTP endpoints.

Style mirrors tests/test_daemon.py: unittest + tempfile.TemporaryDirectory
+ StateStore; HTTP tests use the shared _start_server helper.
"""

from __future__ import annotations

import getpass
import json
import secrets
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from daemon.server import CodeCCTVServer
from daemon.store import StateStore

from retrospective.retrospective import generate_retrospective
from retrospective.skills import (
    case_summarizer,
    evidence_indexer,
    knowledge_extractor,
    skill_candidates,
)


_TEST_APPROVAL_KEY = "test-human-approval-key"


def _start_server(
    store: StateStore, orchestrator: object | None = None,
) -> tuple[CodeCCTVServer, str, str]:
    """Start the daemon on a random port and return (server, base_url, token)."""
    token = secrets.token_hex(16)
    server = CodeCCTVServer(
        ("127.0.0.1", 0), token, store, orchestrator,
        approval_secret=_TEST_APPROVAL_KEY,
    )
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{actual_port}", token


def _minimal_evidence() -> dict:
    """A minimal evidence bundle exercising every summarizer/extractor rule."""
    return {
        "case": {
            "case_id": "case-retro-1",
            "status": "CLOSED",
            "priority": "high",
            "risk_level": "medium",
            "repository_ref": "/tmp/demo_target",
            "base_commit": "abc123",
            "patch_ref": "patch-deadbeef",
            "incident_signature": "sig-KeyError-projects",
            "trace_id": "trace-retro-1",
            "created_at": "2026-08-05T00:00:00Z",
            "closed_at": "2026-08-05T01:00:00Z",
        },
        "sources": [{
            "source_type": "issue", "source_uri": "issue#1",
            "association_state": "linked", "received_at": "2026-08-05T00:00:00Z",
            "content_hash": "0123456789abcdef0123456789abcdef01234567",
            "incident_signature": "sig-KeyError-projects",
            "association_confidence": 1.0,
            "extracted_signals_json": json.dumps({"exception_type": "KeyError"}),
        }],
        "agent_runs": [
            {
                "agent_id": "triage", "status": "completed",
                "started_at": "2026-08-05T00:00:01Z", "finished_at": "2026-08-05T00:00:10Z",
                "output_ref": json.dumps({"action": "triaged inputs"}),
            },
            {
                "agent_id": "diagnosis", "status": "completed",
                "started_at": "2026-08-05T00:00:11Z", "finished_at": "2026-08-05T00:00:20Z",
                "output_ref": json.dumps({
                    "action": "diagnosed",
                    "hypotheses": [{
                        "description": "empty config dereferences missing key",
                        "confidence": 0.85,
                        "code_locations": ["cli.py:25"],
                    }],
                }),
            },
            {
                "agent_id": "verification", "status": "completed",
                "started_at": "2026-08-05T00:00:21Z", "finished_at": "2026-08-05T00:00:30Z",
                "output_ref": json.dumps({"action": "verified", "quality_gate_passed": True}),
            },
        ],
        "tool_runs": [
            {
                "chain_sequence": 1, "tool_name": "sandbox_copy",
                "exit_code": 0, "input_sha256": "a" * 64, "output_sha256": "b" * 64,
                "chain_hash": "c" * 64,
            },
            {
                "chain_sequence": 2, "tool_name": "apply_case_a_patch",
                "exit_code": 0, "input_sha256": "b" * 64, "output_sha256": "d" * 64,
                "chain_hash": "e" * 64,
            },
            {
                "chain_sequence": 3, "tool_name": "quality_gate",
                "exit_code": 0, "input_sha256": "d" * 64, "output_sha256": "f" * 64,
                "chain_hash": "g" * 64,
            },
        ],
        "approvals": [{
            "action": "approve_plan", "decision": "approved",
            "approver": "reviewer", "target_ref": "abc123",
            "reason": "plan looks good",
        }],
        "artifacts": [{
            "kind": "patch_metadata", "uri": "artifacts/case-retro-1/patch.json",
            "sha256": "a" * 64,
        }],
    }


class RetrospectiveSkillTests(unittest.TestCase):
    """Pure-function skills — deterministic, offline-reproducible."""

    def test_case_summarizer_contains_all_sections(self) -> None:
        report = case_summarizer(_minimal_evidence())
        for section in ("Sources", "Agent Runs", "Tool Chain", "Approvals", "Artifacts", "Outcome"):
            self.assertIn(section, report)
        self.assertIn("case-retro-1", report)

    def test_case_summarizer_includes_tool_hashes(self) -> None:
        report = case_summarizer(_minimal_evidence())
        self.assertIn("quality_gate", report)
        self.assertIn("a" * 12, report)  # input_sha256 truncated to 12

    def test_knowledge_extractor_derives_incident_signature(self) -> None:
        entries = knowledge_extractor(_minimal_evidence(), "")
        sig = [e for e in entries if e["category"] == "incident_signature"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["confidence"], 0.9)  # association_confidence 1.0

    def test_knowledge_extractor_derives_quality_gate(self) -> None:
        entries = knowledge_extractor(_minimal_evidence(), "")
        gates = [e for e in entries if e["category"] == "quality_gate"]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["confidence"], 1.0)
        self.assertIn("passed", gates[0]["title"])

    def test_knowledge_extractor_derives_hypothesis_with_stated_confidence(self) -> None:
        entries = knowledge_extractor(_minimal_evidence(), "")
        hypotheses = [e for e in entries if e["category"] == "root_cause"]
        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(hypotheses[0]["confidence"], 0.85)

    def test_knowledge_extractor_confidence_deterministic(self) -> None:
        a = knowledge_extractor(_minimal_evidence(), "")
        b = knowledge_extractor(_minimal_evidence(), "")
        self.assertEqual(a, b)

    def test_knowledge_extractor_empty_evidence_returns_empty(self) -> None:
        entries = knowledge_extractor({"case": {}, "sources": [], "agent_runs": [],
                                       "tool_runs": [], "approvals": [], "artifacts": []}, "")
        self.assertEqual(entries, [])

    def test_skill_candidates_include_retrospective_skills(self) -> None:
        candidates = skill_candidates(_minimal_evidence())
        ids = {c["skill_id"] for c in candidates}
        for expected in ("case_summarizer", "knowledge_extractor", "evidence_indexer", "compliance_checker"):
            self.assertIn(expected, ids)
        quality_gate = [c for c in candidates if c["skill_id"] == "quality_gate"]
        self.assertGreaterEqual(quality_gate[0]["evidence_count"], 1)
        patch_generator = [c for c in candidates if c["skill_id"] == "patch_generator"]
        self.assertGreaterEqual(patch_generator[0]["evidence_count"], 1)

    def test_evidence_indexer_shape(self) -> None:
        index = evidence_indexer("case-retro-1", _minimal_evidence())
        self.assertEqual(index["case_id"], "case-retro-1")
        self.assertIn("evidence_tree", index)
        self.assertIn("hashes", index)
        self.assertIn("trace", index)
        self.assertEqual(len(index["hashes"]), 3)
        self.assertEqual(index["hashes"][0]["chain_hash"], "c" * 64)
        self.assertEqual(index["trace"]["tool_run_count"], 3)


class DevLoopKnowledgeStoreTests(unittest.TestCase):
    """knowledge_records CRUD + review."""

    def _make_store(self):
        return tempfile.TemporaryDirectory()

    def _store_in(self, tmp):
        # `with tempfile.TemporaryDirectory() as tmp` yields a str path
        return StateStore(Path(tmp) / "state.sqlite3")

    def test_record_knowledge_records_defaults_pending_review(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            records = store.record_knowledge_records("case-1", "art-manifest", [
                {"title": "t1", "category": "c1", "content": "x", "confidence": 0.9,
                 "tags": ["a", "b"]},
            ])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending_review")
            self.assertEqual(records[0]["content_ref"], "art-manifest#0")
            self.assertEqual(records[0]["reuse_tags"], ["a", "b"])
            store.close()

    def test_list_knowledge_records_filters_by_case_and_status(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            store.record_knowledge_records("case-1", "m1#", [{"title": "t", "category": "c",
                                                              "content": "x", "confidence": 0.5,
                                                              "tags": []}])
            store.record_knowledge_records("case-2", "m2#", [{"title": "u", "category": "c",
                                                              "content": "y", "confidence": 0.5,
                                                              "tags": []}])
            self.assertEqual(len(store.list_knowledge_records(case_id="case-1")), 1)
            self.assertEqual(len(store.list_knowledge_records()), 2)
            self.assertEqual(len(store.list_knowledge_records(status="pending_review")), 2)
            store.close()

    def test_review_knowledge_record_verified(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            records = store.record_knowledge_records("case-1", "m#", [{"title": "t", "category": "c",
                                                                       "content": "x", "confidence": 0.5,
                                                                       "tags": []}])
            record_id = records[0]["record_id"]
            reviewed = store.review_knowledge_record(record_id, "alice", "verified", "looks good")
            self.assertEqual(reviewed["status"], "verified")
            self.assertEqual(reviewed["reviewed_by"], "alice")
            self.assertIsNotNone(reviewed["reviewed_at"])
            stored = store.get_knowledge_record(record_id)
            self.assertEqual(stored["status"], "verified")
            store.close()

    def test_review_knowledge_record_rejected(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            records = store.record_knowledge_records("case-1", "m#", [{"title": "t", "category": "c",
                                                                       "content": "x", "confidence": 0.5,
                                                                       "tags": []}])
            reviewed = store.review_knowledge_record(records[0]["record_id"], "bob", "rejected")
            self.assertEqual(reviewed["status"], "rejected")
            store.close()

    def test_review_unknown_record_returns_none(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            self.assertIsNone(store.review_knowledge_record("krec-missing", "a", "verified"))
            store.close()

    def test_review_invalid_decision_returns_error(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            result = store.review_knowledge_record("krec-x", "a", "bogus")
            self.assertIn("error", result)
            store.close()

    def test_clear_all_clears_knowledge_records(self) -> None:
        with self._make_store() as tmp:
            store = self._store_in(tmp)
            store.record_knowledge_records("case-1", "m#", [{"title": "t", "category": "c",
                                                             "content": "x", "confidence": 0.5,
                                                             "tags": []}])
            self.assertEqual(len(store.list_knowledge_records()), 1)
            store.clear_all()
            self.assertEqual(len(store.list_knowledge_records()), 0)
            store.close()


class DevLoopRetrospectiveTests(unittest.TestCase):
    """generate_retrospective orchestration + terminal-state trigger."""

    def _make_case(self, store: StateStore, nonce: str) -> str:
        result = store.create_or_find_case({
            "source_type": "issue", "source_uri": f"retro-{nonce}",
            "client_nonce": nonce, "raw_content": "KeyError: missing projects key",
            "repository_ref": "/tmp/demo_target",
            "extracted_signals": {
                "exception_type": "KeyError", "message_pattern": "missing projects key",
                "key_frames": ["cli.py:25"], "keywords": ["projects"],
            },
        })
        return result["case_id"]

    def test_generate_for_closed_case_creates_report_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-a")
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")

            retro = generate_retrospective(store, case_id)
            self.assertEqual(retro["case_id"], case_id)
            self.assertEqual(retro["status"], "CLOSED")
            self.assertTrue(retro["regenerated"])
            self.assertTrue(retro["report_artifact_id"].startswith("art-"))
            self.assertGreater(len(retro["knowledge_entries"]), 0)
            for record in retro["knowledge_entries"]:
                self.assertEqual(record["status"], "pending_review")
            self.assertGreater(len(retro["skill_candidates"]), 0)
            self.assertIn("evidence_index", retro)
            store.close()

    def test_generate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-idem")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")
            first = generate_retrospective(store, case_id)
            second = generate_retrospective(store, case_id)
            self.assertEqual(second["report_artifact_id"], first["report_artifact_id"])
            self.assertFalse(second["regenerated"])
            self.assertEqual(len(store.list_knowledge_records(case_id=case_id)),
                             len(first["knowledge_entries"]))
            # force regenerates a new report
            forced = generate_retrospective(store, case_id, force=True)
            self.assertTrue(forced["regenerated"])
            store.close()

    def test_generate_rejects_non_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-nonterm")
            # still RECEIVED
            result = generate_retrospective(store, case_id)
            self.assertIn("error", result)
            store.close()

    def test_generate_missing_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            result = generate_retrospective(store, "case-missing")
            self.assertEqual(result["error"], "case not found")
            store.close()

    def test_generate_for_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-rolled")
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "REPAIRING")
            store.transition_case(case_id, "VERIFYING")
            store.transition_case(case_id, "RELEASE_APPROVAL")
            store.transition_case(case_id, "RELEASED")
            store.transition_case(case_id, "ROLLED_BACK")
            retro = generate_retrospective(store, case_id)
            self.assertEqual(retro["status"], "ROLLED_BACK")
            store.close()

    def test_generate_populates_evidence_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-evidence")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")
            generate_retrospective(store, case_id)
            evidence = store.get_case_evidence(case_id)
            self.assertIn("knowledge_records", evidence)
            self.assertIn("retrospective", evidence)
            self.assertGreater(len(evidence["knowledge_records"]), 0)
            self.assertIsNotNone(evidence["retrospective"]["report"]["content"])
            self.assertIn("Retrospective", evidence["retrospective"]["report"]["content"])
            store.close()

    def test_transition_closed_fires_retrospective_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-hook")
            fired: list[str] = []
            store.retrospective_hook = fired.append
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")
            self.assertEqual(fired, [case_id])
            store.close()

    def test_transition_rolled_back_fires_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-hook-roll")
            fired: list[str] = []
            store.retrospective_hook = fired.append
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "REPAIRING")
            store.transition_case(case_id, "VERIFYING")
            store.transition_case(case_id, "RELEASE_APPROVAL")
            store.transition_case(case_id, "RELEASED")
            store.transition_case(case_id, "ROLLED_BACK")
            self.assertIn(case_id, fired)
            store.close()

    def test_transition_non_terminal_does_not_fire_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-hook-no")
            fired: list[str] = []
            store.retrospective_hook = fired.append
            store.transition_case(case_id, "TRIAGED")
            store.transition_case(case_id, "DIAGNOSED")
            store.transition_case(case_id, "ESCALATED")  # ESCALATED not in trigger set
            self.assertEqual(fired, [])
            store.close()

    def test_case_retrospective_sse_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            case_id = self._make_case(store, "retro-sse")
            published: list[dict] = []
            store.publish_callback = published.append
            store.retrospective_hook = lambda cid: generate_retrospective(store, cid)
            store.transition_case(case_id, "ESCALATED")
            store.transition_case(case_id, "CLOSED")
            types = [evt["type"] for evt in published]
            self.assertIn("case_transition", types)
            self.assertIn("case_retrospective", types)
            retro_evt = [e for e in published if e["type"] == "case_retrospective"][0]
            self.assertIn("retrospective", retro_evt)
            store.close()


class DevLoopKnowledgeHttpTests(unittest.TestCase):
    """HTTP endpoints for retrospective generation and knowledge review."""

    def _make_terminal_case(self, store: StateStore) -> str:
        result = store.create_or_find_case({
            "source_type": "issue", "source_uri": "http-retro",
            "client_nonce": "http-retro-nonce", "raw_content": "KeyError",
            "repository_ref": "/tmp/demo_target",
            "extracted_signals": {"exception_type": "KeyError", "message_pattern": "x",
                                  "key_frames": ["cli.py:1"], "keywords": ["k"]},
        })
        case_id = result["case_id"]
        store.transition_case(case_id, "TRIAGED")
        store.transition_case(case_id, "ESCALATED")
        store.transition_case(case_id, "CLOSED")
        return case_id

    def test_post_retrospective_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                request = Request(
                    f"{base_url}/api/cases/{case_id}/retrospective",
                    data=b"", method="POST",
                    headers={"X-Code-CCTV-Token": token},
                )
                with urlopen(request, timeout=10) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertTrue(body["retrospective"]["report_artifact_id"].startswith("art-"))
                self.assertGreater(len(body["retrospective"]["knowledge_entries"]), 0)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_retrospective_requires_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, _ = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                request = Request(
                    f"{base_url}/api/cases/{case_id}/retrospective",
                    data=b"", method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=10)
                self.assertEqual(raised.exception.code, HTTPStatus.UNAUTHORIZED)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_knowledge_review_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                retro = generate_retrospective(store, case_id)
                record_id = retro["knowledge_entries"][0]["record_id"]
                request = Request(
                    f"{base_url}/api/knowledge/{record_id}/review",
                    data=json.dumps({"decision": "verified", "note": "root cause confirmed"}).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": token,
                        "X-Code-CCTV-Approval-Key": _TEST_APPROVAL_KEY,
                    },
                )
                with urlopen(request, timeout=10) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["record"]["status"], "verified")
                # Reviewer is the server-side system user, not client-supplied
                self.assertEqual(body["record"]["reviewed_by"], getpass.getuser())
                self.assertEqual(body["record"]["review_note"], "root cause confirmed")
                # review_note is now persisted
                stored = store.get_knowledge_record(record_id)
                self.assertEqual(stored["review_note"], "root cause confirmed")
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_knowledge_review_rejects_service_token(self) -> None:
        """A service token without the human approval key must be forbidden."""
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                retro = generate_retrospective(store, case_id)
                record_id = retro["knowledge_entries"][0]["record_id"]
                request = Request(
                    f"{base_url}/api/knowledge/{record_id}/review",
                    data=json.dumps({"decision": "verified"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "X-Code-CCTV-Token": token},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=10)
                self.assertEqual(raised.exception.code, HTTPStatus.FORBIDDEN)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_post_knowledge_review_invalid_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                retro = generate_retrospective(store, case_id)
                record_id = retro["knowledge_entries"][0]["record_id"]
                request = Request(
                    f"{base_url}/api/knowledge/{record_id}/review",
                    data=json.dumps({"decision": "bogus"}).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Code-CCTV-Token": token,
                        "X-Code-CCTV-Approval-Key": _TEST_APPROVAL_KEY,
                    },
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=10)
                self.assertEqual(raised.exception.code, HTTPStatus.BAD_REQUEST)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_evidence_endpoint_includes_retrospective(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            server, base_url, token = _start_server(store)
            try:
                case_id = self._make_terminal_case(store)
                generate_retrospective(store, case_id)
                request = Request(
                    f"{base_url}/api/cases/{case_id}/evidence",
                    headers={"X-Code-CCTV-Token": token},
                )
                with urlopen(request, timeout=10) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                evidence = body["evidence"]
                self.assertIn("knowledge_records", evidence)
                self.assertIn("retrospective", evidence)
                self.assertGreater(len(evidence["knowledge_records"]), 0)
            finally:
                server.shutdown(); server.server_close(); store.close()


if __name__ == "__main__":
    unittest.main()
