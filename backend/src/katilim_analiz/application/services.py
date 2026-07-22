"""Deterministic application use cases and grounded Turkish answer assembly."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from katilim_analiz.application.answering import (
    AnswerBundle,
    answer_is_grounded,
    build_answer_bundle,
    select_relevant,
)
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
from katilim_analiz.application.ports import (
    AnswerComposerPort,
    CampaignReadPort,
    DashboardReadPort,
)
from katilim_analiz.contracts import (
    ChatAnswer,
    ChatQueryPlan,
    ChatRequest,
    ComparisonRequest,
    ComparisonResponse,
    CoverageEntry,
    QueryIntent,
    RateKind,
    RatePeriod,
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
    # Issue #28: only a monthly profit rate may headline as "Oran".  An annual
    # cost rate, an LTV share, or an unclassified percentage in rates[0] would
    # present a cost or a ceiling as the price; those kinds fall through to the
    # existing amount/reward/term choices instead.
    monthly_profit_rate = next(
        (
            rate
            for rate in data.rates
            if rate.kind is RateKind.FINANCING_PROFIT_RATE and rate.period is RatePeriod.MONTHLY
        ),
        None,
    )
    if monthly_profit_rate is not None:
        return PrimaryValue(
            label="Oran",
            value=f"%{_decimal_text(monthly_profit_rate.value_percent)}",
        )
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
        next_cursor = None
        if page.has_more and ordered:
            last = ordered[-1]
            next_cursor = self._notification_cursor_codec.encode(
                NotificationCursor(feed_sequence=last.feed_sequence)
            )
        elif not ordered and cursor is not None and after is not None:
            canonical = self._notification_cursor_codec.encode(after)
            if canonical != cursor:
                # A legacy v1 cursor decodes to the replay anchor; re-issue the
                # canonical v2 cursor so the client migrates off the v1 format.
                # A canonical cursor on an exhausted feed returns None instead
                # of echoing itself, so `while next_cursor` clients terminate.
                next_cursor = canonical
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


def _abstention(plan: ChatQueryPlan, warnings: Iterable[str] = ()) -> ChatAnswer:
    return ChatAnswer(
        answer="Bu soruyu yanıtlamak için yeterli doğrulanmış kaynak kanıtı bulunamadı.",
        plan=plan,
        citations=[],
        insufficient_evidence=True,
        warnings=list(warnings),
    )


class ChatService:
    """Three-layer grounded chat (issue #16).

    Layer 1 retrieves and ranks validated records deterministically; layer 2
    lets an optional local model rephrase only the retrieved facts; layer 3 is
    a deterministic number-grounding gate that falls back to the template
    answer, so chat keeps working — with the same facts — when the model is
    down or its answer drifts. Model-authored text is never executed and never
    passes the gate with numbers the evidence does not contain.
    """

    def __init__(
        self,
        reads: CampaignReadPort,
        *,
        planner: SafeChatPlanner | None = None,
        composer: AnswerComposerPort | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._reads = reads
        self._planner = planner or SafeChatPlanner()
        self._composer = composer
        self._clock = clock

    async def answer(self, request: ChatRequest) -> ChatAnswer:
        planned = await self._planner.plan(request.question)
        plan = planned.plan
        as_of = _effective_time(request.as_of, self._clock)
        if plan.intent in {QueryIntent.UNKNOWN, QueryIntent.GLOSSARY, QueryIntent.COVERAGE}:
            return _abstention(plan, planned.warnings)

        projections = select_relevant(
            request.question,
            plan,
            await self._retrieve_validated(plan, as_of),
        )
        if not projections:
            return _abstention(plan, planned.warnings)
        bundle = build_answer_bundle(request.question, plan, projections, as_of=as_of)
        if bundle is None:
            return _abstention(plan, planned.warnings)

        answer_text = bundle.template_answer
        warnings = [*planned.warnings, *bundle.warnings]
        composed = await self._compose(request.question, bundle)
        if composed is not None:
            if answer_is_grounded(composed, bundle.facts):
                answer_text = composed
            else:
                warnings.append("Model cevabı kanıt kapısından geçemedi; şablon cevap sunuldu.")
        return ChatAnswer(
            answer=answer_text,
            plan=plan,
            citations=list(bundle.citations),
            insufficient_evidence=False,
            warnings=warnings,
        )

    async def _retrieve_validated(
        self,
        plan: ChatQueryPlan,
        as_of: datetime,
    ) -> list[CampaignProjection]:
        """Run the typed search with a deterministic relaxation ladder.

        The strict plan may over-constrain retrieval (AND-semantics keyword
        text search, or an inferred campaign type the stated records do not
        carry). Each relaxation only widens recall; `select_relevant` restores
        keyword and bank precision in-process before anything is answered.
        """

        attempts = [plan]
        if plan.keywords:
            attempts.append(attempts[-1].model_copy(update={"keywords": []}))
        if plan.campaign_type is not None:
            attempts.append(attempts[-1].model_copy(update={"campaign_type": None}))
        for attempt in attempts:
            validated = [
                item
                for item in await self._reads.search(attempt, as_of=as_of)
                if item.record.status is RecordStatus.VALIDATED
            ]
            if validated:
                return validated
        return []

    async def _compose(self, question: str, bundle: AnswerBundle) -> str | None:
        if self._composer is None:
            return None
        try:
            return await self._composer.compose(question, bundle.facts)
        except Exception:  # a sick model must never break the deterministic answer
            return None
