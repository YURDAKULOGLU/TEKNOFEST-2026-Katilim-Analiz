"""Deterministic application use cases and grounded Turkish answer assembly."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from katilim_analiz.application.cursor import CursorCodec
from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignDetail,
    CampaignListFilters,
    CampaignListResponse,
    CampaignProjection,
    CampaignSummary,
    EvidenceProjection,
    ExtractionProjection,
    NotificationCursor,
    NotificationListResponse,
    PrimaryValue,
)
from katilim_analiz.application.planning import SafeChatPlanner
from katilim_analiz.application.ports import CampaignReadPort, DashboardReadPort
from katilim_analiz.contracts import (
    AnswerCitation,
    ChatAnswer,
    ChatQueryPlan,
    ChatRequest,
    CitationStatus,
    ComparisonRequest,
    ComparisonResponse,
    CoverageEntry,
    EvidenceStatus,
    QueryIntent,
    RecordStatus,
)
from katilim_analiz.domain.comparison import compare_campaigns
from katilim_analiz.notifications.cursor import NotificationCursorCodec


class ApplicationServiceError(RuntimeError):
    code = "application_error"


class CampaignNotFoundError(ApplicationServiceError):
    code = "campaign_not_found"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _effective_time(value: datetime | None, clock: Callable[[], datetime]) -> datetime:
    effective = value or clock()
    if effective.tzinfo is None or effective.utcoffset() is None:
        raise ValueError("application clock and as_of must be timezone-aware")
    return effective


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    rendered = "0" if normalized == 0 else format(normalized, "f")
    return rendered.replace(".", ",")


def _primary_value(projection: CampaignProjection) -> PrimaryValue | None:
    data = projection.record.data
    if data.rates:
        rate = data.rates[0]
        return PrimaryValue(label="Oran", value=f"%{_decimal_text(rate.value_percent)}")
    if data.financing_amounts:
        money = data.financing_amounts[0]
        return PrimaryValue(
            label="Tutar",
            value=f"{_decimal_text(money.amount)} {money.currency}",
        )
    if data.rewards:
        reward = data.rewards[0]
        if reward.money is not None:
            return PrimaryValue(
                label="Fayda",
                value=f"{_decimal_text(reward.money.amount)} {reward.money.currency}",
            )
        if reward.points is not None:
            return PrimaryValue(label="Fayda", value=f"{_decimal_text(reward.points)} puan")
    if data.terms:
        return PrimaryValue(label="Azami vade", value=f"{data.terms[0].maximum_months} ay")
    return None


def _summary(projection: CampaignProjection) -> CampaignSummary:
    record = projection.record
    return CampaignSummary(
        id=record.id,
        campaign_key=projection.campaign_key or record.id,
        version=record.version,
        bank_id=record.data.bank_id,
        bank_name=projection.bank_name,
        title=record.data.title,
        product_family=record.data.product_family,
        campaign_type=record.data.campaign_type,
        summary=record.data.summary,
        currency=record.data.comparison_context.product_currency,
        customer_segments=record.data.customer_segments,
        sales_channel=record.data.comparison_context.sales_channel,
        validity=record.data.validity,
        observed_at=record.observed_at,
        status=record.status,
        evidence_count=len(record.evidence),
        primary_value=_primary_value(projection),
    )


def _detail(projection: CampaignProjection) -> CampaignDetail:
    record = projection.record
    return CampaignDetail(
        campaign=_summary(projection),
        source_document_id=record.source_document_id,
        source_url=projection.source_url,
        source_title=projection.source_title,
        record_sha256=record.record_sha256,
        extraction=ExtractionProjection(
            method=record.extraction.method,
            extractor_version=record.extraction.extractor_version,
            schema_version=record.extraction.schema_version,
            model_id=record.extraction.model_id,
        ),
        evidence=[
            EvidenceProjection(
                id=evidence.id,
                field_pointer=evidence.field_pointer,
                quote=evidence.quote,
                status=evidence.status,
                block_id=evidence.block_id,
            )
            for evidence in sorted(record.evidence, key=lambda item: (item.field_pointer, item.id))
        ],
        validation_issues=record.validation_issues,
    )


class CampaignService:
    def __init__(
        self,
        reads: DashboardReadPort,
        *,
        clock: Callable[[], datetime] = _utc_now,
        cursor_codec: CursorCodec | None = None,
        notification_cursor_codec: NotificationCursorCodec | None = None,
    ) -> None:
        self._reads = reads
        self._clock = clock
        self._cursor_codec = cursor_codec or CursorCodec()
        self._notification_cursor_codec = notification_cursor_codec or NotificationCursorCodec()

    async def list_campaigns(
        self,
        filters: CampaignListFilters,
        *,
        cursor: str | None,
        limit: int,
        as_of: datetime | None,
    ) -> CampaignListResponse:
        effective_as_of = _effective_time(as_of, self._clock)
        after = None if cursor is None else self._cursor_codec.decode(cursor)
        page = await self._reads.list_latest(
            filters=filters,
            after=after,
            limit=limit,
            as_of=effective_as_of,
        )
        ordered = sorted(
            page.items,
            key=lambda item: (-item.record.observed_at.timestamp(), item.record.id),
        )
        next_cursor = None
        if page.has_more and ordered:
            last = ordered[-1].record
            next_cursor = self._cursor_codec.encode(
                CampaignCursor(observed_at=last.observed_at, campaign_id=last.id)
            )
        return CampaignListResponse(
            items=[_summary(item) for item in ordered],
            next_cursor=next_cursor,
            facets=page.facets,
            as_of=effective_as_of,
        )

    async def get_campaign(
        self,
        campaign_id: str,
        *,
        as_of: datetime | None,
    ) -> CampaignDetail:
        projection = await self._reads.get(
            campaign_id,
            as_of=_effective_time(as_of, self._clock),
        )
        if projection is None:
            raise CampaignNotFoundError(f"campaign {campaign_id!r} was not found")
        return _detail(projection)

    async def list_notifications(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> NotificationListResponse:
        after = None if cursor is None else self._notification_cursor_codec.decode(cursor)
        page = await self._reads.list_notifications(after=after, limit=limit)
        unique_items = {item.event.id: item for item in page.items}
        ordered = sorted(
            (
                item
                for item in unique_items.values()
                if after is None or item.feed_sequence > after.feed_sequence
            ),
            key=lambda item: item.feed_sequence,
        )
        next_cursor = None if after is None else self._notification_cursor_codec.encode(after)
        if ordered:
            last = ordered[-1]
            next_cursor = self._notification_cursor_codec.encode(
                NotificationCursor(feed_sequence=last.feed_sequence)
            )
        return NotificationListResponse(
            items=[item.event for item in ordered],
            next_cursor=next_cursor,
        )

    async def coverage(self, *, as_of: datetime | None) -> list[CoverageEntry]:
        entries = await self._reads.latest_coverage(as_of=_effective_time(as_of, self._clock))
        return sorted(entries, key=lambda item: item.bank_id)

    async def compare(self, request: ComparisonRequest) -> ComparisonResponse:
        generated_at = _effective_time(None, self._clock)
        effective_as_of = request.as_of or generated_at
        projections = await self._reads.get_many(request.campaign_ids, as_of=effective_as_of)
        by_id = {item.record.id: item for item in projections}
        missing = [identifier for identifier in request.campaign_ids if identifier not in by_id]
        if missing:
            raise CampaignNotFoundError(f"campaign records were not found: {', '.join(missing)}")
        report = compare_campaigns(
            [by_id[identifier].record for identifier in request.campaign_ids],
            request.dimensions,
            as_of=effective_as_of,
        )
        return report.to_response(generated_at=generated_at)


def _citations(
    projection: CampaignProjection,
    *,
    allowed_ids: set[str] | None = None,
) -> list[AnswerCitation]:
    citations: list[AnswerCitation] = []
    candidates = sorted(projection.record.evidence, key=lambda item: item.id)
    for evidence in candidates:
        if evidence.status is not EvidenceStatus.STATED:
            continue
        if allowed_ids is not None and evidence.id not in allowed_ids:
            continue
        citations.append(
            AnswerCitation(
                id=evidence.id,
                source_document_id=evidence.source_document_id,
                block_id=evidence.block_id,
                source_url=projection.source_url,
                source_title=projection.source_title,
                quote=evidence.quote,
                status=CitationStatus.VERIFIED,
            )
        )
    return citations


def _abstention(plan: ChatQueryPlan, warnings: Iterable[str] = ()) -> ChatAnswer:
    return ChatAnswer(
        answer="Bu soruyu yanıtlamak için yeterli doğrulanmış kaynak kanıtı bulunamadı.",
        plan=plan,
        citations=[],
        insufficient_evidence=True,
        warnings=list(warnings),
    )


class ChatService:
    """Execute typed plans and render templates; never execute model-authored text."""

    def __init__(
        self,
        reads: CampaignReadPort,
        *,
        planner: SafeChatPlanner | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._reads = reads
        self._planner = planner or SafeChatPlanner()
        self._clock = clock

    async def answer(self, request: ChatRequest) -> ChatAnswer:
        planned = await self._planner.plan(request.question)
        plan = planned.plan
        as_of = _effective_time(request.as_of, self._clock)
        if plan.intent in {QueryIntent.UNKNOWN, QueryIntent.GLOSSARY, QueryIntent.COVERAGE}:
            return _abstention(plan, planned.warnings)

        projections = [
            item
            for item in await self._reads.search(plan, as_of=as_of)
            if item.record.status is RecordStatus.VALIDATED
        ]
        if plan.intent is QueryIntent.COMPARE:
            return self._comparison_answer(plan, projections, as_of, planned.warnings)
        return self._list_answer(plan, projections, planned.warnings)

    @staticmethod
    def _list_answer(
        plan: ChatQueryPlan,
        projections: list[CampaignProjection],
        warnings: tuple[str, ...],
    ) -> ChatAnswer:
        cited: list[tuple[CampaignProjection, AnswerCitation]] = []
        for projection in projections:
            citations = _citations(projection)
            if citations:
                cited.append((projection, citations[0]))
        if not cited:
            return _abstention(plan, warnings)
        answer = "Resmî kaynaklarda doğrulanmış şu ifadeler bulundu:"
        selected: list[AnswerCitation] = []
        for projection, citation in cited[:20]:
            line = f"- {projection.bank_name}: “{citation.quote}”"
            if len(answer) + 1 + len(line) > 5_000:
                continue
            answer += f"\n{line}"
            selected.append(citation)
        if not selected:
            return _abstention(plan, warnings)
        return ChatAnswer(
            answer=answer,
            plan=plan,
            citations=selected,
            insufficient_evidence=False,
            warnings=list(warnings),
        )

    @staticmethod
    def _comparison_answer(
        plan: ChatQueryPlan,
        projections: list[CampaignProjection],
        as_of: datetime,
        warnings: tuple[str, ...],
    ) -> ChatAnswer:
        if len(projections) < 2 or not plan.comparison_dimensions:
            return _abstention(
                plan,
                (*warnings, "Karşılaştırma için en az iki kayıt ve açık bir boyut gerekir."),
            )
        report = compare_campaigns(
            [item.record for item in projections],
            plan.comparison_dimensions,
            as_of=as_of,
        )
        evidence_ids = {
            evidence_id
            for item in report.items
            if item.comparable
            for evidence_id in item.evidence_ids
        }
        citations = [
            citation
            for projection in projections
            for citation in _citations(projection, allowed_ids=evidence_ids)
        ]
        citation_by_id = {citation.id: citation for citation in citations[:20]}
        comparable = [
            item
            for item in report.items
            if item.comparable and any(value in citation_by_id for value in item.evidence_ids)
        ]
        if not comparable or not citations:
            return _abstention(plan, (*warnings, *report.warnings))
        projection_by_id = {item.record.id: item for item in projections}
        answer = "Aynı bazdaki doğrulanmış değerlerin karşılaştırması:"
        selected_ids: set[str] = set()
        for item in comparable:
            projection = projection_by_id[item.campaign_id]
            line = f"- {projection.bank_name} — {item.dimension.value}: {item.display_value}"
            if item.rank is not None:
                line += f" (sıra {item.rank})"
            if len(answer) + 1 + len(line) > 5_000:
                continue
            answer += f"\n{line}"
            selected_ids.update(
                evidence_id for evidence_id in item.evidence_ids if evidence_id in citation_by_id
            )
        selected_citations = [
            citation for citation in citations[:20] if citation.id in selected_ids
        ]
        if not selected_citations:
            return _abstention(plan, (*warnings, *report.warnings))
        return ChatAnswer(
            answer=answer,
            plan=plan,
            citations=selected_citations,
            insufficient_evidence=False,
            warnings=[*warnings, *report.warnings],
        )
