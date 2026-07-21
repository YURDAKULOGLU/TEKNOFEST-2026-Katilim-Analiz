from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    ComparisonContext,
    ComparisonDimension,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    FeeBasis,
    FeeKind,
    FeeValue,
    GrossNetBasis,
    MoneyValue,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    RewardBasis,
    RewardKind,
    RewardValue,
    SalesChannel,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.domain.comparison import ComparisonReport, compare_campaigns

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
AS_OF = date(2026, 7, 18)
SHA256 = "0" * 64
ACTIVE_WINDOW = ValidityWindow(
    raw="2026 yılı",
    starts_on=date(2026, 1, 1),
    ends_on=date(2026, 12, 31),
)


def _metadata() -> ExtractionMetadata:
    return ExtractionMetadata(
        method=ExtractionMethod.RULE,
        extractor_version="test",
        schema_version="1",
        started_at=NOW,
        completed_at=NOW,
    )


def _evidence(evidence_id: str, pointer: str) -> EvidenceRef:
    return EvidenceRef(
        id=evidence_id,
        field_pointer=pointer,
        source_document_id="doc",
        block_id="block",
        quote="kanıt",
        start_char=0,
        end_char=5,
        evidence_sha256=SHA256,
        status=EvidenceStatus.STATED,
    )


def _rate(
    value: str,
    *,
    kind: RateKind = RateKind.FINANCING_PROFIT_RATE,
    period: RatePeriod = RatePeriod.MONTHLY,
    gross_net_basis: GrossNetBasis | None = None,
    term_months: int | None = None,
    basis_label: str | None = None,
    reference_starts_on: date | None = None,
    reference_ends_on: date | None = None,
) -> RateValue:
    if gross_net_basis is None:
        gross_net_basis = (
            GrossNetBasis.GROSS
            if kind
            in {
                RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
                RateKind.HISTORICAL_RETURN_RATE,
            }
            else GrossNetBasis.UNSPECIFIED
        )
    if term_months is None and kind in {
        RateKind.FINANCING_PROFIT_RATE,
        RateKind.ANNUAL_COST_RATE,
    }:
        term_months = 12
    if basis_label is None:
        basis_label = {
            RateKind.FINANCING_PROFIT_RATE: "nominal",
            RateKind.ANNUAL_COST_RATE: "annual_cost",
            RateKind.PROFIT_SHARE_DISTRIBUTION_RATE: "distribution",
            RateKind.HISTORICAL_RETURN_RATE: "historical_return",
            RateKind.DISCOUNT_RATE: "discount",
            RateKind.REWARD_RATE: "reward",
        }.get(kind)
    if kind is RateKind.HISTORICAL_RETURN_RATE:
        reference_starts_on = reference_starts_on or date(2025, 1, 1)
        reference_ends_on = reference_ends_on or date(2025, 12, 31)
    return RateValue(
        raw=f"%{value}",
        value_percent=Decimal(value),
        kind=kind,
        period=period,
        gross_net_basis=gross_net_basis,
        reference_starts_on=reference_starts_on,
        reference_ends_on=reference_ends_on,
        term_months=term_months,
        basis_label=basis_label,
    )


def _money(value: str, currency: str = "TRY") -> MoneyValue:
    return MoneyValue(raw=f"{value} {currency}", amount=Decimal(value), currency=currency)


def _fee(
    value: str,
    currency: str = "TRY",
    *,
    kind: FeeKind = FeeKind.ALLOCATION,
    basis: FeeBasis = FeeBasis.ONE_TIME,
) -> FeeValue:
    money = _money(value, currency)
    return FeeValue(raw=money.raw, money=money, kind=kind, basis=basis)


def _context(
    *,
    currency: str | None = "TRY",
    segment_keys: tuple[str, ...] = ("retail",),
    channel: SalesChannel = SalesChannel.ALL,
    new_customer_only: bool | None = False,
    mechanism: str | None = "standard",
    secured: bool | None = False,
) -> ComparisonContext:
    return ComparisonContext(
        product_currency=currency,
        customer_segment_keys=list(segment_keys),
        sales_channel=channel,
        new_customer_only=new_customer_only,
        product_mechanism=mechanism,
        secured=secured,
    )


