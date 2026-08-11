"""Structured handoff contract for a read-only project Review Run.

``ReviewContext`` is intentionally separate from ``CaseContext``: a project
review may surface evidence for a Case, but it cannot assume a Case exists or
advance a Case through its approval-controlled state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReviewContext:
    review_run_id: str
    workspace: str
    scope: dict[str, Any] = field(default_factory=dict)
    browse: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_run_id": self.review_run_id,
            "workspace": self.workspace,
            "scope": self.scope,
            "browse": self.browse,
            "trace_id": self.trace_id,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewContext":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})
