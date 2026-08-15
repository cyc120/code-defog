"""Retrospective module — generates post-Case review reports and knowledge entries.

Triggered asynchronously when a Case reaches a terminal state (CLOSED /
ROLLED_BACK) via ``StateStore.retrospective_hook``, and manually through
``POST /api/cases/{id}/retrospective``.

The orchestration stays here (read evidence → run pure skills → persist →
publish SSE) while ``retrospective.skills`` holds the deterministic,
pure-function skill implementations.
"""

from __future__ import annotations

import json
from typing import Any

from .skills import case_summarizer, knowledge_extractor, skill_candidates, evidence_indexer

_REPORT_KIND = "retrospective_report"
_MANIFEST_KIND = "knowledge_manifest"


def generate_retrospective(store: Any, case_id: str, force: bool = False) -> dict[str, Any]:
    """Generate a retrospective report + knowledge entries for a Case.

    Idempotent by default: if a report already exists and ``force`` is False,
    returns the existing retrospective without regenerating (prevents a
    ROLLED_BACK → CLOSED double-trigger from duplicating knowledge).

    Returns:
        On success: {case_id, status, summary, report_artifact_id, report_uri,
                     knowledge_manifest_artifact_id, knowledge_entries,
                     skill_candidates, evidence_index, regenerated}
        On failure: {"error": "..."} (case missing / not terminal)
    """
    case = store.get_case(case_id)
    if case is None:
        return {"error": "case not found"}
    status = case.get("status", "")
    if status not in ("CLOSED", "ROLLED_BACK", "ESCALATED"):
        return {"error": f"case not in terminal state: {status}"}

    # Serialise the whole check-then-write per Case so concurrent triggers
    # (ROLLED_BACK→CLOSED double-fire, async hook vs manual HTTP) cannot both
    # pass the idempotency guard and duplicate artifacts / knowledge_records.
    with store.retrospective_lock(case_id):
        return _generate_locked(store, case_id, status, force)


def _generate_locked(
    store: Any, case_id: str, status: str, force: bool,
) -> dict[str, Any]:
    """Run the generate path with the per-Case lock already held."""
    existing = store.get_retrospective(case_id)
    if existing and not force:
        report = existing.get("report") or {}
        manifest = existing.get("manifest") or {}
        return {
            "case_id": case_id,
            "status": status,
            "summary": (
                f"Retrospective already generated (report "
                f"{report.get('artifact_id', '')})."
            ),
            "report_artifact_id": report.get("artifact_id", ""),
            "report_uri": report.get("uri", ""),
            "knowledge_manifest_artifact_id": manifest.get("artifact_id", ""),
            "knowledge_entries": existing.get("knowledge_records") or [],
            "skill_candidates": manifest.get("entries") or [],
            "evidence_index": manifest.get("index") or {},
            "regenerated": False,
        }

    evidence = store.get_case_evidence(case_id)
    if evidence is None:
        # The Case vanished between the state check and the evidence read
        # (concurrent clear/close); fail cleanly instead of crashing skills.
        return {"error": "case evidence unavailable"}
    report_md = case_summarizer(evidence)
    entries = knowledge_extractor(evidence, report_md)
    candidates = skill_candidates(evidence)
    index = evidence_indexer(case_id, evidence)

    report_artifact_id = store.record_artifact(
        case_id, _REPORT_KIND, "retrospective/report.md", report_md.encode("utf-8"),
    )
    manifest_payload = json.dumps(
        {"case_id": case_id, "entries": entries, "index": index},
        ensure_ascii=False,
    ).encode("utf-8")
    manifest_artifact_id = store.record_artifact(
        case_id, _MANIFEST_KIND, "retrospective/knowledge.json", manifest_payload,
    )

    try:
        records = store.record_knowledge_records(case_id, manifest_artifact_id, entries)
    except Exception:
        # Partial failure: the report/manifest artifacts are persisted but
        # knowledge rows are not.  A later retry would hit the idempotency
        # guard (get_retrospective found the report) and never recover, so
        # clean up the artifacts and re-raise.
        try:
            store.delete_artifacts_for_case(case_id, (_REPORT_KIND, _MANIFEST_KIND))
        except Exception:
            pass
        raise

    report_artifact = store.get_case_artifact(case_id, _REPORT_KIND) or {}
    retro = {
        "case_id": case_id,
        "status": status,
        "summary": (
            f"Case {case_id} closed as {status}; {len(entries)} knowledge entries, "
            f"{len(candidates)} skill candidates pending review."
        ),
        "report_artifact_id": report_artifact_id,
        "report_uri": report_artifact.get("uri", ""),
        "knowledge_manifest_artifact_id": manifest_artifact_id,
        "knowledge_entries": records,
        "skill_candidates": candidates,
        "evidence_index": index,
        "regenerated": True,
    }
    store.publish_retrospective(case_id, retro)
    return retro
