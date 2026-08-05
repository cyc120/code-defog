"""Orchestrator — coordinates the Case lifecycle across Agents.

Responsibilities (not an Agent itself — see framework Section 5.1):
- Create Case from normalized inputs
- Advance the state machine
- Dispatch tasks to AgentTeams Adapter
- Request approvals at gate states
- Handle verification results → PATCH_REJECTED or RELEASE_APPROVAL
- Handle failures, timeouts, and escalations
"""

from __future__ import annotations

from typing import Any

from .state_machine import (
    is_valid_transition,
    is_terminal,
    requires_approval,
    pending_action_for_state,
    AGENT_ACTIVE_STATES,
)
from .case_context import CaseContext


class Orchestrator:
    """Thin coordinator — delegates to AgentTeams Adapter and StateStore."""

    def __init__(self, store: Any, teams_adapter: Any) -> None:
        self.store = store
        self.teams = teams_adapter

    def advance(self, case_id: str, target_state: str) -> dict[str, Any] | None:
        """Advance a Case to *target_state* if the transition is valid.

        For VERIFYING state, the Verification Agent's result drives the
        next transition: quality gate passed → RELEASE_APPROVAL, failed
        → PATCH_REJECTED."""
        case = self.store.get_case(case_id)
        if case is None:
            return None
        current = case["status"]
        if not is_valid_transition(current, target_state):
            return {"error": f"invalid transition: {current} -> {target_state}"}

        pending = pending_action_for_state(target_state)
        result = self.store.transition_case(case_id, target_state, pending)
        if result is None:
            return None

        # If this state requires Agent work, dispatch and handle result
        if target_state in AGENT_ACTIVE_STATES and self.teams:
            ctx_dict = result if isinstance(result, dict) else {}
            agent_result = self.teams.dispatch_task(case_id, target_state, ctx_dict)

            # Only consume results from a successfully completed Agent run.
            # A 'failed' run (failure_reason set, structured output invalid)
            # must NOT drive any state transition.
            completed = (
                isinstance(agent_result, dict)
                and agent_result.get("status") == "completed"
            )

            # ── REPAIRING: persist patch_ref ───────────────────────────
            if target_state == "REPAIRING" and completed:
                patch_ref = agent_result.get("patch_ref", "")
                if patch_ref:
                    self.store.connection.execute(
                        "UPDATE cases SET patch_ref = ? WHERE case_id = ?",
                        (patch_ref, case_id),
                    )
                    self.store.connection.commit()

            # ── Verification gate: drive next transition ──────────────
            if target_state == "VERIFYING" and isinstance(agent_result, dict):
                qg_error = agent_result.get("quality_gate_error")
                # Gate execution error (timeout, OSError, etc.) → escalate
                if qg_error:
                    self.store.transition_case(case_id, "ESCALATED")
                elif not completed:
                    # Adapter reported failure (invalid output / runtime error)
                    # → escalate rather than silently pausing at VERIFYING
                    self.store.transition_case(case_id, "ESCALATED")
                elif agent_result.get("quality_gate_passed") is True:
                    patch_ref = agent_result.get("patch_ref") or case.get("patch_ref")
                    if not patch_ref:
                        # No patch reference — cannot issue a release grant
                        self.store.transition_case(case_id, "ESCALATED")
                    else:
                        self.store.connection.execute(
                            "UPDATE cases SET patch_ref = ? WHERE case_id = ?",
                            (patch_ref, case_id),
                        )
                        self.store.connection.commit()
                        self.store.transition_case(case_id, "RELEASE_APPROVAL",
                                                    pending_action_for_state("RELEASE_APPROVAL"))
                elif agent_result.get("quality_gate_passed") is False:
                    self.store.transition_case(case_id, "PATCH_REJECTED")
                # quality_gate_passed is None → unchecked (offline/stub mode);
                # leave the case at VERIFYING for manual handling.

            # Refresh result after any automatic transitions
            result = self.store.get_case(case_id)

        return result

    def on_source_received(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process an incoming source observation."""
        result = self.store.create_or_find_case(payload)
        if result.get("duplicate"):
            return result
        case_id = result.get("case_id")
        if case_id:
            self.advance(case_id, "TRIAGED")
        return result

    def resolve_pending(self) -> list[str]:
        """Promote expired pending sources to independent Cases."""
        return self.store.resolve_pending_sources()