def _record(
    campaign_id: str,
    *,
    family: ProductFamily = ProductFamily.FINANCING,
    rates: tuple[RateValue, ...] = (),
    terms: tuple[TermRange, ...] = (),
    fees: tuple[FeeValue, ...] = (),
    rewards: tuple[RewardValue, ...] = (),
    validity: ValidityWindow | None = ACTIVE_WINDOW,
    context: ComparisonContext | None = None,
    conditions: tuple[str, ...] | None = None,
    status: RecordStatus = RecordStatus.VALIDATED,
    evidence: tuple[EvidenceRef, ...] | None = None,
) -> CampaignRecord:
    if context is None:
        context = _context(secured=False if family is ProductFamily.FINANCING else None)
    if conditions is None:
        # Production records always carry bank-specific free-text conditions;
        # two banks never publish verbatim-identical sentences (issue #13).
        conditions = (
            f"Kampanyadan yalnızca bank-{campaign_id} müşterileri yararlanabilir.",
            f"bank-{campaign_id} mobil uygulamasından başvuru yapılması gerekmektedir.",
        )
    data = CampaignData(
        bank_id=f"bank-{campaign_id}",
        title=f"Kampanya {campaign_id}",
        product_family=family,
        campaign_type=CampaignType.FINANCING_RATE,
        rates=list(rates),
        terms=list(terms),
        fees=list(fees),
        rewards=list(rewards),
        validity=validity,
        customer_segments=["Bireysel müşteriler"],
        eligibility_conditions=list(conditions),
        comparison_context=context,
    )
    if evidence is None:
        generated: list[EvidenceRef] = []
        for index in range(len(rates)):
            generated.append(
                _evidence(f"{campaign_id}-rate-{index}", f"/data/rates/{index}/value_percent")
            )
        for index in range(len(terms)):
            generated.append(_evidence(f"{campaign_id}-term-{index}", f"/data/terms/{index}"))
        for index in range(len(fees)):
            generated.append(_evidence(f"{campaign_id}-fee-{index}", f"/data/fees/{index}"))
        for index in range(len(rewards)):
            generated.append(_evidence(f"{campaign_id}-reward-{index}", f"/data/rewards/{index}"))
        generated.append(_evidence(f"{campaign_id}-context", "/data/comparison_context"))
        evidence = tuple(generated)
    return CampaignRecord(
        id=campaign_id,
        version=1,
        source_document_id="doc",
        observed_at=NOW,
        data=data,
        evidence=list(evidence),
        extraction=_metadata(),
        status=status,
        record_sha256=SHA256,
    )


def _compare(
    records: list[CampaignRecord],
    dimension: ComparisonDimension,
    *,
    as_of: date | datetime | None = AS_OF,
) -> ComparisonReport:
    return compare_campaigns(records, [dimension], as_of=as_of)


def _by_campaign(report: ComparisonReport) -> dict[str, object]:
    return {item.campaign_id: item for item in report.items}


def test_financing_rate_is_lower_better_and_evidence_backed() -> None:
    report = _compare(
        [_record("a", rates=(_rate("1.49"),)), _record("b", rates=(_rate("2.10"),))],
        ComparisonDimension.RATE,
    )
    items = _by_campaign(report)

    assert items["a"].rank == 1  # type: ignore[attr-defined]
    assert items["b"].rank == 2  # type: ignore[attr-defined]
    assert items["a"].canonical_value == Decimal("1.49")  # type: ignore[attr-defined]
    assert items["a"].reason_code == "ranked_lower_is_better"  # type: ignore[attr-defined]
    assert items["a"].evidence_ids == ("a-rate-0",)  # type: ignore[attr-defined]
    assert report.global_score is None


