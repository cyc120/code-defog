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
import threading
from pathlib import Path
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
        # Serialises Agent dispatch per Case so two concurrent intake/approval
        # calls cannot double-dispatch the same active state (two agent_runs).
        self._dispatch_locks: dict[str, threading.Lock] = {}
        self._dispatch_locks_guard = threading.Lock()
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

    def _dispatch_lock(self, case_id: str) -> threading.Lock:
        """Return the per-Case dispatch lock (bounded: never evicted)."""
        with self._dispatch_locks_guard:
            lock = self._dispatch_locks.get(case_id)
            if lock is None:
                lock = threading.Lock()
                self._dispatch_locks[case_id] = lock
            return lock

    def _sandbox_ref_allowed(self, sandbox_ref: str) -> bool:
        """True when *sandbox_ref* resolves inside the Store sandbox root.

        A sandbox path is a future subprocess execution target (quality gate),
        so it must never come from the model unchecked."""
        if not sandbox_ref or not self.store.path:
            return False
        try:
            target = Path(sandbox_ref).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        sandbox_root = (Path(self.store.path).parent / "sandboxes").resolve()
        return target == sandbox_root or sandbox_root in target.parents

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
            if isinstance(result, dict) and "error" in result:
                # A concurrent approval/intake changed the state between our
                # read and the store's locked transition: treat as terminal
                # for this call rather than feeding an error dict into the
                # agent context (which would crash CaseContext.from_dict).
                return result

        # If this state requires Agent work, dispatch and handle result
        if target_state in AGENT_ACTIVE_STATES and self.harness:
            with self._dispatch_lock(case_id):
                # Re-read under the lock: a concurrent caller may already have
                # dispatched this state and moved the Case on.
                locked_case = self.store.get_case(case_id)
                if locked_case is not None and locked_case["status"] != target_state:
                    return locked_case
                result = self.store.get_case(case_id) or result
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
            # Only a Store-validated repair (the controlled repair tool,
            # i.e. repair_mode == demo_sandbox) may supply patch_ref and
            # sandbox_repository_ref; an LLM-claimed repair must never
            # anchor a later release grant.
            if target_state == "REPAIRING" and completed:
                repair_trusted = ctx_dict.get("repair_mode") == "demo_sandbox"
                patch_ref = agent_result.get("patch_ref", "") if repair_trusted else ""
                sandbox_ref = agent_result.get("sandbox_repository_ref", "") if repair_trusted else ""
                if repair_trusted and sandbox_ref and not self._sandbox_ref_allowed(sandbox_ref):
                    # LLM-claimed or escaping sandbox path: never persist it.
                    self.store.transition_case(case_id, "ESCALATED")
                    return self.store.get_case(case_id)
                if patch_ref and sandbox_ref:
                    self.store.set_patch_context(case_id, patch_ref, sandbox_ref)
                    # The repair result owns the next state. Verification will
                    # receive the persisted sandbox_ref, not the source path.
                    return self.advance(case_id, "VERIFYING")
                # A completed repair without a valid patch is a no-op that
                # must not leave the case parked at REPAIRING forever.
                self.store.transition_case(case_id, "ESCALATED")
                return self.store.get_case(case_id)

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
                    # Deterministic-only release: a gate verdict is trusted
                    # only when the case carries a Store-persisted sandbox_ref
                    # (set by set_patch_context from the controlled repair).
                    # Without one, the verdict is the LLM's self-assessment
                    # and must not drive RELEASE_APPROVAL.
                    sandbox_ref = result.get("sandbox_ref") or case.get("sandbox_ref")
                    patch_ref = case.get("patch_ref")
                    if not sandbox_ref or not patch_ref:
                        # No Store-validated patch anchor — cannot issue a
                        # release grant; leave at VERIFYING for manual handling.
                        pass
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

    def recover_interrupted(self) -> list[str]:
        """Crash recovery for in-flight work after a daemon restart.

        Marks agent_runs that were left ``running`` as failed and escalates
        Cases sitting in Agent-active states (TRIAGED/DIAGNOSED/REPAIRING/
        VERIFYING) so a human decides whether to resume them.  Approval
        states (PLAN_APPROVAL/RELEASE_APPROVAL) and terminal states are
        untouched.  Returns the recovered case ids."""
        recovered: list[str] = []
        stale_runs = self.store.connection.execute(
            "SELECT run_id, case_id FROM agent_runs WHERE status = 'running'"
        ).fetchall()
        for row in stale_runs:
            self.store.finish_agent_run(
                row["run_id"], "failed",
                json.dumps({"failure_reason": "interrupted by daemon restart",
                            "status": "failed"}, ensure_ascii=False),
            )
        for case_id in self.store.list_active_case_ids():
            case = self.store.get_case(case_id)
            if case is None or case["status"] not in AGENT_ACTIVE_STATES:
                continue
            self.store.transition_case(case_id, "ESCALATED")
            recovered.append(case_id)
        return recovered

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
