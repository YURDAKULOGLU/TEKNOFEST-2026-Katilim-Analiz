from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from katilim_analiz.application.processing import campaign_observation_key
from katilim_analiz.cli import build_parser
from katilim_analiz.contracts import RecordStatus
from katilim_analiz.demo import load_demo_seed, materialize_campaign
from katilim_analiz.extraction.candidate import validate_candidate

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gold_examples() -> dict[str, dict[str, object]]:
    path = PROJECT_ROOT / "datasets/gold/v0.1/examples.jsonl"
    return {
        item["example_id"]: item
        for line in path.read_text(encoding="utf-8").splitlines()
        if (item := json.loads(line))
    }


def test_default_seed_is_versioned_strict_and_derived_from_declared_sources() -> None:
    seed = load_demo_seed()

    assert seed.dataset_version == "1.0.0"
    assert seed.source_registry_version == "2026-07-18.2"
    assert len(seed.coverage) == 10
    assert len({entry.bank_id for entry in seed.coverage}) == 10
    assert len(seed.campaigns) == 4
    assert {campaign.human_review_status for campaign in seed.campaigns} == {"pending"}

    coverage_path = PROJECT_ROOT / seed.source_coverage.path
    examples_path = PROJECT_ROOT / seed.source_examples.path
    assert _sha256(coverage_path) == seed.source_coverage.sha256
    assert _sha256(examples_path) == seed.source_examples.sha256

    coverage_source = json.loads(coverage_path.read_text(encoding="utf-8"))
    expected_coverage = {
        item["bank_id"]: {
            "bank_id": item["bank_id"],
            "bank_name": item["bank_name"],
            "observed_at": item["observed_at"],
            "status": item["status"],
            "source_count": item["source_count"],
            "source_candidate_count": item["campaign_count"],
            "reason": item["reason"],
        }
        for item in coverage_source["banks"]
    }
    actual_coverage = {item.bank_id: item.model_dump(mode="json") for item in seed.coverage}
    assert actual_coverage == expected_coverage

    gold = _gold_examples()
    assert {item.source_example_id for item in seed.campaigns} == set(gold)
    for campaign in seed.campaigns:
        source = gold[campaign.source_example_id]
        fields = source["fields"]
        assert isinstance(fields, dict)
        assert campaign.bank_id == source["bank_id"]
        assert str(campaign.source_url) == source["source_url"]
        assert campaign.title == fields["/data/title"]
        if campaign.seed_id == "tom-restoran-20260718":
            assert campaign.campaign_type.value == "unknown"
            assert "source_proposal_abstained:campaign_type" in campaign.issues
        else:
            assert campaign.campaign_type.value == fields["/data/campaign_type"]
        assert campaign.source_raw_sha256 == source["source_raw_sha256"]
        assert campaign.source_clean_sha256 == source["source_clean_sha256"]

        source_evidence = {
            (
                (
                    "/data/rates/0"
                    if item["field_pointer"] == "/data/rates"
                    else item["field_pointer"]
                ),
                item["quote"],
                item["block_id"],
                item["start_char"],
                item["end_char"],
                item["evidence_sha256"],
            )
            for item in source["evidence"]
        }
        embedded_evidence = {
            (
                item.field_pointer,
                item.quote,
                item.source_block_id,
                item.source_start_char,
                item.source_end_char,
                item.evidence_sha256,
            )
            for item in campaign.evidence
        }
        assert embedded_evidence == source_evidence


def test_published_demo_schema_accepts_the_versioned_asset() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "datasets/demo/demo-seed.schema.json").read_text(encoding="utf-8")
    )
    instance = json.loads((PROJECT_ROOT / "datasets/demo/v1/seed.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


def test_materialized_campaign_is_locatable_evidence_complete_and_never_verified() -> None:
    seed = load_demo_seed()

    for index, campaign in enumerate(seed.campaigns):
        bundle = materialize_campaign(
            seed,
            campaign,
            bank_name=next(
                item.bank_name for item in seed.coverage if item.bank_id == campaign.bank_id
            ),
            record_id=f"record:unit-demo-{index}",
            version=1,
        )
        assert bundle.candidate is not None
        assert bundle.record is not None
        assert bundle.source.job_id is None
        assert bundle.source.require_scan_run_id() == (
            f"demo-seed:{seed.dataset_id}:{seed.dataset_version}"
        )
        assert bundle.source.require_observation_key() == campaign_observation_key(
            bundle.source.require_scan_run_id(),
            bundle.source.campaign_key,
            str(bundle.source.source_url),
        )
        assert bundle.record.status is RecordStatus.NEEDS_REVIEW
        assert "human_review_pending" in bundle.record.validation_issues
        assert bundle.candidate.data == bundle.record.data
        validate_candidate(bundle.candidate, bundle.clean_document)

        assert bundle.clean_document is not None
        blocks = {block.id: block for block in bundle.clean_document.blocks}
        for evidence in bundle.record.evidence:
            block = blocks[evidence.block_id]
            assert block.text[evidence.start_char : evidence.end_char] == evidence.quote
            assert campaign.source_example_id in block.locator


def test_loader_rejects_unknown_fields_and_oversized_assets(tmp_path: Path) -> None:
    original = json.loads((PROJECT_ROOT / "datasets/demo/v1/seed.json").read_text(encoding="utf-8"))
    original["unexpected"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_demo_seed(invalid)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        load_demo_seed(oversized)


def test_cli_exposes_explicit_demo_dataset_and_registry_paths() -> None:
    arguments = build_parser().parse_args(
        ["demo-seed", "--dataset", "seed.json", "--registry", "registry.json"]
    )

    assert arguments.command == "demo-seed"
    assert arguments.dataset == Path("seed.json")
    assert arguments.registry == Path("registry.json")