def test_distribution_and_historical_rates_are_higher_better_when_basis_is_known() -> None:
    distribution = _compare(
        [
            _record(
                "a",
                family=ProductFamily.PARTICIPATION_ACCOUNT,
                rates=(
                    _rate(
                        "70",
                        kind=RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
                        period=RatePeriod.ANNUAL,
                    ),
                ),
            ),
            _record(
                "b",
                family=ProductFamily.PARTICIPATION_ACCOUNT,
                rates=(
                    _rate(
                        "80",
                        kind=RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
                        period=RatePeriod.ANNUAL,
                    ),
                ),
            ),
        ],
        ComparisonDimension.RATE,
    )

    assert _by_campaign(distribution)["b"].rank == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("left", "right", "reason_code"),
    [
        (
            _record("a", rates=(_rate("1.2"),)),
            _record("b", family=ProductFamily.CARD, rates=(_rate("1.1"),)),
            "product_family_mismatch",
        ),
        (
            _record("a", rates=(_rate("1.2"),)),
            _record(
                "b",
                rates=(
                    _rate(
                        "10",
                        kind=RateKind.ANNUAL_COST_RATE,
                        period=RatePeriod.MONTHLY,
                    ),
                ),
            ),
            "rate_kind_mismatch",
        ),
        (
            _record("a", rates=(_rate("1.2"),)),
            _record("b", rates=(_rate("15", period=RatePeriod.ANNUAL),)),
            "rate_period_mismatch",
        ),
        (
            _record("a", rates=(_rate("1.2"),), context=_context(segment_keys=("new",))),
            _record(
                "b",
                rates=(_rate("1.1"),),
                context=_context(segment_keys=("existing",)),
            ),
            "customer_segment_context_mismatch",
        ),
        (
            _record("a", rates=(_rate("1.2"),), context=_context(mechanism="vehicle")),
            _record("b", rates=(_rate("1.1"),), context=_context(mechanism="housing")),
            "product_mechanism_context_mismatch",
        ),
    ],
)
def test_incompatible_family_rate_or_eligibility_context_is_rejected(
    left: CampaignRecord,
    right: CampaignRecord,
    reason_code: str,
) -> None:
    report = _compare([left, right], ComparisonDimension.RATE)

    assert all(not item.comparable for item in report.items)
    assert {item.reason_code for item in report.items} == {reason_code}
    assert all(item.rank is None for item in report.items)


@pytest.mark.parametrize(
    ("rate", "reason_code"),
    [
        (
            _rate(
                "1",
                kind=RateKind.UNKNOWN,
                period=RatePeriod.UNSPECIFIED,
                basis_label="unknown",
            ),
            "rate_kind_unknown",
        ),
        (
            RateValue(
                raw="%1",
                value_percent=Decimal("1"),
                kind=RateKind.FINANCING_PROFIT_RATE,
                period=RatePeriod.MONTHLY,
                term_months=12,
                basis_label=None,
            ),
            "rate_basis_unknown",
        ),
        (_rate("1", term_months=0), "rate_term_context_unknown"),
    ],
)
def test_unknown_rate_semantics_are_not_ranked(rate: RateValue, reason_code: str) -> None:
    report = _compare(
        [_record("a", rates=(rate,)), _record("b", rates=(rate,))],
        ComparisonDimension.RATE,
    )

    assert {item.reason_code for item in report.items} == {reason_code}


def test_bank_specific_condition_wording_does_not_block_comparison() -> None:
    """Issue #13: raw-sentence equality is not an eligibility measure."""

    report = _compare(
        [
            _record(
                "a",
                rates=(_rate("1.49"),),
                conditions=("Kampanya yalnızca maaş müşterileri için geçerlidir.",),
            ),
            _record(
                "b",
                rates=(_rate("2.10"),),
                conditions=("Başvuruların Mobil Şube üzerinden yapılması gerekmektedir.",),
            ),
        ],
        ComparisonDimension.RATE,
    )
    items = _by_campaign(report)

    assert all(item.comparable for item in report.items)  # type: ignore[attr-defined]
    assert items["a"].rank == 1  # type: ignore[attr-defined]
    assert items["b"].rank == 2  # type: ignore[attr-defined]


