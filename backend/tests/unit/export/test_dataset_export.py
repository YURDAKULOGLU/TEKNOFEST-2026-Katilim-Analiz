"""Serialization-shape tests for the public dataset export builder."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from katilim_analiz.application.models import CampaignProjection
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    RecordStatus,
)
from katilim_analiz.export import build_public_dataset, render_public_dataset

GENERATED_AT = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _evidence(field_pointer: str, quote: str) -> EvidenceRef:
    digest = hashlib.sha256(quote.encode()).hexdigest()
    return EvidenceRef(
        id=f"evidence:{digest[:12]}",
        field_pointer=field_pointer,
        source_document_id="clean:test:doc-1",
        block_id="block:test:1",
        quote=quote,
        start_char=0,
        end_char=len(quote),
        evidence_sha256=digest,
        status=EvidenceStatus.STATED,
    )


def _projection(
    *,
    record_id: str = "record:test:1",
    campaign_key: str | None = "test:campaign-1",
    status: RecordStatus = RecordStatus.VALIDATED,
    title: str = "Konut Finansmani Kampanyasi",
) -> CampaignProjection:
    quote = "Aylik kar orani %2,89"
    record = CampaignRecord(
        id=record_id,
        version=1,
        source_document_id="clean:test:doc-1",
        observed_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
        data=CampaignData(
            bank_id="ornek-katilim",
            title=title,
            product_family=ProductFamily.FINANCING,
            campaign_type=CampaignType.FINANCING_RATE,
            summary="Ornek finansman kampanyasi",
        ),
        evidence=[_evidence("/title", title), _evidence("/rates/0", quote)],
        extraction=ExtractionMetadata(
            method=ExtractionMethod.MANUAL,
            extractor_version="human-verified-intake/1.0.0",
            schema_version="campaign-data/1.0",
            started_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
            completed_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
        ),
        status=status,
        validation_issues=["human_verified"],
        record_sha256="a" * 64,
    )
    return CampaignProjection(
        record=record,
        campaign_key=campaign_key,
        bank_name="Ornek Katilim Bankasi A.S.",
        source_url="https://www.ornekkatilim.com.tr/kampanyalar/konut",
        source_title=title,
    )


def test_dataset_shape_carries_provenance_quotes_and_source_urls() -> None:
    dataset = build_public_dataset(
        [_projection()], dataset_version="1.0.0", generated_at=GENERATED_AT
    )

    assert dataset.schema_version == "1.0"
    assert dataset.dataset_id == "katilim-analiz-public-dataset"
    assert dataset.dataset_version == "1.0.0"
    assert dataset.generated_at == GENERATED_AT
    assert dataset.record_count == 1

    record = dataset.records[0]
    assert record.campaign_key == "test:campaign-1"
    assert record.bank_id == "ornek-katilim"
    assert record.bank_name == "Ornek Katilim Bankasi A.S."
    assert record.title == "Konut Finansmani Kampanyasi"
    assert record.product_family == "financing"
    assert record.campaign_type == "financing_rate"
    assert str(record.source_url).startswith("https://www.ornekkatilim.com.tr/")
    assert record.extraction.method == "manual"
    assert record.extraction.extractor_version == "human-verified-intake/1.0.0"
    assert record.extraction.schema_version == "campaign-data/1.0"
    assert [fact.field_pointer for fact in record.facts] == ["/title", "/rates/0"]
    assert record.facts[1].quote == "Aylik kar orani %2,89"
    assert all(fact.evidence_status == "stated" for fact in record.facts)
    assert all(len(fact.evidence_sha256) == 64 for fact in record.facts)
    assert record.data["bank_id"] == "ornek-katilim"


def test_only_validated_records_are_exported() -> None:
    dataset = build_public_dataset(
        [
            _projection(record_id="record:test:1", campaign_key="test:a"),
            _projection(
                record_id="record:test:2",
                campaign_key="test:b",
                status=RecordStatus.NEEDS_REVIEW,
            ),
            _projection(
                record_id="record:test:3",
                campaign_key="test:c",
                status=RecordStatus.REJECTED,
            ),
        ],
        dataset_version="1.0.0",
        generated_at=GENERATED_AT,
    )
    assert dataset.record_count == 1
    assert [record.record_id for record in dataset.records] == ["record:test:1"]


def test_records_are_sorted_deterministically_by_campaign_key() -> None:
    dataset = build_public_dataset(
        [
            _projection(record_id="record:test:2", campaign_key="test:b"),
            _projection(record_id="record:test:1", campaign_key="test:a"),
            _projection(record_id="record:test:3", campaign_key=None),
        ],
        dataset_version="1.0.0",
        generated_at=GENERATED_AT,
    )
    assert [record.campaign_key for record in dataset.records] == [
        "record:test:3",
        "test:a",
        "test:b",
    ]


def test_rendered_json_is_parseable_and_stable() -> None:
    projections = [_projection()]
    first = render_public_dataset(
        build_public_dataset(projections, dataset_version="1.0.0", generated_at=GENERATED_AT)
    )
    second = render_public_dataset(
        build_public_dataset(projections, dataset_version="1.0.0", generated_at=GENERATED_AT)
    )
    assert first == second
    assert first.endswith("\n")

    parsed = json.loads(first)
    assert set(parsed) == {
        "schema_version",
        "dataset_id",
        "dataset_version",
        "generated_at",
        "record_count",
        "records",
    }
    assert set(parsed["records"][0]) >= {
        "campaign_key",
        "bank_id",
        "bank_name",
        "title",
        "product_family",
        "campaign_type",
        "source_url",
        "facts",
        "extraction",
    }


def test_generated_at_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        build_public_dataset(
            [], dataset_version="1.0.0", generated_at=datetime(2026, 7, 24, 12, 0)
        )


def test_dataset_version_must_be_semantic() -> None:
    with pytest.raises(ValidationError):
        build_public_dataset([], dataset_version="v1", generated_at=GENERATED_AT)


def test_cli_parses_dataset_export_arguments() -> None:
    from katilim_analiz.cli import build_parser, parse_export_as_of

    arguments = build_parser().parse_args(
        [
            "dataset-export",
            "--output",
            "artifacts/public-dataset.json",
            "--dataset-version",
            "1.2.3",
            "--as-of",
            "2026-07-24T12:00:00+03:00",
        ]
    )
    assert arguments.command == "dataset-export"
    assert arguments.dataset_version == "1.2.3"
    as_of = parse_export_as_of(arguments.as_of)
    assert as_of is not None and as_of.utcoffset() is not None
    assert parse_export_as_of(None) is None
    with pytest.raises(ValueError, match="timezone"):
        parse_export_as_of("2026-07-24T12:00:00")
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_export_as_of("not-a-datetime")
