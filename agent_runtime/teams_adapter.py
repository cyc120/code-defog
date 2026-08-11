"""Local AgentScope execution adapter with an in-process mock fallback.

This historical module path used to call its AgentScope wrapper an
``AgentTeamsAdapter``.  It does not communicate with an AgentTeams control
plane, create AgentTeams workers, or emit AgentTeams-native traces.  The
public implementation is therefore named :class:`AgentScopeExecutionAdapter`.
``AgentTeamsAdapter`` remains only as a source-compatible alias for older
callers and must not be used as evidence of an AgentTeams integration.

AgentScope mode:
    Wraps the AgentScope SDK.  Creates real Agent objects from
    identities.yaml, dispatches CaseContext as structured tasks,
    and captures Team / Task / Trace evidence.

    Failure detection: iteration exhaustion, empty output, model
    errors, and invalid structured output are recorded as 'failed',
    never as 'completed'.

    Trace honesty: the local UUIDs (devloop_task_id, devloop_run_id,
    devloop_trace_id) are application-layer mapping IDs. The real
    runtime identifiers (reply_id, session_id) and the full event
    stream (MODEL_CALL_*, TOOL_CALL_*, REPLY_END, EXCEED_MAX_ITERS)
    are captured from AgentScope's reply_stream as runtime_events —
    these are the runtime-native trace evidence.

Mock mode (unit tests / offline dev):
    Loads identities, calls Agent entry functions in-process.

The AgentScope reply stream is useful local runtime evidence, but it is not
AgentTeams Team / Task / Trace evidence.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from .harness import AGENT_TASKS, PROJECT_REVIEW_TASK_BY_KEY


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_api_key() -> str:
    """Resolve DEEPSEEK_API_KEY, falling back to a project .env file.

    Loads .env only when the env var is missing, so explicit shell
    exports always win and unit tests are unaffected.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    from agent_runtime.envfile import load_dotenv

    load_dotenv()
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _harness_metadata(context: dict[str, Any]) -> dict[str, str]:
    """Copy Harness-issued dispatch fields into durable run output.

    Direct adapter calls remain supported for low-level tests and local
    experiments, so absent fields are intentionally omitted.
    """
    keys = (
        "harness_id",
        "harness_task_id",
        "harness_task_state",
        "harness_task_kind",
        "harness_task_key",
        "harness_agent_id",
    )
    return {
        key: value
        for key in keys
        if isinstance((value := context.get(key)), str) and value
    }

# ── Failure detection patterns ────────────────────────────────────────────
_ITERATION_EXHAUSTED_PATTERNS = [
    r"maximum\s+reasoning-acting\s+iterations?\s+(?:are\s+)?exceeded",
    r"max\s+iteration\s+(?:numbers?\s+)?(?:is\s+)?(?:exceeded|reached)",
    r"react\s+loop\s+(?:stopped|ended|terminated)",
]
_EMPTY_OUTPUT_PATTERNS = [
    r"^\s*$",
    r"^\s*\[\s*\]\s*$",
]


def _detect_failure(text: str) -> str | None:
    """Return a failure reason string if the output indicates a problem,
    or None if the output looks healthy."""
    if not text or not text.strip():
        return "empty_output"
    stripped = text.strip()
    for pattern in _ITERATION_EXHAUSTED_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            return "iteration_exhausted"
    if len(stripped) < 3:
        return "empty_output"
    lower = stripped.lower()
    if any(marker in lower for marker in ["an error occurred", "i'm sorry, but", "i cannot", "i am unable"]):
        return "model_refusal"
    return None


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from agent output (```json ... ``` or bare {})."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── Structured output schema validation ───────────────────────────────────

# Per-agent required fields and their expected types.  A missing field or
# a wrong-typed value marks the run as invalid_structured_output.
_AGENT_SCHEMAS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "triage": {
        "action": str,
        "priority": str,
        "confidence": (int, float),
    },
    "diagnosis": {
        "action": str,
        "hypotheses": list,
        "impact_scope": str,
        "risk_level": str,
    },
    "repair": {
        "action": str,
        "patch_ref": str,
    },
    "verification": {
        "action": str,
        "quality_gate_passed": bool,
    },
}