def test_currency_context_must_be_known_and_equal() -> None:
    unknown = _compare(
        [
            _record("a", rates=(_rate("1.2"),), context=_context(currency=None)),
            _record("b", rates=(_rate("1.1"),), context=_context(currency=None)),
        ],
        ComparisonDimension.RATE,
    )
    mismatch = _compare(
        [
            _record("a", rates=(_rate("1.2"),), context=_context(currency="TRY")),
            _record("b", rates=(_rate("1.1"),), context=_context(currency="USD")),
        ],
        ComparisonDimension.RATE,
    )

    assert {item.reason_code for item in unknown.items} == {"currency_context_unknown"}
    assert {item.reason_code for item in mismatch.items} == {"currency_context_mismatch"}


def test_as_of_and_validity_prevent_historical_current_conflation() -> None:
    active = _record("a", rates=(_rate("1.2"),))
    expired = _record(
        "b",
        rates=(_rate("1.1"),),
        validity=ValidityWindow(
            raw="2025 yılı",
            starts_on=date(2025, 1, 1),
            ends_on=date(2025, 12, 31),
        ),
    )

    missing_as_of = _compare(
        [active, _record("c", rates=(_rate("1.1"),))],
        ComparisonDimension.RATE,
        as_of=None,
    )
    inactive = _compare([active, expired], ComparisonDimension.RATE)

    assert {item.reason_code for item in missing_as_of.items} == {"as_of_required"}
    assert {item.reason_code for item in inactive.items} == {"campaign_not_active_as_of"}


def test_multiple_common_rate_signatures_require_explicit_selection() -> None:
    rates_a = (
        _rate("1.2"),
        _rate("20", kind=RateKind.ANNUAL_COST_RATE, period=RatePeriod.ANNUAL),
    )
    rates_b = (
        _rate("1.1"),
        _rate("19", kind=RateKind.ANNUAL_COST_RATE, period=RatePeriod.ANNUAL),
    )

    report = _compare(
        [_record("a", rates=rates_a), _record("b", rates=rates_b)],
        ComparisonDimension.RATE,
    )

    assert {item.reason_code for item in report.items} == {"multiple_rate_signatures"}


def test_term_uses_maximum_months_and_dense_tie_ranks() -> None:
    report = _compare(
        [
            _record("a", terms=(TermRange(raw="12-36 ay", minimum_months=12, maximum_months=36),)),
            _record("b", terms=(TermRange(raw="24 ay", minimum_months=24, maximum_months=24),)),
            _record("c", terms=(TermRange(raw="36 ay", minimum_months=36, maximum_months=36),)),
        ],
        ComparisonDimension.TERM,
    )
    items = _by_campaign(report)

    assert items["a"].rank == 1  # type: ignore[attr-defined]
    assert items["c"].rank == 1  # type: ignore[attr-defined]
    assert items["b"].rank == 2  # type: ignore[attr-defined]


def test_missing_fee_is_not_zero_but_explicit_zero_is_ranked() -> None:
    missing = _record("missing")
    zero = _record("zero", fees=(_fee("0"),))
    positive = _record("positive", fees=(_fee("10"),))

    incomplete = _by_campaign(_compare([missing, positive], ComparisonDimension.FEE))
    ranked = _by_campaign(_compare([zero, positive], ComparisonDimension.FEE))

    assert incomplete["missing"].reason_code == "fee_missing"  # type: ignore[attr-defined]
    assert incomplete["positive"].reason_code == "peer_fee_missing"  # type: ignore[attr-defined]
    assert all(not item.comparable for item in incomplete.values())  # type: ignore[attr-defined]
    assert ranked["zero"].canonical_value == Decimal("0")  # type: ignore[attr-defined]
    assert ranked["zero"].rank == 1  # type: ignore[attr-defined]
    assert ranked["positive"].rank == 2  # type: ignore[attr-defined]


def _waived_fee(
    raw: str = "Dosya masrafı alınmaz.",
    *,
    kind: FeeKind = FeeKind.ALLOCATION,
    basis: FeeBasis = FeeBasis.ONE_TIME,
    waiver_limit: MoneyValue | None = None,
) -> FeeValue:
    return FeeValue(
        raw=raw,
        kind=kind,
        basis=basis,
        description=raw,
        waived=True,
        waiver_limit=waiver_limit,
    )


