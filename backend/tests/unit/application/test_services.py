from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from katilim_analiz.application.cursor import CursorCodec
from katilim_analiz.application.models import (
    CampaignFacets,
    CampaignListFilters,
    CampaignProjection,
    CampaignReadSlice,
)
from katilim_analiz.application.services import CampaignNotFoundError, CampaignService, ChatService
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    ChatRequest,
    ComparisonContext,
    ComparisonDimension,
    ComparisonRequest,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    QueryIntent,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    ValidityWindow,
)

NOW = datetime(2026, 7, 18, 18, 30, tzinfo=UTC)
SHA = "0" * 64


def projection(
    identifier: str,
    *,
    bank_id: str = "bank-a",
    bank_name: str = "Banka A",
    family: ProductFamily = ProductFamily.FINANCING,
    rate: str = "1.50",
    with_evidence: bool = True,
    evidence_quote: str | None = None,
) -> CampaignProjection:
    quote = evidence_quote or f"Aylık kâr oranı %{rate}"
    evidence = (
        [
            EvidenceRef(
                id=f"evidence:{identifier}",
                field_pointer="/data/rates/0/value_percent",
                source_document_id=f"doc:{identifier}",
                block_id=f"block:{identifier}",
                quote=quote,
                start_char=0,
                end_char=len(quote),
                evidence_sha256=SHA,
                status=EvidenceStatus.STATED,
            )
        ]
        if with_evidence
        else []
    )
    record = CampaignRecord(
        id=identifier,
        version=1,
        source_document_id=f"doc:{identifier}",
        observed_at=NOW,
        data=CampaignData(
            bank_id=bank_id,
            title=f"Kampanya {identifier}",
            product_family=family,
            campaign_type=CampaignType.FINANCING_RATE,
            rates=[
                RateValue(
                    raw=f"%{rate}",
                    value_percent=Decimal(rate),
                    kind=RateKind.FINANCING_PROFIT_RATE,
                    period=RatePeriod.MONTHLY,
                    term_months=12,
                    basis_label="nominal",
                )
            ],
            validity=ValidityWindow(
                raw="2026",
                starts_on=date(2026, 1, 1),
                ends_on=date(2026, 12, 31),
            ),
            customer_segments=["Bireysel"],
            comparison_context=ComparisonContext(
                product_currency="TRY",
                customer_segment_keys=["retail"],
                sales_channel=SalesChannel.ALL,
                new_customer_only=False,
                product_mechanism="standard",
                secured=False,
            ),
        ),
        evidence=evidence,
        extraction=ExtractionMetadata(
            method=ExtractionMethod.RULE,
            extractor_version="test",
            schema_version="1",
            started_at=NOW,
            completed_at=NOW,
        ),
        status=RecordStatus.VALIDATED,
        record_sha256=SHA,
    )
    return CampaignProjection(
        record=record,
        bank_name=bank_name,
        source_url=f"https://example.test/{identifier}",
        source_title=f"Kaynak {identifier}",
    )


class FakeCampaignReads:
    def __init__(self, items: list[CampaignProjection]) -> None:
        self.items = items
        self.last_filters: CampaignListFilters | None = None

    async def list_latest(self, *, filters, after, limit, as_of):  # type: ignore[no-untyped-def]
        self.last_filters = filters
        return CampaignReadSlice(
            items=self.items[:limit],
            has_more=len(self.items) > limit,
            facets=CampaignFacets(),
        )

    async def get(self, campaign_id, *, as_of):  # type: ignore[no-untyped-def]
        return next((item for item in self.items if item.record.id == campaign_id), None)

    async def get_many(self, campaign_ids, *, as_of):  # type: ignore[no-untyped-def]
        wanted = set(campaign_ids)
        return [item for item in self.items if item.record.id in wanted]

    async def search(self, plan, *, as_of):  # type: ignore[no-untyped-def]
        return self.items[: plan.limit]

    async def latest_coverage(self, *, as_of):  # type: ignore[no-untyped-def]
        return []


@pytest.mark.asyncio
async def test_list_maps_projection_and_emits_cursor_for_more_rows() -> None:
    reads = FakeCampaignReads([projection("a"), projection("b")])
    service = CampaignService(reads, clock=lambda: NOW)

    page = await service.list_campaigns(CampaignListFilters(), cursor=None, limit=1, as_of=None)

    assert [item.id for item in page.items] == ["a"]
    assert page.items[0].bank_name == "Banka A"
    assert page.items[0].primary_value is not None
    assert page.next_cursor is not None
    assert CursorCodec().decode(page.next_cursor).campaign_id == "a"
    assert page.as_of == NOW


@pytest.mark.asyncio
async def test_comparison_uses_pure_domain_engine_and_rejects_missing_records() -> None:
    reads = FakeCampaignReads([projection("a", rate="1.20"), projection("b", rate="1.80")])
    service = CampaignService(reads, clock=lambda: NOW)
    request = ComparisonRequest(
        campaign_ids=["b", "a"],
        dimensions=[ComparisonDimension.RATE],
        as_of=NOW,
    )

    response = await service.compare(request)

    assert [item.campaign_id for item in response.items] == ["a", "b"]
    assert [item.rank for item in response.items] == [1, 2]
    historical = await service.compare(
        request.model_copy(update={"as_of": NOW - timedelta(days=1)})
    )
    assert historical.generated_at == NOW
    with pytest.raises(CampaignNotFoundError, match="missing"):
        await service.compare(
            ComparisonRequest(
                campaign_ids=["a", "missing"],
                dimensions=[ComparisonDimension.RATE],
                as_of=NOW,
            )
        )


@pytest.mark.asyncio
async def test_chat_answer_is_template_based_and_cited() -> None:
    reads = FakeCampaignReads([projection("a")])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question="finansman kampanyalarını listele", as_of=NOW)
    )

    assert answer.plan.intent is QueryIntent.LIST
    assert answer.insufficient_evidence is False
    assert answer.citations[0].id == "evidence:a"
    # Issue #16: the answer is a composed Turkish sentence built from record
    # fields, not a quote listing; every number must still come from evidence.
    assert "%1.50" in answer.answer
    assert "sunulmaktadır" in answer.answer
    assert "%1.50" in answer.citations[0].quote


@pytest.mark.asyncio
async def test_chat_abstains_when_no_verified_citation_exists() -> None:
    reads = FakeCampaignReads([projection("a", with_evidence=False)])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question="kampanyaları listele", as_of=NOW)
    )

    assert answer.insufficient_evidence is True
    assert answer.citations == []
    assert "doğrulanmış" in answer.answer.lower()


@pytest.mark.asyncio
async def test_chat_bounds_long_stored_quotes_without_detaching_citations() -> None:
    reads = FakeCampaignReads(
        [
            projection(
                f"campaign-{index}",
                bank_name="B" * 500,
                evidence_quote=str(index) + "x" * 999,
            )
            for index in range(5)
        ]
    )
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        # QA fix 1: an unscoped LIST question now abstains, so this bounds
        # test names the product family to stay on the answering path.
        ChatRequest(question="finansman kampanyalarını listele", as_of=NOW)
    )

    assert len(answer.answer) <= 5_000
    assert 1 <= len(answer.citations) <= 5
    assert answer.insufficient_evidence is False
