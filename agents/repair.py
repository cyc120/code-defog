"""Repair Agent for the reviewed, isolated Direction 3 demo workflow."""

from typing import Any


def run(context: dict[str, Any]) -> dict[str, Any]:
    case_id = context.get("case_id", "unknown")
    store = context.get("_state_store")
    if context.get("repair_mode") == "demo_sandbox" and store is not None:
        from tools.controlled_repair import ControlledRepairError, apply_case_a_patch

        try:
            repair_result = apply_case_a_patch(context, store)
        except ControlledRepairError as exc:
            return {
                "agent": "repair",
                "case_id": case_id,
                "action": "patch_not_applied",
                "patch_ref": "",
                "branch": "",
                "files_changed": [],
                "note": f"Controlled demo repair rejected: {exc}",
            }
        return {
            "agent": "repair",
            "case_id": case_id,
            "action": "patched",
            "branch": f"sandbox/{case_id}",
            "note": "Reviewed Case A patch applied in an isolated sandbox.",
            **repair_result,
        }

    return {
        "agent": "repair",
        "case_id": case_id,
        "action": "patched",
        "patch_ref": "",
        "branch": "",
        "files_changed": [],
        "note": "Repair skipped: no explicit controlled demo sandbox context.",
    }
