from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from katilim_analiz.application.models import CampaignListFilters
from katilim_analiz.contracts import ExtractionMethod, RecordStatus
from katilim_analiz.intake import ingest_human_verified
from katilim_analiz.storage.models import CampaignRecordRow, EvidenceRefRow, SourceBlockRow
from katilim_analiz.storage.read_adapter import PostgresCampaignReadAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_PATH = PROJECT_ROOT / "datasets/human-verified/ornek-sablon.json"
BLOCKED_BANKS = {"kuveyt-turk", "turkiye-finans", "hayat-finans", "adil-katilim"}


async def test_human_verified_ingest_is_idempotent_and_visible_through_read_apis(database) -> None:  # type: ignore[no-untyped-def]
    first = await ingest_human_verified(database, intake_path=TEMPLATE_PATH)
    second = await ingest_human_verified(database, intake_path=TEMPLATE_PATH)

    assert first.campaign_count == second.campaign_count == 4
    assert first.campaigns_created == 4
    assert second.campaigns_created == 0
    assert first.human_verified_count == second.human_verified_count == 4
    assert first.machine_validated_count == second.machine_validated_count == 0
    assert first.status == second.status == "ingested"

    async with database.session() as session:
        record_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.campaign_key.like("human:%"))
            )
        ).scalar_one()
        non_validated = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(
                    CampaignRecordRow.campaign_key.like("human:%"),
                    CampaignRecordRow.status != RecordStatus.VALIDATED.value,
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

    assert record_count == 4
    assert non_validated == 0
    assert mismatched_evidence == 0

    reads = PostgresCampaignReadAdapter(database.session_factory)
    as_of = datetime(2026, 7, 24, 23, 59, tzinfo=UTC)
    campaigns = await reads.list_latest(
        filters=CampaignListFilters(), after=None, limit=100, as_of=as_of
    )
    assert len(campaigns.items) == 4
    assert {item.record.status for item in campaigns.items} == {RecordStatus.VALIDATED}
    assert {item.record.extraction.method for item in campaigns.items} == {ExtractionMethod.MANUAL}
    assert all("human_verified" in item.record.validation_issues for item in campaigns.items)
    assert all(item.record.evidence for item in campaigns.items)

    coverage = await reads.latest_coverage(as_of=as_of)
    by_bank = {item.bank_id: item for item in coverage}
    assert set(by_bank) >= BLOCKED_BANKS
    for bank_id in BLOCKED_BANKS:
        assert by_bank[bank_id].campaign_count == 1
        assert by_bank[bank_id].reason == "human_verified_manual_intake"
