"""SQLAlchemy read adapter for the application-owned campaign query port."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError
from sqlalchemy import Select, and_, asc, desc, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignFacets,
    CampaignListFilters,
    CampaignProjection,
    CampaignReadSlice,
    FacetOption,
    NotificationCursor,
    NotificationReadItem,
    NotificationReadSlice,
    ValidityFilter,
)
from katilim_analiz.application.ports import CampaignReadPort
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    ChatQueryPlan,
    CoverageEntry,
    CoverageStatus,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    RecordStatus,
)
from katilim_analiz.notifications import (
    CAMPAIGN_CHANGE_EVENT_TYPE,
    CAMPAIGN_CHANGE_TOPIC,
    campaign_change_event_from_outbox,
)
from katilim_analiz.storage.models import (
    CampaignRecordRow,
    CleanDocumentRow,
    CoverageEntryRow,
    EvidenceRefRow,
    OutboxEvent,
    RecordEvidenceLink,
    Source,
    SourceBlockRow,
)

_MAX_PAGE_SIZE = 100
_MAX_GET_MANY = 100
_MAX_FACET_OPTIONS = 100


class InvalidReadCursorError(ValueError):
    """A typed cursor does not belong to the requested filtered snapshot."""


class ReadModelIntegrityError(RuntimeError):
    """Persisted provenance cannot safely be projected through public contracts."""


class PostgresCampaignReadAdapter(CampaignReadPort):
    """Fixed-query PostgreSQL implementation; no ORM entity crosses this boundary."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def list_latest(
        self,
        *,
        filters: CampaignListFilters,
        after: CampaignCursor | None,
        limit: int,
        as_of: datetime,
    ) -> CampaignReadSlice:
        _require_aware(as_of, "as_of")
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
        if after is not None and after.observed_at > as_of:
            raise InvalidReadCursorError("cursor is later than the requested as_of snapshot")

        latest = _latest_record_ids(as_of)
        predicates = _filter_predicates(filters, as_of)
        async with self._sessions() as session:
            if after is not None and not await self._cursor_exists(
                session, latest, predicates, after
            ):
                raise InvalidReadCursorError(
                    "cursor does not belong to the requested filtered snapshot"
                )

            statement = _projection_select(latest).where(*predicates)
            if after is not None:
                statement = statement.where(
                    or_(
                        CampaignRecordRow.observed_at < after.observed_at,
                        and_(
                            CampaignRecordRow.observed_at == after.observed_at,
                            CampaignRecordRow.id > after.campaign_id,
                        ),
                    )
                )
            statement = statement.order_by(
                desc(CampaignRecordRow.observed_at), asc(CampaignRecordRow.id)
            ).limit(limit + 1)
            rows = list((await session.execute(statement)).all())
            selected = rows[:limit]
            evidence = await self._evidence_by_record(session, [row[0].id for row in selected])
            facets = await self._facets(session, latest, predicates)

        return CampaignReadSlice(
            items=[self._project(row, evidence.get(row[0].id, [])) for row in selected],
            has_more=len(rows) > limit,
            facets=facets,
        )

    async def get(self, campaign_id: str, *, as_of: datetime) -> CampaignProjection | None:
        _require_aware(as_of, "as_of")
        _require_id(campaign_id)
        # Mirror list_latest: only the latest, non-REJECTED version of a
        # logical campaign is served; a stale or rejected id resolves to None
        # instead of presenting retired data as current (issue #6).
        latest = _latest_record_ids(as_of)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    _projection_select(latest).where(CampaignRecordRow.id == campaign_id).limit(1)
                )
            ).first()
            if row is None:
                return None
            evidence = await self._evidence_by_record(session, [campaign_id])
        return self._project(row, evidence.get(campaign_id, []))

    async def get_many(
        self,
        campaign_ids: list[str],
        *,
        as_of: datetime,
    ) -> list[CampaignProjection]:
        _require_aware(as_of, "as_of")
        if len(campaign_ids) > _MAX_GET_MANY:
            raise ValueError(f"campaign_ids cannot contain more than {_MAX_GET_MANY} IDs")
        for campaign_id in campaign_ids:
            _require_id(campaign_id)
        if not campaign_ids:
            return []

        unique_ids = list(dict.fromkeys(campaign_ids))
        async with self._sessions() as session:
            rows = list(
                (
                    await session.execute(
                        _projection_select().where(
                            CampaignRecordRow.id.in_(unique_ids),
                            CampaignRecordRow.observed_at <= as_of,
                        )
                    )
                ).all()
            )
            evidence = await self._evidence_by_record(session, [row[0].id for row in rows])
        projections = {row[0].id: self._project(row, evidence.get(row[0].id, [])) for row in rows}
        return [projections[item] for item in campaign_ids if item in projections]

    async def search(
        self,
        plan: ChatQueryPlan,
        *,
        as_of: datetime,
    ) -> list[CampaignProjection]:
        _require_aware(as_of, "as_of")
        latest = _latest_record_ids(as_of)
        statement = _projection_select(latest).where(
            CampaignRecordRow.status == RecordStatus.VALIDATED.value
        )
        if plan.bank_ids:
            statement = statement.where(CampaignRecordRow.bank_id.in_(plan.bank_ids))
        if plan.product_family is not None:
            statement = statement.where(
                CampaignRecordRow.product_family == plan.product_family.value
            )
        if plan.campaign_type is not None:
            statement = statement.where(CampaignRecordRow.campaign_type == plan.campaign_type.value)

        keywords = " ".join(plan.keywords).strip()
        if keywords:
            ts_query = func.websearch_to_tsquery("turkish", func.immutable_unaccent(keywords))
            statement = statement.where(
                CampaignRecordRow.search_vector.op("@@")(ts_query)
            ).order_by(
                desc(func.ts_rank_cd(CampaignRecordRow.search_vector, ts_query)),
                desc(CampaignRecordRow.observed_at),
                asc(CampaignRecordRow.id),
            )
        else:
            statement = statement.order_by(
                desc(CampaignRecordRow.observed_at), asc(CampaignRecordRow.id)
            )
        statement = statement.limit(plan.limit)

        async with self._sessions() as session:
            rows = list((await session.execute(statement)).all())
            evidence = await self._evidence_by_record(session, [row[0].id for row in rows])
        return [self._project(row, evidence.get(row[0].id, [])) for row in rows]

    async def latest_coverage(self, *, as_of: datetime) -> list[CoverageEntry]:
        _require_aware(as_of, "as_of")
        ranked = (
            select(
                CoverageEntryRow.id.label("id"),
                func.row_number()
                .over(
                    partition_by=CoverageEntryRow.source_id,
                    order_by=(
                        desc(CoverageEntryRow.observed_at),
                        desc(CoverageEntryRow.id),
                    ),
                )
                .label("snapshot_rank"),
            )
            .join(Source, Source.id == CoverageEntryRow.source_id)
            .where(CoverageEntryRow.observed_at <= as_of, Source.active.is_(True))
            .cte("ranked_coverage")
        )
        latest = select(ranked.c.id).where(ranked.c.snapshot_rank == 1).cte("latest_coverage")
        statement = (
            select(CoverageEntryRow)
            .join(latest, latest.c.id == CoverageEntryRow.id)
            .join(Source, Source.id == CoverageEntryRow.source_id)
            .order_by(Source.listing_order, Source.id)
        )
        async with self._sessions() as session:
            rows = list((await session.execute(statement)).scalars().all())
        return [
            CoverageEntry(
                bank_id=row.source_id,
                bank_name=row.bank_name,
                observed_at=row.observed_at,
                status=CoverageStatus(row.status),
                source_count=row.source_count,
                campaign_count=row.campaign_count,
                reason=row.reason,
            )
            for row in rows
        ]

    async def list_notifications(
        self,
        *,
        after: NotificationCursor | None,
        limit: int,
    ) -> NotificationReadSlice:
        """Read campaign-change rows independently of publisher delivery state."""

        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
        statement = select(OutboxEvent).where(
            OutboxEvent.topic == CAMPAIGN_CHANGE_TOPIC,
            OutboxEvent.event_type == CAMPAIGN_CHANGE_EVENT_TYPE,
        )
        if after is not None:
            statement = statement.where(OutboxEvent.feed_sequence > after.feed_sequence)
        statement = statement.order_by(asc(OutboxEvent.feed_sequence)).limit(limit + 1)
        async with self._sessions() as session:
            rows = list((await session.execute(statement)).scalars().all())

        return NotificationReadSlice(
            items=[
                NotificationReadItem(
                    feed_sequence=row.feed_sequence,
                    event=campaign_change_event_from_outbox(
                        event_id=row.id,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        payload=row.payload,
                        created_at=row.created_at,
                    ),
                )
                for row in rows[:limit]
            ],
            has_more=len(rows) > limit,
        )

    async def _cursor_exists(
        self,
        session: AsyncSession,
        latest: Any,
        predicates: Sequence[Any],
        cursor: CampaignCursor,
    ) -> bool:
        statement = (
            select(literal(True))
            .select_from(CampaignRecordRow)
            .join(latest, latest.c.id == CampaignRecordRow.id)
            .join(CleanDocumentRow, CleanDocumentRow.id == CampaignRecordRow.source_document_id)
            .join(Source, Source.id == CampaignRecordRow.bank_id)
            .where(
                CleanDocumentRow.source_id == CampaignRecordRow.bank_id,
                Source.active.is_(True),
                CampaignRecordRow.id == cursor.campaign_id,
                CampaignRecordRow.observed_at == cursor.observed_at,
                *predicates,
            )
            .limit(1)
        )
        return (await session.execute(statement)).scalar_one_or_none() is True

    async def _evidence_by_record(
        self, session: AsyncSession, record_ids: list[str]
    ) -> dict[str, list[EvidenceRef]]:
        if not record_ids:
            return {}
        statement = (
            select(RecordEvidenceLink.record_id, EvidenceRefRow, SourceBlockRow.text)
            .join(EvidenceRefRow, EvidenceRefRow.id == RecordEvidenceLink.evidence_id)
            .join(SourceBlockRow, SourceBlockRow.id == EvidenceRefRow.block_id)
            .where(RecordEvidenceLink.record_id.in_(record_ids))
            .order_by(
                RecordEvidenceLink.record_id,
                EvidenceRefRow.field_pointer,
                EvidenceRefRow.id,
            )
        )
        grouped: defaultdict[str, list[EvidenceRef]] = defaultdict(list)
        for record_id, row, block_text in (await session.execute(statement)).all():
            if block_text[row.start_char : row.end_char] != row.quote:
                raise ReadModelIntegrityError(
                    f"evidence {row.id!r} no longer resolves to its block span"
                )
            if hashlib.sha256(row.quote.encode()).hexdigest() != row.evidence_sha256:
                raise ReadModelIntegrityError(f"evidence {row.id!r} has an invalid quote hash")
            grouped[record_id].append(
                EvidenceRef(
                    id=row.id,
                    field_pointer=row.field_pointer,
                    source_document_id=row.source_document_id,
                    block_id=row.block_id,
                    quote=row.quote,
                    start_char=row.start_char,
                    end_char=row.end_char,
                    evidence_sha256=row.evidence_sha256,
                    status=EvidenceStatus(row.status),
                )
            )
        return dict(grouped)

    async def _facets(
        self,
        session: AsyncSession,
        latest: Any,
        predicates: Sequence[Any],
    ) -> CampaignFacets:
        statement = (
            select(
                CampaignRecordRow.bank_id,
                Source.legal_name,
                CampaignRecordRow.product_family,
                CampaignRecordRow.product_currency,
                CampaignRecordRow.data,
            )
            .join(latest, latest.c.id == CampaignRecordRow.id)
            .join(CleanDocumentRow, CleanDocumentRow.id == CampaignRecordRow.source_document_id)
            .join(Source, Source.id == CampaignRecordRow.bank_id)
            .where(
                CleanDocumentRow.source_id == CampaignRecordRow.bank_id,
                Source.active.is_(True),
                *predicates,
            )
        )
        banks: Counter[tuple[str, str]] = Counter()
        families: Counter[str] = Counter()
        currencies: Counter[str] = Counter()
        segments: Counter[str] = Counter()
        segment_labels: defaultdict[str, Counter[str]] = defaultdict(Counter)
        channels: Counter[str] = Counter()
        for bank_id, bank_name, family, currency, data in (await session.execute(statement)).all():
            banks[(bank_id, bank_name)] += 1
            families[family] += 1
            if currency is not None:
                currencies[currency] += 1
            try:
                parsed = CampaignData.model_validate(data)
            except ValidationError as exc:
                raise ReadModelIntegrityError(
                    "facet source cannot be projected through canonical contracts"
                ) from exc
            # Facet on a casefolded key so "Bireysel" and "bireysel" count as
            # one segment; the stored evidence text itself stays verbatim.
            for segment in {value.casefold() for value in parsed.customer_segments}:
                segments[segment] += 1
            for value in set(parsed.customer_segments):
                segment_labels[value.casefold()][value] += 1
            channels[parsed.comparison_context.sales_channel.value] += 1
        return CampaignFacets(
            banks=_facet_options(
                ((value, label, count) for (value, label), count in banks.items())
            ),
            product_families=_facet_options(
                (value, value, count) for value, count in families.items()
            ),
            currencies=_facet_options((value, value, count) for value, count in currencies.items()),
            customer_segments=_facet_options(
                (value, _dominant_label(segment_labels[value]), count)
                for value, count in segments.items()
            ),
            sales_channels=_facet_options(
                (value, value, count) for value, count in channels.items()
            ),
        )

    def _project(
        self,
        result_row: Any,
        evidence: list[EvidenceRef],
    ) -> CampaignProjection:
        row: CampaignRecordRow = result_row[0]
        bank_name: str = result_row[1]
        allowed_hosts: list[str] = result_row[2]
        source_url: str = result_row[3]
        source_title: str | None = result_row[4]
        host = urlsplit(source_url).hostname
        normalized_host = "" if host is None else host.rstrip(".").casefold()
        if normalized_host not in allowed_hosts:
            raise ReadModelIntegrityError(
                f"document {row.source_document_id!r} is outside its source allowlist"
            )
        try:
            data = CampaignData.model_validate(row.data)
            extraction = ExtractionMetadata.model_validate(row.extraction)
            if data.bank_id != row.bank_id:
                raise ReadModelIntegrityError(f"record {row.id!r} disagrees with its source bank")
            record = CampaignRecord(
                id=row.id,
                version=row.version,
                source_document_id=row.source_document_id,
                observed_at=row.observed_at,
                data=data,
                evidence=evidence,
                extraction=extraction,
                status=RecordStatus(row.status),
                validation_issues=row.validation_issues,
                record_sha256=row.record_sha256,
            )
            return CampaignProjection(
                record=record,
                campaign_key=row.campaign_key,
                bank_name=bank_name,
                source_url=HttpUrl(source_url),
                source_title=source_title,
            )
        except (ValidationError, ValueError) as exc:
            raise ReadModelIntegrityError(
                f"record {row.id!r} cannot be projected through canonical contracts"
            ) from exc


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _require_id(value: str) -> None:
    if not value or len(value) > 191:
        raise ValueError("campaign ID must contain 1..191 characters")


