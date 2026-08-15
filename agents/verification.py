"""Verification Release Agent — quality gates, canary simulation.

Runs quality_gate.py against the patched code and returns a real
pass/fail result that the orchestrator uses to drive state transitions
(RELEASE_APPROVAL vs PATCH_REJECTED).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Path to the quality gate script, relative to the demo_target directory
_QUALITY_GATE_SCRIPT = Path(__file__).resolve().parents[1] / "demo_target" / "quality_gate.py"
_POLICY_VERSION = "demo-quality-gate-v1"


def _run_quality_gate(repo_path: str) -> dict[str, Any]:
    """Execute the quality gate script and return structured results.

    Catches subprocess failures (timeout, OSError) and returns an error
    dict so the orchestrator can escalate rather than leaving the Case
    stuck at VERIFYING.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(_QUALITY_GATE_SCRIPT), str(repo_path)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": None, "passed": None,
            "quality_gate_error": f"Quality gate timed out after 30s: {e}",
            "stdout": (e.stdout or "").strip() if e.stdout else "",
            "stderr": (e.stderr or "").strip() if e.stderr else "",
        }
    except OSError as e:
        return {
            "exit_code": None, "passed": None,
            "quality_gate_error": f"Quality gate failed to start: {e}",
            "stdout": "", "stderr": "",
        }
    return {
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _record_gate_evidence(
    store: Any,
    context: dict[str, Any],
    repo_path: str,
    gate_result: dict[str, Any],
) -> dict[str, str]:
    """Persist the deterministic gate report and its immutable tool record."""
    case_id = str(context.get("case_id", ""))
    cli_path = Path(repo_path) / "cli.py"
    input_sha256 = hashlib.sha256(cli_path.read_bytes()).hexdigest()
    report = {
        "policy_version": _POLICY_VERSION,
        "case_id": case_id,
        "patch_ref": context.get("patch_ref", ""),
        "sandbox_repository_ref": repo_path,
        "exit_code": gate_result.get("exit_code"),
        "passed": gate_result.get("passed"),
        "stdout": gate_result.get("stdout", ""),
        "stderr": gate_result.get("stderr", ""),
        "quality_gate_error": gate_result.get("quality_gate_error", ""),
    }
    report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    artifact_id = store.record_artifact(case_id, "quality_gate_report", "quality_gate.json", report_bytes)
    tool_run_id = store.record_tool_run({
        "case_id": case_id,
        "agent_id": "verification",
        "tool_name": "quality_gate",
        "command_template": "python quality_gate.py <isolated-sandbox>",
        "actual_argv": f"{sys.executable} {_QUALITY_GATE_SCRIPT} {repo_path}",
        "working_directory": repo_path,
        "policy_version": _POLICY_VERSION,
        "input_sha256": input_sha256,
        "output_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "exit_code": gate_result.get("exit_code") if gate_result.get("exit_code") is not None else -1,
        "result_ref": artifact_id,
    })
    return {"quality_gate_artifact_ref": artifact_id, "quality_gate_tool_run_id": tool_run_id}


_DEMO_TARGET = Path(__file__).resolve().parents[1] / "demo_target"


def _gate_target_allowed(repo_ref: str, store: Any) -> bool:
    """A quality gate executes code (cli.py) — only Store-controlled
    sandboxes or the exact bundled demo target may be executed."""
    try:
        target = Path(repo_ref).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    allowed_roots: list[Path] = [_DEMO_TARGET.resolve(strict=True)]
    if store is not None and getattr(store, "path", None) is not None:
        sandbox_root = (Path(store.path).parent / "sandboxes").resolve()
        if sandbox_root.exists():
            allowed_roots.append(sandbox_root)
    for root in allowed_roots:
        if target == root or root in target.parents:
            return True
    return False


def run(context: dict[str, Any]) -> dict[str, Any]:
    """Entry point called by the configured execution adapter or its mock.

    The quality gate executes code, so it runs only against a
    Store-controlled sandbox (``sandbox_ref``) or the bundled demo target.
    The intake-supplied ``repository_ref`` is deliberately never used as an
    execution target.  Without a runnable allowed target the agent returns
    an unchecked stub.
    """
    case_id = context.get("case_id", "unknown")
    store = context.get("_state_store")
    repo_ref = context.get("sandbox_ref") or ""

    if repo_ref and not _gate_target_allowed(repo_ref, store):
        return {
            "agent": "verification",
            "case_id": case_id,
            "action": "verified",
            "quality_gate_passed": None,
            "quality_gate_error": f"refusing to run quality gate outside Store sandbox: {repo_ref}",
            "recommendation": "escalate",
            "note": "Deterministic verification rejected a non-sandbox gate target.",
        }

    if repo_ref and Path(repo_ref, "cli.py").exists():
        gate_result = _run_quality_gate(repo_ref)
        evidence_refs: dict[str, str] = {}
        if store is not None and case_id:
            evidence_refs = _record_gate_evidence(store, context, repo_ref, gate_result)
        passed = gate_result.get("passed")
        error  = gate_result.get("quality_gate_error")
        if error:
            return {
                "agent": "verification",
                "case_id": case_id,
                "action": "verified",
                "quality_gate_passed": None,
                "quality_gate_error": error,
                "recommendation": "escalate",
                "note": f"Quality gate execution failed: {error}",
                **evidence_refs,
            }
        return {
            "agent": "verification",
            "case_id": case_id,
            "action": "verified",
            "quality_gate_passed": passed,
            "quality_gate_exit_code": gate_result.get("exit_code"),
            "quality_gate_stdout": gate_result.get("stdout", ""),
            "quality_gate_stderr": gate_result.get("stderr", ""),
            "recommendation": "release" if passed else "reject",
            "note": (
                "Quality gate passed — ready for release approval."
                if passed
                else f"Quality gate FAILED — patch must be rejected. {gate_result.get('stderr', '')}"
            ),
            **evidence_refs,
        }

    if context.get("sandbox_ref"):
        return {
            "agent": "verification",
            "case_id": case_id,
            "action": "verified",
            "quality_gate_passed": None,
            "quality_gate_error": f"Sandbox has no runnable cli.py: {repo_ref}",
            "recommendation": "escalate",
            "note": "Deterministic verification could not find the isolated sandbox target.",
        }

    # No runnable target — stub (unit test / offline dev mode)
    return {
        "agent": "verification",
        "case_id": case_id,
        "action": "verified",
        "quality_gate_passed": None,
        "recommendation": "unchecked",
        "note": "Verification skipped — no runnable cli.py found at the Store sandbox.",
    }
