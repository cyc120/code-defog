"""Structured CaseContext — the canonical handoff contract between Agents.

All Agents exchange *CaseContext* objects, never raw natural-language
memories.  Every Agent call, tool call, approval, and state transition
carries a *trace_id* for end-to-end auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class CaseContext:
    case_id: str
    status: str
    priority: str = "medium"
    risk_level: str = "low"
    repository_ref: str = ""
    base_commit: str = ""
    source_events: list[dict[str, Any]] = field(default_factory=list)
    normalized_symptoms: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    diagnosis_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    impact_scope: str = ""
    remediation_plan: str = ""
    patch_ref: str = ""
    test_reports: list[dict[str, Any]] = field(default_factory=list)
    release_report: dict[str, Any] = field(default_factory=dict)
    approval_refs: list[str] = field(default_factory=list)
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseContext":
        # Only take fields that exist in the dataclass
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)
