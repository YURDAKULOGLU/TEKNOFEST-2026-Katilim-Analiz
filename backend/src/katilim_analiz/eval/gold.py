"""Deterministic gold-set evaluation of the extraction layer, per field.

The harness answers "is extractor X better than extractor Y?" with numbers.
It loads the versioned gold dataset (``datasets/gold/extraction/<version>``),
runs the production ``ExtractionPipeline`` over each example's synthetic
``CleanDocument``, and scores every field the pending model decision cares
about: product_family, campaign_type, rates (kind/period/term_months), terms,
financing_amounts, fees, rewards, and customer_segments.

Expected-null is first-class: a gold field whose expected value is ``null`` (or
an empty list) states that the honest answer is abstention, and the harness
scores abstention accuracy separately from precision/recall.  The default
profile is rules-only and runs fully offline; a model profile can be selected
through the existing ``Settings``/``ModelProfile`` mechanism.

Run from ``backend/``:

    uv run python -m katilim_analiz.eval.gold
    uv run python -m katilim_analiz.eval.gold --profile workstation
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from katilim_analiz.config import ModelProfile, Settings
from katilim_analiz.contracts import CampaignType, CleanDocument, ProductFamily, SourceBlock
from katilim_analiz.extraction.pipeline import (
    ExtractionPipeline,
    ExtractionResult,
    pipeline_from_settings,
)

GOLD_SCHEMA_VERSION = "1.0"
DEFAULT_DATASET_SUBDIR = Path("datasets") / "gold" / "extraction" / "v0.1"

#: Fixed clock so candidate digests and validity normalization are stable in CI.
_REFERENCE_TIME = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

#: The fields the harness scores, in report order.
SCORED_LIST_FIELDS = (
    "rates",
    "terms",
    "financing_amounts",
    "fees",
    "rewards",
    "customer_segments",
)
SCORED_SCALAR_FIELDS = ("product_family", "campaign_type")
SCORED_FIELDS = SCORED_SCALAR_FIELDS + SCORED_LIST_FIELDS

_BLOCK_KINDS = frozenset({"heading", "paragraph", "list_item", "table", "metadata", "other"})


class GoldDatasetError(ValueError):
    """The gold dataset on disk is missing, tampered with, or malformed."""


@dataclass(frozen=True, slots=True)
class GoldExample:
    example_id: str
    category: str
    bank_id: str
    canonical_url: str
    blocks: tuple[tuple[str, str], ...]
    expected_outcome: str
    expected: dict[str, Any]
    notes: str


@dataclass(frozen=True, slots=True)
class GoldDataset:
    dataset_id: str
    dataset_version: str
    examples: tuple[GoldExample, ...]

    def counts_by_category(self) -> dict[str, int]:
        counts: Counter[str] = Counter(example.category for example in self.examples)
        return dict(sorted(counts.items()))


@dataclass(frozen=True, slots=True)
class FieldScore:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    abstain_expected: int = 0
    abstain_correct: int = 0

    @property
    def precision(self) -> float | None:
        denominator = self.true_positives + self.false_positives
        return None if denominator == 0 else self.true_positives / denominator

    @property
    def recall(self) -> float | None:
        denominator = self.true_positives + self.false_negatives
        return None if denominator == 0 else self.true_positives / denominator

    @property
    def abstention_accuracy(self) -> float | None:
        if self.abstain_expected == 0:
            return None
        return self.abstain_correct / self.abstain_expected


@dataclass(frozen=True, slots=True)
class ExampleResult:
    example_id: str
    category: str
    expected_outcome: str
    actual_outcome: str
    missing: tuple[tuple[str, str], ...]
    spurious: tuple[tuple[str, str], ...]

    @property
    def clean(self) -> bool:
        return (
            self.expected_outcome == self.actual_outcome and not self.missing and not self.spurious
        )


@dataclass(frozen=True, slots=True)
class GoldReport:
    dataset_id: str
    dataset_version: str
    profile: str
    example_count: int
    outcome_correct: int
    field_scores: dict[str, FieldScore] = field(default_factory=dict)
    example_results: tuple[ExampleResult, ...] = ()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldDatasetError(message)


def load_gold_dataset(dataset_dir: Path) -> GoldDataset:
    """Load and integrity-check one versioned gold dataset directory."""

    manifest_path = dataset_dir / "manifest.json"
    _require(manifest_path.is_file(), f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(
        manifest.get("schema_version") == GOLD_SCHEMA_VERSION,
        "unsupported gold manifest schema_version",
    )
    examples_path = dataset_dir / str(manifest["examples_path"])
    _require(examples_path.is_file(), f"missing examples file: {examples_path}")
    declared = {item["path"]: item["sha256"] for item in manifest.get("content_files", [])}
    _require(
        manifest["examples_path"] in declared,
        "manifest does not declare a sha256 for its examples file",
    )
    actual_sha = _sha256_file(examples_path)
    _require(
        declared[manifest["examples_path"]] == actual_sha,
        "examples file sha256 does not match the manifest "
        f"(expected {declared[manifest['examples_path']]}, found {actual_sha})",
    )

    examples: list[GoldExample] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(
        examples_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        example = _parse_example(raw, line_number)
        _require(
            example.example_id not in seen_ids,
            f"duplicate example_id: {example.example_id}",
        )
        seen_ids.add(example.example_id)
        examples.append(example)
    _require(
        len(examples) == int(manifest["example_count"]),
        "manifest example_count does not match the examples file",
    )
    return GoldDataset(
        dataset_id=str(manifest["dataset_id"]),
        dataset_version=str(manifest["dataset_version"]),
        examples=tuple(examples),
    )


def _parse_example(raw: dict[str, Any], line_number: int) -> GoldExample:
    context = f"examples.jsonl line {line_number}"
    for key in ("example_id", "category", "bank_id", "canonical_url", "blocks", "expected"):
        _require(key in raw, f"{context}: missing key {key}")
    blocks: list[tuple[str, str]] = []
    _require(bool(raw["blocks"]), f"{context}: blocks must not be empty")
    for block in raw["blocks"]:
        _require(
            block.get("kind") in _BLOCK_KINDS,
            f"{context}: unknown block kind {block.get('kind')!r}",
        )
        _require(bool(str(block.get("text", "")).strip()), f"{context}: empty block text")
        blocks.append((block["kind"], block["text"]))
    expected = raw["expected"]
    outcome = expected.get("outcome")
    _require(outcome in {"candidate", "abstained"}, f"{context}: invalid expected outcome")
    for field_name in SCORED_FIELDS:
        _require(field_name in expected, f"{context}: expected is missing field {field_name}")
    if outcome == "abstained":
        _require(
            all(not expected[name] for name in SCORED_FIELDS),
            f"{context}: an abstained example must expect every field null or empty",
        )
    return GoldExample(
        example_id=str(raw["example_id"]),
        category=str(raw["category"]),
        bank_id=str(raw["bank_id"]),
        canonical_url=str(raw["canonical_url"]),
        blocks=tuple(blocks),
        expected_outcome=str(outcome),
        expected=expected,
        notes=str(raw.get("notes", "")),
    )


def document_from_example(example: GoldExample) -> CleanDocument:
    """Build the CleanDocument-shaped input the pipeline consumes."""

    blocks = []
    for index, (kind, text) in enumerate(example.blocks):
        blocks.append(
            SourceBlock(
                id=f"{example.example_id}-block-{index}",
                ordinal=index,
                kind=kind,  # type: ignore[arg-type]
                text=text,
                locator=f"main > {kind}:nth-of-type({index + 1})",
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
    canonical = "\0".join(block.text_sha256 for block in blocks)
    return CleanDocument(
        id=f"gold-doc-{example.example_id}",
        fetch_artifact_id=f"gold-fetch-{example.example_id}",
        bank_id=example.bank_id,
        canonical_url=example.canonical_url,
        title=None,
        cleaned_at=_REFERENCE_TIME,
        cleaner_version="gold-harness/1",
        clean_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        language="tr",
        blocks=blocks,
    )


def _decimal_key(value: Any) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _optional_decimal_key(value: Any) -> str | None:
    return None if value is None else _decimal_key(value)


def _expected_items(example: GoldExample, field_name: str) -> tuple[str, ...]:
    """Canonicalize one expected field into comparable item keys."""

    expected = example.expected[field_name]
    if field_name in SCORED_SCALAR_FIELDS:
        return () if expected is None else (str(expected),)
    items: list[str] = []
    for item in expected:
        if field_name == "rates":
            key = _rate_key(
                item["value_percent"], item["kind"], item["period"], item.get("term_months")
            )
        elif field_name == "terms":
            key = _term_key(item.get("minimum_months"), item["maximum_months"])
        elif field_name == "financing_amounts":
            key = _amount_key(item["amount"], item["currency"])
        elif field_name == "fees":
            key = _fee_key(
                item["kind"],
                item["basis"],
                item["waived"],
                item.get("amount"),
                item.get("rate_percent"),
            )
        elif field_name == "rewards":
            key = _reward_key(
                item["kind"],
                item.get("amount"),
                item.get("points"),
                item.get("rate_percent"),
            )
        else:
            key = str(item)
        items.append(key)
    return tuple(items)


def _rate_key(value_percent: Any, kind: str, period: str, term_months: int | None) -> str:
    term = "-" if term_months is None else str(term_months)
    return f"%{_decimal_key(value_percent)} kind={kind} period={period} term={term}"


def _term_key(minimum_months: int | None, maximum_months: int) -> str:
    minimum = "-" if minimum_months is None else str(minimum_months)
    return f"months={minimum}..{maximum_months}"


def _amount_key(amount: Any, currency: str) -> str:
    return f"{_decimal_key(amount)} {currency}"


def _fee_key(
    kind: str,
    basis: str,
    waived: bool,
    amount: Any,
    rate_percent: Any,
) -> str:
    amount_key = _optional_decimal_key(amount) or "-"
    rate_key = _optional_decimal_key(rate_percent) or "-"
    return f"kind={kind} basis={basis} waived={waived} amount={amount_key} rate%={rate_key}"


def _reward_key(kind: str, amount: Any, points: Any, rate_percent: Any) -> str:
    amount_key = _optional_decimal_key(amount) or "-"
    points_key = _optional_decimal_key(points) or "-"
    rate_key = _optional_decimal_key(rate_percent) or "-"
    return f"kind={kind} amount={amount_key} points={points_key} rate%={rate_key}"


def _predicted_items(result: ExtractionResult) -> dict[str, tuple[str, ...]]:
    """Canonicalize one pipeline result into the same per-field item keys."""

    empty: dict[str, tuple[str, ...]] = {name: () for name in SCORED_FIELDS}
    if result.candidate is None:
        return empty
    data = result.candidate.data
    predicted = dict(empty)
    if data.product_family is not ProductFamily.UNKNOWN:
        predicted["product_family"] = (data.product_family.value,)
    if data.campaign_type is not CampaignType.UNKNOWN:
        predicted["campaign_type"] = (data.campaign_type.value,)
    predicted["rates"] = tuple(
        _rate_key(rate.value_percent, rate.kind.value, rate.period.value, rate.term_months)
        for rate in data.rates
    )
    predicted["terms"] = tuple(
        _term_key(term.minimum_months, term.maximum_months) for term in data.terms
    )
    predicted["financing_amounts"] = tuple(
        _amount_key(money.amount, money.currency) for money in data.financing_amounts
    )
    predicted["fees"] = tuple(
        _fee_key(
            fee.kind.value,
            fee.basis.value,
            fee.waived,
            None if fee.money is None else fee.money.amount,
            None if fee.rate is None else fee.rate.value_percent,
        )
        for fee in data.fees
    )
    predicted["rewards"] = tuple(
        _reward_key(
            reward.kind.value,
            None if reward.money is None else reward.money.amount,
            reward.points,
            None if reward.rate is None else reward.rate.value_percent,
        )
        for reward in data.rewards
    )
    predicted["customer_segments"] = tuple(data.comparison_context.customer_segment_keys)
    return predicted


@dataclass(slots=True)
class _MutableScore:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    abstain_expected: int = 0
    abstain_correct: int = 0

    def frozen(self) -> FieldScore:
        return FieldScore(
            true_positives=self.true_positives,
            false_positives=self.false_positives,
            false_negatives=self.false_negatives,
            abstain_expected=self.abstain_expected,
            abstain_correct=self.abstain_correct,
        )


async def evaluate_gold_dataset(
    dataset: GoldDataset,
    pipeline: ExtractionPipeline,
    *,
    profile: str = ModelProfile.RULES_ONLY.value,
) -> GoldReport:
    """Run the pipeline over every gold example and score each field."""

    scores: dict[str, _MutableScore] = {name: _MutableScore() for name in SCORED_FIELDS}
    example_results: list[ExampleResult] = []
    outcome_correct = 0
    for example in dataset.examples:
        document = document_from_example(example)
        result = await pipeline.extract(document)
        actual_outcome = result.outcome.value
        if actual_outcome == example.expected_outcome:
            outcome_correct += 1
        predicted = _predicted_items(result)
        missing: list[tuple[str, str]] = []
        spurious: list[tuple[str, str]] = []
        for field_name in SCORED_FIELDS:
            # Set semantics: an identical value stated twice on the page is one
            # fact, so repeated extractions of the same canonical item neither
            # help nor hurt.
            expected_keys = set(_expected_items(example, field_name))
            predicted_keys = set(predicted[field_name])
            score = scores[field_name]
            score.true_positives += len(expected_keys & predicted_keys)
            for key in sorted(predicted_keys - expected_keys):
                score.false_positives += 1
                spurious.append((field_name, key))
            for key in sorted(expected_keys - predicted_keys):
                score.false_negatives += 1
                missing.append((field_name, key))
            if not expected_keys:
                score.abstain_expected += 1
                if not predicted_keys:
                    score.abstain_correct += 1
        example_results.append(
            ExampleResult(
                example_id=example.example_id,
                category=example.category,
                expected_outcome=example.expected_outcome,
                actual_outcome=actual_outcome,
                missing=tuple(missing),
                spurious=tuple(spurious),
            )
        )
    return GoldReport(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        profile=profile,
        example_count=len(dataset.examples),
        outcome_correct=outcome_correct,
        field_scores={name: score.frozen() for name, score in scores.items()},
        example_results=tuple(example_results),
    )


def _ratio(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:.3f}"


def render_report(report: GoldReport, *, show_mismatches: bool = True) -> str:
    """Render the plain-text evaluation table the team reads in CI logs."""

    lines = [
        f"Gold extraction evaluation: {report.dataset_id} "
        f"v{report.dataset_version} | profile={report.profile} | "
        f"examples={report.example_count}",
        f"Outcome (candidate/abstained) agreement: {report.outcome_correct}/{report.example_count}",
        "",
        f"{'field':<20} {'TP':>4} {'FP':>4} {'FN':>4} {'prec':>6} {'rec':>6} {'abst-acc':>9}",
    ]
    for field_name in SCORED_FIELDS:
        score = report.field_scores[field_name]
        abstention = (
            "      n/a"
            if score.abstention_accuracy is None
            else f"{score.abstention_accuracy:.3f} ({score.abstain_correct}"
            f"/{score.abstain_expected})"
        )
        lines.append(
            f"{field_name:<20} {score.true_positives:>4} {score.false_positives:>4} "
            f"{score.false_negatives:>4} {_ratio(score.precision):>6} "
            f"{_ratio(score.recall):>6} {abstention:>9}"
        )
    total_tp = sum(score.true_positives for score in report.field_scores.values())
    total_fp = sum(score.false_positives for score in report.field_scores.values())
    total_fn = sum(score.false_negatives for score in report.field_scores.values())
    overall_precision = None if total_tp + total_fp == 0 else total_tp / (total_tp + total_fp)
    overall_recall = None if total_tp + total_fn == 0 else total_tp / (total_tp + total_fn)
    lines.append(
        f"{'OVERALL (micro)':<20} {total_tp:>4} {total_fp:>4} {total_fn:>4} "
        f"{_ratio(overall_precision):>6} {_ratio(overall_recall):>6}"
    )
    clean = sum(1 for item in report.example_results if item.clean)
    lines.append("")
    lines.append(f"Examples fully clean: {clean}/{report.example_count}")
    if show_mismatches:
        mismatched = [item for item in report.example_results if not item.clean]
        if mismatched:
            lines.append("Mismatches:")
            for item in mismatched:
                if item.actual_outcome != item.expected_outcome:
                    lines.append(
                        f"  {item.example_id}: outcome expected "
                        f"{item.expected_outcome}, got {item.actual_outcome}"
                    )
                for field_name, key in item.missing:
                    lines.append(f"  {item.example_id}: missing {field_name}: {key}")
                for field_name, key in item.spurious:
                    lines.append(f"  {item.example_id}: spurious {field_name}: {key}")
    return "\n".join(lines)


def _find_default_dataset_dir() -> Path:
    """Walk up from this module to the repository's gold dataset directory."""

    for parent in Path(__file__).resolve().parents:
        candidate = parent / DEFAULT_DATASET_SUBDIR
        if candidate.is_dir():
            return candidate
    raise GoldDatasetError(
        f"could not locate {DEFAULT_DATASET_SUBDIR} above {Path(__file__).resolve()}"
    )


