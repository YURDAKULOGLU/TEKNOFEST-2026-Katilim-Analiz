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

# Issue #30: live table rows, verbatim.  The header names the profit column;
# the rows themselves carry no context words at all.
EMLAK_PROFIT_TABLE_HEADER = "Finansman Tutarı | Vade | Kâr Oranı"
EMLAK_PROFIT_TABLE_ROW = "30.000,00 ₺ | 12 Ay | 1,69%"
VAKIF_PROFIT_TABLE_HEADER = (
    "Finansman Tutarı | Vade | Kâr Oranı | Tahsis Ücreti | Yıllık Maliyet Oranı"
)
VAKIF_PROFIT_TABLE_ROW = "100.000 TL | 12 Ay | %3.50 | 500 ₺ | %72,5541"


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


def test_profit_rate_table_header_hint_recovers_bare_row_percentage() -> None:
    """Issue #30, emlak-katilim ihtiyac: '30.000,00 TL | 12 Ay | 1,69%'.

    The 'Kar Orani' header plus the Vade/Ay column signature carries the
    financing-profit + monthly meaning into the bare row, so the one real
    monthly rate is recovered instead of falling to abstention.
    """

    document = make_document(
        ("heading", "İhtiyaç Finansmanı"),
        ("table", EMLAK_PROFIT_TABLE_HEADER),
        ("table", EMLAK_PROFIT_TABLE_ROW),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("1.69")
    assert rate.kind is RateKind.FINANCING_PROFIT_RATE
    assert rate.period is RatePeriod.MONTHLY
    assert rate.status is EvidenceStatus.INFERRED
    # The evidence span is the exact profit-column cell of the row.
    assert draft.rates[0].span.quote == "1,69%"
    assert ModelFactField.RATE not in draft.unresolved_fields


def test_two_percent_row_attaches_hint_only_to_the_profit_column() -> None:
    """Issue #30, vakif-katilim tasit: '100.000 TL | 12 Ay | %3.50 | ... | %72,5541'.

    The row carries two percentages: the monthly kar orani (%3.50) and the
    annual cost (%72,5541).  Positional attribution against the header keeps
    the hint on the kar orani column only; the cost percentage is never
    recorded as a rate.
    """

    document = make_document(
        ("heading", "Taşıt Finansmanı"),
        ("table", VAKIF_PROFIT_TABLE_HEADER),
        ("table", VAKIF_PROFIT_TABLE_ROW),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("3.50")
    assert rate.kind is RateKind.FINANCING_PROFIT_RATE
    assert rate.period is RatePeriod.MONTHLY
    assert rate.status is EvidenceStatus.INFERRED
    assert draft.rates[0].span.quote == "%3.50"
    assert all(item.value.value_percent != Decimal("72.5541") for item in draft.rates)
    assert ModelFactField.RATE not in draft.unresolved_fields


def test_row_that_cannot_be_attributed_to_a_column_abstains() -> None:
    """A ragged multi-percent row under a multi-rate header records nothing."""

    document = make_document(
        ("heading", "Taşıt Finansmanı"),
        ("table", VAKIF_PROFIT_TABLE_HEADER),
        ("table", "12 Ay | %3.50 | %72,5541"),
    )

    draft = extract_rules(document)

    assert draft.rates == ()
    assert ModelFactField.RATE in draft.unresolved_fields


def test_participation_account_profit_table_never_becomes_a_financing_rate() -> None:
    """A deposit 'Vade | Kar Payi Orani' table is a distribution share, not a price."""

    document = make_document(
        ("heading", "Katılma Hesabı Kâr Payı Dağıtım Oranları"),
        ("table", "Vade | Kâr Payı Oranı"),
        ("table", "12 Ay | %45,00"),
    )

    draft = extract_rules(document)

    assert all(item.value.kind is not RateKind.FINANCING_PROFIT_RATE for item in draft.rates)
    assert draft.rates == ()


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
