"""Triage Evidence Agent — aggregates, deduplicates, and classifies inputs.

**P1 STATUS: Stub.**  Real logic (P2) will be driven by the model calling
this Agent's defined tools (issue_normalizer, symptom_extractor, incident_matcher).
"""
# P2: Replace stub with actual model-driven Agent logic.
#      Connect to tools/{issue_normalizer,symptom_extractor,incident_matcher}.py

from typing import Any


def run(context: dict[str, Any]) -> dict[str, Any]:
    """Entry point called by AgentTeams Adapter (or its mock)."""
    case_id = context.get("case_id", "unknown")
    # Stub: in production this would call issue_normalizer, symptom_extractor,
    # and incident_matcher tools via the model.
    return {
        "agent": "triage",
        "case_id": case_id,
        "action": "classified",
        "priority": context.get("priority", "medium"),
        "confidence": 0.85,
        "note": "Triage stub — model-driven classification pending.",
    }
