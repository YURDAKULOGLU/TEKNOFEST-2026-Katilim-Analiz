from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignListFilters,
    ValidityFilter,
)
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    ChatQueryPlan,
    ComparisonContext,
    CoverageEntry,
    CoverageStatus,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    FetchArtifact,
    FetchStatus,
    ProductFamily,
    QueryIntent,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    SourceBlock,
    ValidityWindow,
)
from katilim_analiz.storage.health import PostgresDatabaseHealth
from katilim_analiz.storage.read_adapter import (
    InvalidReadCursorError,
    PostgresCampaignReadAdapter,
)
from katilim_analiz.storage.repositories import (
    ArtifactRepository,
    CampaignRepository,
    CoverageRepository,
    SourceRepository,
)
from katilim_analiz.storage.serialization import canonical_sha256

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class SeededReadModel:
    as_of: datetime
    first_id: str
    second_id: str
    old_id: str
    future_id: str
    bank_a: str
    bank_b: str
    bank_a_name: str


async def _source(
    session,  # type: ignore[no-untyped-def]
    *,
    bank_id: str,
    legal_name: str,
    host: str,
    registry_version: str,
    order: int,
) -> None:
    await SourceRepository(session).upsert(
        source_id=bank_id,
        registry_version=registry_version,
        listing_order=order,
        legal_name=legal_name,
        homepage_url=f"https://{host}",
        allowed_hosts=[host],
        digital_bank=False,
    )


async def _campaign(
    session,  # type: ignore[no-untyped-def]
    *,
    prefix: str,
    bank_id: str,
    host: str,
    logical_key: str,
    record_id: str,
    version: int,
    observed_at: datetime,
    title: str,
    family: ProductFamily,
    currency: str,
    segment: str,
    channel: SalesChannel,
    status: RecordStatus = RecordStatus.VALIDATED,
) -> None:
    identity = hashlib.sha256(record_id.encode()).hexdigest()
    raw_text = f"{title}: aylık kâr payı oranı %1,50"
    raw_bytes = raw_text.encode()
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    fetch_id = f"fetch:{prefix}:{identity[:16]}"
    document_id = f"clean:{identity}"
    block_id = f"block:{prefix}:{identity[:16]}"
    source_url = f"https://{host}/kampanyalar/{identity[:12]}"
    artifacts = ArtifactRepository(session)
    await artifacts.add_fetch(
        FetchArtifact(
            id=fetch_id,
            bank_id=bank_id,
            requested_url=source_url,
            final_url=source_url,
            status=FetchStatus.SUCCESS,
            http_status=200,
            fetched_at=observed_at - timedelta(seconds=3),
            robots_allowed=True,
            content_type="text/html",
            raw_sha256=raw_hash,
            raw_size_bytes=len(raw_bytes),
            private_raw_path=f"{raw_hash}.html",
        )
    )
    from katilim_analiz.contracts import CleanDocument

    await artifacts.add_document(
        CleanDocument(
            id=document_id,
            fetch_artifact_id=fetch_id,
            bank_id=bank_id,
            canonical_url=source_url,
            title=f"Kaynak {title}",
            cleaned_at=observed_at - timedelta(seconds=2),
            cleaner_version="test/1",
            clean_sha256=identity,
            blocks=[
                SourceBlock(
                    id=block_id,
                    ordinal=0,
                    kind="paragraph",
                    text=raw_text,
                    locator="main > p",
                    text_sha256=raw_hash,
                )
            ],
        )
    )
    quote = "%1,50"
    start = raw_text.index(quote)
    evidence = EvidenceRef(
        id=f"evidence:{prefix}:{identity[:16]}",
        field_pointer="/rates/0/value_percent",
        source_document_id=document_id,
        block_id=block_id,
        quote=quote,
        start_char=start,
        end_char=start + len(quote),
        evidence_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        status=EvidenceStatus.STATED,
    )
    extraction = ExtractionMetadata(
        method=ExtractionMethod.RULE,
        extractor_version="read-test/1",
        schema_version="1.0",
        started_at=observed_at - timedelta(seconds=1),
        completed_at=observed_at,
    )
    data = CampaignData(
        bank_id=bank_id,
        title=title,
        product_family=family,
        campaign_type=CampaignType.FINANCING_RATE,
        rates=[
            RateValue(
                raw="%1,50",
                value_percent=Decimal("1.50"),
                kind=RateKind.FINANCING_PROFIT_RATE,
                period=RatePeriod.MONTHLY,
            )
        ],
        validity=ValidityWindow(
            raw="Temmuz 2026",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 7, 31),
        ),
        customer_segments=[segment],
        comparison_context=ComparisonContext(
            product_currency=currency,
            customer_segment_keys=[segment.casefold()],
            sales_channel=channel,
        ),
    )
    await CampaignRepository(session).add_record(
        logical_key,
        CampaignRecord(
            id=record_id,
            version=version,
            source_document_id=document_id,
            observed_at=observed_at,
            data=data,
            evidence=[evidence],
            extraction=extraction,
            status=status,
            record_sha256=canonical_sha256(
                {
                    "logical_key": logical_key,
                    "version": version,
                    "data": data,
                    "status": status,
                }
            ),
        ),
    )


