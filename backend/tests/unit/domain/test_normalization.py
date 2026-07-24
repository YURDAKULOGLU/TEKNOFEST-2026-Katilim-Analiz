from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from katilim_analiz.contracts import EvidenceStatus, RateKind, RatePeriod
from katilim_analiz.domain.normalization import (
    NormalizationStatus,
    normalize_date,
    normalize_money,
    normalize_rate,
    normalize_term,
    normalize_terms,
    normalize_validity,
)


@pytest.mark.parametrize(
    ("raw", "amount", "currency"),
    [
        ("1.234,56 TL", Decimal("1234.56"), "TRY"),
        ("₺ 12 500", Decimal("12500"), "TRY"),
        ("TRY 5.000", Decimal("5000"), "TRY"),
        ("1,5 milyon TL", Decimal("1500000.0"), "TRY"),
        ("25 bin Türk lirası", Decimal("25000"), "TRY"),
        ("1,250.75 USD", Decimal("1250.75"), "USD"),
        ("2.500,00 €", Decimal("2500.00"), "EUR"),
        ("GBP 99.95", Decimal("99.95"), "GBP"),
        # Turkish dot-grouping keeps its magnitude in any currency: 10.000 USD
        # is ten thousand dollars, never ten (gold-edge-cur-001).
        ("10.000 USD", Decimal("10000"), "USD"),
        ("2.500 EUR", Decimal("2500"), "EUR"),
    ],
)
def test_normalize_money_handles_turkish_and_international_variants(
    raw: str,
    amount: Decimal,
    currency: str,
) -> None:
    result = normalize_money(raw)

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.value is not None
    assert result.value.amount == amount
    assert result.value.currency == currency
    assert result.value.raw == raw


def test_money_missing_is_distinct_from_explicit_zero() -> None:
    missing = normalize_money("belirtilmemiş")
    numeric_zero = normalize_money("0,00 TL")
    fee_waiver = normalize_money(
        "Bu işlem için ücret alınmaz",
        currency_hint="TRY",
        allow_fee_waiver=True,
    )

    assert missing.status is NormalizationStatus.MISSING
    assert missing.value is None
    assert numeric_zero.value is not None
    assert numeric_zero.value.amount == Decimal("0.00")
    assert fee_waiver.value is not None
    assert fee_waiver.value.amount == Decimal("0")
    assert fee_waiver.value.status is EvidenceStatus.INFERRED


def test_money_does_not_invent_zero_or_choose_between_conflicting_values() -> None:
    negated_waiver = normalize_money(
        "Ücretsiz değil",
        currency_hint="TRY",
        allow_fee_waiver=True,
    )
    two_values = normalize_money("1.000 TL veya 30 USD")

    assert negated_waiver.status is NormalizationStatus.INVALID
    assert negated_waiver.value is None
    assert two_values.status is NormalizationStatus.AMBIGUOUS
    assert two_values.value is None


def test_money_can_use_an_explicit_caller_currency_context() -> None:
    result = normalize_money("1.250", currency_hint="TRY")

    assert result.value is not None
    assert result.value.amount == Decimal("1250")
    assert result.value.currency == "TRY"
    assert result.value.status is EvidenceStatus.INFERRED


@pytest.mark.parametrize(
    ("raw", "percent", "kind", "period"),
    [
        (
            "Aylık finansman kâr payı oranı %1,49",
            Decimal("1.49"),
            RateKind.FINANCING_PROFIT_RATE,
            RatePeriod.MONTHLY,
        ),
        (
            "Yıllık maliyet oranı yüzde 35.40",
            Decimal("35.40"),
            RateKind.ANNUAL_COST_RATE,
            RatePeriod.ANNUAL,
        ),
        (
            "Kâr dağıtım oranı %80",
            Decimal("80"),
            RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
            RatePeriod.UNSPECIFIED,
        ),
        (
            "Geçmiş yıllık getiri: %42,5",
            Decimal("42.5"),
            RateKind.HISTORICAL_RETURN_RATE,
            RatePeriod.ANNUAL,
        ),
        (
            "Vade sonunda %10 indirim",
            Decimal("10"),
            RateKind.DISCOUNT_RATE,
            RatePeriod.TERM_TOTAL,
        ),
        (
            "150 baz puan aylık finansman oranı",
            Decimal("1.5"),
            RateKind.FINANCING_PROFIT_RATE,
            RatePeriod.MONTHLY,
        ),
    ],
)
def test_normalize_rate_preserves_percent_units_and_infers_semantics(
    raw: str,
    percent: Decimal,
    kind: RateKind,
    period: RatePeriod,
) -> None:
    result = normalize_rate(raw)

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.value is not None
    assert result.value.value_percent == percent
    assert result.value.kind is kind
    assert result.value.period is period


