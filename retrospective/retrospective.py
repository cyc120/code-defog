"""Retrospective module — generates post-Case review reports and knowledge entries.

Triggered asynchronously when a Case reaches CLOSED / ROLLED_BACK.
"""

from typing import Any


def generate_retrospective(store: Any, case_id: str) -> dict[str, Any]:
    """Generate a retrospective report for a closed Case."""
    case = store.get_case(case_id)
    if case is None:
        return {"error": "case not found"}
    status = case.get("status", "")
    if status not in ("CLOSED", "ROLLED_BACK", "ESCALATED"):
        return {"error": f"case not in terminal state: {status}"}

    evidence = store.get_case_evidence(case_id)
    # Stub: real implementation will call case_summarizer and
    # knowledge_extractor skills.
    return {
        "case_id": case_id,
        "status": status,
        "summary": f"Retrospective for {case_id} — detailed analysis pending.",
        "knowledge_entries": [],
        "skill_candidates": [],
    }