def test_waived_fee_ranks_ahead_of_every_priced_fee() -> None:
    """Issue #12: 'masraf yok' must beat any stated price, not drop out."""

    report = _compare(
        [_record("waived", fees=(_waived_fee(),)), _record("priced", fees=(_fee("500"),))],
        ComparisonDimension.FEE,
    )
    items = _by_campaign(report)

    assert all(item.comparable for item in report.items)
    assert items["waived"].rank == 1  # type: ignore[attr-defined]
    assert items["priced"].rank == 2  # type: ignore[attr-defined]
    assert items["waived"].reason_code == "ranked_lower_is_better"  # type: ignore[attr-defined]
    assert items["waived"].evidence_ids == ("waived-fee-0",)  # type: ignore[attr-defined]


def test_waived_fees_tie_for_first() -> None:
    report = _compare(
        [
            _record("a", fees=(_waived_fee(),)),
            _record(
                "b",
                fees=(
                    _waived_fee(
                        "50.000 TL'ye kadar dosya masrafı alınmaz.",
                        waiver_limit=_money("50000"),
                    ),
                ),
            ),
        ],
        ComparisonDimension.FEE,
    )
    items = _by_campaign(report)

    assert items["a"].rank == 1  # type: ignore[attr-defined]
    assert items["b"].rank == 1  # type: ignore[attr-defined]
    # The capped waiver keeps its ceiling visible in the displayed sentence.
    assert "50.000 TL" in items["b"].display_value  # type: ignore[attr-defined]


def test_missing_fee_stays_missing_even_against_a_waiver() -> None:
    """ADR-002: an unstated fee is unknown, never an implicit waiver."""

    report = _compare(
        [_record("silent"), _record("waived", fees=(_waived_fee(),))],
        ComparisonDimension.FEE,
    )
    items = _by_campaign(report)

    assert items["silent"].reason_code == "fee_missing"  # type: ignore[attr-defined]
    assert items["waived"].reason_code == "peer_fee_missing"  # type: ignore[attr-defined]
    assert all(not item.comparable for item in report.items)


def test_waiver_of_a_different_fee_kind_is_not_comparable() -> None:
    report = _compare(
        [
            _record("waived", fees=(_waived_fee(kind=FeeKind.ANNUAL, basis=FeeBasis.PER_YEAR),)),
            _record("priced", fees=(_fee("500"),)),
        ],
        ComparisonDimension.FEE,
    )

    assert {item.reason_code for item in report.items} == {"fee_kind_mismatch"}


@pytest.mark.parametrize(
    ("left", "right", "reason_code"),
    [
        (_fee("1", "TRY"), _fee("1", "USD"), "fee_currency_mismatch"),
        (
            _fee("1", basis=FeeBasis.ONE_TIME),
            _fee("1", basis=FeeBasis.PER_MONTH),
            "fee_basis_mismatch",
        ),
        (
            _fee("1", kind=FeeKind.ALLOCATION),
            _fee("1", kind=FeeKind.ANNUAL),
            "fee_kind_mismatch",
        ),
    ],
)
def test_incompatible_fee_semantics_are_rejected(
    left: FeeValue,
    right: FeeValue,
    reason_code: str,
) -> None:
    report = _compare(
        [_record("a", fees=(left,)), _record("b", fees=(right,))],
        ComparisonDimension.FEE,
    )

    assert {item.reason_code for item in report.items} == {reason_code}


def test_reward_kind_basis_program_and_caps_are_comparison_context() -> None:
    money_reward = RewardValue(
        kind=RewardKind.MONEY,
        basis=RewardBasis.PER_CUSTOMER,
        raw="100 TL",
        money=_money("100"),
    )
    point_reward = RewardValue(
        kind=RewardKind.POINTS,
        basis=RewardBasis.PER_CUSTOMER,
        program_name="Puan A",
        raw="100 puan",
        points=Decimal("100"),
    )
    report = _compare(
        [_record("a", rewards=(money_reward,)), _record("b", rewards=(point_reward,))],
        ComparisonDimension.REWARD,
    )

    assert {item.reason_code for item in report.items} == {"reward_kind_mismatch"}


