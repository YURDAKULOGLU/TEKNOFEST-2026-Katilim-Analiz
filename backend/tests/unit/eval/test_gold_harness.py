"""End-to-end rules-only run of the gold evaluation harness.

The suite pins the harness contract, not the current scores: the gold set will
grow, so only structurally guaranteed facts and a couple of known-easy
examples are asserted exactly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from katilim_analiz.eval.gold import (
    SCORED_FIELDS,
    GoldDatasetError,
    GoldReport,
    evaluate_gold_dataset,
    load_gold_dataset,
    render_report,
)
from katilim_analiz.extraction.pipeline import ExtractionPipeline

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DATASET_DIR = _REPO_ROOT / "datasets" / "gold" / "extraction" / "v0.1"
_CLOCK = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _rules_only_pipeline() -> ExtractionPipeline:
    return ExtractionPipeline(model_enabled=False, clock=lambda: _CLOCK)


async def _run_report() -> GoldReport:
    dataset = load_gold_dataset(_DATASET_DIR)
    return await evaluate_gold_dataset(dataset, _rules_only_pipeline())


def test_gold_dataset_loads_with_verified_manifest_sha256() -> None:
    dataset = load_gold_dataset(_DATASET_DIR)

    assert dataset.dataset_id == "katilim-extraction-gold"
    assert len(dataset.examples) >= 20
    counts = dataset.counts_by_category()
    for category in (
        "financing_product_sheet",
        "card_installment_title",
        "branded_points",
        "discount",
        "cashback",
        "fee_percent_trap",
        "ltv_table",
        "ambiguous_abstention",
        "prompt_injection",
    ):
        assert counts.get(category, 0) >= 2, f"category too thin: {category}"


def test_tampered_examples_file_is_rejected(tmp_path: Path) -> None:
    for name in ("manifest.json", "examples.jsonl"):
        (tmp_path / name).write_bytes((_DATASET_DIR / name).read_bytes())
    examples = tmp_path / "examples.jsonl"
    examples.write_text(
        examples.read_text(encoding="utf-8").replace("700", "900"),
        encoding="utf-8",
    )

    with pytest.raises(GoldDatasetError, match="sha256"):
        load_gold_dataset(tmp_path)


async def test_harness_runs_rules_only_end_to_end_and_reports_every_field() -> None:
    report = await _run_report()

    assert report.example_count == len(report.example_results)
    assert set(report.field_scores) == set(SCORED_FIELDS)
    text = render_report(report)
    for field_name in SCORED_FIELDS:
        assert field_name in text
    assert "OVERALL" in text


async def test_known_easy_examples_score_clean_rules_only() -> None:
    report = await _run_report()
    by_id = {item.example_id: item for item in report.example_results}

    # A plain financing sheet and a branded point credit extract exactly.
    for easy in ("gold-fin-001", "gold-points-001"):
        result = by_id[easy]
        assert result.actual_outcome == "candidate"
        assert result.missing == ()
        assert result.spurious == ()

    # A page without an evidence-backed title must abstain entirely.
    abstained = by_id["gold-amb-002"]
    assert abstained.actual_outcome == "abstained"
    assert abstained.clean

    # The fee-percent trap must never leak the fee percentage into rates.
    trap = by_id["gold-fee-001"]
    assert not any(field_name == "rates" for field_name, _key in trap.spurious)

    # The quarantined injection block must not produce any spurious fact.
    for injected in ("gold-inj-001", "gold-inj-002"):
        assert by_id[injected].spurious == ()


async def test_report_is_deterministic_across_runs() -> None:
    first = render_report(await _run_report())
    second = render_report(await _run_report())

    assert first == second


async def test_expected_null_fields_are_scored_as_abstention() -> None:
    report = await _run_report()

    # The dataset deliberately contains expected-null examples for both
    # classifications, so abstention accuracy must be measurable.
    for field_name in ("product_family", "campaign_type"):
        assert report.field_scores[field_name].abstain_expected > 0


def test_manifest_example_count_matches_file() -> None:
    manifest = json.loads((_DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    lines = [
        line
        for line in (_DATASET_DIR / "examples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert manifest["example_count"] == len(lines)
