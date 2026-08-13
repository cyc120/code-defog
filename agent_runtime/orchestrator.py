"""Orchestrator — coordinates the Case lifecycle across Agents.

Responsibilities (not an Agent itself — see framework Section 5.1):
- Create Case from normalized inputs
- Advance the state machine
- Dispatch tasks to the configured execution adapter
- Request approvals at gate states
- Handle verification results → PATCH_REJECTED or RELEASE_APPROVAL
- Handle failures, timeouts, and escalations
"""

from __future__ import annotations

import json
from typing import Any

from .state_machine import (
    is_valid_transition,
    is_terminal,
    requires_approval,
    pending_action_for_state,
    AGENT_ACTIVE_STATES,
)
from .case_context import CaseContext
from .harness import DevLoopHarness


class Orchestrator:
    """Thin coordinator — delegates to an execution adapter and StateStore."""

    def __init__(self, store: Any, teams_adapter: Any) -> None:
        self.store = store
        # Existing callers may still provide an execution adapter directly.
        # The adapter is wrapped here so every business-Agent invocation goes
        # through the same explicit local task graph.
        if teams_adapter is None:
            self.harness = None
            self.teams = None
        elif hasattr(teams_adapter, "dispatch"):
            self.harness = teams_adapter
            self.teams = getattr(teams_adapter, "executor", None)
        else:
            self.harness = DevLoopHarness(teams_adapter)
            self.teams = teams_adapter

    def _build_agent_context(self, case_id: str, case: dict[str, Any]) -> dict[str, Any]:
        """Hydrate the canonical CaseContext with persisted handoffs.

        Agent output is read back from ``agent_runs`` rather than kept in
        process memory.  A resumed Repair task therefore receives the same
        diagnosis that produced the plan awaiting approval.
        """
        context = CaseContext.from_dict(case)
        evidence = self.store.get_case_evidence(case_id) or {}
        source_events: list[dict[str, Any]] = []
        normalized_symptoms: list[str] = []

        for source in evidence.get("sources", []):
            try:
                signals = json.loads(source.get("extracted_signals_json", "{}"))
            except (TypeError, json.JSONDecodeError):
                signals = {}
            if not isinstance(signals, dict):
                signals = {}

            exception_type = signals.get("exception_type")
            message_pattern = signals.get("message_pattern")
            if exception_type or message_pattern:
                normalized_symptoms.append(
                    ": ".join(part for part in (exception_type, message_pattern) if part)
                )
            source_events.append({
                "observation_id": source.get("observation_id"),
                "source_type": source.get("source_type"),
                "source_uri": source.get("source_uri"),
                "received_at": source.get("received_at"),
                "content_hash": source.get("content_hash"),
                "signals": signals,
            })

        context.source_events = source_events
        context.normalized_symptoms = normalized_symptoms
        context.evidence_refs = [
            artifact["uri"] for artifact in evidence.get("artifacts", [])
            if artifact.get("uri")
        ]

        triage = self.store.get_latest_completed_agent_output(case_id, "triage")
        if triage:
            priority = triage.get("priority")
            if isinstance(priority, str) and priority:
                context.priority = priority

        diagnosis = self.store.get_latest_completed_agent_output(case_id, "diagnosis")
        if diagnosis:
            hypotheses = diagnosis.get("hypotheses")
            if isinstance(hypotheses, list):
                context.diagnosis_hypotheses = hypotheses
            impact_scope = diagnosis.get("impact_scope")
            if isinstance(impact_scope, str):
                context.impact_scope = impact_scope
            risk_level = diagnosis.get("risk_level")
            if isinstance(risk_level, str) and risk_level:
                context.risk_level = risk_level
            remediation = diagnosis.get("remediation_strategy")
            if not isinstance(remediation, str):
                remediation = diagnosis.get("remediation_plan")
            if isinstance(remediation, str):
                context.remediation_plan = remediation

        context_dict = context.to_dict()
        # Repair execution is opt-in and carried as persisted source evidence.
        # A path by itself must never enable a mutating tool.
        for source in source_events:
            repair_mode = source["signals"].get("repair_mode")
            if repair_mode:
                context_dict["repair_mode"] = repair_mode
                break
        return context_dict

    def run_active_state(self, case_id: str) -> dict[str, Any] | None:
        """Resume the Agent assigned to a Case after an external approval."""
        case = self.store.get_case(case_id)
        if case is None:
            return None
        state = case["status"]
        if state not in AGENT_ACTIVE_STATES:
            return {"error": f"case is not in an active Agent state: {state}"}
        return self.advance(case_id, state)

    def advance(self, case_id: str, target_state: str) -> dict[str, Any] | None:
        """Advance a Case to *target_state* if the transition is valid.

        For VERIFYING state, the Verification Agent's result drives the
        next transition: quality gate passed → RELEASE_APPROVAL, failed
        → PATCH_REJECTED."""
        case = self.store.get_case(case_id)
        if case is None:
            return None
        current = case["status"]
        if current == target_state and target_state in AGENT_ACTIVE_STATES:
            # An approval moves the Case into REPAIRING in the StateStore.
            # Resume that active state without inventing a self-transition.
            result = case
        else:
            if not is_valid_transition(current, target_state):
                return {"error": f"invalid transition: {current} -> {target_state}"}
            pending = pending_action_for_state(target_state)
            result = self.store.transition_case(case_id, target_state, pending)
            if result is None:
                return None

        # If this state requires Agent work, dispatch and handle result
        if target_state in AGENT_ACTIVE_STATES and self.harness:
            ctx_dict = self._build_agent_context(case_id, result)
            agent_result = self.harness.dispatch(case_id, target_state, ctx_dict)

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
                    self.store.set_patch_context(
                        case_id,
                        patch_ref,
                        agent_result.get("sandbox_repository_ref", ""),
                    )
                    # The repair result owns the next state. Verification will
                    # receive the persisted sandbox_ref, not the source path.
                    return self.advance(case_id, "VERIFYING")

            # ── Persisted handoffs: Triage → Diagnosis → plan approval ──
            # The adapter writes each completed result to agent_runs before it
            # returns.  The recursive call rebuilds context from that durable
            # evidence, so no Agent-to-Agent decision relies on RAM state.
            if target_state == "TRIAGED" and completed:
                return self.advance(case_id, "DIAGNOSED")

            if target_state == "DIAGNOSED" and completed:
                return self.advance(case_id, "PLAN_APPROVAL")

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
                        self.store.set_patch_ref(case_id, patch_ref)
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
            advanced = self.advance(case_id, "TRIAGED")
            if isinstance(advanced, dict) and "error" not in advanced:
                return advanced
        return result

    def resolve_pending(self) -> list[str]:
        """Promote expired pending sources to independent Cases."""
        return self.store.resolve_pending_sources()
