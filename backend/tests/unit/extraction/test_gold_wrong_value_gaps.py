"""Wrong-value defects exposed by the gold set (dataset 0.2.0), fixed in 1.12.

Each test mirrors one gold example whose defect published an incorrect value.
A missed fact is safe; a wrong fact is not; when genuinely ambiguous, abstain.
"""

from __future__ import annotations

from decimal import Decimal

from _factories import make_document

from katilim_analiz.contracts import CampaignType, FeeBasis, FeeKind, RateKind, RatePeriod
from katilim_analiz.extraction.rules import extract_rules


def test_adjacent_table_hint_does_not_bleed_past_a_new_header() -> None:
    """Gold-edge-table-003: the %41,00 sits under its own Yillik Maliyet Orani
    header; the first table's monthly-profit hint must not price it."""

    document = make_document(
        ("heading", "Taşıt Finansmanı Oranları"),
        ("table", "Vade | Kar Oranı"),
        ("table", "12 Ay | %3,10"),
        ("table", "Vade | Yıllık Maliyet Oranı"),
        ("table", "12 Ay | %41,00"),
    )

    draft = extract_rules(document)

    by_percent = {rate.value.value_percent: rate.value for rate in draft.rates}
    assert set(by_percent) == {Decimal("3.10"), Decimal("41.00")}
    profit = by_percent[Decimal("3.10")]
    assert profit.kind is RateKind.FINANCING_PROFIT_RATE
    assert profit.period is RatePeriod.MONTHLY
    assert profit.term_months == 12
    cost = by_percent[Decimal("41.00")]
    assert cost.kind is RateKind.ANNUAL_COST_RATE
    assert cost.period is RatePeriod.ANNUAL
    assert cost.term_months == 12


def test_hint_bleed_reset_keeps_unhinted_following_table_out_of_rates() -> None:
    """A following header the rules cannot read yields no rate at all — the
    previous table's hint must not flow in as a substitute."""

    document = make_document(
        ("heading", "Taşıt Finansmanı Oranları"),
        ("table", "Vade | Kar Oranı"),
        ("table", "12 Ay | %3,10"),
        ("table", "Kategori | Gösterge"),
        ("table", "Standart | %41,00"),
    )

    draft = extract_rules(document)

    assert all(rate.value.value_percent != Decimal("41.00") for rate in draft.rates)


def test_ltv_table_header_is_not_a_financing_rate_campaign() -> None:
    """Gold-ltv-001/002, gold-edge-table-004: '(Azami) Finansman Orani' in a
    value-bracket table is the LTV share, not a priced campaign."""

    for header in (
        "Tutar Aralığı | Finansman Oranı | Vade (Ay)",
        "Tutar Aralığı | Azami Finansman Oranı | Vade",
    ):
        document = make_document(
            ("heading", "Konut Finansmanı Oranları"),
            ("table", header),
            ("table", "0-1.000.000 TL | %80 | 120 Ay"),
        )

        draft = extract_rules(document)

        assert draft.campaign_type is None, header
        assert any(rate.value.kind is RateKind.LTV_RATIO for rate in draft.rates), header


def test_real_financing_rate_wording_still_marks_the_campaign() -> None:
    document = make_document(
        ("heading", "Taşıt Finansmanı Özel Oran"),
        ("paragraph", "Kampanya kapsamında finansman kâr payı oranı aylık %1,89 uygulanır."),
    )

    draft = extract_rules(document)

    assert draft.campaign_type is not None
    assert draft.campaign_type.value is CampaignType.FINANCING_RATE


def test_expired_rate_is_skipped_and_in_force_rate_publishes() -> None:
    """Gold-edge-conf-002: relative to the clock (2026-07-18) only the %3,60
    window is in force; the withdrawn %3,20 must not be published, and the
    'tarihinden itibaren' wording is a validity date, not a rate bound."""

    document = make_document(
        ("heading", "Taşıt Finansmanı Dönem Oranları"),
        (
            "paragraph",
            "30 Haziran 2026 tarihine kadar geçerli aylık kar oranı %3,20 olarak uygulanmıştır.",
        ),
        (
            "paragraph",
            "1 Temmuz 2026 tarihinden itibaren geçerli aylık kar oranı %3,60 olarak uygulanır.",
        ),
    )

    draft = extract_rules(document)

    assert len(draft.rates) == 1
    rate = draft.rates[0].value
    assert rate.value_percent == Decimal("3.60")
    assert rate.kind is RateKind.FINANCING_PROFIT_RATE
    assert rate.period is RatePeriod.MONTHLY


def test_future_dated_rate_is_not_published_yet() -> None:
    document = make_document(
        ("heading", "Taşıt Finansmanı Dönem Oranları"),
        (
            "paragraph",
            "1 Ağustos 2026 tarihinden itibaren geçerli aylık kar oranı %4,10 olarak uygulanır.",
        ),
    )

    draft = extract_rules(document)

    assert draft.rates == ()


def test_basis_points_price_an_early_exit_fee_not_a_points_campaign() -> None:
    """Gold-edge-num-003: '150 baz puan' is a pricing unit (1,5%), the fee is
    an early-closure commission, and no points campaign exists."""

    document = make_document(
        ("heading", "Konut Finansmanı Masraf Tarifesi"),
        ("paragraph", "Tahsis ücreti binde 2 olarak uygulanır."),
        ("paragraph", "Erken kapama komisyonu 150 baz puan olarak tahsil edilir."),
    )

    draft = extract_rules(document)

    assert draft.campaign_type is None
    by_kind = {fee.value.kind: fee.value for fee in draft.fees}
    assert set(by_kind) == {FeeKind.ALLOCATION, FeeKind.EARLY_EXIT}
    early_exit = by_kind[FeeKind.EARLY_EXIT]
    assert early_exit.basis is FeeBasis.PERCENT_OF_AMOUNT
    assert early_exit.rate is not None
    assert early_exit.rate.value_percent == Decimal("1.5")


def test_side_perk_free_eft_does_not_type_the_campaign_fee_waiver() -> None:
    """Gold-edge-camp-003: a free-EFT perk sentence waives a side-service fee
    without establishing a fee-waiver campaign."""

    document = make_document(
        ("heading", "Konut Finansmanı Avantaj Paketi"),
        ("paragraph", "Konut finansmanı müşterileri için EFT işlemleri ücretsiz olarak sunulur."),
    )

    draft = extract_rules(document)

    assert draft.campaign_type is None
    assert len(draft.fees) == 1
    assert draft.fees[0].value.waived is True


def test_fee_waiver_marker_in_the_title_still_types_the_campaign() -> None:
    """Gold-fee-003: a heading that itself carries the waiver marker names the
    campaign even when the body sentence only negates the charge."""

    document = make_document(
        ("heading", "Konut Finansmanı'nda Masrafsız Dönem"),
        ("paragraph", "500.000 TL'ye kadar tahsis masrafı alınmaz."),
    )

    draft = extract_rules(document)

    assert draft.campaign_type is not None
    assert draft.campaign_type.value is CampaignType.FEE_WAIVER