def test_equal_eligibility_context_is_explainable_but_not_numerically_ranked() -> None:
    report = _compare(
        [_record("a"), _record("b")],
        ComparisonDimension.ELIGIBILITY,
    )

    assert all(item.comparable for item in report.items)
    assert all(item.rank is None for item in report.items)
    assert {item.reason_code for item in report.items} == {"eligibility_context_compatible"}


def test_missing_dimension_evidence_blocks_ranking() -> None:
    without_rate_evidence = _record(
        "a",
        rates=(_rate("1.2"),),
        evidence=(_evidence("a-context", "/data/comparison_context"),),
    )
    with_evidence = _record("b", rates=(_rate("1.1"),))

    report = _compare([without_rate_evidence, with_evidence], ComparisonDimension.RATE)

    assert _by_campaign(report)["a"].reason_code == "rate_evidence_missing"  # type: ignore[attr-defined]
    assert _by_campaign(report)["b"].reason_code == "peer_rate_evidence_missing"  # type: ignore[attr-defined]


def test_unvalidated_records_are_rejected() -> None:
    report = _compare(
        [
            _record("a", rates=(_rate("1.2"),)),
            _record("b", rates=(_rate("1.1"),), status=RecordStatus.NEEDS_REVIEW),
        ],
        ComparisonDimension.RATE,
    )

    assert {item.reason_code for item in report.items} == {"record_not_validated"}


def test_results_and_hash_are_independent_of_input_order() -> None:
    records = [_record("a", rates=(_rate("1.2"),)), _record("b", rates=(_rate("1.1"),))]

    forward = _compare(records, ComparisonDimension.RATE)
    reversed_report = _compare(list(reversed(records)), ComparisonDimension.RATE)

    assert forward.items == reversed_report.items
    assert forward.canonical_sha256 == reversed_report.canonical_sha256
    response = forward.to_response(generated_at=NOW)
    assert response.canonical_sha256 == forward.canonical_sha256


def test_comparison_requires_unique_campaigns_and_a_dimension() -> None:
    record = _record("a", rates=(_rate("1.2"),))

    with pytest.raises(ValueError, match="at least two"):
        compare_campaigns([record], [ComparisonDimension.RATE], as_of=AS_OF)
    with pytest.raises(ValueError, match="unique"):
        compare_campaigns([record, record], [ComparisonDimension.RATE], as_of=AS_OF)
    with pytest.raises(ValueError, match="dimension"):
        compare_campaigns([record, _record("b")], [], as_of=AS_OF)


def test_ltv_share_and_profit_rate_are_never_compared_on_the_rate_axis() -> None:
    """Issue #28: a %70 financing share must not meet a %1,69 profit rate."""

    report = _compare(
        [
            _record(
                "a",
                rates=(_rate("70", kind=RateKind.LTV_RATIO, period=RatePeriod.UNSPECIFIED),),
            ),
            _record("b", rates=(_rate("1.69"),)),
        ],
        ComparisonDimension.RATE,
    )

    assert not any(item.comparable for item in report.items)
    assert {item.reason_code for item in report.items} == {"rate_kind_mismatch"}


def test_ltv_shares_of_the_same_kind_rank_higher_first() -> None:
    report = _compare(
        [
            _record(
                "a",
                rates=(_rate("70", kind=RateKind.LTV_RATIO, period=RatePeriod.UNSPECIFIED),),
            ),
            _record(
                "b",
                rates=(_rate("80", kind=RateKind.LTV_RATIO, period=RatePeriod.UNSPECIFIED),),
            ),
        ],
        ComparisonDimension.RATE,
    )

    items = _by_campaign(report)
    assert items["b"].rank == 1  # type: ignore[attr-defined]
    assert items["a"].rank == 2  # type: ignore[attr-defined]
    assert items["b"].reason_code == "ranked_higher_is_better"  # type: ignore[attr-defined]