def _build_pipeline(profile: ModelProfile) -> ExtractionPipeline:
    if profile is ModelProfile.RULES_ONLY:
        return ExtractionPipeline(model_enabled=False, clock=lambda: _REFERENCE_TIME)
    settings = Settings(model_profile=profile)
    return pipeline_from_settings(settings, clock=lambda: _REFERENCE_TIME)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="katilim_analiz.eval.gold",
        description="Evaluate the extraction layer against the gold dataset.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Gold dataset directory (default: datasets/gold/extraction/v0.1 in the repo).",
    )
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in ModelProfile],
        default=ModelProfile.RULES_ONLY.value,
        help="Model profile; rules_only (default) needs no live model and runs offline.",
    )
    parser.add_argument(
        "--no-mismatches",
        action="store_true",
        help="Print only the score table, without per-example mismatch lines.",
    )
    args = parser.parse_args(argv)
    dataset_dir = args.dataset if args.dataset is not None else _find_default_dataset_dir()
    dataset = load_gold_dataset(dataset_dir)
    profile = ModelProfile(args.profile)
    pipeline = _build_pipeline(profile)
    report = asyncio.run(evaluate_gold_dataset(dataset, pipeline, profile=profile.value))
    print(render_report(report, show_mismatches=not args.no_mismatches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
