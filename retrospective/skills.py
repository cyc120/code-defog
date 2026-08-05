"""Pure-function retrospective skills — deterministic, offline-reproducible.

Each skill takes the structured ``evidence`` dict returned by
``StateStore.get_case_evidence`` and produces a deterministically-derived
artifact.  No LLM calls, no random — the same evidence always yields the
same output, so review reports and knowledge entries are auditable.

Mirrors the framework's §11.1 retrospective/audit skill catalogue:
    case_summarizer   → retrospective report (Markdown)
    knowledge_extractor → [{title, category, content, confidence, tags}]
    evidence_indexer  → {case_id, evidence_tree[], hashes[], trace}
"""

from __future__ import annotations

import json
from typing import Any


def _clean_text(value: Any, limit: int = 1200) -> str:
    """Collapse whitespace and cap length (mirror of store.clean_text).

    Kept local so these skills stay pure functions with no dependency on the
    store module — they operate purely on the evidence dict they receive.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def _short(value: Any, length: int = 12) -> str:
    """Return a lowercase truncated digest-style rendering of a value."""
    text = _clean_text(value, 200)
    return text[:length].lower() if text else "-"


def _clamp(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 2)


def _signals_field(source: dict[str, Any], field: str) -> str:
    """Read a field from a case_sources row's extracted_signals_json."""
    signals_json = source.get("extracted_signals_json")
    if isinstance(signals_json, dict):
        signals = signals_json
    else:
        try:
            signals = json.loads(signals_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    if not isinstance(signals, dict):
        return ""
    return _clean_text(signals.get(field), 80)


def _parse_output_ref(output_ref: Any) -> dict[str, Any]:
    """Best-effort parse of an agent_runs.output_ref JSON column.

    In production mode the adapter writes ``{status, structured_output: {...},
    **promoted, ...}`` where ``action`` / ``hypotheses`` live under
    ``structured_output`` and only a few fields (``_TOP_LEVEL_FIELDS``) are
    promoted to the top level.  Unwrap ``structured_output`` so downstream
    readers see both promoted top-level fields and nested LLM output.
    """
    if not output_ref:
        return {}
    if isinstance(output_ref, dict):
        parsed = output_ref
    else:
        try:
            parsed = json.loads(output_ref)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(parsed, dict):
        return {}
    nested = parsed.get("structured_output")
    if isinstance(nested, dict):
        # Top-level promoted fields win; structured_output fills the rest.
        merged = dict(nested)
        merged.update({k: v for k, v in parsed.items() if k != "structured_output"})
        return merged
    return parsed


# ── case_summarizer ────────────────────────────────────────────────────────

def case_summarizer(evidence: dict[str, Any]) -> str:
    """Build a human-readable retrospective report (Markdown).

    Deterministic rendering of the case's full evidence bundle: sources,
    agent runs, the immutable tool chain (with hashes), approvals, and
    artifacts.  Every field passes through ``clean_text`` so untrusted
    evidence can never inject extra Markdown.
    """
    case = evidence.get("case") or {}
    case_id = _clean_text(case.get("case_id"), 100)
    lines: list[str] = []
    lines.append(f"# Retrospective — {case_id or 'unknown'}")
    lines.append("")
    lines.append(
        "- status: {0} · priority: {1} · risk: {2} · repository: {3} · base_commit: {4}"
        .format(
            _clean_text(case.get("status")),
            _clean_text(case.get("priority")),
            _clean_text(case.get("risk_level")),
            _clean_text(case.get("repository_ref"), 160),
            _clean_text(case.get("base_commit"), 40),
        )
    )
    lines.append(
        "- created: {0} · closed: {1} · patch_ref: {2}".format(
            _clean_text(case.get("created_at")),
            _clean_text(case.get("closed_at")),
            _clean_text(case.get("patch_ref"), 100),
        )
    )
    if case.get("trace_id"):
        lines.append(f"- trace_id: {_clean_text(case.get('trace_id'), 100)}")
    lines.append("")

    sources = evidence.get("sources") or []
    lines.append(f"## 1. Sources ({len(sources)})")
    if sources:
        for src in sources:
            lines.append(
                "- {0} | {1} | {2} | {3} | hash {4}".format(
                    _clean_text(src.get("source_type"), 30),
                    _clean_text(src.get("source_uri"), 120),
                    _clean_text(src.get("association_state"), 20),
                    _clean_text(src.get("received_at")),
                    _short(src.get("content_hash")),
                )
            )
    else:
        lines.append("_none_")
    lines.append("")

    agent_runs = evidence.get("agent_runs") or []
    lines.append(f"## 2. Agent Runs ({len(agent_runs)})")
    for run in agent_runs:
        output = _parse_output_ref(run.get("output_ref"))
        action = _clean_text(output.get("action"), 160) or ""
        summary = f" — {action}" if action else ""
        lines.append(
            "- {0} | {1} | {2} → {3}{4}".format(
                _clean_text(run.get("agent_id"), 30),
                _clean_text(run.get("status"), 20),
                _clean_text(run.get("started_at")),
                _clean_text(run.get("finished_at")) or "…",
                summary,
            )
        )
    lines.append("")

    tool_runs = evidence.get("tool_runs") or []
    lines.append(f"## 3. Tool Chain ({len(tool_runs)})")
    if tool_runs:
        lines.append("| seq | tool | exit | in | out | chain |")
        lines.append("|---|---|---|---|---|---|")
        for run in tool_runs:
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                    run.get("chain_sequence", "-"),
                    _clean_text(run.get("tool_name"), 40),
                    run.get("exit_code", "-"),
                    _short(run.get("input_sha256")),
                    _short(run.get("output_sha256")),
                    _short(run.get("chain_hash")),
                )
            )
    else:
        lines.append("_none_")
    lines.append("")

    approvals = evidence.get("approvals") or []
    lines.append(f"## 4. Approvals ({len(approvals)})")
    for appr in approvals:
        lines.append(
            "- {0} → {1} | approver: {2} | target: {3} | reason: {4}".format(
                _clean_text(appr.get("action"), 30),
                _clean_text(appr.get("decision"), 30),
                _clean_text(appr.get("approver"), 60),
                _clean_text(appr.get("target_ref"), 60),
                _clean_text(appr.get("reason"), 120),
            )
        )
    lines.append("")

    artifacts = evidence.get("artifacts") or []
    lines.append(f"## 5. Artifacts ({len(artifacts)})")
    for art in artifacts:
        lines.append(
            "- {0} | {1} | sha256 {2}".format(
                _clean_text(art.get("kind"), 40),
                _clean_text(art.get("uri"), 120),
                _short(art.get("sha256")),
            )
        )
    lines.append("")

    status = _clean_text(case.get("status"))
    lines.append("## 6. Outcome")
    lines.append(f"Case closed as **{status or 'unknown'}**.")
    return "\n".join(lines) + "\n"