def _latest_record_ids(as_of: datetime) -> Any:
    ranked = (
        select(
            CampaignRecordRow.id.label("id"),
            CampaignRecordRow.status.label("status"),
            func.row_number()
            .over(
                partition_by=CampaignRecordRow.campaign_key,
                order_by=(
                    desc(CampaignRecordRow.observed_at),
                    desc(CampaignRecordRow.version),
                    asc(CampaignRecordRow.id),
                ),
            )
            .label("version_rank"),
        )
        .join(CleanDocumentRow, CleanDocumentRow.id == CampaignRecordRow.source_document_id)
        .join(Source, Source.id == CampaignRecordRow.bank_id)
        .where(
            CampaignRecordRow.observed_at <= as_of,
            CleanDocumentRow.source_id == CampaignRecordRow.bank_id,
            Source.active.is_(True),
        )
        .cte("ranked_campaign_records")
    )
    return (
        select(ranked.c.id)
        .where(
            ranked.c.version_rank == 1,
            ranked.c.status != RecordStatus.REJECTED.value,
        )
        .cte("latest_campaign_records")
    )


def _projection_select(latest: Any | None = None) -> Select[Any]:
    statement = (
        select(
            CampaignRecordRow,
            Source.legal_name,
            Source.allowed_hosts,
            CleanDocumentRow.canonical_url,
            CleanDocumentRow.title,
        )
        .join(CleanDocumentRow, CleanDocumentRow.id == CampaignRecordRow.source_document_id)
        .join(Source, Source.id == CampaignRecordRow.bank_id)
        .where(
            CleanDocumentRow.source_id == CampaignRecordRow.bank_id,
            Source.active.is_(True),
        )
    )
    if latest is not None:
        statement = statement.join(latest, latest.c.id == CampaignRecordRow.id)
    return statement


