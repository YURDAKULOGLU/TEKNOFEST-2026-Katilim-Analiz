from __future__ import annotations

from evals.runner import GateStatus, evaluate_extraction


def test_abstention_is_a_false_negative_not_an_unsupported_assertion() -> None:
    gold = {
        "a": {
            "fields": {"/data/campaign_type": "cashback"},
            "human_review": {"status": "verified"},
        }
    }
    predictions = {"a": {"abstained": True, "fields": {}, "evidence": []}}

    gate = evaluate_extraction(
        gold,
        predictions,
        fields=("/data/campaign_type",),
        minimum_examples=1,
        minimum_field_support=1,
        threshold=0.90,
    )

    assert gate.status is GateStatus.FAIL
    assert gate.details["unsupported_assertions"] == 0
    assert gate.details["field_metrics"]["/data/campaign_type"]["false_negative"] == 1


def test_small_verified_set_never_reports_success() -> None:
    gold = {
        "a": {
            "fields": {"/data/campaign_type": "cashback"},
            "human_review": {"status": "verified"},
        }
    }

    gate = evaluate_extraction(
        gold,
        {},
        fields=("/data/campaign_type",),
        minimum_examples=20,
        minimum_field_support=5,
        threshold=0.90,
    )

    assert gate.status is GateStatus.INSUFFICIENT_DATA
    assert gate.details["verified_examples"] == 1
