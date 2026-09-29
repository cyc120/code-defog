#!/usr/bin/env python3
"""Score a manually adjudicated review report against the pilot gold set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON file {path}: {exc}") from exc


def score(dataset: dict[str, Any], report_record: dict[str, Any], adjudication: dict[str, Any]) -> dict[str, Any]:
    if not all(isinstance(item, dict) for item in (dataset, report_record, adjudication)):
        raise ValueError("dataset, report, and adjudication roots must be JSON objects")
    report = report_record.get("report", report_record)
    if not isinstance(report, dict):
        raise ValueError("report root must be a JSON object")
    findings = report.get("findings") if isinstance(report, dict) else None
    if not isinstance(findings, list):
        raise ValueError("report must contain a findings array (directly or under 'report')")

    gold = dataset.get("gold_findings")
    if not isinstance(gold, list) or not gold:
        raise ValueError("benchmark must contain at least one gold finding")
    if any(not isinstance(item, dict) or not isinstance(item.get("case_id"), str) or not item["case_id"] for item in gold):
        raise ValueError("each gold finding needs a non-empty string case_id")
    gold_ids = {item["case_id"] for item in gold}
    if len(gold_ids) != len(gold):
        raise ValueError("gold finding case_id values must be present and unique")

    decisions = adjudication.get("finding_decisions")
    if not isinstance(decisions, list):
        raise ValueError("adjudication must contain a finding_decisions array")
    seen_indices: set[int] = set()
    matched_cases: set[str] = set()
    counts = {"true_positive": 0, "false_positive": 0, "unverified": 0}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("each finding decision must be an object")
        index = decision.get("finding_index")
        outcome = decision.get("result")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(findings):
            raise ValueError(f"finding_index must refer to a report finding: {index!r}")
        if index in seen_indices:
            raise ValueError(f"finding_index {index} is adjudicated more than once")
        seen_indices.add(index)
        if outcome not in counts:
            raise ValueError("result must be true_positive, false_positive, or unverified")
        if outcome == "true_positive":
            case_id = decision.get("case_id")
            if case_id not in gold_ids:
                raise ValueError(f"true_positive needs a known gold case_id: {case_id!r}")
            if case_id in matched_cases:
                raise ValueError(f"gold case {case_id} was matched more than once")
            matched_cases.add(case_id)
        elif decision.get("case_id"):
            raise ValueError("case_id is only valid for a true_positive decision")
        counts[outcome] += 1

    if seen_indices != set(range(len(findings))):
        missing = sorted(set(range(len(findings))) - seen_indices)
        raise ValueError(f"adjudicate every report finding; missing indices: {missing}")

    false_negatives = len(gold_ids - matched_cases)
    adjudicated = counts["true_positive"] + counts["false_positive"]
    review_metadata = report.get("review")
    if not isinstance(review_metadata, dict):
        review_metadata = {}
    return {
        "report_id": adjudication.get("report_id") or review_metadata.get("id", "unknown"),
        "benchmark": dataset.get("name", "unnamed"),
        "maturity": dataset.get("maturity", "unknown"),
        "gold_positive_cases": len(gold_ids),
        "true_positives": counts["true_positive"],
        "false_negatives": false_negatives,
        "false_positives": counts["false_positive"],
        "unverified_findings": counts["unverified"],
        "adjudication_coverage": len(seen_indices) / len(findings) if findings else 1.0,
        "precision_among_adjudicated": counts["true_positive"] / adjudicated if adjudicated else None,
        "recall_on_pilot_gold_set": counts["true_positive"] / len(gold_ids),
        "false_discovery_proportion_among_adjudicated": counts["false_positive"] / adjudicated if adjudicated else None,
        "clean_change_controls": len(dataset.get("clean_change_controls", [])),
        "limitations": dataset.get("notes", []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="Skill or Code Defog report JSON")
    parser.add_argument("--adjudication", required=True, type=Path, help="Human decisions for each finding")
    parser.add_argument("--dataset", type=Path, default=Path("cases/benchmark.json"))
    parser.add_argument("--output", type=Path, help="Optional path to save score JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = score(load_json(args.dataset), load_json(args.report), load_json(args.adjudication))
    except ValueError as exc:
        print(f"evaluation input error: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
