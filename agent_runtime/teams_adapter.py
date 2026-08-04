"""AgentTeams Runtime Adapter — interface + local mock + production AgentScope.

Production mode (P2):
    Wraps the AgentScope SDK.  Creates real Agent objects from
    identities.yaml, dispatches CaseContext as structured tasks,
    and captures Team / Task / Trace evidence.

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
import uuid
from pathlib import Path
from typing import Any, Protocol


_REPO_ROOT = Path(__file__).resolve().parents[1]


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
        from agentscope.tool import Toolkit
        from agentscope.tool._builtin import Read

        # Use DeepSeek model if key is available
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = None
        if api_key:
            from agentscope.credential._deepseek import DeepSeekCredential
            from agentscope.model._deepseek._model import DeepSeekChatModel
            credential = DeepSeekCredential(api_key=api_key)
            model = DeepSeekChatModel(credential=credential, model="deepseek-chat")

        toolkit = Toolkit(tools=[Read()])

        team_id = f"team-{uuid.uuid4().hex[:12]}"
        agents: dict[str, Any] = {}

        for ident in config.get("identities", []):
            agent_id = ident["agent_id"]
            agents[agent_id] = Agent(
                name=ident["name"],
                system_prompt=ident["description"],
                model=model,
                toolkit=toolkit,
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
        """In-process Agent call for local dev and offline demos."""
        import time

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self._store.connection.execute(
            """INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
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
                """UPDATE agent_runs SET status = 'completed', output_ref = ?,
                   finished_at = ? WHERE run_id = ?""",
                (json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        except Exception as exc:
            self._store.connection.execute(
                """UPDATE agent_runs SET status = 'failed', output_ref = ?,
                   finished_at = ? WHERE run_id = ?""",
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
        """Real AgentScope Agent dispatch.

        Builds a structured prompt from the CaseContext, sends it to the
        Agent, and captures the reply + trace evidence.
        """
        import time

        agent = self._agents.get(agent_key)
        if agent is None:
            return {"error": f"Agent '{agent_key}' not initialised — call set_mode('production') first"}

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        trace_id = context.get("trace_id", f"trace-{uuid.uuid4().hex[:16]}")
        run_id = f"run-{uuid.uuid4().hex[:8]}"

        self._store.connection.execute(
            """INSERT INTO agent_runs (run_id, case_id, agent_id, status, trace_id, started_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (run_id, case_id, agent_key, trace_id,
             time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._store.connection.commit()

        # Build task prompt from CaseContext
        prompt = self._build_task_prompt(state, context)

        try:
            from agentscope.message import UserMsg
            import asyncio

            # Agent.reply() is async — run in event loop
            reply = asyncio.run(agent.reply(UserMsg("Orchestrator", prompt)))

            # Extract result
            result_text = ""
            if hasattr(reply, "content"):
                result_text = str(reply.content)[:4000]
            elif hasattr(reply, "text"):
                result_text = str(reply.text)[:4000]
            else:
                result_text = str(reply)[:4000]

            result = {
                "agent": agent_key,
                "case_id": case_id,
                "task_id": task_id,
                "trace_id": trace_id,
                "team_id": self._team["team_id"] if self._team else "",
                "status": "completed",
                "result_summary": result_text[:500],
                "full_result_length": len(result_text),
            }

            self._store.connection.execute(
                """UPDATE agent_runs SET status = 'completed', output_ref = ?,
                   finished_at = ? WHERE run_id = ?""",
                (json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        except Exception as exc:
            result = {"agent": agent_key, "case_id": case_id,
                      "error": str(exc), "task_id": task_id, "trace_id": trace_id}
            self._store.connection.execute(
                """UPDATE agent_runs SET status = 'failed', output_ref = ?,
                   finished_at = ? WHERE run_id = ?""",
                (json.dumps({"error": str(exc)}, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
        self._store.connection.commit()
        return result

    @staticmethod
    def _build_task_prompt(state: str, context: dict[str, Any]) -> str:
        """Build a structured task prompt from the CaseContext."""
        case_id = context.get("case_id", "unknown")
        repo = context.get("repository_ref", "")

        prompts = {
            "TRIAGED": (
                f"Case {case_id}: Triage the following inputs. "
                f"Aggregate, deduplicate, and classify severity. "
                f"Repository: {repo}. Context: {json.dumps(context, ensure_ascii=False)[:2000]}"
            ),
            "DIAGNOSED": (
                f"Case {case_id}: Diagnose the root cause. "
                f"Search relevant code in {repo}, check git history, "
                f"and assess impact scope. "
                f"Evidence: {json.dumps(context.get('evidence_refs', []), ensure_ascii=False)}"
            ),
            "REPAIRING": (
                f"Case {case_id}: Generate a minimal patch. "
                f"Repository: {repo}. Create an isolated branch and apply the fix. "
                f"Diagnosis: {json.dumps(context.get('diagnosis_hypotheses', []), ensure_ascii=False)[:1000]}"
            ),
            "VERIFYING": (
                f"Case {case_id}: Run quality gates on the patch. "
                f"Execute tests, static checks, and canary simulation. "
                f"Return quality_gate_passed: true/false and a recommendation."
            ),
        }
        return prompts.get(state, f"Case {case_id}: Process state {state}. "
                                  f"Context: {json.dumps(context, ensure_ascii=False)[:2000]}")

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