# Fields that are promoted to the top level of the result so the
# orchestrator can drive state transitions without unwrapping.
_TOP_LEVEL_FIELDS = {
    "triage":       ("priority", "confidence"),
    "diagnosis":    ("risk_level", "impact_scope"),
    "repair":       ("patch_ref", "branch", "files_changed"),
    "verification": ("quality_gate_passed", "recommendation"),
}


def _validate_structured(agent_key: str, structured: dict[str, Any] | None) -> str | None:
    """Validate structured output against the agent schema.

    Returns an error string if invalid, or None on success.
    """
    if not isinstance(structured, dict):
        return "missing or non-object structured output"
    schema = _AGENT_SCHEMAS.get(agent_key, {})
    for field, expected in schema.items():
        if field not in structured:
            return f"missing required field: {field}"
        value = structured[field]
        if expected is bool and isinstance(value, (int, float)) and not isinstance(value, bool):
            # allow 0/1 as bool in LLM output
            if value in (0, 1):
                    structured[field] = bool(value)
                    continue
            return f"field '{field}' must be boolean, got {type(value).__name__}"
        if not isinstance(value, expected):
            return f"field '{field}' wrong type: expected {expected.__name__}, got {type(value).__name__}"
    return None


class AgentEntrypoint(Protocol):
    def __call__(self, case_context: dict[str, Any]) -> dict[str, Any]: ...