# ── knowledge_extractor ────────────────────────────────────────────────────

def knowledge_extractor(
    evidence: dict[str, Any],
    report_md: str,  # noqa: ARG001 — kept as the framework's §11.1 input contract
) -> list[dict[str, Any]]:
    """Derive reusable knowledge entries deterministically from evidence.

    Returns ``[{title, category, content, confidence, tags}]``.  Confidence
    is derived from structured signals (human approvals = 1.0, deterministic
    gates = 1.0, LLM hypotheses = their stated confidence, escalations = 0.5),
    never guessed.  ``report_md`` is accepted for interface compatibility with
    the framework but the extraction is evidence-driven so it stays
    reproducible offline.
    """
    case = evidence.get("case") or {}
    case_id = _clean_text(case.get("case_id"), 100)
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(title: str, category: str, content: str, confidence: float,
            tags: list[str] | None = None) -> None:
        key = (category, _clean_text(title, 160))
        if key in seen:
            return
        seen.add(key)
        tags_list = tags or []
        # De-duplicate and cap tags
        unique: list[str] = []
        for tag in tags_list:
            cleaned = _clean_text(tag, 60)
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
            if len(unique) >= 5:
                break
        entries.append({
            "title": _clean_text(title, 200),
            "category": _clean_text(category, 60),
            "content": _clean_text(content, 800),
            "confidence": _clamp(confidence),
            "tags": unique,
        })

    # 1. Incident signature (cross-source correlation)
    incident = case.get("incident_signature")
    if incident:
        exception_type = ""
        for src in evidence.get("sources") or []:
            sig = src.get("incident_signature") or ""
            if sig == incident:
                exception_type = _signals_field(src, "exception_type")
                break
        add(
            f"Incident: {_clean_text(incident, 120)}",
            "incident_signature",
            f"Cross-source incident signature {_clean_text(incident, 120)} for {case_id}.",
            0.9,
            tags=["incident_signature", exception_type[:40]],
        )

    # 2. Agent runs → successes / failures / structured insights
    for run in evidence.get("agent_runs") or []:
        agent_id = _clean_text(run.get("agent_id"), 30)
        status = _clean_text(run.get("status"), 20)
        output = _parse_output_ref(run.get("output_ref"))
        action = _clean_text(output.get("action"), 160)
        if status == "completed":
            add(
                f"{agent_id} completed: {action or 'no action'}",
                "agent_run",
                f"Agent {agent_id} completed for {case_id}: {action or 'no action'}.",
                1.0,
                tags=[agent_id, "completed"],
            )
        elif status == "failed":
            error = _clean_text(output.get("error") or output.get("failure_reason"), 160) or "unknown"
            add(
                f"{agent_id} failed: {error}",
                "agent_failure",
                f"Agent {agent_id} failed for {case_id}: {error}.",
                0.5,
                tags=[agent_id, "failed"],
            )
        # Diagnosis hypotheses
        for hypo in output.get("hypotheses") or []:
            if not isinstance(hypo, dict):
                continue
            description = _clean_text(hypo.get("description"), 200)
            if not description:
                continue
            confidence = _clamp(hypo.get("confidence", 0.7))
            add(
                f"Hypothesis: {description[:120]}",
                "root_cause",
                description,
                confidence,
                tags=[agent_id, "hypothesis"],
            )
        # Verification gate result
        gate = output.get("quality_gate_passed")
        if gate is not None:
            passed = "passed" if gate else "failed"
            add(
                f"Quality gate {passed}",
                "quality_gate",
                f"Deterministic quality gate {passed} for {case_id}.",
                1.0,
                tags=["quality_gate", passed],
            )

    # 3. Tool chain (group by chain_sequence)
    tool_runs = evidence.get("tool_runs") or []
    if tool_runs:
        chain_ok = all(run.get("exit_code") == 0 for run in tool_runs)
        tool_names = ", ".join(_clean_text(run.get("tool_name"), 30) for run in tool_runs)
        add(
            f"Tool chain: {tool_names[:120]}",
            "tool_chain",
            f"Executed {len(tool_runs)} immutable tool runs for {case_id}: {tool_names}.",
            0.9 if chain_ok else 0.5,
            tags=[_clean_text(run.get("tool_name"), 30) for run in tool_runs],
        )

    # 4. Approval gates (human decisions already persisted)
    for appr in evidence.get("approvals") or []:
        add(
            f"{_clean_text(appr.get('action'), 30)} → {_clean_text(appr.get('decision'), 30)}",
            "approval_gate",
            f"Human {_clean_text(appr.get('decision'), 30)} on {_clean_text(appr.get('action'), 30)} "
            f"by {_clean_text(appr.get('approver'), 60)}.",
            1.0,
            tags=[_clean_text(appr.get("action"), 30), _clean_text(appr.get("decision"), 30)],
        )

    # 5. Escalation / rejection signals
    status = _clean_text(case.get("status"))
    for appr in evidence.get("approvals") or []:
        if _clean_text(appr.get("decision")) == "rejected":
            add(
                f"Rejected: {_clean_text(appr.get('reason'), 120)}",
                "escalation",
                f"An approval was rejected: {_clean_text(appr.get('reason'), 200)}.",
                0.5,
                tags=["rejected"],
            )
            break
    if status == "ESCALATED":
        add(
            "Case escalated",
            "escalation",
            f"Case {case_id} was escalated for manual handling.",
            0.5,
            tags=["escalated"],
        )

    return entries


