from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

from katilim_analiz.application.processing import (
    CoverageObservation,
    PersistenceBundle,
    SourceRequest,
)
from katilim_analiz.config import ModelProfile
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    CleanDocument,
    ComparisonContext,
    CoverageStatus,
    EvidenceRef,
    EvidenceStatus,
    ExtractionCandidate,
    ExtractionMetadata,
    ExtractionMethod,
    FetchArtifact,
    FetchStatus,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    SourceBlock,
    ValidityWindow,
)
from katilim_analiz.runtime.composition import ApiRuntime
from katilim_analiz.storage.repositories import SourceRepository
from katilim_analiz.storage.serialization import canonical_sha256

OBSERVED_AT = datetime(2026, 7, 18, 12, tzinfo=UTC)
AS_OF = datetime(2026, 7, 18, 18, 30, tzinfo=UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manual_bundle(
    *,
    bank_id: str,
    bank_name: str,
    rate: str,
    status: RecordStatus,
) -> PersistenceBundle:
    host = f"{bank_id}.positive-e2e.invalid"
    source_url = f"https://{host}/manual-financing"
    quote = f"%{rate}"
    block_text = f"SENTETİK E2E KAYDI {bank_name}: Aylık finansman kâr oranı {quote}."
    raw_sha256 = _sha256(block_text)
    document_sha256 = _sha256(f"document:{bank_id}:{block_text}")
    document_id = f"clean:{document_sha256}"
    block_id = f"block:e2e:{bank_id}"
    evidence_id = f"evidence:e2e:{bank_id}:rate"
    quote_start = block_text.index(quote)

    fetch = FetchArtifact(
        id=f"fetch:e2e:{bank_id}",
        bank_id=bank_id,
        requested_url=source_url,
        final_url=source_url,
        status=FetchStatus.SUCCESS,
        http_status=200,
        fetched_at=OBSERVED_AT,
        robots_allowed=True,
        content_type="text/html",
        raw_sha256=raw_sha256,
        raw_size_bytes=len(block_text.encode()),
        private_raw_path=f"e2e-only/{raw_sha256}.html",
    )
    block = SourceBlock(
        id=block_id,
        ordinal=0,
        kind="paragraph",
        text=block_text,
        locator="e2e:manual",
        text_sha256=raw_sha256,
    )
    document = CleanDocument(
        id=document_id,
        fetch_artifact_id=fetch.id,
        bank_id=bank_id,
        canonical_url=source_url,
        title=f"Sentetik E2E Finansman {bank_name}",
        cleaned_at=OBSERVED_AT + timedelta(seconds=1),
        cleaner_version="e2e-manual/1",
        clean_sha256=document_sha256,
        blocks=[block],
    )
    evidence = EvidenceRef(
        id=evidence_id,
        field_pointer="/data/rates/0/value_percent",
        source_document_id=document.id,
        block_id=block.id,
        quote=quote,
        start_char=quote_start,
        end_char=quote_start + len(quote),
        evidence_sha256=_sha256(quote),
        status=EvidenceStatus.STATED,
    )
    data = CampaignData(
        bank_id=bank_id,
        title=f"Sentetik E2E Finansman {bank_name}",
        summary="Yalnızca E2E doğrulaması için sentetik manuel kayıt.",
        product_family=ProductFamily.FINANCING,
        campaign_type=CampaignType.FINANCING_RATE,
        rates=[
            RateValue(
                raw=quote,
                value_percent=Decimal(rate),
                kind=RateKind.FINANCING_PROFIT_RATE,
                period=RatePeriod.MONTHLY,
                term_months=12,
                basis_label="nominal",
            )
        ],
        validity=ValidityWindow(
            raw="2026 yılı sentetik test aralığı",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        ),
        customer_segments=["Sentetik bireysel"],
        comparison_context=ComparisonContext(
            product_currency="TRY",
            customer_segment_keys=["synthetic_retail"],
            sales_channel=SalesChannel.ALL,
            new_customer_only=False,
            product_mechanism="synthetic_standard",
            secured=False,
        ),
    )
    metadata = ExtractionMetadata(
        method=ExtractionMethod.MANUAL,
        extractor_version="e2e-manual/1",
        schema_version="1.0",
        started_at=OBSERVED_AT + timedelta(seconds=2),
        completed_at=OBSERVED_AT + timedelta(seconds=3),
    )
    issues = [] if status is RecordStatus.VALIDATED else ["synthetic_review_required"]
    candidate = ExtractionCandidate(
        id=f"candidate:e2e:{bank_id}",
        source_document_id=document.id,
        data=data,
        evidence=[evidence],
        metadata=metadata,
        issues=issues,
    )
    record_payload = {
        "source_document_id": document.id,
        "data": data,
        "evidence": [evidence],
        "extraction": metadata,
        "status": status,
        "validation_issues": issues,
    }
    record = CampaignRecord(
        id=f"record:e2e:{bank_id}",
        version=1,
        source_document_id=document.id,
        observed_at=OBSERVED_AT + timedelta(seconds=4),
        data=data,
        evidence=[evidence],
        extraction=metadata,
        status=status,
        validation_issues=issues,
        record_sha256=canonical_sha256(record_payload),
    )
    return PersistenceBundle(
        source=SourceRequest(
            bank_id=bank_id,
            bank_name=bank_name,
            source_url=source_url,
            campaign_key=f"{bank_id}:synthetic-financing",
            job_id=f"job:e2e:{bank_id}",
        ),
        fetch_artifact=fetch,
        clean_document=document,
        candidate=candidate,
        record=record,
        coverage_observation=CoverageObservation(
            bank_id=bank_id,
            bank_name=bank_name,
            observed_at=record.observed_at,
            status=(
                CoverageStatus.SUCCESS
                if status is RecordStatus.VALIDATED
                else CoverageStatus.PARTIAL
            ),
            reason=None if status is RecordStatus.VALIDATED else "synthetic_review_required",
        ),
    )


async def _seed_sources(runtime: ApiRuntime, bundles: list[PersistenceBundle]) -> None:
    async with runtime.database.transaction() as session:
        repository = SourceRepository(session)
        for listing_order, bundle in enumerate(bundles, start=1):
            host = bundle.source.source_url.host
            assert host is not None and host.endswith(".invalid")
            await repository.upsert(
                source_id=bundle.source.bank_id,
                registry_version="e2e-synthetic-v1",
                listing_order=listing_order,
                legal_name=bundle.source.bank_name,
                homepage_url=f"https://{host}",
                allowed_hosts=[host],
                digital_bank=False,
            )


@pytest.mark.asyncio
async def test_production_composition_compares_and_cites_only_validated_manual_records(
    production_app: FastAPI,
    production_client: httpx.AsyncClient,
) -> None:
    runtime = cast(ApiRuntime, production_app.state.runtime)
    bundles = [
        _manual_bundle(
            bank_id="e2e-synthetic-a",
            bank_name="Sentetik E2E Katılım A",
            rate="1.25",
            status=RecordStatus.VALIDATED,
        ),
        _manual_bundle(
            bank_id="e2e-synthetic-b",
            bank_name="Sentetik E2E Katılım B",
            rate="1.75",
            status=RecordStatus.VALIDATED,
        ),
        _manual_bundle(
            bank_id="e2e-synthetic-review",
            bank_name="Sentetik E2E İnceleme Kaydı",
            rate="1.10",
            status=RecordStatus.NEEDS_REVIEW,
        ),
    ]

    assert runtime.settings.model_profile is ModelProfile.RULES_ONLY
    assert runtime.settings.ingest_network_enabled is False
    assert runtime.model_health is None
    assert runtime.model_http_client is None

    await _seed_sources(runtime, bundles)
    persisted = [await runtime.writes.persist(bundle) for bundle in bundles]
    record_ids = [item.record_id for item in persisted]
    assert all(record_id is not None for record_id in record_ids)
    validated_ids = cast(list[str], record_ids[:2])
    review_id = cast(str, record_ids[2])

    comparison = await production_client.post(
        "/api/v1/comparisons",
        json={
            "campaign_ids": validated_ids,
            "dimensions": ["rate"],
            "as_of": AS_OF.isoformat(),
        },
    )
    assert comparison.status_code == 200
    comparison_items = comparison.json()["items"]
    assert {item["campaign_id"] for item in comparison_items} == set(validated_ids)
    assert {item["rank"] for item in comparison_items} == {1, 2}
    assert {item["reason_code"] for item in comparison_items} == {"ranked_lower_is_better"}
    assert all(item["comparable"] is True for item in comparison_items)

    chat = await production_client.post(
        "/api/v1/chat",
        json={
            "question": "Finansman kampanyalarını listele",
            "as_of": AS_OF.isoformat(),
        },
    )
    assert chat.status_code == 200
    chat_payload = chat.json()
    assert chat_payload["insufficient_evidence"] is False
    assert chat_payload["plan"]["intent"] == "list"
    assert chat_payload["plan"]["keywords"] == []
    assert {citation["id"] for citation in chat_payload["citations"]} == {
        "evidence:e2e:e2e-synthetic-a:rate",
        "evidence:e2e:e2e-synthetic-b:rate",
    }
    assert all(
        urlsplit(citation["source_url"]).hostname.endswith(".invalid")
        and citation["status"] == "verified"
        for citation in chat_payload["citations"]
    )
    assert "Sentetik E2E İnceleme Kaydı" not in chat_payload["answer"]

    detail_evidence_ids: set[str] = set()
    for record_id in validated_ids:
        detail = await production_client.get(f"/api/v1/campaigns/{record_id}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["extraction"]["method"] == "manual"
        detail_evidence_ids.update(item["id"] for item in detail_payload["evidence"])
    assert {citation["id"] for citation in chat_payload["citations"]} <= detail_evidence_ids

    rejected = await production_client.post(
        "/api/v1/comparisons",
        json={
            "campaign_ids": [validated_ids[0], review_id],
            "dimensions": ["rate"],
            "as_of": AS_OF.isoformat(),
        },
    )
    assert rejected.status_code == 200
    assert all(item["comparable"] is False for item in rejected.json()["items"])
    assert {item["reason_code"] for item in rejected.json()["items"]} == {"record_not_validated"}
