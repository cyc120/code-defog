#!/usr/bin/env python3
"""Tests for the local CLI agent runtime (agent_runtime/cli_teams_*.py).

External processes are faked the way this project always fakes them: with
real, tiny, deterministic subprocesses (a stub CLI script on disk), never by
monkeypatching subprocess itself.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.cli_teams_adapter import (
    CLI_SANDBOX_MODE,
    CLITeamsAdapter,
    CLITeamsConfig,
)
from agent_runtime.cli_teams_preflight import (
    CLIPreflightCheck,
    CLITeamsPreflightReport,
    inspect_cli_teams_preflight,
)
from agent_runtime.harness import DevLoopHarness
from agent_runtime.orchestrator import Orchestrator
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter
from daemon.store import StateStore

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEMO_TARGET = _REPO_ROOT / "demo_target"

# A deterministic stand-in for `codex exec` / `claude -p`.  The prompt is
# always the last argv element.  Behaviour is selected by FAKE_CLI_MODE.
FAKE_CLI = '''
import json
import os
import sys
import time

prompt = sys.argv[-1]
mode = os.environ.get("FAKE_CLI_MODE", "json")

BUGGY = """    return {
        "projects": config["projects"],
        "required_field": config["required_field"],
    }
"""
PATCHED = """    return {
        "projects": config.get("projects", []),
        "required_field": config.get("required_field"),
    }
