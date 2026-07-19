from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from katilim_analiz.application.models import CampaignListFilters
from katilim_analiz.contracts import RecordStatus
from katilim_analiz.demo import seed_demo
from katilim_analiz.storage.models import (
    CampaignRecordRow,
    CoverageEntryRow,
    EvidenceRefRow,
    Source,
    SourceBlockRow,
)
from katilim_analiz.storage.read_adapter import PostgresCampaignReadAdapter


async def test_demo_seed_is_postgresql_idempotent_and_makes_dashboard_non_empty(database) -> None:  # type: ignore[no-untyped-def]
    first = await seed_demo(database)
    second = await seed_demo(database)

    assert first.coverage_count == second.coverage_count == 10
    assert first.campaign_count == second.campaign_count == 4
    assert first.coverage_created == 10
    assert first.campaigns_created == 4
    assert second.coverage_created == 0
    assert second.campaigns_created == 0
    assert first.human_verified_count == second.human_verified_count == 0

    async with database.session() as session:
        source_count = (
            await session.execute(select(func.count()).select_from(Source))
        ).scalar_one()
        campaign_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.campaign_key.like("demo:%"))
            )
        ).scalar_one()
        coverage_count = (
            await session.execute(select(func.count()).select_from(CoverageEntryRow))
        ).scalar_one()
        unsupported_records = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(
                    CampaignRecordRow.campaign_key.like("demo:%"),
                    CampaignRecordRow.status != RecordStatus.NEEDS_REVIEW.value,
                )
            )
        ).scalar_one()
        mismatched_evidence = (
            await session.execute(
                select(func.count())
                .select_from(EvidenceRefRow)
                .join(SourceBlockRow, SourceBlockRow.id == EvidenceRefRow.block_id)
                .where(
                    func.substr(
                        SourceBlockRow.text,
                        EvidenceRefRow.start_char + 1,
                        EvidenceRefRow.end_char - EvidenceRefRow.start_char,
                    )
                    != EvidenceRefRow.quote
                )
            )
        ).scalar_one()

    assert source_count == 10
    assert campaign_count == 4
    assert coverage_count == 14  # four pending observations plus the ten-bank snapshot
    assert unsupported_records == 0
    assert mismatched_evidence == 0

    reads = PostgresCampaignReadAdapter(database.session_factory)
    as_of = datetime(2026, 7, 19, 23, 59, tzinfo=UTC)
    campaigns = await reads.list_latest(
        filters=CampaignListFilters(), after=None, limit=100, as_of=as_of
    )
    assert len(campaigns.items) == 4
    assert {item.record.status for item in campaigns.items} == {RecordStatus.NEEDS_REVIEW}
    assert all(item.record.evidence for item in campaigns.items)

    coverage = await reads.latest_coverage(as_of=as_of)
    assert len(coverage) == 10
    assert {item.campaign_count for item in coverage} == {0}
    assert {item.bank_id for item in coverage} == {
        "adil-katilim",
        "albaraka-turk",
        "dunya-katilim",
        "hayat-finans",
        "kuveyt-turk",
        "tom-katilim",
        "emlak-katilim",
        "turkiye-finans",
        "vakif-katilim",
        "ziraat-katilim",
    }