# ── skill_candidates ───────────────────────────────────────────────────────

# Map tool names → framework §11.1 skill catalogue ids
_TOOL_SKILL_MAP: dict[str, str] = {
    "quality_gate": "quality_gate",
    "canary_simulator": "canary_simulator",
    "sandbox_copy": "patch_generator",
    "apply_case_a_patch": "patch_generator",
    "git_checkout": "patch_generator",
    "git_blame": "git_blamer",
    "git_blamer": "git_blamer",
    "code_searcher": "code_searcher",
    "grep": "code_searcher",
    "impact_analyzer": "impact_analyzer",
}
_AGENT_SKILL_MAP: dict[str, list[str]] = {
    "triage": ["issue_normalizer", "symptom_extractor", "incident_matcher"],
    "diagnosis": ["code_searcher", "git_blamer", "impact_analyzer"],
    "repair": ["patch_generator", "test_augmenter"],
    "verification": ["quality_gate", "canary_simulator"],
}
_RETROSPECTIVE_SKILLS = [
    "case_summarizer",
    "knowledge_extractor",
    "evidence_indexer",
    "compliance_checker",
]


def skill_candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Propose reusable skill candidates grounded in observed tool/agent use.

    Returns ``[{skill_id, rationale, evidence_count}]``.  Tools and agent
    roles map to the framework §11.1 catalogue; the four retrospective/audit
    skills are always proposed as the natural owners of this pipeline.
    """
    counts: dict[str, int] = {}
    rationales: dict[str, str] = {}

    for run in evidence.get("tool_runs") or []:
        tool = _clean_text(run.get("tool_name"), 40).lower()
        skill = _TOOL_SKILL_MAP.get(tool)
        if skill:
            counts[skill] = counts.get(skill, 0) + 1
            rationales.setdefault(skill, f"used tool {tool}")

    for run in evidence.get("agent_runs") or []:
        agent = _clean_text(run.get("agent_id"), 30)
        for skill in _AGENT_SKILL_MAP.get(agent, []):
            counts[skill] = counts.get(skill, 0) + 1
            rationales.setdefault(skill, f"role {agent}")

    for skill in _RETROSPECTIVE_SKILLS:
        counts.setdefault(skill, 0)
        rationales.setdefault(skill, "retrospective/audit pipeline")

    return [
        {
            "skill_id": _clean_text(skill, 60),
            "rationale": _clean_text(rationales.get(skill, ""), 160),
            "evidence_count": count,
        }
        for skill, count in sorted(counts.items())
    ]


# ── evidence_indexer ───────────────────────────────────────────────────────

def evidence_indexer(case_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Produce an audit-friendly evidence index.

    Mirrors the framework §11.1 ``evidence_indexer`` contract:
    ``{case_id, evidence_tree[], hashes[], trace}``.  ``hashes`` carries the
    immutable tool-run hash chain (input/output/chain) for replay verification.
    """
    case = evidence.get("case") or {}
    sections = ("case", "sources", "agent_runs", "tool_runs", "approvals", "artifacts")

    def _item(row: Any) -> dict[str, Any] | str:
        """Clean a row for the evidence tree: keep every field, scrub values."""
        if not isinstance(row, dict):
            return _clean_text(row, 200)
        return {_clean_text(k, 60): _clean_text(v, 400) for k, v in row.items()}

    evidence_tree = [
        {
            "node": name,
            "items": [
                _item(row)
                for row in (
                    (case.items() if name == "case" else (evidence.get(name) or []))
                )
            ],
        }
        for name in sections
    ]
    hashes = [
        {
            "chain_sequence": run.get("chain_sequence"),
            "tool_name": _clean_text(run.get("tool_name"), 60),
            "input_sha256": run.get("input_sha256"),
            "output_sha256": run.get("output_sha256"),
            "chain_hash": run.get("chain_hash"),
        }
        for run in evidence.get("tool_runs") or []
    ]
    return {
        "case_id": _clean_text(case_id, 100),
        "evidence_tree": evidence_tree,
        "hashes": hashes,
        "trace": {
            "trace_id": _clean_text(case.get("trace_id"), 100),
            "agent_run_count": len(evidence.get("agent_runs") or []),
            "tool_run_count": len(evidence.get("tool_runs") or []),
            "approval_count": len(evidence.get("approvals") or []),
        },
    }
