"""Command-line entrypoint for the versioned WP-070 evaluation run."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.comparison_eval import evaluate_comparisons
from evals.dataset_validation import validate_gold_rows, validate_split_manifest
from evals.loading import DatasetError, load_json, load_jsonl, load_jsonl_index
from evals.normalization_eval import evaluate_normalization
from evals.runner import (
    EvaluationGate,
    GateStatus,
    evaluate_classification,
    evaluate_coverage,
    evaluate_extraction,
    evaluate_schema_evidence,
)
from evals.security_eval import evaluate_security

_CORE_FIELDS = (
    "/data/title",
    "/data/product_family",
    "/data/campaign_type",
    "/data/rates",
    "/data/financing_amounts",
    "/data/terms",
    "/data/fees",
    "/data/rewards",
    "/data/validity",
    "/data/customer_segments",
    "/data/eligibility_conditions",
)


def _predictions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    return load_jsonl_index(path, id_field="example_id")


def _security_predictions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    return load_jsonl_index(path, id_field="case_id")


def _gate_dict(gate: EvaluationGate) -> dict[str, Any]:
    return {
        "pointer": gate.pointer,
        "status": gate.status.value,
        "summary": gate.summary,
        "details": gate.details,
    }


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# WP-070 Evaluation Summary",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Overall status: **{result['overall_status']}**",
        f"- Generated at: `{result['generated_at']}`",
        "",
        "| Gate | Status | Summary |",
        "|---|---|---|",
    ]
    for gate in result["gates"]:
        lines.append(f"| {gate['pointer']} | {gate['status']} | {gate['summary']} |")
    lines.extend(
        [
            "",
            "## Dataset diagnostics",
            "",
            f"- Gold examples: {result['dataset']['gold_examples']}",
            f"- Human-verified gold examples: {result['dataset']['verified_gold_examples']}",
            f"- Cross-split leaks: {len(result['dataset']['cross_split_leaks'])}",
            "",
            "`insufficient_data` is not a pass. Fine-tuning remains unauthorized until held-out gold coverage and all declared gates are complete.",
            "",
        ]
    )
    return "\n".join(lines)


def run(arguments: argparse.Namespace) -> int:
    coverage = load_json(arguments.coverage)
    registry = load_json(arguments.registry)
    gold_rows = load_jsonl(arguments.gold)
    validate_gold_rows(gold_rows)
    splits = load_json(arguments.splits)
    leaks = validate_split_manifest(gold_rows, splits)
    gold = {str(row["example_id"]): row for row in gold_rows}
    predictions = _predictions(arguments.predictions)
    normalization_cases = load_jsonl(arguments.normalization)
    comparison_payload = load_json(arguments.comparisons)
    security_cases = load_jsonl(arguments.security)
    security_predictions = _security_predictions(arguments.security_predictions)
    unknown_prediction_ids = sorted(set(predictions) - set(gold))
    if unknown_prediction_ids:
        raise DatasetError(
            f"predictions contain unknown example IDs: {unknown_prediction_ids[:10]}"
        )
    security_case_ids = {str(case.get("case_id", "")) for case in security_cases}
    unknown_security_ids = sorted(set(security_predictions) - security_case_ids)
    if unknown_security_ids:
        raise DatasetError(
            f"security predictions contain unknown case IDs: {unknown_security_ids[:10]}"
        )
    if not isinstance(registry, dict) or not isinstance(registry.get("banks"), list):
        raise DatasetError("bank registry is malformed")
    expected_bank_ids = [str(row["id"]) for row in registry["banks"]]
    if not isinstance(comparison_payload, dict) or not isinstance(
        comparison_payload.get("cases"), list
    ):
        raise DatasetError("comparison cases are malformed")

    gates = [
        evaluate_coverage(coverage, expected_bank_ids=expected_bank_ids),
        evaluate_normalization(normalization_cases),
        evaluate_extraction(
            gold,
            predictions,
            fields=_CORE_FIELDS,
            minimum_examples=20,
            minimum_field_support=5,
            threshold=0.90,
        ),
        evaluate_schema_evidence(gold, predictions, minimum_examples=20),
        evaluate_classification(
            gold,
            predictions,
            minimum_examples=20,
            minimum_classes=4,
            minimum_class_support=3,
            threshold=0.90,
        ),
        evaluate_comparisons(comparison_payload["cases"]),
        evaluate_security(security_cases, security_predictions),
    ]
    statuses = {gate.status for gate in gates}
    if GateStatus.FAIL in statuses:
        overall = GateStatus.FAIL
    elif GateStatus.INSUFFICIENT_DATA in statuses:
        overall = GateStatus.INSUFFICIENT_DATA
    else:
        overall = GateStatus.PASS
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    result = {
        "schema_version": "1.0",
        "run_id": f"wp070-{generated_at.replace(':', '').replace('+00:00', 'Z')}",
        "generated_at": generated_at,
        "overall_status": overall.value,
        "threshold_notice": "Project acceptance goals; not external-standard mandates.",
        "fine_tuning_authorized": False,
        "dataset": {
            "gold_examples": len(gold_rows),
            "verified_gold_examples": sum(
                1
                for row in gold_rows
                if isinstance(row.get("human_review"), dict)
                and row["human_review"].get("status") == "verified"
            ),
            "prediction_examples": len(predictions),
            "security_prediction_cases": len(security_predictions),
            "cross_split_leaks": list(leaks),
        },
        "gates": [_gate_dict(gate) for gate in gates],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path = arguments.summary or arguments.output.with_suffix(".md")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "summary": str(summary_path),
                "status": overall.value,
            }
        )
    )
    return 0 if overall is GateStatus.PASS or arguments.allow_incomplete else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the versioned evaluation suite")
    run_parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/registry/bddk-participation-banks-2026-07-18.json"),
    )
    run_parser.add_argument(
        "--coverage", type=Path, default=Path("datasets/coverage/2026-07-18.json")
    )
    run_parser.add_argument(
        "--gold", type=Path, default=Path("datasets/gold/v0.1/examples.jsonl")
    )
    run_parser.add_argument(
        "--splits", type=Path, default=Path("datasets/gold/v0.1/splits.json")
    )
    run_parser.add_argument(
        "--normalization",
        type=Path,
        default=Path("datasets/normalization/v1/cases.jsonl"),
    )
    run_parser.add_argument(
        "--comparisons", type=Path, default=Path("datasets/comparison/v1/cases.json")
    )
    run_parser.add_argument(
        "--security",
        type=Path,
        default=Path("datasets/security/v1/prompt-injection-cases.jsonl"),
    )
    run_parser.add_argument("--predictions", type=Path)
    run_parser.add_argument("--security-predictions", type=Path)
    run_parser.add_argument(
        "--output", type=Path, default=Path("evals/results/baseline-2026-07-18.json")
    )
    run_parser.add_argument("--summary", type=Path)
    run_parser.add_argument("--allow-incomplete", action="store_true")
    run_parser.set_defaults(handler=run)
    return parser


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        return int(arguments.handler(arguments))
    except DatasetError as exc:
        parser.error(str(exc))
    return 2
