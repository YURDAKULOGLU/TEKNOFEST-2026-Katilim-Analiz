"""Small, explicit metric primitives with missing distinct from numeric zero."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Counts:
    true_positive: int
    false_positive: int
    false_negative: int
    support: int

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        precision = self.precision
        recall = self.recall
        return (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "support": self.support,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass(frozen=True, slots=True)
class FieldReport:
    per_field: Mapping[str, Counts]
    macro_precision: float
    macro_recall: float
    macro_f1: float


@dataclass(frozen=True, slots=True)
class ClassificationReport:
    per_class: Mapping[str, Counts]
    supported_classes: tuple[str, ...]
    macro_precision: float
    macro_recall: float
    macro_f1: float


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def field_report(
    *,
    gold: Mapping[str, Mapping[str, object]],
    predicted: Mapping[str, Mapping[str, object]],
    fields: Sequence[str],
) -> FieldReport:
    """Score exact canonical field values with wrong values counting FP and FN."""

    per_field: dict[str, Counts] = {}
    for field in fields:
        true_positive = false_positive = false_negative = support = 0
        for example_id, gold_fields in gold.items():
            gold_value = gold_fields.get(field)
            predicted_value = predicted.get(example_id, {}).get(field)
            gold_present = gold_value is not None
            predicted_present = predicted_value is not None
            support += int(gold_present)
            if (
                gold_present
                and predicted_present
                and _canonical(gold_value) == _canonical(predicted_value)
            ):
                true_positive += 1
            elif gold_present and predicted_present:
                false_positive += 1
                false_negative += 1
            elif gold_present:
                false_negative += 1
            elif predicted_present:
                false_positive += 1
        per_field[field] = Counts(
            true_positive=true_positive,
            false_positive=false_positive,
            false_negative=false_negative,
            support=support,
        )
    supported = [metric for metric in per_field.values() if metric.support > 0]
    return FieldReport(
        per_field=per_field,
        macro_precision=_mean([metric.precision for metric in supported]),
        macro_recall=_mean([metric.recall for metric in supported]),
        macro_f1=_mean([metric.f1 for metric in supported]),
    )


def classification_report(
    *, gold: Mapping[str, str], predicted: Mapping[str, str]
) -> ClassificationReport:
    """Compute one-vs-rest macro metrics over the union of observed labels."""

    labels = tuple(sorted(set(gold.values()) | set(predicted.values())))
    supported = tuple(sorted(set(gold.values())))
    per_class: dict[str, Counts] = {}
    for label in labels:
        true_positive = sum(
            1
            for example_id, value in gold.items()
            if value == label and predicted.get(example_id) == label
        )
        false_positive = sum(
            1
            for example_id, value in predicted.items()
            if value == label and gold.get(example_id) != label
        )
        false_negative = sum(
            1
            for example_id, value in gold.items()
            if value == label and predicted.get(example_id) != label
        )
        support = sum(1 for value in gold.values() if value == label)
        per_class[label] = Counts(
            true_positive, false_positive, false_negative, support
        )
    metrics = [per_class[label] for label in labels]
    return ClassificationReport(
        per_class=per_class,
        supported_classes=supported,
        macro_precision=_mean([metric.precision for metric in metrics]),
        macro_recall=_mean([metric.recall for metric in metrics]),
        macro_f1=_mean([metric.f1 for metric in metrics]),
    )
