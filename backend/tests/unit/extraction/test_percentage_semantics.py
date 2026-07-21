"""Issue #28: percentage semantics from live financing pages.

The four fixtures below are verbatim sentences from the 21 July live scan that
were all recorded as a primary "Oran" value.  Only one of them is a real
monthly profit rate; the rules must classify each percentage by its Turkish
context before it may become a rate, and must abstain when the context is
unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from _factories import make_document

from katilim_analiz.application.models import CampaignProjection
from katilim_analiz.application.services import _primary_value
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    TermRange,
)
from katilim_analiz.extraction.rules import extract_rules, rate_semantics_unresolved
from katilim_analiz.llm.contracts import ModelFactField

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

# Live examples from issue #28, verbatim.
ANNUAL_COST_SENTENCE = "Yıllık Maliyet Oranı % 81,59"
ALLOCATION_FEE_SENTENCE = "*Tahsis ücreti finansman tutarının %0,5'idir (BSMV Hariç)"
LTV_TABLE_HEADER = "Tutar Aralığı | Finansman Oranı | Vade (Ay)"
LTV_TABLE_ROW = "0-400,000 TL | %70 | 48"
MONTHLY_PROFIT_SENTENCE = "Aylık kâr payı oranı %1,69"


def test_annual_cost_rate_is_classified_annual_and_never_monthly() -> None:
    """Albaraka konut: 'Yillik Maliyet Orani % 81,59' is a cost, not the rate."""

    document = make_document(
        ("heading", "Konut Finansmanı"),
        ("paragraph", ANNUAL_COST_SENTENCE),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("81.59")
    assert rate.kind is RateKind.ANNUAL_COST_RATE
    assert rate.period is RatePeriod.ANNUAL


def test_allocation_fee_percentage_is_never_recorded_as_a_rate() -> None:
    """Vakif Katilim tasit: the %0,5 allocation fee must stay out of rates."""

    document = make_document(
        ("heading", "Taşıt Finansmanı"),
        ("paragraph", ALLOCATION_FEE_SENTENCE),
    )

    draft = extract_rules(document)

    assert all(rate.value.value_percent != Decimal("0.5") for rate in draft.rates)
    assert draft.rates == ()


def test_ltv_table_row_is_classified_as_financing_share_not_profit_rate() -> None:
    """Emlak Katilim tasit: the %70 in a value-bracket table is the LTV share."""

    document = make_document(
        ("heading", "Taşıt Finansmanı"),
        ("table", LTV_TABLE_HEADER),
        ("table", LTV_TABLE_ROW),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("70")
    assert rate.kind is RateKind.LTV_RATIO
    assert rate.status is EvidenceStatus.INFERRED
    # An LTV share alone never answers the pricing-rate question.
    assert ModelFactField.RATE in draft.unresolved_fields


def test_monthly_profit_rate_is_classified_monthly_profit() -> None:
    """Emlak Katilim ihtiyac: %1,69 aylik kar payi is the one real rate."""

    document = make_document(
        ("heading", "İhtiyaç Finansmanı"),
        ("paragraph", MONTHLY_PROFIT_SENTENCE),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("1.69")
    assert rate.kind is RateKind.FINANCING_PROFIT_RATE
    assert rate.period is RatePeriod.MONTHLY


def test_unknown_context_percentage_is_not_recorded_as_a_rate() -> None:
    document = make_document(
        ("heading", "Kampanya Duyurusu"),
        ("paragraph", "Kampanya kapsamında %20 avantaj sağlanır."),
    )

    draft = extract_rules(document)

    assert draft.rates == ()
    assert ModelFactField.RATE in draft.unresolved_fields


def test_rate_semantics_unresolved_treats_ltv_as_contextual() -> None:
    monthly = RateValue(
        raw="%1,69",
        value_percent=Decimal("1.69"),
        kind=RateKind.FINANCING_PROFIT_RATE,
        period=RatePeriod.MONTHLY,
    )
    ltv = RateValue(
        raw="%70",
        value_percent=Decimal("70"),
        kind=RateKind.LTV_RATIO,
        period=RatePeriod.UNSPECIFIED,
    )

    assert rate_semantics_unresolved(()) is True
    assert rate_semantics_unresolved((ltv,)) is True
    assert rate_semantics_unresolved((monthly,)) is False
    assert rate_semantics_unresolved((monthly, ltv)) is False


def _projection(rates: tuple[RateValue, ...]) -> CampaignProjection:
    record = CampaignRecord(
        id="campaign-28",
        version=1,
        source_document_id="doc-28",
        observed_at=_NOW,
        data=CampaignData(
            bank_id="bank-28",
            title="Taşıt Finansmanı",
            product_family=ProductFamily.FINANCING,
            campaign_type=CampaignType.FINANCING_RATE,
            rates=list(rates),
            terms=[TermRange(raw="48 ay", minimum_months=48, maximum_months=48)],
        ),
        evidence=[],
        extraction=ExtractionMetadata(
            method=ExtractionMethod.RULE,
            extractor_version="test",
            schema_version="1",
            started_at=_NOW,
            completed_at=_NOW,
        ),
        status=RecordStatus.VALIDATED,
        record_sha256="0" * 64,
    )
    return CampaignProjection(
        record=record,
        bank_name="Banka 28",
        source_url="https://example.test/kampanya",
    )


def test_primary_value_is_chosen_only_from_monthly_profit_rates() -> None:
    annual_cost = RateValue(
        raw="% 81,59",
        value_percent=Decimal("81.59"),
        kind=RateKind.ANNUAL_COST_RATE,
        period=RatePeriod.ANNUAL,
    )
    ltv = RateValue(
        raw="%70",
        value_percent=Decimal("70"),
        kind=RateKind.LTV_RATIO,
        period=RatePeriod.UNSPECIFIED,
    )
    monthly = RateValue(
        raw="%1,69",
        value_percent=Decimal("1.69"),
        kind=RateKind.FINANCING_PROFIT_RATE,
        period=RatePeriod.MONTHLY,
    )

    with_monthly = _primary_value(_projection((annual_cost, ltv, monthly)))
    assert with_monthly is not None
    assert with_monthly.label == "Oran"
    assert with_monthly.value == "%1,69"

    # Without a monthly profit rate the showcase falls back to the term
    # instead of presenting a cost or a ceiling as the price.
    without_monthly = _primary_value(_projection((annual_cost, ltv)))
    assert without_monthly is not None
    assert without_monthly.label == "Azami vade"
    assert without_monthly.value == "48 ay"