def _filter_predicates(filters: CampaignListFilters, as_of: datetime) -> list[Any]:
    predicates: list[Any] = []
    if filters.bank_id is not None:
        predicates.append(CampaignRecordRow.bank_id == filters.bank_id)
    if filters.product_family is not None:
        predicates.append(CampaignRecordRow.product_family == filters.product_family.value)
    if filters.currency is not None:
        predicates.append(CampaignRecordRow.product_currency == filters.currency)
    if filters.customer_segment is not None:
        # Case-insensitive membership so a normalized facet value like
        # "bireysel" also matches records whose verbatim text is "Bireysel".
        segment_values = func.jsonb_array_elements_text(
            CampaignRecordRow.data["customer_segments"]
        ).table_valued("value")
        predicates.append(
            select(literal(True))
            .select_from(segment_values)
            .where(func.lower(segment_values.c.value) == func.lower(filters.customer_segment))
            .correlate(CampaignRecordRow)
            .exists()
        )
    if filters.sales_channel is not None:
        predicates.append(
            CampaignRecordRow.data["comparison_context"]["sales_channel"].astext
            == filters.sales_channel.value
        )
    if filters.validity is ValidityFilter.ACTIVE:
        predicates.extend(
            [
                or_(
                    CampaignRecordRow.valid_from.is_(None),
                    CampaignRecordRow.valid_from <= as_of.date(),
                ),
                or_(
                    CampaignRecordRow.valid_to.is_(None),
                    CampaignRecordRow.valid_to >= as_of.date(),
                ),
                or_(
                    CampaignRecordRow.valid_from.is_not(None),
                    CampaignRecordRow.valid_to.is_not(None),
                ),
            ]
        )
    elif filters.validity is ValidityFilter.UPCOMING:
        predicates.append(CampaignRecordRow.valid_from > as_of.date())
    elif filters.validity is ValidityFilter.EXPIRED:
        predicates.append(CampaignRecordRow.valid_to < as_of.date())
    elif filters.validity is ValidityFilter.UNKNOWN:
        predicates.extend(
            [
                CampaignRecordRow.valid_from.is_(None),
                CampaignRecordRow.valid_to.is_(None),
            ]
        )
    return predicates


def _dominant_label(variants: Counter[str]) -> str:
    """Most frequent verbatim casing; ties break to the lexicographic minimum."""

    return max(sorted(variants.items()), key=lambda item: item[1])[0]


def _facet_options(values: Iterable[tuple[str, str, int]]) -> list[FacetOption]:
    ordered = sorted(values, key=lambda item: (-item[2], item[0]))[:_MAX_FACET_OPTIONS]
    return [FacetOption(value=value, label=label, count=count) for value, label, count in ordered]