def test_rate_hints_fill_context_but_conflicts_are_rejected() -> None:
    hinted = normalize_rate(
        "%2,15",
        kind_hint=RateKind.FINANCING_PROFIT_RATE,
        period_hint=RatePeriod.MONTHLY,
    )
    conflict = normalize_rate(
        "Yıllık maliyet oranı %20",
        kind_hint=RateKind.DISCOUNT_RATE,
    )

    assert hinted.value is not None
    assert hinted.value.kind is RateKind.FINANCING_PROFIT_RATE
    assert hinted.value.period is RatePeriod.MONTHLY
    assert hinted.value.status is EvidenceStatus.INFERRED
    assert conflict.status is NormalizationStatus.AMBIGUOUS
    assert conflict.value is None


def test_generic_profit_share_wording_does_not_invent_a_rate_kind() -> None:
    for raw in ("Aylık kâr payı oranı %1,49", "Aylık Kar Oranı %3,95"):
        result = normalize_rate(raw)

        assert result.value is not None
        assert result.value.kind is RateKind.UNKNOWN
        assert result.value.period is RatePeriod.MONTHLY


def test_rate_missing_zero_and_multiple_values_remain_distinct() -> None:
    missing = normalize_rate("oran belirtilmemiş")
    zero = normalize_rate(
        "%0,00",
        kind_hint=RateKind.FINANCING_PROFIT_RATE,
        period_hint=RatePeriod.MONTHLY,
    )
    range_value = normalize_rate("Aylık oran %1,49 ile %2,19 arasında")

    assert missing.status is NormalizationStatus.MISSING
    assert zero.value is not None
    assert zero.value.value_percent == Decimal("0.00")
    assert range_value.status is NormalizationStatus.AMBIGUOUS
    assert range_value.value is None


@pytest.mark.parametrize(
    ("raw", "minimum", "maximum"),
    [
        ("36 ay", 36, 36),
        ("3 yıl", 36, 36),
        ("1 yıl 6 ay", 18, 18),
        ("12-24 ay", 12, 24),
        ("12 aydan 36 aya kadar", 12, 36),
        ("12 ila 24 ay vade", 12, 24),
        ("en fazla 36 ay", None, 36),
        ("48 aya varan vade", None, 48),
        ("3,5 yıl", 42, 42),
        ("36 aylık vade", 36, 36),
    ],
)
def test_normalize_term_handles_turkish_duration_variants(
    raw: str,
    minimum: int | None,
    maximum: int,
) -> None:
    result = normalize_term(raw)

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.value is not None
    assert result.value.minimum_months == minimum
    assert result.value.maximum_months == maximum


def test_discrete_term_options_are_not_silently_presented_as_continuous() -> None:
    result = normalize_terms("3, 6, 9 ve 12 ay taksit seçenekleri")

    assert result.value is not None
    assert [term.minimum_months for term in result.value] == [3, 6, 9, 12]
    assert [term.maximum_months for term in result.value] == [3, 6, 9, 12]
    assert all(term.status is EvidenceStatus.STATED for term in result.value)
    assert normalize_term("3, 6, 9 ve 12 ay taksit seçenekleri").value is None


def test_term_missing_and_semantically_mixed_terms_are_not_guessed() -> None:
    assert normalize_term("vade bilgisi bulunmuyor").status is NormalizationStatus.MISSING
    assert normalize_term("3 ay erteleme ve 12 ay taksit").status is NormalizationStatus.AMBIGUOUS
    assert normalize_term("6 taksit").status is NormalizationStatus.UNSUPPORTED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("31.12.2026", date(2026, 12, 31)),
        ("31/12/2026", date(2026, 12, 31)),
        ("2026-12-31", date(2026, 12, 31)),
        ("31 Aralık 2026", date(2026, 12, 31)),
        ("18 TEMMUZ 2026", date(2026, 7, 18)),
    ],
)
def test_normalize_date_handles_numeric_iso_and_turkish_months(
    raw: str,
    expected: date,
) -> None:
    result = normalize_date(raw)

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.value == expected