"""

if mode == "sleep":
    time.sleep(30)
if mode == "exit3":
    print("boom", file=sys.stderr)
    sys.exit(3)
if mode == "nojson":
    print("I'm sorry, but I cannot help with that.")
    sys.exit(0)
if mode == "edit":
    with open(os.path.join(os.getcwd(), "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write(os.environ.get("FAKE_CLI_CONTENT", "written by the fake CLI\\n"))
elif mode == "fix":
    cli_path = os.path.join(os.getcwd(), "cli.py")
    with open(cli_path, encoding="utf-8") as handle:
        text = handle.read().replace("\\r\\n", "\\n")
    with open(cli_path, "w", encoding="utf-8", newline="\\n") as handle:
        handle.write(text.replace(BUGGY, PATCHED, 1))

if "Triage the following" in prompt:
    payload = {
        "action": "classified",
        "priority": "high",
        "classification": "config-bug",
        "symptoms": ["KeyError: projects"],
        "evidence_sources": ["issue"],
        "confidence": 0.9,
    }
elif "Diagnose the root cause" in prompt:
    payload = {
        "action": "diagnosed",
        "hypotheses": [
            {"description": "unguarded dict access", "confidence": 0.7,
             "code_locations": ["cli.py:10"]},
        ],
        "impact_scope": "config loading",
        "risk_level": "medium",
        "remediation_strategy": "use dict.get with defaults",
    }
elif "Generate a minimal patch" in prompt:
    payload = {
        "action": "patched",
        "patch_ref": "claimed-by-the-model",
        "branch": "model-claim",
        "files_changed": ["cli.py"],
        "test_results": [{"test": "smoke", "passed": True}],
    }
else:
    payload = {"action": "processed", "priority": "medium", "confidence": 0.5}

print("```json")
print(json.dumps(payload))
print("```")
'''


class CLITeamsTestCase(unittest.TestCase):
    """Base: temp dir with a StateStore, a source repo, and a stub CLI."""

    def setUp(self) -> None:
        self._directory = Path(tempfile.mkdtemp(prefix="code-defog-cli-"))
        self.addCleanup(shutil.rmtree, self._directory, True)
        self.store = StateStore(self._directory / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.fake_cli = self._directory / "fake_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        self.repo = self._directory / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def config(self, **overrides: object) -> CLITeamsConfig:
        base: dict[str, object] = {
            "provider": "custom",
            "command": f'"{sys.executable}" "{self.fake_cli}"',
            "timeout_s": 60.0,
        }
        base.update(overrides)
        return CLITeamsConfig(**base)  # type: ignore[arg-type]

    def adapter(self, **overrides: object) -> CLITeamsAdapter:
        return CLITeamsAdapter(self.store, self.config(**overrides))

    def make_case(self, *, repair_mode: str = "", repo: Path | None = None) -> str:
        signals = {
            "exception_type": "KeyError",
            "message_pattern": "projects",
            "key_frames": ["cli.py:1"],
            "keywords": ["projects"],
        }
        if repair_mode:
            signals["repair_mode"] = repair_mode
        repository_ref = str(repo or self.repo)
        created = self.store.create_or_find_case({
            "source_type": "issue",
            "source_uri": "cli-test://case",
            "client_nonce": f"cli-{repair_mode or 'plain'}",
            "raw_content": "KeyError: projects",
            "repository_ref": repository_ref,
            "extracted_signals": signals,
        })
        return str(created["case_id"])


class CLIPreflightTests(unittest.TestCase):
    def test_missing_binary_blocks_with_remedy(self) -> None:
        report = inspect_cli_teams_preflight(
            provider="codex", environment={}, which=lambda name: None,
        )
        self.assertFalse(report.ready)
        binary_check = report.checks[0]
        self.assertFalse(binary_check.passed)
        self.assertIn("Remedy", report.format_text())
        with self.assertRaises(Exception):
            report.require_ready()

    def test_present_binary_is_ready(self) -> None:
        report = inspect_cli_teams_preflight(
            provider="claude",
            environment={"ANTHROPIC_API_KEY": "sk-test"},
            which=lambda name: f"/opt/bin/{name}",
        )
        self.assertTrue(report.ready)
        self.assertIn("found at /opt/bin/claude", report.format_text())

    def test_custom_provider_uses_the_command_first_token(self) -> None:
        report = inspect_cli_teams_preflight(
            provider="custom",
            command="my-agent run --json",
            environment={},
            which=lambda name: "/usr/local/bin/my-agent" if name == "my-agent" else None,
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.checks[0].name, "cli_binary:my-agent")

    def test_custom_provider_without_command_is_blocked(self) -> None:
        report = inspect_cli_teams_preflight(
            provider="custom", command="", environment={}, which=lambda name: "/bin/true",
        )
        self.assertFalse(report.ready)
        self.assertIn("command template", report.checks[0].remediation)


class ServeCLIModeTests(unittest.TestCase):
    def test_cli_runtime_request_stops_before_state_store(self) -> None:
        blocked = CLITeamsPreflightReport(checks=(
            CLIPreflightCheck(
                name="cli_binary:codex",
                passed=False,
                detail="'codex' not found on PATH",
                remediation="Install the codex CLI.",
            ),
        ))
        with (
            patch.object(
                sys, "argv",
                ["serve.py", "--runtime-mode", "cli", "--cli-provider", "codex"],
            ),
            patch("daemon.serve.inspect_cli_teams_preflight", return_value=blocked) as preflight,
            patch("daemon.serve.StateStore") as state_store,
        ):
            from daemon import serve

            with self.assertRaises(SystemExit) as raised:
                serve.main()

        self.assertEqual(raised.exception.code, 2)
        state_store.assert_not_called()
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs["provider"], "codex")


class CLITeamsAdapterTests(CLITeamsTestCase):
    def test_dispatch_triaged_completes_with_promoted_fields(self) -> None:
        case_id = self.make_case()
        result = self.adapter().dispatch_task(case_id, "TRIAGED", {
            "case_id": case_id, "repository_ref": str(self.repo),
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["priority"], "high")
        self.assertEqual(result["confidence"], 0.9)
        evidence = self.store.get_case_evidence(case_id)
        runs = [json.loads(run["output_ref"]) for run in evidence["agent_runs"]]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["execution_mode"], "cli")

    def test_dispatch_diagnosed_completes(self) -> None:
        case_id = self.make_case()
        result = self.adapter().dispatch_task(case_id, "DIAGNOSED", {
            "case_id": case_id, "repository_ref": str(self.repo),
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["impact_scope"], "config loading")

    def test_dispatch_repairing_materialises_sandbox_patch_and_evidence(self) -> None:
        case_id = self.make_case(repair_mode=CLI_SANDBOX_MODE)
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "edit"}):
            result = self.adapter().dispatch_task(case_id, "REPAIRING", {
                "case_id": case_id,
                "repository_ref": str(self.repo),
                "repair_mode": CLI_SANDBOX_MODE,
            })
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["patch_ref"].startswith("patch-"))
        self.assertEqual(result["files_changed"], ["notes.txt"])
        sandbox_ref = Path(result["sandbox_repository_ref"])
        sandbox_root = (self._directory / "sandboxes").resolve()
        self.assertIn(sandbox_root, sandbox_ref.resolve().parents)
        self.assertTrue((sandbox_ref / "notes.txt").is_file())
        # The original repository is untouched.
        self.assertFalse((self.repo / "notes.txt").exists())

        evidence = self.store.get_case_evidence(case_id)
        kinds = [artifact["kind"] for artifact in evidence["artifacts"]]
        self.assertIn("cli_transcript", kinds)
        self.assertIn("cli_patch", kinds)
        tool_names = [run["tool_name"] for run in evidence["tool_runs"]]
        self.assertIn("cli_custom", tool_names)
        self.assertTrue(self.store.verify_tool_chain(case_id)["ok"])

    def test_verifying_delegates_to_the_deterministic_gate(self) -> None:
        # Delegation runs the REAL quality gate: the unfixed bundled demo
        # target fails CHECK 1, and the report lands as an artifact.
        case_id = self.make_case(repo=_DEMO_TARGET)
        result = self.adapter().dispatch_task(case_id, "VERIFYING", {
            "case_id": case_id, "sandbox_ref": str(_DEMO_TARGET),
        })
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["quality_gate_passed"])
        evidence = self.store.get_case_evidence(case_id)
        kinds = [artifact["kind"] for artifact in evidence["artifacts"]]
        self.assertIn("quality_gate_report", kinds)

    def test_unparseable_output_marks_failed(self) -> None:
        case_id = self.make_case()
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "nojson"}):
            result = self.adapter().dispatch_task(case_id, "TRIAGED", {
                "case_id": case_id, "repository_ref": str(self.repo),
            })
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid_structured_output", result["failure_reason"])

    def test_nonzero_exit_marks_failed(self) -> None:
        case_id = self.make_case()
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "exit3"}):
            result = self.adapter().dispatch_task(case_id, "TRIAGED", {
                "case_id": case_id, "repository_ref": str(self.repo),
            })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "cli_exit_3")

    def test_timeout_kills_and_marks_failed(self) -> None:
        case_id = self.make_case()
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "sleep"}):
            result = self.adapter(timeout_s=1.0).dispatch_task(case_id, "TRIAGED", {
                "case_id": case_id, "repository_ref": str(self.repo),
            })
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["failure_reason"].startswith("cli_timeout"))
        evidence = self.store.get_case_evidence(case_id)
        self.assertEqual(evidence["tool_runs"][0]["exit_code"], -1)

    def test_review_and_interpreter_tasks_fail_without_mock_fallback(self) -> None:
        adapter = self.adapter()
        review = adapter.dispatch_review_task("review-a", "project_review", {})
        interpreter = adapter.dispatch_code_interpreter_task({"case_id": "x"})
        self.assertEqual(review["status"], "failed")
        self.assertEqual(interpreter["status"], "failed")
        self.assertIn("does not serve", review["failure_reason"])
        self.assertIn("does not serve", interpreter["failure_reason"])

    def test_unknown_state_returns_error_without_agent_run(self) -> None:
        case_id = self.make_case()
        result = self.adapter().dispatch_task(case_id, "PLAN_APPROVAL", {
            "case_id": case_id,
        })
        self.assertIn("error", result)
        self.assertEqual(self.store.get_case_evidence(case_id)["agent_runs"], [])

    def test_agent_exception_becomes_failed_run(self) -> None:
        case_id = self.make_case()
        adapter = self.adapter()
        with patch.object(adapter, "_ensure_sandbox", side_effect=RuntimeError("boom")):
            result = adapter.dispatch_task(case_id, "TRIAGED", {
                "case_id": case_id, "repository_ref": str(self.repo),
            })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "exception: boom")


class CLITeamsOrchestratorTests(CLITeamsTestCase):
    def orchestrator(self, **overrides: object) -> Orchestrator:
        return Orchestrator(self.store, DevLoopHarness(self.adapter(**overrides)))

    def test_repair_without_changes_escalates(self) -> None:
        case_id = self.make_case(repair_mode=CLI_SANDBOX_MODE)
        orchestrator = self.orchestrator()
        self.assertEqual(orchestrator.advance(case_id, "TRIAGED")["status"], "PLAN_APPROVAL")
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "json"}):
            result = orchestrator.advance(case_id, "REPAIRING")
        self.assertEqual(result["status"], "ESCALATED")
        case = self.store.get_case(case_id)
        self.assertIsNone(case["patch_ref"])

    def test_untrusted_repair_mode_cannot_persist_sandbox(self) -> None:
        # Same invariant as tests/test_core_boundaries.py: without a trusted
        # sandbox mode the adapter's patch claim is dropped and escalated.
        case_id = self.make_case()
        orchestrator = self.orchestrator()
        orchestrator.advance(case_id, "TRIAGED")
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "edit"}):
            result = orchestrator.advance(case_id, "REPAIRING")
        self.assertEqual(result["status"], "ESCALATED")
        case = self.store.get_case(case_id)
        self.assertIsNone(case["sandbox_ref"], "untrusted sandbox_ref must not persist")
        self.assertIsNone(case["patch_ref"], "untrusted patch_ref must not persist")

    def test_cli_sandbox_patch_persists_and_gate_advances_to_release_approval(self) -> None:
        demo_copy = self._directory / "demo-copy"
        shutil.copytree(_DEMO_TARGET, demo_copy)
        case_id = self.make_case(repair_mode=CLI_SANDBOX_MODE, repo=demo_copy)
        orchestrator = self.orchestrator()
        self.assertEqual(orchestrator.advance(case_id, "TRIAGED")["status"], "PLAN_APPROVAL")
        with patch.dict("os.environ", {"FAKE_CLI_MODE": "fix"}):
            result = orchestrator.advance(case_id, "REPAIRING")
        # Repair -> deterministic gate in the sandbox -> human release gate.
        self.assertEqual(result["status"], "RELEASE_APPROVAL")
        case = self.store.get_case(case_id)
        self.assertTrue(case["patch_ref"].startswith("patch-"))
        self.assertTrue(case["sandbox_ref"])
        sandbox_root = (self._directory / "sandboxes").resolve()
        self.assertIn(sandbox_root, Path(case["sandbox_ref"]).resolve().parents)
        # The original demo copy is never modified.
        self.assertIn('config["projects"]', (demo_copy / "cli.py").read_text(encoding="utf-8"))

        evidence = self.store.get_case_evidence(case_id)
        kinds = [artifact["kind"] for artifact in evidence["artifacts"]]
        for expected in ("cli_transcript", "cli_patch", "quality_gate_report"):
            self.assertIn(expected, kinds)
        self.assertTrue(self.store.verify_tool_chain(case_id)["ok"])

    def test_mock_runtime_still_uses_the_demo_sandbox_mode(self) -> None:
        # Guard the seam: the CLI adapter must not leak into the default
        # runtime, and the demo trust mode keeps working untouched.
        self.assertIsInstance(AgentScopeExecutionAdapter(self.store).mode, str)
        orchestrator = Orchestrator(self.store, DevLoopHarness(AgentScopeExecutionAdapter(self.store)))
        case_id = self.make_case(repair_mode="demo_sandbox", repo=_DEMO_TARGET)
        self.assertEqual(orchestrator.advance(case_id, "TRIAGED")["status"], "PLAN_APPROVAL")


if __name__ == "__main__":
    unittest.main()