@pytest.fixture
async def seeded_reads(database) -> SeededReadModel:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex[:8]
    registry_version = f"read-{prefix}"
    bank_a = f"read-{prefix}-a"
    bank_b = f"read-{prefix}-b"
    bank_a_name = f"Alfa Katılım {prefix}"
    bank_b_name = f"Beta Katılım {prefix}"
    host_a = f"{prefix}.a.example.test"
    host_b = f"{prefix}.b.example.test"
    base = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    as_of = base + timedelta(hours=2)
    old_id = f"record:{prefix}:old"
    first_id = f"record:{prefix}:a"
    second_id = f"record:{prefix}:z"
    future_id = f"record:{prefix}:future"
    async with database.transaction() as session:
        await _source(
            session,
            bank_id=bank_a,
            legal_name=bank_a_name,
            host=host_a,
            registry_version=registry_version,
            order=1,
        )
        await _source(
            session,
            bank_id=bank_b,
            legal_name=bank_b_name,
            host=host_b,
            registry_version=registry_version,
            order=2,
        )
        await _campaign(
            session,
            prefix=prefix,
            bank_id=bank_a,
            host=host_a,
            logical_key=f"{prefix}:logical-a",
            record_id=old_id,
            version=1,
            observed_at=base,
            title="Eski Kâr Payı",
            family=ProductFamily.FINANCING,
            currency="TRY",
            segment="Bireysel",
            channel=SalesChannel.MOBILE,
        )
        await _campaign(
            session,
            prefix=prefix,
            bank_id=bank_a,
            host=host_a,
            logical_key=f"{prefix}:logical-a",
            record_id=second_id,
            version=2,
            observed_at=as_of,
            title="Yeni Kâr Payı",
            family=ProductFamily.FINANCING,
            currency="TRY",
            segment="Bireysel",
            channel=SalesChannel.MOBILE,
        )
        await _campaign(
            session,
            prefix=prefix,
            bank_id=bank_b,
            host=host_b,
            logical_key=f"{prefix}:logical-b",
            record_id=first_id,
            version=1,
            observed_at=as_of,
            title="Ticari Kâr Payı",
            family=ProductFamily.CARD,
            currency="USD",
            segment="Ticari",
            channel=SalesChannel.BRANCH,
        )
        await _campaign(
            session,
            prefix=prefix,
            bank_id=bank_a,
            host=host_a,
            logical_key=f"{prefix}:logical-future",
            record_id=future_id,
            version=1,
            observed_at=as_of + timedelta(hours=1),
            title="Gelecek Kampanya",
            family=ProductFamily.FINANCING,
            currency="TRY",
            segment="Bireysel",
            channel=SalesChannel.WEB,
        )
        coverage = CoverageRepository(session)
        await coverage.add(
            CoverageEntry(
                bank_id=bank_a,
                bank_name=bank_a_name,
                observed_at=base,
                status=CoverageStatus.PARTIAL,
                source_count=1,
                campaign_count=1,
            )
        )
        await coverage.add(
            CoverageEntry(
                bank_id=bank_a,
                bank_name=bank_a_name,
                observed_at=as_of,
                status=CoverageStatus.SUCCESS,
                source_count=2,
                campaign_count=2,
            )
        )
        await coverage.add(
            CoverageEntry(
                bank_id=bank_b,
                bank_name=bank_b_name,
                observed_at=as_of,
                status=CoverageStatus.NOT_FOUND,
                source_count=1,
                campaign_count=0,
                reason="Kampanya bulunamadı",
            )
        )
    return SeededReadModel(
        as_of=as_of,
        first_id=first_id,
        second_id=second_id,
        old_id=old_id,
        future_id=future_id,
        bank_a=bank_a,
        bank_b=bank_b,
        bank_a_name=bank_a_name,
    )


