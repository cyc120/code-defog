# Case state machine for Code CCTV DevLoop.
#
# Valid transitions (v0.5):
#   RECEIVED → TRIAGED → DIAGNOSED → PLAN_APPROVAL
#   PLAN_APPROVAL → REPAIRING | ESCALATED
#   REPAIRING → VERIFYING
#   VERIFYING → PATCH_REJECTED | RELEASE_APPROVAL
#   PATCH_REJECTED → REPAIRING | CLOSED
#   RELEASE_APPROVAL → RELEASED | ESCALATED
#   RELEASED → CLOSED | ROLLED_BACK
#   ROLLED_BACK → CLOSED
#   ESCALATED → CLOSED

from __future__ import annotations

from typing import Optional

# Ordered list for display / progress tracking
ALL_STATES = [
    "RECEIVED",
    "TRIAGED",
    "DIAGNOSED",
    "PLAN_APPROVAL",
    "REPAIRING",
    "VERIFYING",
    "PATCH_REJECTED",
    "RELEASE_APPROVAL",
    "RELEASED",
    "ROLLED_BACK",
    "ESCALATED",
    "CLOSED",
]

TERMINAL_STATES = frozenset({"CLOSED"})

# Valid next states keyed by current state
TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED":           frozenset({"TRIAGED", "ESCALATED"}),
    "TRIAGED":            frozenset({"DIAGNOSED", "ESCALATED"}),
    "DIAGNOSED":          frozenset({"PLAN_APPROVAL", "ESCALATED"}),
    "PLAN_APPROVAL":      frozenset({"REPAIRING", "ESCALATED"}),
    "REPAIRING":          frozenset({"VERIFYING", "ESCALATED"}),
    "VERIFYING":          frozenset({"PATCH_REJECTED", "RELEASE_APPROVAL", "ESCALATED"}),
    "PATCH_REJECTED":     frozenset({"REPAIRING", "CLOSED"}),
    "RELEASE_APPROVAL":   frozenset({"RELEASED", "ESCALATED"}),
    "RELEASED":           frozenset({"CLOSED", "ROLLED_BACK"}),
    "ROLLED_BACK":        frozenset({"CLOSED"}),
    "ESCALATED":          frozenset({"CLOSED", "REPAIRING"}),
    "CLOSED":             frozenset(),
}

# States that require human approval before proceeding
APPROVAL_STATES = frozenset({"PLAN_APPROVAL", "RELEASE_APPROVAL"})

# States where an Agent is actively working
AGENT_ACTIVE_STATES = frozenset({"TRIAGED", "DIAGNOSED", "REPAIRING", "VERIFYING"})


def is_valid_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def next_states(current: str) -> frozenset[str]:
    return TRANSITIONS.get(current, frozenset())


def requires_approval(state: str) -> bool:
    return state in APPROVAL_STATES


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def pending_action_for_state(state: str) -> Optional[str]:
    """Return the pending_action value that should be set when entering this state."""
    mapping = {
        "PLAN_APPROVAL": "approve_plan",
        "RELEASE_APPROVAL": "approve_release",
    }
    return mapping.get(state)
