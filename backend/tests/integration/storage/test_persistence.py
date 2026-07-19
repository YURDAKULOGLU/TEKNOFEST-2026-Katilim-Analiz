from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from factories import NOW, candidate, clean_document, evidence, fetch_artifact, record
from sqlalchemy import func, select

from katilim_analiz.contracts import CoverageEntry, CoverageStatus
from katilim_analiz.storage.models import (
    CampaignRecordRow,
    CoverageEntryRow,
    FetchDocumentLink,
)
from katilim_analiz.storage.repositories import (
    ArtifactRepository,
    CampaignRepository,
    CoverageRepository,
    EvidenceIntegrityError,
    EvidenceRepository,
    ExtractionRepository,
    SourceRepository,
)


async def _seed_source(session) -> None:  # type: ignore[no-untyped-def]
    repository = SourceRepository(session)
    created = await repository.upsert(
        source_id="bank-a",
        registry_version="2026-07-18.1",
        listing_order=1,
        legal_name="Örnek Katılım Bankası",
        homepage_url="https://bank.example",
        allowed_hosts=["bank.example"],
        digital_bank=False,
    )
    assert created


@pytest.mark.asyncio
async def test_evidence_first_records_are_idempotent_searchable_and_precise(database) -> None:  # type: ignore[no-untyped-def]
    first_fetch = fetch_artifact()
    document = clean_document()
    extraction = candidate()
    campaign = record()

    async with database.transaction() as session:
        await _seed_source(session)
        artifacts = ArtifactRepository(session)
        assert (await artifacts.add_fetch(first_fetch)).created
        assert not (await artifacts.add_fetch(first_fetch)).created
        assert (await artifacts.add_document(document)).created
        assert not (await artifacts.add_document(document)).created

        extractions = ExtractionRepository(session)
        assert (await extractions.add_candidate(extraction)).created
        assert not (await extractions.add_candidate(extraction)).created

        campaigns = CampaignRepository(session)
        assert (await campaigns.add_record("bank-a:kampanya", campaign)).created
        assert not (await campaigns.add_record("bank-a:kampanya", campaign)).created

        coverage = CoverageRepository(session)
        coverage_entry = CoverageEntry(
            bank_id="bank-a",
            bank_name="Örnek Katılım Bankası",
            observed_at=NOW,
            status=CoverageStatus.SUCCESS,
            source_count=1,
            campaign_count=1,
        )
        assert await coverage.add(coverage_entry)
        assert not await coverage.add(coverage_entry)

    second_fetch = fetch_artifact(suffix="2", fetched_at=NOW + timedelta(hours=1))
    async with database.transaction() as session:
        artifacts = ArtifactRepository(session)
        assert (await artifacts.add_fetch(second_fetch)).created
        assert not (await artifacts.add_document(clean_document(fetch_id=second_fetch.id))).created

    async with database.session() as session:
        stored = (
            await session.execute(
                select(CampaignRecordRow).where(CampaignRecordRow.id == campaign.id)
            )
        ).scalar_one()
        link_count = (
            await session.execute(select(func.count()).select_from(FetchDocumentLink))
        ).scalar_one()
        coverage_count = (
            await session.execute(select(func.count()).select_from(CoverageEntryRow))
        ).scalar_one()
        search_results = await CampaignRepository(session).search("kar payi")

    assert stored.rate_min == Decimal("1.990000")
    assert stored.amount_max == Decimal("100000.0000")
    assert stored.data["rates"][0]["value_percent"] == "1.990000"
    assert link_count == 2
    assert coverage_count == 1
    assert [item.id for item in search_results] == [campaign.id]


@pytest.mark.asyncio
async def test_evidence_must_match_the_stored_block_span(database) -> None:  # type: ignore[no-untyped-def]
    async with database.transaction() as session:
        source = SourceRepository(session)
        await source.upsert(
            source_id="bank-a",
            registry_version="2026-07-18.1",
            listing_order=1,
            legal_name="Örnek Katılım Bankası",
            homepage_url="https://bank.example",
            allowed_hosts=["bank.example"],
            digital_bank=False,
        )
        artifacts = ArtifactRepository(session)
        await artifacts.add_fetch(fetch_artifact())
        await artifacts.add_document(clean_document())

        with pytest.raises(EvidenceIntegrityError, match="does not match"):
            await EvidenceRepository(session).put(evidence(quote="bozuk"))
