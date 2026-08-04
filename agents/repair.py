"""Repair Agent — generates minimal patches in isolated worktrees.

Stub implementation for P1.
"""

from typing import Any


def run(context: dict[str, Any]) -> dict[str, Any]:
    case_id = context.get("case_id", "unknown")
    return {
        "agent": "repair",
        "case_id": case_id,
        "action": "patched",
        "patch_ref": "",
        "branch": "",
        "files_changed": [],
        "note": "Repair stub — patch generation pending.",
    }