@pytest.mark.asyncio
async def test_latest_cursor_ties_cutoff_filters_and_facets(database, seeded_reads) -> None:  # type: ignore[no-untyped-def]
    adapter = PostgresCampaignReadAdapter(database.session_factory)
    first = await adapter.list_latest(
        filters=CampaignListFilters(), after=None, limit=1, as_of=seeded_reads.as_of
    )
    assert [item.record.id for item in first.items] == [seeded_reads.first_id]
    assert first.has_more
    assert {option.value: option.count for option in first.facets.banks}[seeded_reads.bank_a] == 1
    assert {option.value: option.count for option in first.facets.banks}[seeded_reads.bank_b] == 1

    cursor = CampaignCursor(
        observed_at=first.items[-1].record.observed_at,
        campaign_id=first.items[-1].record.id,
    )
    second = await adapter.list_latest(
        filters=CampaignListFilters(), after=cursor, limit=10, as_of=seeded_reads.as_of
    )
    assert [item.record.id for item in second.items] == [seeded_reads.second_id]
    assert not second.has_more
    assert seeded_reads.old_id not in {item.record.id for item in first.items + second.items}
    assert seeded_reads.future_id not in {item.record.id for item in first.items + second.items}

    filtered = await adapter.list_latest(
        filters=CampaignListFilters(
            bank_id=seeded_reads.bank_a,
            currency="TRY",
            customer_segment="Bireysel",
            sales_channel=SalesChannel.MOBILE,
            validity=ValidityFilter.ACTIVE,
        ),
        after=None,
        limit=10,
        as_of=seeded_reads.as_of,
    )
    assert [item.record.id for item in filtered.items] == [seeded_reads.second_id]
    assert [(item.value, item.count) for item in filtered.facets.banks] == [
        (seeded_reads.bank_a, 1)
    ]

    with pytest.raises(InvalidReadCursorError):
        await adapter.list_latest(
            filters=CampaignListFilters(bank_id=seeded_reads.bank_a),
            after=cursor,
            limit=10,
            as_of=seeded_reads.as_of,
        )


@pytest.mark.asyncio
async def test_detail_many_search_and_evidence_projection(database, seeded_reads) -> None:  # type: ignore[no-untyped-def]
    adapter = PostgresCampaignReadAdapter(database.session_factory)
    detail = await adapter.get(seeded_reads.second_id, as_of=seeded_reads.as_of)
    assert detail is not None
    assert detail.bank_name == seeded_reads.bank_a_name
    assert detail.source_url.host is not None
    assert detail.source_title == "Kaynak Yeni Kâr Payı"
    assert detail.record.evidence[0].quote == "%1,50"
    assert detail.record.extraction.extractor_version == "read-test/1"
    assert await adapter.get("missing", as_of=seeded_reads.as_of) is None
    assert await adapter.get(seeded_reads.future_id, as_of=seeded_reads.as_of) is None
    # A superseded version must not be served as current through the detail path.
    assert await adapter.get(seeded_reads.old_id, as_of=seeded_reads.as_of) is None

    many = await adapter.get_many(
        [seeded_reads.second_id, seeded_reads.first_id, seeded_reads.second_id, "missing"],
        as_of=seeded_reads.as_of,
    )
    assert [item.record.id for item in many] == [
        seeded_reads.second_id,
        seeded_reads.first_id,
        seeded_reads.second_id,
    ]

    matches = await adapter.search(
        ChatQueryPlan(
            intent=QueryIntent.LIST,
            bank_ids=[seeded_reads.bank_a],
            keywords=["kar payi"],
            limit=10,
        ),
        as_of=seeded_reads.as_of,
    )
    assert [item.record.id for item in matches] == [seeded_reads.second_id]


