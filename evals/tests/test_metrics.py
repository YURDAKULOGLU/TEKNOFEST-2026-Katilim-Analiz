from __future__ import annotations

from evals.metrics import classification_report, field_report


def test_wrong_field_value_counts_as_false_positive_and_false_negative() -> None:
    report = field_report(
        gold={"a": {"/data/amount": 100}},
        predicted={"a": {"/data/amount": 200}},
        fields=("/data/amount",),
    )

    metric = report.per_field["/data/amount"]
    assert (metric.true_positive, metric.false_positive, metric.false_negative) == (
        0,
        1,
        1,
    )
    assert metric.f1 == 0.0


def test_missing_is_not_treated_as_explicit_zero() -> None:
    report = field_report(
        gold={"a": {"/data/fee": 0}, "b": {"/data/fee": None}},
        predicted={"a": {"/data/fee": None}, "b": {"/data/fee": 0}},
        fields=("/data/fee",),
    )

    metric = report.per_field["/data/fee"]
    assert (metric.true_positive, metric.false_positive, metric.false_negative) == (
        0,
        1,
        1,
    )


def test_classification_macro_f1_includes_each_supported_gold_class() -> None:
    report = classification_report(
        gold={"a": "cashback", "b": "cashback", "c": "discount"},
        predicted={"a": "cashback", "b": "discount", "c": "discount"},
    )

    assert report.supported_classes == ("cashback", "discount")
    assert report.per_class["cashback"].f1 == 2 / 3
    assert report.per_class["discount"].f1 == 2 / 3
    assert report.macro_f1 == 2 / 3
