"""AgentTeams Runtime Adapter — interface + local mock + production AgentScope.

Production mode (P2):
    Wraps the AgentScope SDK.  Creates real Agent objects from
    identities.yaml, dispatches CaseContext as structured tasks,
    and captures Team / Task / Trace evidence.

    Failure detection: iteration exhaustion, empty output, and model
    errors are recorded as 'failed', never as 'completed'.

Mock mode (unit tests / offline dev):
    Loads identities, calls Agent entry functions in-process.

P2 HARD REQUIREMENT (framework Section 6.1):
    Both demo cases must run on the real Runtime and export real
    Team / Task / Trace evidence.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Protocol


_REPO_ROOT = Path(__file__).resolve().parents[1]

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
    # Check for model-level error markers
    lower = stripped.lower()
    if any(marker in lower for marker in ["an error occurred", "i'm sorry, but", "i cannot", "i am unable"]):
        return "model_refusal"
    return None


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from agent output (```json ... ``` or bare {})."""
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


class AgentEntrypoint(Protocol):
    def __call__(self, case_context: dict[str, Any]) -> dict[str, Any]: ...


class AgentTeamsAdapter:
    """AgentScope SDK wrapper with local mock fallback.

    Modes:
        "mock"       — in-process stub calls (unit tests, offline dev)
        "production" — real AgentScope Agent dispatch with LLM
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
        if mode not in ("mock", "production"):
            raise ValueError(f"Unknown mode: {mode}")
        self._mode = mode
        if mode == "production":
            self._init_production_team()

    # ── Production team initialisation ───────────────────────────────────

    def _init_production_team(self) -> None:
        """Create the four DevLoop Agents as real AgentScope Agent objects."""
        import yaml

        identities_path = _REPO_ROOT / "agent_runtime" / "identities.yaml"
        with open(identities_path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)

        from agentscope.agent import Agent
        from agentscope.agent._config import ReActConfig
        from agentscope.tool import Toolkit
        from agentscope.tool._builtin import Read

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = None
        if api_key:
            from agentscope.credential._deepseek import DeepSeekCredential
            from agentscope.model._deepseek._model import DeepSeekChatModel
            credential = DeepSeekCredential(api_key=api_key)
            model = DeepSeekChatModel(credential=credential, model="deepseek-chat")

        read_toolkit = Toolkit(tools=[Read()])
        # Agents that don't need tool access use an empty toolkit for
        # faster, cheaper completion without ReAct loop overhead.
        no_tools = Toolkit(tools=[])

        # Triage and Verification are analysis-only — no tools needed
        toolkit_map = {
            "triage":       no_tools,
            "diagnosis":    read_toolkit,
            "repair":       read_toolkit,
            "verification": no_tools,
        }

        # 5 iterations is enough for a direct JSON response
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

    agent_state_map = {
        "TRIAGED":    "triage",
        "DIAGNOSED":  "diagnosis",
        "REPAIRING":  "repair",
        "VERIFYING":  "verification",
    }

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
            return self._dispatch_production(case_id, state, agent_key, context)

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
            result = agent_fn(context)
            self._store.connection.execute(
                "UPDATE agent_runs SET status = 'completed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps(result, ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        except Exception as exc:
            self._store.connection.execute(
                "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps({"error": str(exc)}, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
            result = {"error": str(exc)}
        self._store.connection.commit()
        return result

    # ── Production dispatch ──────────────────────────────────────────────

    def _dispatch_production(
        self, case_id: str, state: str, agent_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Real AgentScope Agent dispatch with failure detection.

        Returns:
            On success:  {agent, case_id, task_id, trace_id, team_id,
                          status: "completed", structured_output: {...},
                          result_summary: "...", raw_text: "..."}
            On failure:  {agent, case_id, task_id, trace_id, team_id,
                          status: "failed", failure_reason: "...",
                          raw_text: "..."}
        """
        import time

        agent = self._agents.get(agent_key)
        if agent is None:
            return {"error": f"Agent '{agent_key}' not initialised — call set_mode('production') first"}

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        trace_id = context.get("trace_id", f"trace-{uuid.uuid4().hex[:16]}")
        run_id = f"run-{uuid.uuid4().hex[:8]}"

        self._store.connection.execute(
            "INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, case_id, agent_key, trace_id, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._store.connection.commit()

        prompt = self._build_task_prompt(state, context)

        try:
            from agentscope.message import UserMsg
            import asyncio

            reply = asyncio.run(agent.reply(UserMsg("Orchestrator", prompt)))

            # Extract text — reply.content may be a list of TextBlock objects
            raw_text = ""
            content = getattr(reply, "content", None)
            if content is not None:
                if isinstance(content, list):
                    raw_text = "\n".join(
                        getattr(block, "text", str(block))
                        for block in content
                    )
                else:
                    raw_text = str(content)
            elif hasattr(reply, "text"):
                raw_text = str(reply.text)
            else:
                raw_text = str(reply)

            # ── Failure detection ────────────────────────────────────
            failure = _detect_failure(raw_text)
            if failure:
                result = {
                    "agent": agent_key, "case_id": case_id,
                    "task_id": task_id, "trace_id": trace_id,
                    "team_id": self._team["team_id"] if self._team else "",
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
                # ── Try structured JSON extraction ──────────────────
                structured = _extract_json_block(raw_text)
                result = {
                    "agent": agent_key, "case_id": case_id,
                    "task_id": task_id, "trace_id": trace_id,
                    "team_id": self._team["team_id"] if self._team else "",
                    "status": "completed",
                    "structured_output": structured,
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
                "task_id": task_id, "trace_id": trace_id,
                "team_id": self._team["team_id"] if self._team else "",
                "status": "failed",
                "failure_reason": f"exception: {exc}",
            }
            self._store.connection.execute(
                "UPDATE agent_runs SET status = 'failed', output_ref = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps({"error": str(exc)}, ensure_ascii=False),
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
        """Export Team / Task / Trace evidence for a Case."""
        agent_runs = self._store.connection.execute(
            "SELECT * FROM agent_runs WHERE case_id = ? ORDER BY started_at",
            (case_id,),
        ).fetchall()

        return {
            "case_id": case_id,
            "team": self._team,
            "agent_runs": [dict(r) for r in agent_runs],
            "mode": self._mode,
        }