@pytest.mark.asyncio
async def test_public_latest_hides_rejected_without_resurrection_and_search_is_validated(  # type: ignore[no-untyped-def]
    database,
) -> None:
    prefix = uuid4().hex[:8]
    bank_id = f"visibility-{prefix}"
    bank_name = f"Görünürlük Katılım {prefix}"
    host = f"{prefix}.visibility.example.test"
    base = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    rejected_old_id = f"record:{prefix}:rejected-old"
    rejected_latest_id = f"record:{prefix}:rejected-latest"
    review_id = f"record:{prefix}:0-review"
    validated_id = f"record:{prefix}:z-validated"
    async with database.transaction() as session:
        await _source(
            session,
            bank_id=bank_id,
            legal_name=bank_name,
            host=host,
            registry_version=f"visibility-{prefix}",
            order=1,
        )
        common = {
            "session": session,
            "prefix": prefix,
            "bank_id": bank_id,
            "host": host,
            "family": ProductFamily.FINANCING,
            "currency": "TRY",
            "segment": "Bireysel",
            "channel": SalesChannel.MOBILE,
        }
        await _campaign(
            **common,
            logical_key=f"{prefix}:retired",
            record_id=rejected_old_id,
            version=1,
            observed_at=base,
            title="Eski Kâr Payı",
        )
        await _campaign(
            **common,
            logical_key=f"{prefix}:retired",
            record_id=rejected_latest_id,
            version=2,
            observed_at=base + timedelta(minutes=1),
            title="Kaldırılan Kâr Payı",
            status=RecordStatus.REJECTED,
        )
        await _campaign(
            **common,
            logical_key=f"{prefix}:review",
            record_id=review_id,
            version=1,
            observed_at=base + timedelta(minutes=2),
            title="İnceleme Kâr Payı",
            status=RecordStatus.NEEDS_REVIEW,
        )
        await _campaign(
            **common,
            logical_key=f"{prefix}:validated",
            record_id=validated_id,
            version=1,
            observed_at=base + timedelta(minutes=2),
            title="Doğrulanmış Kâr Payı",
        )

    as_of = base + timedelta(minutes=3)
    adapter = PostgresCampaignReadAdapter(database.session_factory)
    public = await adapter.list_latest(
        filters=CampaignListFilters(bank_id=bank_id),
        after=None,
        limit=10,
        as_of=as_of,
    )
    public_ids = {item.record.id for item in public.items}
    assert public_ids == {review_id, validated_id}
    assert rejected_old_id not in public_ids
    assert rejected_latest_id not in public_ids
    assert [(item.value, item.count) for item in public.facets.banks] == [(bank_id, 2)]

    matches = await adapter.search(
        ChatQueryPlan(
            intent=QueryIntent.LIST,
            bank_ids=[bank_id],
            keywords=["kar payi"],
            limit=1,
        ),
        as_of=as_of,
    )
    assert [item.record.id for item in matches] == [validated_id]

    # The detail path must apply the same visibility contract as the list:
    # rejected and superseded versions resolve to None, live versions resolve.
    assert await adapter.get(rejected_latest_id, as_of=as_of) is None
    assert await adapter.get(rejected_old_id, as_of=as_of) is None
    review_detail = await adapter.get(review_id, as_of=as_of)
    assert review_detail is not None
    assert review_detail.record.id == review_id
    validated_detail = await adapter.get(validated_id, as_of=as_of)
    assert validated_detail is not None
    assert validated_detail.record.status is RecordStatus.VALIDATED


@pytest.mark.asyncio
async def test_latest_coverage_and_query_count_are_stable(database, seeded_reads) -> None:  # type: ignore[no-untyped-def]
    adapter = PostgresCampaignReadAdapter(database.session_factory)
    coverage = await adapter.latest_coverage(as_of=seeded_reads.as_of)
    by_bank = {item.bank_id: item for item in coverage}
    assert by_bank[seeded_reads.bank_a].status is CoverageStatus.SUCCESS
    assert by_bank[seeded_reads.bank_a].source_count == 2
    assert by_bank[seeded_reads.bank_b].status is CoverageStatus.NOT_FOUND

    statements: list[str] = []

    def count_statement(*args) -> None:  # type: ignore[no-untyped-def]
        statements.append(args[2])

    event.listen(database.engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        await adapter.list_latest(
            filters=CampaignListFilters(), after=None, limit=10, as_of=seeded_reads.as_of
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", count_statement)
    assert len(statements) == 3


@pytest.mark.asyncio
async def test_database_health_reports_current_head_drift_and_unavailable(database) -> None:  # type: ignore[no-untyped-def]
    health = PostgresDatabaseHealth(database.engine, alembic_config=_BACKEND_ROOT / "alembic.ini")
    assert await health.ping()
    revisions = await health.migration_revisions()
    assert revisions.current == revisions.head

    async with database.transaction() as session:
        await session.execute(text("UPDATE alembic_version SET version_num = 'drift-test'"))
    try:
        drifted = await health.migration_revisions()
        assert drifted.current == "drift-test"
        assert drifted.current != drifted.head
    finally:
        async with database.transaction() as session:
            await session.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": revisions.head},
            )

    unavailable_engine = create_async_engine(
        "postgresql+asyncpg://invalid:invalid@127.0.0.1:1/unavailable",
        connect_args={"timeout": 0.2},
    )
    try:
        unavailable = PostgresDatabaseHealth(
            unavailable_engine, alembic_config=_BACKEND_ROOT / "alembic.ini"
        )
        assert not await unavailable.ping()
    finally:
        await unavailable_engine.dispose()