def test_yearless_date_requires_explicit_reference_context() -> None:
    without_context = normalize_date("31 Aralık")
    with_context = normalize_date("31 Aralık", reference_date=date(2026, 7, 18))

    assert without_context.status is NormalizationStatus.AMBIGUOUS
    assert with_context.value == date(2026, 12, 31)
    assert with_context.warnings == ("year_inferred_from_reference_date",)


@pytest.mark.parametrize(
    ("raw", "starts_on", "ends_on"),
    [
        ("01.07.2026 - 31.08.2026", date(2026, 7, 1), date(2026, 8, 31)),
        ("1 Temmuz - 31 Ağustos 2026", date(2026, 7, 1), date(2026, 8, 31)),
        ("1-31 Temmuz 2026", date(2026, 7, 1), date(2026, 7, 31)),
        ("15 Aralık - 15 Ocak 2027", date(2026, 12, 15), date(2027, 1, 15)),
        ("31 Aralık 2026'ya kadar", None, date(2026, 12, 31)),
        ("1 Temmuz 2026'dan itibaren", date(2026, 7, 1), None),
    ],
)
def test_normalize_validity_handles_ranges_and_open_boundaries(
    raw: str,
    starts_on: date | None,
    ends_on: date | None,
) -> None:
    result = normalize_validity(raw)

    assert result.status is NormalizationStatus.NORMALIZED
    assert result.value is not None
    assert result.value.starts_on == starts_on
    assert result.value.ends_on == ends_on


def test_invalid_or_ambiguous_dates_are_never_coerced() -> None:
    assert normalize_date("31.02.2026").status is NormalizationStatus.INVALID
    assert normalize_date("1 Temmuz ve 2 Ağustos 2026").status is NormalizationStatus.AMBIGUOUS
    assert normalize_validity("31 Aralık 2026").status is NormalizationStatus.AMBIGUOUS


def test_high_risk_money_variants_abstain_instead_of_selecting_an_endpoint() -> None:
    assert normalize_money("1.000-2.000 TL").status is NormalizationStatus.AMBIGUOUS
    assert (
        normalize_money("500 TL üzeri harcamaya 100 TL iade").status
        is NormalizationStatus.AMBIGUOUS
    )
    assert normalize_money("1.234").reason_code == "money_currency_unknown"
    assert normalize_money("$100").reason_code == "currency_symbol_ambiguous"
    assert normalize_money("12.34 TL").status is NormalizationStatus.INVALID


def test_currency_before_scaled_amount_and_fee_specific_zero_are_supported() -> None:
    scaled = normalize_money("TL 1,5 milyon")
    waived = normalize_money(
        "Tahsis ücreti yoktur",
        currency_hint="TRY",
        allow_fee_waiver=True,
    )

    assert scaled.value is not None
    assert scaled.value.amount == Decimal("1500000.0")
    assert waived.value is not None
    assert waived.value.amount == Decimal("0")


def test_rate_bounds_per_mille_and_term_context_are_not_conflated() -> None:
    bound = normalize_rate("Aylık %1,49'dan başlayan oran")
    per_mille = normalize_rate(
        "binde 5 indirim",
        period_hint=RatePeriod.TERM_TOTAL,
    )
    term_only = normalize_rate(
        "12 ay vadede %1,79 finansman oranı",
        kind_hint=RateKind.FINANCING_PROFIT_RATE,
    )

    assert bound.status is NormalizationStatus.AMBIGUOUS
    assert per_mille.value is not None
    assert per_mille.value.value_percent == Decimal("0.5")
    assert term_only.value is not None
    assert term_only.value.period is RatePeriod.UNSPECIFIED


def test_reference_bound_date_parsing_never_uses_the_process_clock() -> None:
    reference = date(2026, 7, 18)

    yearless = normalize_date("31.12", reference_date=reference)
    relative = normalize_date("yarın", reference_date=reference)

    assert yearless.value == date(2026, 12, 31)
    assert relative.value == date(2026, 7, 19)
    assert normalize_date("31.12.26", reference_date=reference).value is None


def test_validity_rejects_distinct_date_roles_and_time_cutoffs() -> None:
    roles = normalize_validity("Son başvuru 31.07.2026, kullanım 31.08.2026")
    cutoff = normalize_validity("31.07.2026 17:00'a kadar")
    bare_month = normalize_validity("Temmuz 2026")

    assert roles.status is NormalizationStatus.AMBIGUOUS
    assert cutoff.status is NormalizationStatus.UNSUPPORTED
    assert bare_month.status is NormalizationStatus.AMBIGUOUS
