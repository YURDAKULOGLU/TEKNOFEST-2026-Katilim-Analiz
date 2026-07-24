from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from katilim_analiz.application.processing import campaign_observation_key
from katilim_analiz.cli import build_parser
from katilim_analiz.contracts import ExtractionMethod, RecordStatus
from katilim_analiz.extraction.candidate import validate_candidate
from katilim_analiz.intake import (
    load_human_verified_intake,
    materialize_human_verified_campaign,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_PATH = PROJECT_ROOT / "datasets/human-verified/ornek-sablon.json"

BANK_NAMES = {
    "kuveyt-turk": "KUVEYT TURK KATILIM BANKASI A.S.",
    "turkiye-finans": "TURKIYE FINANS KATILIM BANKASI A.S.",
    "hayat-finans": "HAYAT FINANS KATILIM BANKASI A.S.",
    "adil-katilim": "ADIL KATILIM BANKASI A.S.",
}


def _template() -> dict[str, Any]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_template_for_the_four_blocked_banks_parses() -> None:
    intake = load_human_verified_intake(TEMPLATE_PATH)

    assert intake.schema_version == "1.0"
    assert intake.dataset_id == "human-verified-intake"
    assert intake.intake_version == "1.0.0"
    assert {campaign.bank_id for campaign in intake.campaigns} == set(BANK_NAMES)
    assert all("ORNEK-DOLDUR" in campaign.title for campaign in intake.campaigns)
    for campaign in intake.campaigns:
        assert campaign.attested_by
        assert campaign.attested_on is not None
        assert campaign.title_quote == campaign.title


def test_rate_fact_without_quote_is_rejected(tmp_path: Path) -> None:
    payload = _template()
    del payload["campaigns"][0]["rates"][0]["quote"]

    with pytest.raises(ValidationError, match="quote"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_stated_product_family_without_quote_is_rejected(tmp_path: Path) -> None:
    payload = _template()
    payload["campaigns"][0]["product_family_quote"] = None

    with pytest.raises(ValidationError, match="requires product_family_quote"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_quote_that_does_not_name_the_family_is_rejected(tmp_path: Path) -> None:
    payload = _template()
    payload["campaigns"][0]["product_family_quote"] = "ORNEK-DOLDUR alakasiz bir cumle"

    with pytest.raises(ValidationError, match="does not name the attested product family"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_title_quote_must_match_title_verbatim(tmp_path: Path) -> None:
    payload = _template()
    payload["campaigns"][0]["title_quote"] = "ORNEK-DOLDUR farkli bir metin"

    with pytest.raises(ValidationError, match="title_quote must equal"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_bad_enum_value_is_rejected(tmp_path: Path) -> None:
    payload = _template()
    payload["campaigns"][0]["product_family"] = "mortgage"

    with pytest.raises(ValidationError, match="product_family"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_unparseable_rate_quote_is_rejected(tmp_path: Path) -> None:
    payload = _template()
    payload["campaigns"][0]["rates"][0]["quote"] = "ORNEK-DOLDUR oran belirtilmemis"

    with pytest.raises(ValidationError, match="not deterministically parseable"):
        load_human_verified_intake(_write(tmp_path, payload))


def test_loader_rejects_unknown_fields_and_oversized_assets(tmp_path: Path) -> None:
    payload = _template()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_human_verified_intake(_write(tmp_path, payload))

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        load_human_verified_intake(oversized)


def test_materialized_campaign_is_manual_validated_and_evidence_locatable() -> None:
    intake = load_human_verified_intake(TEMPLATE_PATH)

    for index, campaign in enumerate(intake.campaigns):
        bundle = materialize_human_verified_campaign(
            intake,
            campaign,
            bank_name=BANK_NAMES[campaign.bank_id],
            record_id=f"record:unit-human-{index}",
            version=1,
        )
        assert bundle.candidate is not None
        assert bundle.record is not None
        assert bundle.clean_document is not None
        assert bundle.record.status is RecordStatus.VALIDATED
        assert bundle.record.extraction.method is ExtractionMethod.MANUAL
        assert "human_verified" in bundle.record.validation_issues
        assert f"attested_by:{campaign.attested_by}" in bundle.record.validation_issues
        assert f"attested_on:{campaign.attested_on.isoformat()}" in bundle.record.validation_issues
        assert bundle.source.require_scan_run_id() == (
            f"human-intake:{intake.dataset_id}:{intake.intake_version}"
        )
        assert bundle.source.require_observation_key() == campaign_observation_key(
            bundle.source.require_scan_run_id(),
            bundle.source.campaign_key,
            str(bundle.source.source_url),
        )
        validate_candidate(bundle.candidate, bundle.clean_document)

        blocks = {block.id: block for block in bundle.clean_document.blocks}
        for evidence in bundle.record.evidence:
            block = blocks[evidence.block_id]
            assert block.text[evidence.start_char : evidence.end_char] == evidence.quote


def test_materialization_is_deterministic_across_replays() -> None:
    intake = load_human_verified_intake(TEMPLATE_PATH)
    campaign = intake.campaigns[0]

    first = materialize_human_verified_campaign(
        intake, campaign, bank_name=BANK_NAMES[campaign.bank_id], record_id="r", version=1
    )
    second = materialize_human_verified_campaign(
        intake, campaign, bank_name=BANK_NAMES[campaign.bank_id], record_id="r", version=1
    )
    assert first.candidate is not None and second.candidate is not None
    assert first.record is not None and second.record is not None
    assert first.candidate.id == second.candidate.id
    assert first.record.record_sha256 == second.record.record_sha256


def test_cli_exposes_human_verified_ingest_command() -> None:
    arguments = build_parser().parse_args(
        ["human-verified-ingest", "--intake", "intake.json", "--registry", "registry.json"]
    )

    assert arguments.command == "human-verified-ingest"
    assert arguments.intake == Path("intake.json")
    assert arguments.registry == Path("registry.json")