class AgentScopeExecutionAdapter:
    """AgentScope SDK wrapper with a local mock fallback.

    Modes:
        "mock"       — in-process stub calls (unit tests, offline dev)
        "agentscope" — real AgentScope Agent dispatch with an LLM

    This adapter is intentionally local.  It must never be presented as a
    substitute for the external AgentTeams runtime.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._mode: str = "mock"
        self._team: dict[str, Any] | None = None
        self._agents: dict[str, Any] = {}

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        # ``production`` was the previous public spelling.  Retain it only
        # for callers upgrading in place; the resulting mode is explicitly
        # AgentScope rather than AgentTeams.
        if mode == "production":
            mode = "agentscope"
        if mode not in ("mock", "agentscope"):
            raise ValueError(f"Unknown mode: {mode}")
        self._mode = mode
        if mode == "agentscope":
            self._init_agentscope_team()

    # ── AgentScope team initialisation ────────────────────────────────────

    def _init_agentscope_team(self) -> None:
        """Create the four DevLoop Agents as real AgentScope Agent objects."""
        import yaml

        identities_path = _REPO_ROOT / "agent_runtime" / "identities.yaml"
        with open(identities_path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)

        from agentscope.agent import Agent
        from agentscope.agent._config import ReActConfig
        from agentscope.tool import Toolkit
        from agentscope.tool._builtin import Read

        api_key = _resolve_api_key()
        model = None
        if api_key:
            from agentscope.credential._deepseek import DeepSeekCredential
            from agentscope.model._deepseek._model import DeepSeekChatModel
            credential = DeepSeekCredential(api_key=api_key)
            model = DeepSeekChatModel(credential=credential, model="deepseek-chat")

        read_toolkit = Toolkit(tools=[Read()])
        no_tools = Toolkit(tools=[])

        toolkit_map = {
            "triage":       no_tools,
            "diagnosis":    no_tools,
            "repair":       no_tools,
            "verification": no_tools,
        }

        react_cfg = ReActConfig(max_iters=5, stop_on_reject=True)

        team_id = f"team-{uuid.uuid4().hex[:12]}"
        agents: dict[str, Any] = {}

        for ident in config.get("identities", []):
            agent_id = ident["agent_id"]
            agents[agent_id] = Agent(
                name=ident["name"],
                system_prompt=ident["description"],
                model=model,
                toolkit=toolkit_map.get(agent_id, read_toolkit),
                react_config=react_cfg,
            )

        self._team = {
            "team_id": team_id,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            "agents": {
                aid: {"agent_id": f"{aid}-{uuid.uuid4().hex[:8]}", "name": ag.name}
                for aid, ag in agents.items()
            },
        }
        self._agents = agents

    # ── Dispatch ─────────────────────────────────────────────────────────

    # The Harness owns the canonical task graph.  The adapter only resolves
    # the Agent identity needed to execute the already-approved task.
    agent_state_map = {task.state: task.agent_id for task in AGENT_TASKS}

    def dispatch_task(
        self, case_id: str, state: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a task to the appropriate Agent based on *state*."""
        agent_key = self.agent_state_map.get(state)
        if agent_key is None:
            return {"error": f"no agent mapped for state: {state}"}

        if self._mode == "mock":
            module_path = f"agents.{agent_key}"
            return self._dispatch_mock(case_id, state, module_path, context)
        else:
            return self._dispatch_agentscope(case_id, state, agent_key, context)

    def dispatch_review_task(
        self, review_run_id: str, task_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the project-review Agent without creating a Case agent run.

        Project reviews have their own durable ``review_task_runs`` ledger.
        The current review Agent is deterministic and local in both runtime
        modes, because it only summarizes bounded browse metadata already
        collected by the read-only driver.  It is never represented as an
        external AgentTeams task or a runtime-native AgentScope trace.
        """
        task = PROJECT_REVIEW_TASK_BY_KEY.get(task_key)
        if task is None:
            return {"status": "failed", "failure_reason": f"unknown review task: {task_key}"}
        if not isinstance(context, dict):
            return {"status": "failed", "failure_reason": "review context must be an object"}
        if context.get("review_run_id") and context["review_run_id"] != review_run_id:
            return {"status": "failed", "failure_reason": "review run id mismatch"}
        try:
            module = importlib.import_module("agents.project_review")
            result = module.run({**context, "_state_store": self._store})
            if not isinstance(result, dict):
                raise RuntimeError("Project Review Agent returned a non-object result")
            result.setdefault("status", "completed")
            if result["status"] not in ("completed", "failed"):
                raise RuntimeError("Project Review Agent returned invalid status")
            return {
                **result,
                "review_run_id": review_run_id,
                "agent": task.agent_id,
                "execution_mode": self._mode,
                "runtime_kind": "local_deterministic_review_agent",
                **_harness_metadata(context),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "failure_reason": str(exc),
                "review_run_id": review_run_id,
                "agent": task.agent_id,
                "execution_mode": self._mode,
                "runtime_kind": "local_deterministic_review_agent",
                **_harness_metadata(context),
            }

    # ── Mock dispatch ────────────────────────────────────────────────────

    def _dispatch_mock(
        self, case_id: str, state: str, module_path: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        import time
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self._store.connection.execute(
            "INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, case_id, module_path, context.get("trace_id", ""),
             time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._store.connection.commit()
        try:
            mod = importlib.import_module(module_path)
            agent_fn = getattr(mod, "run", None)
            if agent_fn is None:
                raise RuntimeError(f"Agent module {module_path} has no run() function")
            # The StateStore is injected only into the in-process mock. It is
            # never serialized into an AgentScope runtime task prompt.
            result = agent_fn({**context, "_state_store": self._store})
            if not isinstance(result, dict):
                raise RuntimeError(f"Agent module {module_path} returned a non-object result")
            # Mock and AgentScope modes expose the same completion contract so
            # the orchestrator never has to infer success from the execution mode.
            result.setdefault("status", "completed")
            if result["status"] not in ("completed", "failed"):
                raise RuntimeError(f"Agent module {module_path} returned invalid status")
            result = {**result, **_harness_metadata(context)}
            self._store.connection.execute(
                "UPDATE agent_runs SET status = ?, output_ref = ?, finished_at = ? WHERE run_id = ?",
                (result["status"], json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        except Exception as exc:
            result = {"status": "failed", "failure_reason": str(exc), **_harness_metadata(context)}
            self._store.connection.execute(
                "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        self._store.connection.commit()
        return result

    # ── AgentScope dispatch ──────────────────────────────────────────────

    def _dispatch_agentscope(
        self, case_id: str, state: str, agent_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """AgentScope Agent dispatch with failure detection and
        structured output validation.

        Returns:
            On success:  {agent, case_id, devloop_task_id, devloop_trace_id,
                          team_id, status: "completed", structured_output: {...},
                          <top-level fields promoted from structured_output>,
                          result_summary, raw_text, runtime_events}
            On failure:  {agent, case_id, devloop_task_id, devloop_trace_id,
                          team_id, status: "failed", failure_reason: "...",
                          raw_text, runtime_events}
        """
        import time

        agent = self._agents.get(agent_key)
        if agent is None:
            return {"error": f"Agent '{agent_key}' not initialised — call set_mode('agentscope') first"}

        # Application-layer IDs (NOT runtime-native identifiers)
        devloop_task_id = f"devtask-{uuid.uuid4().hex[:12]}"
        devloop_trace_id = context.get("trace_id", f"devtrace-{uuid.uuid4().hex[:16]}")
        run_id = f"run-{uuid.uuid4().hex[:8]}"

        self._store.connection.execute(
            "INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, case_id, agent_key, devloop_trace_id, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._store.connection.commit()

        prompt = self._build_task_prompt(state, context)

        try:
            from agentscope.message import UserMsg
            import asyncio

            # Use reply_stream to capture real runtime events (trace evidence)
            runtime_events: list[dict[str, Any]] = []
            runtime_reply_id: str | None = None
            runtime_session_id: str | None = None
            final_message = None

            async def _run_with_events():
                nonlocal runtime_reply_id, runtime_session_id, final_message
                stream = agent.reply_stream(UserMsg("Orchestrator", prompt), yield_final_msg=True)
                async for evt in stream:
                    if hasattr(evt, "type"):
                        event_type = getattr(evt, "type", None)
                        record = {
                            "type": str(event_type),
                            "reply_id": getattr(evt, "reply_id", None),
                            "created_at": getattr(evt, "created_at", None),
                        }
                        if event_type == "MODEL_CALL_END":
                            record["input_tokens"] = getattr(evt, "input_tokens", None)
                            record["output_tokens"] = getattr(evt, "output_tokens", None)
                            record["finished_reason"] = getattr(evt, "finished_reason", None)
                        elif event_type == "REPLY_START":
                            runtime_reply_id = getattr(evt, "reply_id", None)
                            runtime_session_id = getattr(evt, "session_id", None)
                        elif event_type == "REPLY_END":
                            record["finished_reason"] = getattr(evt, "finished_reason", None)
                            record["error"] = getattr(evt, "error", None)
                        runtime_events.append(record)
                    else:
                        # Final message (Msg) when yield_final_msg=True
                        final_message = evt

            asyncio.run(_run_with_events())

            # Extract text from final message
            raw_text = ""
            if final_message is not None:
                content = getattr(final_message, "content", None)
                if content is not None:
                    if isinstance(content, list):
                        raw_text = "\n".join(
                            getattr(block, "text", str(block))
                            for block in content
                        )
                    else:
                        raw_text = str(content)
                elif hasattr(final_message, "text"):
                    raw_text = str(final_message.text)

            # ── Failure detection (text-level) ────────────────────────
            failure = _detect_failure(raw_text)

            # ── Failure detection (runtime event-level) ───────────────
            if failure is None:
                for evt in runtime_events:
                    if evt["type"] == "EXCEED_MAX_ITERS":
                        failure = "iteration_exhausted"
                        break
                    if evt["type"] == "REPLY_END" and evt.get("error"):
                        failure = f"runtime_error: {evt['error']}"
                        break

            # Persist the full runtime event stream as an evidence artifact
            import hashlib
            events_json = json.dumps(runtime_events, ensure_ascii=False, default=str)
            events_sha256 = hashlib.sha256(events_json.encode()).hexdigest()
            artifact_id = self._store.record_artifact(
                case_id, "runtime_events", f"runtime_events/{run_id}.json",
                events_json.encode(),
            )

            base_result = {
                "agent": agent_key, "case_id": case_id,
                "devloop_task_id": devloop_task_id,
                "devloop_trace_id": devloop_trace_id,
                "team_id": self._team["team_id"] if self._team else "",
                "runtime_reply_id": runtime_reply_id,
                "runtime_session_id": runtime_session_id,
                "runtime_event_count": len(runtime_events),
                "runtime_event_types": [e["type"] for e in runtime_events],
                "runtime_events_artifact_id": artifact_id,
                "runtime_events_sha256": events_sha256,
                **_harness_metadata(context),
            }

            if failure:
                result = {
                    **base_result,
                    "status": "failed",
                    "failure_reason": failure,
                    "raw_text": raw_text[:2000],
                }
                self._store.connection.execute(
                    "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                    (json.dumps(result, ensure_ascii=False),
                     time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
                )
            else:
                # ── Structured JSON extraction + validation ──────────
                structured = _extract_json_block(raw_text)
                validation_error = _validate_structured(agent_key, structured)
                if validation_error:
                    result = {
                        **base_result,
                        "status": "failed",
                        "failure_reason": f"invalid_structured_output: {validation_error}",
                        "raw_text": raw_text[:2000],
                    }
                    self._store.connection.execute(
                        "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                        (json.dumps(result, ensure_ascii=False),
                         time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
                    )
                else:
                    # Runtime text is advisory for a mutating demo repair and
                    # for its release decision. The controlled repair tool and
                    # deterministic quality gate provide the authoritative
                    # fields consumed by the orchestrator.
                    authoritative: dict[str, Any] = {}
                    if agent_key == "repair" and context.get("repair_mode") == "demo_sandbox":
                        from agents.repair import run as run_controlled_repair
                        authoritative = run_controlled_repair({
                            **context,
                            "_state_store": self._store,
                        })
                        structured["patch_ref"] = authoritative.get("patch_ref", "")
                    elif agent_key == "verification" and context.get("sandbox_ref"):
                        from agents.verification import run as run_deterministic_verification
                        authoritative = run_deterministic_verification({
                            **context,
                            "_state_store": self._store,
                        })
                        structured["quality_gate_passed"] = authoritative.get("quality_gate_passed")
                        structured["recommendation"] = authoritative.get("recommendation", "escalate")

                    # Promote schema fields to top level for the orchestrator
                    promoted: dict[str, Any] = {}
                    for field in _TOP_LEVEL_FIELDS.get(agent_key, ()):
                        if field in structured:
                            promoted[field] = structured[field]
                    result = {
                        **base_result,
                        "status": "completed",
                        "structured_output": structured,
                        **promoted,
                        **authoritative,
                        "result_summary": raw_text[:500],
                        "raw_text": raw_text[:4000],
                    }
                    self._store.connection.execute(
                        "UPDATE agent_runs SET status = 'completed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                        (json.dumps(result, ensure_ascii=False),
                         time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
                    )
        except Exception as exc:
            result = {
                "agent": agent_key, "case_id": case_id,
                "devloop_task_id": devloop_task_id,
                "devloop_trace_id": devloop_trace_id,
                "team_id": self._team["team_id"] if self._team else "",
                "status": "failed",
                "failure_reason": f"exception: {exc}",
                **_harness_metadata(context),
            }
            self._store.connection.execute(
                "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        self._store.connection.commit()
        return result

    @staticmethod
    def _build_task_prompt(state: str, context: dict[str, Any]) -> str:
        """Build a structured task prompt that requests JSON output."""
        case_id = context.get("case_id", "unknown")
        repo = context.get("repository_ref", "")
        ctx_json = json.dumps(context, ensure_ascii=False)

        base_instruction = (
            "Return your response as a JSON object inside a ```json code block. "
            "Do not include any text outside the JSON block. "
            "The JSON object must include an 'action' field describing what you did.\n\n"
        )

        prompts = {
            "TRIAGED": (
                f"{base_instruction}"
                f"Case {case_id}: Triage the following software defect inputs. "
                f"Aggregate, deduplicate, and classify severity. "
                f"Extract reproduction conditions and build an evidence index.\n\n"
                f"Repository: {repo}\n"
                f"Context: {ctx_json[:2000]}\n\n"
                f"Return JSON with fields: action, priority (low|medium|high|critical), "
                f"classification, symptoms[], evidence_sources[], confidence (0.0-1.0)"
            ),
            "DIAGNOSED": (
                f"{base_instruction}"
                f"Case {case_id}: Diagnose the root cause. "
                f"Search relevant code paths in {repo}, check git history for recent changes, "
                f"and assess impact scope.\n\n"
                f"Evidence refs: {json.dumps(context.get('evidence_refs', []), ensure_ascii=False)}\n"
                f"Context: {ctx_json[:2000]}\n\n"
                f"Return JSON with fields: action, hypotheses[{{description, confidence, code_locations[]}}], "
                f"impact_scope, risk_level (low|medium|high|critical), remediation_strategy"
            ),
            "REPAIRING": (
                f"{base_instruction}"
                f"Case {case_id}: Generate a minimal patch. "
                f"Create or describe the fix for the diagnosed issue.\n\n"
                f"Repository: {repo}\n"
                f"Diagnosis: {json.dumps(context.get('diagnosis_hypotheses', []), ensure_ascii=False)[:1000]}\n\n"
                f"Return JSON with fields: action, patch_ref (string identifying the patch), "
                f"branch, files_changed[], test_results[{{test, passed}}]"
            ),
            "VERIFYING": (
                f"{base_instruction}"
                f"Case {case_id}: Run quality gates on the patch. "
                f"Execute tests, static checks, and assess whether the fix is safe to release.\n\n"
                f"Return JSON with fields: action, quality_gate_passed (true|false), "
                f"checks[{{name, passed, detail}}], recommendation (release|reject|rollback)"
            ),
        }
        return prompts.get(state, (
            f"{base_instruction}"
            f"Case {case_id}: Process state {state}. "
            f"Context: {ctx_json[:2000]}"
        ))

    # ── Trace export ────────────────────────────────────────────────────

    def export_trace(self, case_id: str) -> dict[str, Any]:
        """Export Team / Task / Trace evidence for a Case.

        Note: 'team_id' and 'devloop_task_id' are application-layer
        mapping IDs.  Runtime-native evidence is captured in
        agent_runs.output_ref (runtime_events, runtime_reply_id).
        """
        agent_runs = self._store.connection.execute(
            "SELECT * FROM agent_runs WHERE case_id = ? ORDER BY started_at",
            (case_id,),
        ).fetchall()

        return {
            "case_id": case_id,
            "team": self._team,
            "agent_runs": [dict(r) for r in agent_runs],
            "mode": self._mode,
            "note": (
                "team_id / devloop_task_id / devloop_trace_id are application-layer "
                "mapping IDs. Runtime-native evidence lives in agent_runs.output_ref: "
                "runtime_reply_id, runtime_session_id, runtime_events (MODEL_CALL_*, "
                "TOOL_CALL_*, REPLY_END, EXCEED_MAX_ITERS)."
            ),
        }


# Source compatibility for callers written before the adapter was named
# correctly.  This alias is local AgentScope execution only, never an
# AgentTeams control-plane integration.
AgentTeamsAdapter = AgentScopeExecutionAdapter
