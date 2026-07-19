"""Fail-closed evaluation gates shared by the CLI and unit tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from katilim_analiz.contracts import ExtractionCandidate

from evals.evidence import verify_evidence_binding
from evals.metrics import classification_report, field_report


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class EvaluationGate:
    pointer: str
    status: GateStatus
    summary: str
    details: dict[str, Any]


def evaluate_coverage(
    coverage: Mapping[str, Any], *, expected_bank_ids: Sequence[str]
) -> EvaluationGate:
    rows = coverage.get("banks")
    valid_statuses = {"success", "not_found", "unreachable", "blocked"}
    errors: list[str] = []
    status_counts = {status: 0 for status in sorted(valid_statuses)}
    seen: set[str] = set()
    if not isinstance(rows, list):
        errors.append("banks_not_array")
        rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            errors.append("bank_entry_not_object")
            continue
        bank_id = row.get("bank_id")
        status = row.get("status")
        if not isinstance(bank_id, str) or bank_id in seen:
            errors.append("invalid_or_duplicate_bank_id")
        else:
            seen.add(bank_id)
        if status not in valid_statuses:
            errors.append(f"invalid_status:{bank_id}")
        else:
            status_counts[str(status)] += 1
        if not isinstance(row.get("observed_at"), str):
            errors.append(f"missing_observed_at:{bank_id}")
        if not isinstance(row.get("source_url"), str):
            errors.append(f"missing_source_url:{bank_id}")
        if not isinstance(row.get("reason"), str) or not row.get("reason"):
            errors.append(f"missing_reason:{bank_id}")
    if seen != set(expected_bank_ids):
        errors.append("bank_set_mismatch")
    details: dict[str, Any] = {
        "expected_banks": len(expected_bank_ids),
        "observed_banks": len(seen),
        "status_counts": status_counts,
        "errors": errors,
    }
    return EvaluationGate(
        "EVAL-001",
        GateStatus.PASS if not errors else GateStatus.FAIL,
        "All registry banks have an explicit live coverage state."
        if not errors
        else "Coverage manifest is incomplete or invalid.",
        details,
    )


def evaluate_classification(
    gold: Mapping[str, Mapping[str, object]],
    predictions: Mapping[str, Mapping[str, object]],
    *,
    minimum_examples: int,
    minimum_classes: int,
    minimum_class_support: int,
    threshold: float,
) -> EvaluationGate:
    verified: dict[str, Mapping[str, object]] = {}
    for example_id, row in gold.items():
        review = row.get("human_review")
        if isinstance(review, Mapping) and review.get("status") == "verified":
            verified[example_id] = row
    reports: dict[str, Any] = {}
    coverage_errors: list[str] = []
    f1_values: list[float] = []
    for pointer in ("/data/product_family", "/data/campaign_type"):
        gold_labels = {
            example_id: str(fields[pointer])
            for example_id, row in verified.items()
            if isinstance((fields := row.get("fields")), Mapping)
            and fields.get(pointer) is not None
        }
        predicted_labels = {
            example_id: str(fields[pointer])
            for example_id, row in predictions.items()
            if example_id in verified
            and isinstance((fields := row.get("fields")), Mapping)
            and fields.get(pointer) is not None
        }
        report = classification_report(gold=gold_labels, predicted=predicted_labels)
        reports[pointer] = {
            "macro_precision": report.macro_precision,
            "macro_recall": report.macro_recall,
            "macro_f1": report.macro_f1,
            "supported_classes": list(report.supported_classes),
            "per_class": {
                name: metric.as_dict() for name, metric in report.per_class.items()
            },
        }
        f1_values.append(report.macro_f1)
        if len(report.supported_classes) < minimum_classes:
            coverage_errors.append(f"too_few_classes:{pointer}")
        if any(
            report.per_class[label].support < minimum_class_support
            for label in report.supported_classes
        ):
            coverage_errors.append(f"low_class_support:{pointer}")
    macro_f1 = sum(f1_values) / len(f1_values) if f1_values else 0.0
    details: dict[str, Any] = {
        "verified_examples": len(verified),
        "minimum_examples": minimum_examples,
        "minimum_classes": minimum_classes,
        "minimum_class_support": minimum_class_support,
        "threshold": threshold,
        "macro_f1": macro_f1,
        "coverage_errors": coverage_errors,
        "reports": reports,
    }
    if len(verified) < minimum_examples or coverage_errors:
        return EvaluationGate(
            "EVAL-006",
            GateStatus.INSUFFICIENT_DATA,
            "Classification gold coverage is below the declared minimum.",
            details,
        )
    passed = macro_f1 >= threshold
    return EvaluationGate(
        "EVAL-006",
        GateStatus.PASS if passed else GateStatus.FAIL,
        "Classification threshold met."
        if passed
        else "Classification threshold not met.",
        details,
    )


def evaluate_schema_evidence(
    gold: Mapping[str, Mapping[str, object]],
    predictions: Mapping[str, Mapping[str, object]],
    *,
    minimum_examples: int,
) -> EvaluationGate:
    verified_ids = {
        example_id
        for example_id, row in gold.items()
        if isinstance((review := row.get("human_review")), Mapping)
        and review.get("status") == "verified"
    }
    completed_ids = sorted(verified_ids & set(predictions))
    schema_errors: list[str] = []
    evidence_errors: list[str] = []
    unsupported_assertions = 0
    asserted_fields = 0
    for example_id in completed_ids:
        row = predictions[example_id]
        candidate = row.get("candidate")
        try:
            ExtractionCandidate.model_validate(candidate)
        except ValidationError:
            schema_errors.append(example_id)
        fields = row.get("fields")
        evidence = row.get("evidence")
        blocks = row.get("source_blocks")
        if not isinstance(fields, Mapping):
            schema_errors.append(example_id)
            continue
        evidence_rows = evidence if isinstance(evidence, list) else []
        block_map = blocks if isinstance(blocks, Mapping) else {}
        gold_fields = gold[example_id].get("fields")
        known_gold = gold_fields if isinstance(gold_fields, Mapping) else {}
        for pointer, value in fields.items():
            if value is None:
                continue
            asserted_fields += 1
            if pointer not in known_gold:
                unsupported_assertions += 1
            bindings = [
                binding
                for binding in evidence_rows
                if isinstance(binding, Mapping)
                and binding.get("field_pointer") == pointer
            ]
            if not bindings or not any(
                verify_evidence_binding(str(pointer), binding, block_map).valid
                for binding in bindings
            ):
                evidence_errors.append(f"{example_id}:{pointer}")
    details: dict[str, Any] = {
        "verified_examples": len(verified_ids),
        "completed_examples": len(completed_ids),
        "minimum_examples": minimum_examples,
        "schema_valid_examples": len(completed_ids) - len(set(schema_errors)),
        "schema_errors": sorted(set(schema_errors)),
        "asserted_fields": asserted_fields,
        "evidence_errors": evidence_errors,
        "unsupported_assertions": unsupported_assertions,
    }
    if len(verified_ids) < minimum_examples or len(completed_ids) < len(verified_ids):
        return EvaluationGate(
            "EVAL-005",
            GateStatus.INSUFFICIENT_DATA,
            "Schema/evidence evaluation lacks enough verified completed examples.",
            details,
        )
    passed = not schema_errors and not evidence_errors and unsupported_assertions == 0
    return EvaluationGate(
        "EVAL-005",
        GateStatus.PASS if passed else GateStatus.FAIL,
        "Schema and evidence gate passed."
        if passed
        else "Schema or evidence defect found.",
        details,
    )


def evaluate_extraction(
    gold: Mapping[str, Mapping[str, object]],
    predictions: Mapping[str, Mapping[str, object]],
    *,
    fields: Sequence[str],
    minimum_examples: int,
    minimum_field_support: int,
    threshold: float,
) -> EvaluationGate:
    """Evaluate only human-verified examples and never pass a small set."""

    verified: dict[str, Mapping[str, object]] = {}
    for example_id, row in gold.items():
        human_review = row.get("human_review")
        if (
            isinstance(human_review, Mapping)
            and human_review.get("status") == "verified"
        ):
            verified[example_id] = row
    gold_fields: dict[str, Mapping[str, object]] = {}
    for example_id, row in verified.items():
        fields_value = row.get("fields")
        if isinstance(fields_value, Mapping):
            gold_fields[example_id] = fields_value
    predicted_fields: dict[str, Mapping[str, object]] = {}
    for example_id, row in predictions.items():
        fields_value = row.get("fields")
        if example_id in verified and isinstance(fields_value, Mapping):
            predicted_fields[example_id] = fields_value
    report = field_report(gold=gold_fields, predicted=predicted_fields, fields=fields)
    field_metrics = {
        name: metric.as_dict() for name, metric in report.per_field.items()
    }
    unsupported_assertions = 0
    for example_id, row in predictions.items():
        fields_value = row.get("fields")
        if example_id not in verified or not isinstance(fields_value, Mapping):
            continue
        unsupported_assertions += sum(
            1
            for pointer, value in fields_value.items()
            if value is not None and pointer not in gold_fields.get(example_id, {})
        )
    details: dict[str, Any] = {
        "verified_examples": len(verified),
        "prediction_examples": len(predicted_fields),
        "minimum_examples": minimum_examples,
        "minimum_field_support": minimum_field_support,
        "threshold": threshold,
        "macro_precision": report.macro_precision,
        "macro_recall": report.macro_recall,
        "macro_f1": report.macro_f1,
        "unsupported_assertions": unsupported_assertions,
        "field_metrics": field_metrics,
    }
    low_support = [
        name
        for name, metric in report.per_field.items()
        if metric.support < minimum_field_support
    ]
    if len(verified) < minimum_examples or low_support:
        details["low_support_fields"] = low_support
        return EvaluationGate(
            "EVAL-004",
            GateStatus.INSUFFICIENT_DATA,
            "Verified gold coverage is below the declared minimum.",
            details,
        )
    passed = (
        report.macro_precision >= threshold
        and report.macro_recall >= threshold
        and report.macro_f1 >= threshold
        and unsupported_assertions == 0
    )
    return EvaluationGate(
        "EVAL-004",
        GateStatus.PASS if passed else GateStatus.FAIL,
        "Extraction threshold met." if passed else "Extraction threshold not met.",
        details,
    )
