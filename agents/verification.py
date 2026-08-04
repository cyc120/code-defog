"""Verification Release Agent — quality gates, canary simulation.

Runs quality_gate.py against the patched code and returns a real
pass/fail result that the orchestrator uses to drive state transitions
(RELEASE_APPROVAL vs PATCH_REJECTED).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Path to the quality gate script, relative to the demo_target directory
_QUALITY_GATE_SCRIPT = Path(__file__).resolve().parents[1] / "demo_target" / "quality_gate.py"


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


def run(context: dict[str, Any]) -> dict[str, Any]:
    """Entry point called by AgentTeams Adapter (or its mock).

    If *context* contains ``repository_ref`` pointing to a directory
    with cli.py, the agent runs the real quality gate.  Otherwise it
    returns a stub result marked as unchecked.
    """
    case_id = context.get("case_id", "unknown")
    repo_ref = context.get("repository_ref", "")

    if repo_ref and Path(repo_ref, "cli.py").exists():
        gate_result = _run_quality_gate(repo_ref)
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
            }
        return {
            "agent": "verification",
            "case_id": case_id,
            "action": "verified",
            "quality_gate_passed": passed,
            "quality_gate_exit_code": gate_result.get("exit_code"),
            "quality_gate_stderr": gate_result.get("stderr", ""),
            "recommendation": "release" if passed else "reject",
            "note": (
                "Quality gate passed — ready for release approval."
                if passed
                else f"Quality gate FAILED — patch must be rejected. {gate_result.get('stderr', '')}"
            ),
        }

    # No runnable target — stub (unit test / offline dev mode)
    return {
        "agent": "verification",
        "case_id": case_id,
        "action": "verified",
        "quality_gate_passed": None,
        "recommendation": "unchecked",
        "note": "Verification skipped — no runnable cli.py found at repository_ref.",
    }
