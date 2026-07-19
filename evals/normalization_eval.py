"""Run versioned Turkish normalization cases against the public domain API."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from katilim_analiz.domain import (
    NormalizationResult,
    normalize_date,
    normalize_money,
    normalize_rate,
    normalize_terms,
)

from evals.runner import EvaluationGate, GateStatus


def _project_value(kind: str, value: object) -> object:
    if kind == "date":
        assert isinstance(value, date)
        return value.isoformat()
    if kind == "term":
        assert isinstance(value, tuple)
        return [
            {
                "minimum_months": item.minimum_months,
                "maximum_months": item.maximum_months,
            }
            for item in value
        ]
    dumped = value.model_dump(mode="json")  # type: ignore[attr-defined]
    if kind == "money":
        return {"amount": dumped["amount"], "currency": dumped["currency"]}
    selected = {"value_percent", "kind", "period", "gross_net_basis"} & set(dumped)
    return {key: dumped[key] for key in selected}


def _run_case(case: Mapping[str, Any]) -> tuple[bool, object, str]:
    kind = case.get("kind")
    raw = case.get("raw")
    kwargs = dict(case.get("kwargs", {}))
    if "reference_date" in kwargs:
        kwargs["reference_date"] = date.fromisoformat(kwargs["reference_date"])
    result: NormalizationResult[Any]
    if kind == "money":
        result = normalize_money(raw, **kwargs)
    elif kind == "rate":
        result = normalize_rate(raw, **kwargs)
    elif kind == "date":
        result = normalize_date(raw, **kwargs)
    elif kind == "term":
        result = normalize_terms(raw)
    else:
        return False, None, "unsupported_case_kind"
    if not result.is_normalized or result.value is None:
        return False, None, result.reason_code
    actual = _project_value(kind, result.value)
    expected = case.get("expected")
    if isinstance(expected, dict) and isinstance(actual, dict):
        actual = {key: actual.get(key) for key in expected}
    return actual == expected, actual, result.reason_code


def evaluate_normalization(
    cases: Sequence[Mapping[str, Any]], *, minimum_cases_per_kind: int = 4
) -> EvaluationGate:
    verified = [case for case in cases if case.get("review_status") == "verified"]
    by_kind = {
        kind: sum(1 for case in verified if case.get("kind") == kind)
        for kind in ("money", "rate", "date", "term")
    }
    outcomes = []
    for case in verified:
        passed, actual, reason = _run_case(case)
        outcomes.append(
            {
                "case_id": case.get("case_id"),
                "kind": case.get("kind"),
                "passed": passed,
                "actual": actual,
                "expected": case.get("expected"),
                "reason_code": reason,
            }
        )
    details: dict[str, Any] = {
        "verified_cases": len(verified),
        "minimum_cases_per_kind": minimum_cases_per_kind,
        "cases_per_kind": by_kind,
        "passed_cases": sum(1 for outcome in outcomes if outcome["passed"]),
        "accuracy": (
            sum(1 for outcome in outcomes if outcome["passed"]) / len(outcomes)
            if outcomes
            else 0.0
        ),
        "outcomes": outcomes,
    }
    if any(count < minimum_cases_per_kind for count in by_kind.values()):
        return EvaluationGate(
            "EVAL-003",
            GateStatus.INSUFFICIENT_DATA,
            "Normalization case coverage is below the per-kind minimum.",
            details,
        )
    passed = all(outcome["passed"] for outcome in outcomes)
    return EvaluationGate(
        "EVAL-003",
        GateStatus.PASS if passed else GateStatus.FAIL,
        "All normalization cases passed."
        if passed
        else "At least one normalization case failed.",
        details,
    )
