"""Local Harness for the DevLoop Agent workflow.

The Harness is the single entry point for business-Agent dispatch.  It owns
the explicit task graph, attaches stable dispatch metadata, and delegates
execution to an interchangeable adapter.  It is deliberately local: this is
not evidence of an externally deployed AgentTeams TeamHarness or Matrix run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


class AgentExecutionAdapter(Protocol):
    """The narrow execution port used by the local Harness."""

    def dispatch_task(
        self, case_id: str, state: str, context: dict[str, Any],
    ) -> dict[str, Any]: ...


class ProjectReviewExecutionAdapter(Protocol):
    """Optional read-only execution port for project Review Runs."""

    def dispatch_review_task(
        self, review_run_id: str, task_key: str, context: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HarnessTask:
    """One explicit business-Agent task in the Case workflow."""

    state: str
    agent_id: str
    title: str
    order: int
    handoff_to: str
    boundary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "agent_id": self.agent_id,
            "title": self.title,
            "order": self.order,
            "handoff_to": self.handoff_to,
            "boundary": self.boundary,
        }


# This is the canonical Case task graph.  The execution adapter consumes this
# mapping; it must not maintain a second state-to-Agent dispatch table.
AGENT_TASKS: tuple[HarnessTask, ...] = (
    HarnessTask(
        state="TRIAGED", agent_id="triage", title="分诊证据",
        order=1, handoff_to="DIAGNOSED",
        boundary="归并和分类输入，不修改代码或执行审批。",
    ),
    HarnessTask(
        state="DIAGNOSED", agent_id="diagnosis", title="诊断影响",
        order=2, handoff_to="PLAN_APPROVAL",
        boundary="形成结构化诊断建议，不写入工作树或发布。",
    ),
    HarnessTask(
        state="REPAIRING", agent_id="repair", title="修复执行",
        order=3, handoff_to="VERIFYING",
        boundary="仅允许受控隔离沙箱，不写主分支或执行审批。",
    ),
    HarnessTask(
        state="VERIFYING", agent_id="verification", title="验证发布",
        order=4, handoff_to="RELEASE_APPROVAL",
        boundary="质量门禁只给出建议，不执行真实发布或审批。",
    ),
)
TASK_BY_STATE = {task.state: task for task in AGENT_TASKS}


@dataclass(frozen=True)
class ProjectReviewTask:
    """An Agent-owned task that belongs to a project review, never a Case."""

    task_key: str
    agent_id: str
    title: str
    order: int
    boundary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_key": self.task_key,
            "agent_id": self.agent_id,
            "title": self.title,
            "order": self.order,
            "boundary": self.boundary,
        }


PROJECT_REVIEW_TASKS: tuple[ProjectReviewTask, ...] = (
    ProjectReviewTask(
        task_key="project_review",
        agent_id="project_review",
        title="项目结构审查",
        order=2,
        boundary="只读分析项目结构和已收集元数据；不执行命令、不改代码、不创建审批。",
    ),
)
PROJECT_REVIEW_TASK_BY_KEY = {task.task_key: task for task in PROJECT_REVIEW_TASKS}


class DevLoopHarness:
    """Coordinate every business-Agent task through one local control point.

    The Harness has no approval credential and never advances the Case state.
    Those responsibilities stay with the StateStore and Orchestrator.  This
    keeps a future external workflow bridge replaceable behind
    :class:`AgentExecutionAdapter` without changing Agent business logic.
    """

    harness_id = "devloop-local-harness-v1"

    def __init__(self, executor: AgentExecutionAdapter) -> None:
        self.executor = executor

    def describe(self) -> dict[str, object]:
        """Return a safe, read-only manifest for the operator console."""
        return {
            "id": self.harness_id,
            "kind": "local",
            "execution_mode": getattr(self.executor, "mode", "custom"),
            "agent_count": len(AGENT_TASKS),
            "tasks": [task.to_dict() for task in AGENT_TASKS],
            "review_agent_count": len(PROJECT_REVIEW_TASKS),
            "review_tasks": [task.to_dict() for task in PROJECT_REVIEW_TASKS],
            "approval_boundary": (
                "审批状态不进入 Harness；Harness 不持有 service token、"
                "审批密钥或 approval token。"
            ),
            "runtime_claim": (
                "本地 Harness 统一调度 Mock/AgentScope 适配器；"
                "不代表已部署 AgentTeams TeamHarness、Matrix 或官方 Trace。"
            ),
        }

    def dispatch(
        self, case_id: str, state: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and dispatch one active Case task through the executor."""
        task = TASK_BY_STATE.get(state)
        if task is None:
            return {
                "status": "failed",
                "failure_reason": f"harness has no business-Agent task for state: {state}",
            }
        if not isinstance(context, dict):
            return {
                "status": "failed",
                "failure_reason": "harness context must be an object",
            }
        context_case_id = context.get("case_id")
        if context_case_id and context_case_id != case_id:
            return {
                "status": "failed",
                "failure_reason": "harness context case_id does not match dispatch case_id",
            }

        dispatch_metadata = {
            "harness_id": self.harness_id,
            "harness_task_id": f"htask-{uuid.uuid4().hex[:12]}",
            "harness_task_state": task.state,
            "harness_agent_id": task.agent_id,
        }
        task_context = {**context, **dispatch_metadata}
        try:
            result = self.executor.dispatch_task(case_id, state, task_context)
        except Exception as exc:
            return {
                "status": "failed",
                "failure_reason": f"harness executor exception: {exc}",
                **dispatch_metadata,
            }
        if not isinstance(result, dict):
            return {
                "status": "failed",
                "failure_reason": "harness executor returned a non-object result",
                **dispatch_metadata,
            }
        # Dispatch metadata originates at the Harness and cannot be supplied
        # by an Agent result.
        return {**result, **dispatch_metadata}

    def dispatch_review(
        self, review_run_id: str, task_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch the read-only Project Review Agent through the Harness.

        This intentionally has no ``state`` argument and cannot select any
        Case Agent.  Repair and Verification remain reachable only through the
        existing Case lifecycle and its approval boundaries.
        """
        task = PROJECT_REVIEW_TASK_BY_KEY.get(task_key)
        if task is None:
            return {
                "status": "failed",
                "failure_reason": f"harness has no project-review task: {task_key}",
            }
        if not isinstance(context, dict):
            return {"status": "failed", "failure_reason": "review context must be an object"}
        if context.get("review_run_id") and context["review_run_id"] != review_run_id:
            return {
                "status": "failed",
                "failure_reason": "review context review_run_id does not match dispatch review_run_id",
            }
        dispatch = getattr(self.executor, "dispatch_review_task", None)
        if not callable(dispatch):
            return {
                "status": "failed",
                "failure_reason": "harness executor does not support project-review tasks",
            }
        dispatch_metadata = {
            "harness_id": self.harness_id,
            "harness_task_id": f"hrtask-{uuid.uuid4().hex[:12]}",
            "harness_task_kind": "project_review",
            "harness_task_key": task.task_key,
            "harness_agent_id": task.agent_id,
        }
        try:
            result = dispatch(review_run_id, task_key, {**context, **dispatch_metadata})
        except Exception as exc:
            return {
                "status": "failed",
                "failure_reason": f"harness review executor exception: {exc}",
                **dispatch_metadata,
            }
        if not isinstance(result, dict):
            return {
                "status": "failed",
                "failure_reason": "harness review executor returned a non-object result",
                **dispatch_metadata,
            }
        return {**result, **dispatch_metadata}
