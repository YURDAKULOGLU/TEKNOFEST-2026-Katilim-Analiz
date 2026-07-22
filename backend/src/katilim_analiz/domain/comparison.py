"""Pure, basis-aware and explainable campaign comparison rules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from katilim_analiz.contracts import (
    CampaignRecord,
    ComparisonDimension,
    ComparisonResponse,
    EvidenceStatus,
    FeeBasis,
    FeeKind,
    FeeValue,
    GrossNetBasis,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    RewardBasis,
    RewardKind,
    RewardValue,
    SalesChannel,
)
from katilim_analiz.contracts.models import ComparisonItem
from katilim_analiz.domain.normalization import canonicalize_turkish_text

RULESET_VERSION = "comparison-v2"

#: Optional-context axes soften on *symmetric* absence (issues #22 and #37
#: precedent: an optional context may not permanently mute the product).  When
#: NEITHER record carries any evidence for an axis, no fact is fabricated
#: (ADR-002) — the axis values stay unknown — but the comparison gate treats
#: the pair as compatible and says so with an explicit "kisit kaniti yok"
#: note in the report warnings.  The moment exactly ONE side carries evidence
#: the asymmetry is real and the pair stays blocked with the unknown reason.
_ABSENCE_COMPATIBLE_NOTES: dict[str, str] = {
    "sales_channel": (
        "Satış kanalı ekseni 'kısıt kanıtı yok' olarak uyumlu sayıldı: "
        "hiçbir kayıtta kanal kanıtı bulunmuyor."
    ),
    "new_customer": (
        "Yeni müşteri ekseni 'kısıt kanıtı yok' olarak uyumlu sayıldı: "
        "hiçbir kayıtta yeni müşteri kısıtına dair kanıt bulunmuyor."
    ),
    "product_mechanism": (
        "Ürün mekanizması ekseni 'kısıt kanıtı yok' olarak uyumlu sayıldı: "
        "hiçbir kayıtta mekanizma kanıtı bulunmuyor."
    ),
    "secured": (
        "Teminat ekseni 'kısıt kanıtı yok' olarak uyumlu sayıldı: "
        "hiçbir kayıtta teminat kanıtı bulunmuyor."
    ),
    "validity": (
        "Geçerlilik ekseni 'süre kanıtı yok' olarak uyumlu sayıldı: "
        "hiçbir kayıt bir kampanya süresi beyan etmiyor (duran ürün sayfaları)."
    ),
}

_REASONS: dict[str, str] = {
    "record_not_validated": "Doğrulanmamış kayıt karşılaştırmaya alınamaz.",
    "product_family_unknown": "Ürün ailesi yeterince belirli değil.",
    "product_family_mismatch": "Ürün aileleri farklı.",
    "currency_context_unknown": "Ürün para birimi bilinmiyor.",
    "currency_context_mismatch": "Ürün para birimleri farklı.",
    "customer_segment_context_unknown": "Kanonik müşteri segmenti bilinmiyor.",
    "customer_segment_context_mismatch": "Müşteri segmentleri farklı.",
    "sales_channel_context_unknown": "Satış kanalı bilinmiyor.",
    "sales_channel_context_mismatch": "Satış kanalları farklı.",
    "new_customer_context_unknown": "Yeni müşteri koşulu bilinmiyor.",
    "new_customer_context_mismatch": "Yeni müşteri koşulları farklı.",
    "product_mechanism_context_unknown": "Ürün mekanizması bilinmiyor.",
    "product_mechanism_context_mismatch": "Ürün mekanizmaları farklı.",
    "security_context_unknown": "Teminat bağlamı bilinmiyor.",
    "security_context_mismatch": "Teminat bağlamları farklı.",
    "as_of_required": "Geçerlilik kontrolü için karşılaştırma tarihi gerekli.",
    "not_observed_as_of": "Kayıt seçilen tarihte henüz gözlemlenmemişti.",
    "validity_unknown": "Kampanya geçerliliği bilinmiyor.",
    "campaign_not_active_as_of": "Kampanya seçilen tarihte aktif değil.",
    "rate_missing": "Oran bilgisi eksik.",
    "peer_rate_missing": "Karşılaştırılan kampanyalardan birinin oranı eksik.",
    "rate_evidence_missing": "Oran için alan düzeyinde kanıt yok.",
    "peer_rate_evidence_missing": "Karşılaştırılan bir oranın alan düzeyinde kanıtı yok.",
    "rate_kind_unknown": "Oran türü bilinmiyor.",
    "rate_period_unknown": "Oran dönemi bilinmiyor.",
    "rate_basis_unknown": "Oran bazı bilinmiyor.",
    "rate_term_context_unknown": "Oranın bağlı olduğu vade bilinmiyor.",
    "rate_gross_net_basis_unknown": "Oranın brüt/net bazı bilinmiyor.",
    "rate_reference_window_unknown": "Tarihsel oranın referans dönemi bilinmiyor.",
    "rate_kind_mismatch": "Oran türleri farklı.",
    "rate_period_mismatch": "Oran dönemleri farklı.",
    "rate_basis_mismatch": "Oran bazları farklı.",
    "multiple_rate_signatures": "Birden fazla ortak oran imzası var; açık seçim gerekli.",
    "multiple_rate_values_for_signature": "Aynı oran imzası için birden fazla değer var.",
    "term_missing": "Vade bilgisi eksik.",
    "peer_term_missing": "Karşılaştırılan kampanyalardan birinin vadesi eksik.",
    "term_evidence_missing": "Vade için alan düzeyinde kanıt yok.",
    "peer_term_evidence_missing": "Karşılaştırılan bir vadenin alan düzeyinde kanıtı yok.",
    "term_ambiguous": "Vade bilgisi belirsiz.",
    "fee_missing": "Ücret bilgisi eksik; sıfır kabul edilmedi.",
    "peer_fee_missing": "Karşılaştırılan kampanyalardan birinin ücreti eksik.",
    "fee_evidence_missing": "Ücret için alan düzeyinde kanıt yok.",
    "peer_fee_evidence_missing": "Karşılaştırılan bir ücretin alan düzeyinde kanıtı yok.",
    "fee_kind_unknown": "Ücret türü bilinmiyor.",
    "fee_basis_unknown": "Ücret bazı bilinmiyor.",
    "fee_not_numeric": "Ücret sayısal olarak karşılaştırılamıyor.",
    "fee_rate_context_unknown": "Oransal ücretin dönem veya baz bağlamı bilinmiyor.",
    "fee_kind_mismatch": "Ücret türleri farklı.",
    "fee_basis_mismatch": "Ücret bazları farklı.",
    "fee_currency_mismatch": "Ücret para birimleri farklı.",
    "fee_value_type_mismatch": "Ücret değer türleri farklı.",
    "multiple_fee_signatures": "Birden fazla ortak ücret imzası var; açık seçim gerekli.",
    "reward_missing": "Ödül bilgisi eksik.",
    "peer_reward_missing": "Karşılaştırılan kampanyalardan birinin ödülü eksik.",
    "reward_evidence_missing": "Ödül için alan düzeyinde kanıt yok.",
    "peer_reward_evidence_missing": "Karşılaştırılan bir ödülün alan düzeyinde kanıtı yok.",
    "reward_basis_unknown": "Ödül bazı bilinmiyor.",
    "reward_program_unknown": "Puan programı bilinmiyor.",
    "reward_not_numeric": "Ödül sayısal olarak karşılaştırılamıyor.",
    "reward_rate_context_unknown": "Oransal ödülün oran bağlamı bilinmiyor.",
    "reward_kind_mismatch": "Ödül türleri farklı.",
    "reward_basis_mismatch": "Ödül bazları farklı.",
    "reward_program_mismatch": "Ödül programları farklı.",
    "reward_context_mismatch": "Ödül eşik veya üst sınırları farklı.",
    "multiple_reward_signatures": "Birden fazla ortak ödül imzası var; açık seçim gerekli.",
    "eligibility_evidence_missing": "Uygunluk bağlamı için alan düzeyinde kanıt yok.",
    "peer_eligibility_evidence_missing": "Karşılaştırılan bir uygunluk bağlamının kanıtı yok.",
    "eligibility_context_compatible": (
        "Kanonik uygunluk eksenleri uyumlu; sayısal sıralama yapılmadı."
    ),
    "ranked_lower_is_better": "Aynı bazdaki daha düşük değer bu boyutta önce gelir.",
    "ranked_higher_is_better": "Aynı bazdaki daha yüksek değer bu boyutta önce gelir.",
}


@dataclass(frozen=True, slots=True)
class ComparisonOutcome:
    """One explainable, dimension-specific decision."""

    campaign_id: str
    dimension: ComparisonDimension
    comparable: bool
    rank: int | None
    canonical_value: Decimal | int | None
    unit: str | None
    display_value: str
    reason_code: str
    explanation: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """A deterministic report with no opaque cross-dimension score."""

    ruleset_version: str
    as_of: date | datetime | None
    items: tuple[ComparisonOutcome, ...]
    warnings: tuple[str, ...]
    canonical_sha256: str

    @property
    def global_score(self) -> None:
        return None

    def to_response(self, *, generated_at: datetime) -> ComparisonResponse:
        """Map the pure report to the root-owned transport contract."""

        return ComparisonResponse(
            ruleset_version=self.ruleset_version,
            generated_at=generated_at,
            items=[
                ComparisonItem(
                    campaign_id=item.campaign_id,
                    dimension=item.dimension,
                    rank=item.rank,
                    display_value=item.display_value,
                    comparable=item.comparable,
                    reason_code=item.reason_code,
                    evidence_ids=list(item.evidence_ids),
                )
                for item in self.items
            ],
            warnings=list(self.warnings),
            canonical_sha256=self.canonical_sha256,
        )


@dataclass(frozen=True, slots=True)
class _SelectedValue:
    record: CampaignRecord
    index: int
    value: Decimal | int | None
    unit: str | None
    display: str
    evidence_ids: tuple[str, ...]
    #: Issue #12: a stated fee waiver carries no figure yet outranks every
    #: priced fee on the lower-is-better axis. Only the fee dimension sets it.
    waived: bool = False


def _message(reason_code: str) -> str:
    return _REASONS.get(reason_code, "Karşılaştırma kuralı bu değerleri uyumlu bulmadı.")


def _short(value: str, maximum: int = 500) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of datetime must include a timezone offset")
        return value.date()
    return value


def _first_global_incompatibility(
    records: Sequence[CampaignRecord],
    as_of: date | datetime | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Return (blocking reason, absence-compatibility notes).

    A non-``None`` reason blocks every dimension and carries no notes.  A
    ``None`` reason may carry notes: each optional-context axis on which no
    record has any evidence was treated as compatible-by-absence, explicitly
    flagged instead of silently blocking forever (issues #22/#37 precedent);
    the underlying facts stay unknown per ADR-002.
    """

    if any(record.status is not RecordStatus.VALIDATED for record in records):
        return "record_not_validated", ()

    families = {record.data.product_family for record in records}
    if families & {ProductFamily.UNKNOWN, ProductFamily.OTHER}:
        return "product_family_unknown", ()
    if len(families) != 1:
        return "product_family_mismatch", ()

    contexts = [record.data.comparison_context for record in records]
    currencies = {context.product_currency for context in contexts}
    if None in currencies:
        return "currency_context_unknown", ()
    if len(currencies) != 1:
        return "currency_context_mismatch", ()

    segment_keys = {tuple(sorted(context.customer_segment_keys)) for context in contexts}
    if any(not key for key in segment_keys):
        return "customer_segment_context_unknown", ()
    if len(segment_keys) != 1:
        return "customer_segment_context_mismatch", ()

    notes: list[str] = []

    channels = {context.sales_channel for context in contexts}
    known_channels = channels - {SalesChannel.UNKNOWN}
    if not known_channels:
        notes.append(_ABSENCE_COMPATIBLE_NOTES["sales_channel"])
    elif SalesChannel.UNKNOWN in channels:
        return "sales_channel_context_unknown", ()
    elif len(known_channels) != 1:
        return "sales_channel_context_mismatch", ()

    new_customer_flags = {context.new_customer_only for context in contexts}
    known_new_customer = {flag for flag in new_customer_flags if flag is not None}
    if not known_new_customer:
        notes.append(_ABSENCE_COMPATIBLE_NOTES["new_customer"])
    elif None in new_customer_flags:
        return "new_customer_context_unknown", ()
    elif len(known_new_customer) != 1:
        return "new_customer_context_mismatch", ()

    mechanisms = {context.product_mechanism for context in contexts}
    known_mechanisms = {mechanism for mechanism in mechanisms if mechanism is not None}
    if not known_mechanisms:
        notes.append(_ABSENCE_COMPATIBLE_NOTES["product_mechanism"])
    elif None in mechanisms:
        return "product_mechanism_context_unknown", ()
    elif len(known_mechanisms) != 1:
        return "product_mechanism_context_mismatch", ()

    if next(iter(families)) is ProductFamily.FINANCING:
        secured_flags = {context.secured for context in contexts}
        known_secured = {flag for flag in secured_flags if flag is not None}
        if not known_secured:
            notes.append(_ABSENCE_COMPATIBLE_NOTES["secured"])
        elif None in secured_flags:
            return "security_context_unknown", ()
        elif len(known_secured) != 1:
            return "security_context_mismatch", ()

    # Issue #13: free-text eligibility sentences are bank-specific prose; two
    # banks never publish verbatim-identical wording, so raw-sentence equality
    # is not an eligibility measure. The eligibility context is compared only
    # through the canonical comparison_context axes above (segments, channel,
    # new-customer flag, mechanism, security); the sentences themselves remain
    # evidence for display and citation.

    if as_of is None:
        return "as_of_required", ()
    selected_date = _as_date(as_of)
    for record in records:
        if isinstance(as_of, datetime):
            if record.observed_at > as_of:
                return "not_observed_as_of", ()
        elif record.observed_at.date() > selected_date:
            return "not_observed_as_of", ()

    def _validity_stated(record: CampaignRecord) -> bool:
        validity = record.data.validity
        return (
            validity is not None
            and validity.status not in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}
            and (validity.starts_on is not None or validity.ends_on is not None)
        )

    stated = [record for record in records if _validity_stated(record)]
    if not stated:
        # Standing product sheets never declare a campaign window. Symmetric
        # absence is compatible-with-note, mirroring the other context axes;
        # a pair where only ONE side declares a window stays blocked below.
        notes.append(_ABSENCE_COMPATIBLE_NOTES["validity"])
    elif len(stated) != len(records):
        return "validity_unknown", ()
    else:
        for record in stated:
            validity = record.data.validity
            assert validity is not None
            if validity.starts_on is not None and selected_date < validity.starts_on:
                return "campaign_not_active_as_of", ()
            if validity.ends_on is not None and selected_date > validity.ends_on:
                return "campaign_not_active_as_of", ()
    return None, tuple(notes)


def _display_for(record: CampaignRecord, dimension: ComparisonDimension) -> str:
    if dimension is ComparisonDimension.RATE:
        return _short("; ".join(value.raw for value in record.data.rates) or "Belirtilmemiş")
    if dimension is ComparisonDimension.TERM:
        return _short("; ".join(value.raw for value in record.data.terms) or "Belirtilmemiş")
    if dimension is ComparisonDimension.FEE:
        return _short("; ".join(value.raw for value in record.data.fees) or "Belirtilmemiş")
    if dimension is ComparisonDimension.REWARD:
        return _short("; ".join(value.raw for value in record.data.rewards) or "Belirtilmemiş")
    context = record.data.comparison_context
    segments = ", ".join(sorted(context.customer_segment_keys)) or "belirsiz"
    return _short(
        f"segment={segments}; kanal={context.sales_channel.value}; "
        f"yeni_müşteri={context.new_customer_only}"
    )


def _reject_all(
    records: Sequence[CampaignRecord],
    dimension: ComparisonDimension,
    reason_code: str,
) -> tuple[ComparisonOutcome, ...]:
    return tuple(
        ComparisonOutcome(
            campaign_id=record.id,
            dimension=dimension,
            comparable=False,
            rank=None,
            canonical_value=None,
            unit=None,
            display_value=_display_for(record, dimension),
            reason_code=reason_code,
            explanation=_message(reason_code),
        )
        for record in records
    )


def _reject_own_and_peers(
    records: Sequence[CampaignRecord],
    dimension: ComparisonDimension,
    affected_ids: set[str],
    own_reason: str,
    peer_reason: str,
) -> tuple[ComparisonOutcome, ...]:
    return tuple(
        ComparisonOutcome(
            campaign_id=record.id,
            dimension=dimension,
            comparable=False,
            rank=None,
            canonical_value=None,
            unit=None,
            display_value=_display_for(record, dimension),
            reason_code=own_reason if record.id in affected_ids else peer_reason,
            explanation=_message(own_reason if record.id in affected_ids else peer_reason),
        )
        for record in records
    )


def _evidence_ids(record: CampaignRecord, pointer_prefix: str) -> tuple[str, ...]:
    alternate = pointer_prefix.removeprefix("/data")
    return tuple(
        sorted(
            evidence.id
            for evidence in record.evidence
            if evidence.field_pointer.startswith(pointer_prefix)
            or evidence.field_pointer.startswith(alternate)
        )
    )


def _rank_key(item: _SelectedValue) -> tuple[int, Decimal | int]:
    """Order a selected value; a stated waiver precedes every priced value.

    Issue #12: a waived fee has no figure, yet costs less than any charged
    amount, so it sorts ahead of all numbers on the lower-is-better axis and
    waived fees tie with each other. The integer lead keeps the missing figure
    from ever meeting a number in the comparison.
    """

    if item.waived:
        return (0, 0)
    assert item.value is not None
    return (1, item.value)


def _rank(
    selected: Sequence[_SelectedValue],
    dimension: ComparisonDimension,
    *,
    higher_is_better: bool,
) -> tuple[ComparisonOutcome, ...]:
    distinct = sorted({_rank_key(item) for item in selected}, reverse=higher_is_better)
    ranks = {key: index + 1 for index, key in enumerate(distinct)}
    reason_code = "ranked_higher_is_better" if higher_is_better else "ranked_lower_is_better"
    return tuple(
        ComparisonOutcome(
            campaign_id=item.record.id,
            dimension=dimension,
            comparable=True,
            rank=ranks[_rank_key(item)],
            canonical_value=item.value,
            unit=item.unit,
            display_value=_short(item.display),
            reason_code=reason_code,
            explanation=_message(reason_code),
            evidence_ids=item.evidence_ids,
        )
        for item in selected
    )


def _rate_issue(rate: RateValue) -> str | None:
    if rate.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}:
        return "rate_basis_unknown"
    if rate.kind is RateKind.UNKNOWN:
        return "rate_kind_unknown"
    if rate.kind is RateKind.LTV_RATIO:
        # A financing share of the asset value has no period, gross/net basis
        # or pricing basis by nature; it is complete as a bare percentage.  The
        # kind-aware signature still keeps it from ever meeting a pricing rate.
        return None
    if rate.period is RatePeriod.UNSPECIFIED:
        return "rate_period_unknown"
    # A missing basis label no longer disqualifies the rate on its own: the
    # signature embeds the canonicalized label ("" when absent), so unlabeled
    # rates can only ever meet other unlabeled rates of the same kind, period
    # and term — symmetric absence, like the other context axes. A labeled
    # rate never meets an unlabeled one.
    if rate.kind in {RateKind.FINANCING_PROFIT_RATE, RateKind.ANNUAL_COST_RATE} and (
        rate.term_months is None or rate.term_months <= 0
    ):
        return "rate_term_context_unknown"
    if (
        rate.kind
        in {
            RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
            RateKind.HISTORICAL_RETURN_RATE,
        }
        and rate.gross_net_basis is GrossNetBasis.UNSPECIFIED
    ):
        return "rate_gross_net_basis_unknown"
    if rate.kind is RateKind.HISTORICAL_RETURN_RATE and (
        rate.reference_starts_on is None or rate.reference_ends_on is None
    ):
        return "rate_reference_window_unknown"
    return None


def _rate_signature(rate: RateValue) -> tuple[object, ...]:
    return (
        rate.kind,
        rate.period,
        rate.gross_net_basis,
        rate.reference_starts_on,
        rate.reference_ends_on,
        rate.term_months,
        canonicalize_turkish_text(rate.basis_label or ""),
    )


def _rate_mismatch_reason(rate_sets: Sequence[Sequence[RateValue]]) -> str:
    kind_sets = [{rate.kind for rate in rates} for rates in rate_sets]
    if not set.intersection(*kind_sets):
        return "rate_kind_mismatch"
    period_sets = [{(rate.kind, rate.period) for rate in rates} for rates in rate_sets]
    if not set.intersection(*period_sets):
        return "rate_period_mismatch"
    return "rate_basis_mismatch"


def _compare_rates(records: Sequence[CampaignRecord]) -> tuple[ComparisonOutcome, ...]:
    missing = {record.id for record in records if not record.data.rates}
    if missing:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.RATE,
            missing,
            "rate_missing",
            "peer_rate_missing",
        )
    for record in records:
        for rate in record.data.rates:
            issue = _rate_issue(rate)
            if issue is not None:
                return _reject_all(records, ComparisonDimension.RATE, issue)

    rate_sets = [record.data.rates for record in records]
    signatures = [{_rate_signature(rate) for rate in rates} for rates in rate_sets]
    common = set.intersection(*signatures)
    if not common:
        return _reject_all(
            records,
            ComparisonDimension.RATE,
            _rate_mismatch_reason(rate_sets),
        )
    if len(common) != 1:
        return _reject_all(records, ComparisonDimension.RATE, "multiple_rate_signatures")
    signature = next(iter(common))

    selected: list[_SelectedValue] = []
    missing_evidence: set[str] = set()
    for record in records:
        matches = [
            (index, rate)
            for index, rate in enumerate(record.data.rates)
            if _rate_signature(rate) == signature
        ]
        if len(matches) != 1:
            return _reject_all(
                records,
                ComparisonDimension.RATE,
                "multiple_rate_values_for_signature",
            )
        index, rate = matches[0]
        evidence_ids = _evidence_ids(record, f"/data/rates/{index}")
        if not evidence_ids:
            missing_evidence.add(record.id)
        selected.append(
            _SelectedValue(
                record=record,
                index=index,
                value=rate.value_percent,
                unit="percent",
                display=rate.raw,
                evidence_ids=evidence_ids,
            )
        )
    if missing_evidence:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.RATE,
            missing_evidence,
            "rate_evidence_missing",
            "peer_rate_evidence_missing",
        )
    kind = next(rate.kind for rate in records[0].data.rates if _rate_signature(rate) == signature)
    higher_is_better = kind in {
        RateKind.PROFIT_SHARE_DISTRIBUTION_RATE,
        RateKind.HISTORICAL_RETURN_RATE,
        RateKind.DISCOUNT_RATE,
        RateKind.REWARD_RATE,
        # A higher financing share means a smaller required down payment.
        RateKind.LTV_RATIO,
    }
    return _rank(selected, ComparisonDimension.RATE, higher_is_better=higher_is_better)


def _compare_terms(records: Sequence[CampaignRecord]) -> tuple[ComparisonOutcome, ...]:
    missing = {record.id for record in records if not record.data.terms}
    if missing:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.TERM,
            missing,
            "term_missing",
            "peer_term_missing",
        )
    if any(
        term.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}
        for record in records
        for term in record.data.terms
    ):
        return _reject_all(records, ComparisonDimension.TERM, "term_ambiguous")

    selected: list[_SelectedValue] = []
    missing_evidence: set[str] = set()
    for record in records:
        evidence_ids = _evidence_ids(record, "/data/terms")
        if not evidence_ids:
            missing_evidence.add(record.id)
        maximum = max(term.maximum_months for term in record.data.terms)
        selected.append(
            _SelectedValue(
                record=record,
                index=0,
                value=maximum,
                unit="months",
                display="; ".join(term.raw for term in record.data.terms),
                evidence_ids=evidence_ids,
            )
        )
    if missing_evidence:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.TERM,
            missing_evidence,
            "term_evidence_missing",
            "peer_term_evidence_missing",
        )
    return _rank(selected, ComparisonDimension.TERM, higher_is_better=True)


def _fee_issue(fee: FeeValue) -> str | None:
    if fee.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}:
        return "fee_basis_unknown"
    if fee.kind is FeeKind.UNKNOWN:
        return "fee_kind_unknown"
    if fee.basis is FeeBasis.UNSPECIFIED:
        return "fee_basis_unknown"
    if fee.waived:
        # Issue #12: a stated waiver is complete without a figure; the source
        # says the fee is not charged, which is itself the comparable fact.
        return None
    if (fee.money is None) == (fee.rate is None):
        return "fee_not_numeric"
    if fee.rate is not None and (
        fee.rate.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}
        or fee.rate.period is RatePeriod.UNSPECIFIED
        or fee.rate.basis_label is None
    ):
        return "fee_rate_context_unknown"
    return None


def _fee_pair(fee: FeeValue) -> tuple[FeeKind, FeeBasis]:
    return fee.kind, fee.basis


def _fee_signature(fee: FeeValue) -> tuple[object, ...]:
    if fee.waived:
        # A waiver prices nothing, so it carries no value mode or currency of
        # its own; `_common_fee_signatures` treats it as compatible with any
        # priced signature that shares its kind and basis.
        value_signature: tuple[object, ...] = ("waived",)
    elif fee.money is not None:
        value_signature = ("money", fee.money.currency)
    else:
        assert fee.rate is not None
        value_signature = (
            "rate",
            fee.rate.period,
            fee.rate.gross_net_basis,
            canonicalize_turkish_text(fee.rate.basis_label or ""),
        )
    return *_fee_pair(fee), *value_signature


def _common_fee_signatures(fee_sets: Sequence[Sequence[FeeValue]]) -> set[tuple[object, ...]]:
    """Collect the signatures every record can serve, waiver-compatibly.

    Issue #12: a priced signature is common when each record either states a
    fee with exactly that signature or waives a fee of the same kind and
    basis — the waiver is currency- and value-mode-agnostic because it
    charges nothing in any currency. The all-waived signature is common only
    when no record prices that kind/basis pair, so a waiver never absorbs a
    stated price into a signature that hides it.
    """

    priced = [{_fee_signature(fee) for fee in fees if not fee.waived} for fees in fee_sets]
    waived = [{_fee_pair(fee) for fee in fees if fee.waived} for fees in fee_sets]

    common: set[tuple[object, ...]] = set()
    priced_union = set.union(*priced)
    for signature in priced_union:
        pair = signature[:2]
        if all(
            signature in record_priced or pair in record_waived
            for record_priced, record_waived in zip(priced, waived, strict=True)
        ):
            common.add(signature)
    for pair in set.intersection(*waived):
        if not any(priced_signature[:2] == pair for priced_signature in priced_union):
            common.add((*pair, "waived"))
    return common


def _fee_matches_signature(fee: FeeValue, signature: tuple[object, ...]) -> bool:
    if fee.waived:
        return _fee_pair(fee) == signature[:2]
    return _fee_signature(fee) == signature


def _fee_mismatch_reason(fee_sets: Sequence[Sequence[FeeValue]]) -> str:
    if not set.intersection(*[{fee.kind for fee in fees} for fees in fee_sets]):
        return "fee_kind_mismatch"
    if not set.intersection(*[{_fee_pair(fee) for fee in fees} for fees in fee_sets]):
        return "fee_basis_mismatch"
    modes = [
        {
            (*_fee_pair(fee), mode)
            for fee in fees
            # A waiver charges nothing in either mode, so it meets both.
            for mode in (("money", "rate") if fee.waived else _fee_value_modes(fee))
        }
        for fees in fee_sets
    ]
    if not set.intersection(*modes):
        return "fee_value_type_mismatch"
    return "fee_currency_mismatch"


def _fee_value_modes(fee: FeeValue) -> tuple[str, ...]:
    return ("money",) if fee.money is not None else ("rate",)


def _compare_fees(records: Sequence[CampaignRecord]) -> tuple[ComparisonOutcome, ...]:
    missing = {record.id for record in records if not record.data.fees}
    if missing:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.FEE,
            missing,
            "fee_missing",
            "peer_fee_missing",
        )
    for record in records:
        for fee in record.data.fees:
            issue = _fee_issue(fee)
            if issue is not None:
                return _reject_all(records, ComparisonDimension.FEE, issue)

    fee_sets = [record.data.fees for record in records]
    common = _common_fee_signatures(fee_sets)
    if not common:
        return _reject_all(records, ComparisonDimension.FEE, _fee_mismatch_reason(fee_sets))
    if len(common) != 1:
        return _reject_all(records, ComparisonDimension.FEE, "multiple_fee_signatures")
    signature = next(iter(common))

    selected: list[_SelectedValue] = []
    missing_evidence: set[str] = set()
    for record in records:
        matches = [
            (index, fee)
            for index, fee in enumerate(record.data.fees)
            if _fee_matches_signature(fee, signature)
        ]
        if len(matches) != 1:
            return _reject_all(records, ComparisonDimension.FEE, "multiple_fee_signatures")
        index, fee = matches[0]
        evidence_ids = _evidence_ids(record, f"/data/fees/{index}")
        if not evidence_ids:
            missing_evidence.add(record.id)
        if fee.waived:
            # Issue #12: the stated waiver ranks ahead of every priced fee via
            # `_rank_key`; it publishes no figure of its own (ADR-002: absence
            # of a number is never coerced to zero — the waiver is a stated
            # fact, not a default).
            value: Decimal | int | None = None
            unit: str | None = None
        elif fee.money is not None:
            value = fee.money.amount
            unit = fee.money.currency
        else:
            assert fee.rate is not None
            value = fee.rate.value_percent
            unit = "percent"
        selected.append(
            _SelectedValue(record, index, value, unit, fee.raw, evidence_ids, waived=fee.waived)
        )
    if missing_evidence:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.FEE,
            missing_evidence,
            "fee_evidence_missing",
            "peer_fee_evidence_missing",
        )
    return _rank(selected, ComparisonDimension.FEE, higher_is_better=False)


def _money_context(value: object) -> tuple[object, ...] | None:
    if value is None:
        return None
    money = value
    return money.currency, str(money.amount)  # type: ignore[attr-defined]


def _reward_issue(reward: RewardValue) -> str | None:
    if reward.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.AMBIGUOUS}:
        return "reward_basis_unknown"
    if reward.basis is RewardBasis.UNSPECIFIED:
        return "reward_basis_unknown"
    if reward.kind is RewardKind.POINTS and reward.program_name is None:
        return "reward_program_unknown"
    if reward.kind is RewardKind.MONEY and reward.money is None:
        return "reward_not_numeric"
    if reward.kind is RewardKind.POINTS and reward.points is None:
        return "reward_not_numeric"
    if reward.kind is RewardKind.DISCOUNT and reward.money is None and reward.rate is None:
        return "reward_not_numeric"
    if reward.rate is not None and _rate_issue(reward.rate) is not None:
        return "reward_rate_context_unknown"
    if reward.kind in {RewardKind.FREE_SERVICE, RewardKind.OTHER}:
        return "reward_not_numeric"
    return None


def _reward_signature(reward: RewardValue) -> tuple[object, ...]:
    if reward.kind is RewardKind.MONEY:
        assert reward.money is not None
        value_signature: tuple[object, ...] = ("money", reward.money.currency)
    elif reward.kind is RewardKind.POINTS:
        value_signature = ("points", canonicalize_turkish_text(reward.program_name or ""))
    else:
        if reward.money is not None:
            value_signature = ("money", reward.money.currency)
        else:
            assert reward.rate is not None
            value_signature = ("rate", *_rate_signature(reward.rate))
    return (
        reward.kind,
        reward.basis,
        *value_signature,
        _money_context(reward.minimum_spend),
        _money_context(reward.maximum_money),
        None if reward.maximum_points is None else str(reward.maximum_points),
    )


def _reward_mismatch_reason(reward_sets: Sequence[Sequence[RewardValue]]) -> str:
    if not set.intersection(*[{reward.kind for reward in rewards} for rewards in reward_sets]):
        return "reward_kind_mismatch"
    if not set.intersection(
        *[{(reward.kind, reward.basis) for reward in rewards} for rewards in reward_sets]
    ):
        return "reward_basis_mismatch"
    programs = [
        {(reward.kind, canonicalize_turkish_text(reward.program_name or "")) for reward in rewards}
        for rewards in reward_sets
    ]
    if not set.intersection(*programs):
        return "reward_program_mismatch"
    return "reward_context_mismatch"


def _compare_rewards(records: Sequence[CampaignRecord]) -> tuple[ComparisonOutcome, ...]:
    missing = {record.id for record in records if not record.data.rewards}
    if missing:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.REWARD,
            missing,
            "reward_missing",
            "peer_reward_missing",
        )
    for record in records:
        for reward in record.data.rewards:
            issue = _reward_issue(reward)
            if issue is not None:
                return _reject_all(records, ComparisonDimension.REWARD, issue)

    reward_sets = [record.data.rewards for record in records]
    common = set.intersection(
        *[{_reward_signature(reward) for reward in rewards} for rewards in reward_sets]
    )
    if not common:
        return _reject_all(
            records,
            ComparisonDimension.REWARD,
            _reward_mismatch_reason(reward_sets),
        )
    if len(common) != 1:
        return _reject_all(records, ComparisonDimension.REWARD, "multiple_reward_signatures")
    signature = next(iter(common))

    selected: list[_SelectedValue] = []
    missing_evidence: set[str] = set()
    for record in records:
        matches = [
            (index, reward)
            for index, reward in enumerate(record.data.rewards)
            if _reward_signature(reward) == signature
        ]
        if len(matches) != 1:
            return _reject_all(records, ComparisonDimension.REWARD, "multiple_reward_signatures")
        index, reward = matches[0]
        evidence_ids = _evidence_ids(record, f"/data/rewards/{index}")
        if not evidence_ids:
            missing_evidence.add(record.id)
        if reward.money is not None:
            value: Decimal | int = reward.money.amount
            unit = reward.money.currency
        elif reward.points is not None:
            value = reward.points
            unit = "points"
        else:
            assert reward.rate is not None
            value = reward.rate.value_percent
            unit = "percent"
        selected.append(_SelectedValue(record, index, value, unit, reward.raw, evidence_ids))
    if missing_evidence:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.REWARD,
            missing_evidence,
            "reward_evidence_missing",
            "peer_reward_evidence_missing",
        )
    return _rank(selected, ComparisonDimension.REWARD, higher_is_better=True)


def _compare_eligibility(records: Sequence[CampaignRecord]) -> tuple[ComparisonOutcome, ...]:
    missing_evidence = {
        record.id for record in records if not _evidence_ids(record, "/data/comparison_context")
    }
    if missing_evidence:
        return _reject_own_and_peers(
            records,
            ComparisonDimension.ELIGIBILITY,
            missing_evidence,
            "eligibility_evidence_missing",
            "peer_eligibility_evidence_missing",
        )
    reason_code = "eligibility_context_compatible"
    return tuple(
        ComparisonOutcome(
            campaign_id=record.id,
            dimension=ComparisonDimension.ELIGIBILITY,
            comparable=True,
            rank=None,
            canonical_value=None,
            unit=None,
            display_value=_display_for(record, ComparisonDimension.ELIGIBILITY),
            reason_code=reason_code,
            explanation=_message(reason_code),
            evidence_ids=_evidence_ids(record, "/data/comparison_context"),
        )
        for record in records
    )


def _compare_dimension(
    records: Sequence[CampaignRecord],
    dimension: ComparisonDimension,
) -> tuple[ComparisonOutcome, ...]:
    if dimension is ComparisonDimension.RATE:
        return _compare_rates(records)
    if dimension is ComparisonDimension.TERM:
        return _compare_terms(records)
    if dimension is ComparisonDimension.FEE:
        return _compare_fees(records)
    if dimension is ComparisonDimension.REWARD:
        return _compare_rewards(records)
    return _compare_eligibility(records)


def _canonical_hash(
    ruleset_version: str,
    as_of: date | datetime | None,
    items: Sequence[ComparisonOutcome],
    warnings: Sequence[str],
) -> str:
    payload = {
        "ruleset_version": ruleset_version,
        "as_of": None if as_of is None else as_of.isoformat(),
        "items": [
            {
                "campaign_id": item.campaign_id,
                "dimension": item.dimension.value,
                "comparable": item.comparable,
                "rank": item.rank,
                "canonical_value": _canonical_number(item.canonical_value),
                "unit": item.unit,
                "display_value": item.display_value,
                "reason_code": item.reason_code,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in items
        ],
        "warnings": list(warnings),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_number(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def compare_campaigns(
    records: Sequence[CampaignRecord],
    dimensions: Iterable[ComparisonDimension],
    *,
    as_of: date | datetime | None = None,
    ruleset_version: str = RULESET_VERSION,
) -> ComparisonReport:
    """Compare validated campaigns without conversion, I/O, time or global scoring."""

    ordered_records = tuple(sorted(records, key=lambda record: record.id))
    if len(ordered_records) < 2:
        raise ValueError("comparison requires at least two campaigns")
    if len({record.id for record in ordered_records}) != len(ordered_records):
        raise ValueError("campaign IDs must be unique")
    ordered_dimensions = tuple(sorted(set(dimensions), key=lambda dimension: dimension.value))
    if not ordered_dimensions:
        raise ValueError("at least one comparison dimension is required")
    if not ruleset_version.strip():
        raise ValueError("ruleset_version must not be empty")

    global_reason, context_notes = _first_global_incompatibility(ordered_records, as_of)
    items: list[ComparisonOutcome] = []
    for dimension in ordered_dimensions:
        if global_reason is not None:
            items.extend(_reject_all(ordered_records, dimension, global_reason))
        else:
            items.extend(_compare_dimension(ordered_records, dimension))
    warnings = (
        *context_notes,
        "Boyutlar arasında birleşik bir puan veya genel kazanan üretilmedi.",
    )
    canonical_sha256 = _canonical_hash(ruleset_version, as_of, items, warnings)
    return ComparisonReport(
        ruleset_version=ruleset_version,
        as_of=as_of,
        items=tuple(items),
        warnings=warnings,
        canonical_sha256=canonical_sha256,
    )


def compatibility_reasons(
    left: CampaignRecord,
    right: CampaignRecord,
    *,
    dimension: ComparisonDimension,
    as_of: date | datetime | None,
) -> tuple[str, ...]:
    """Return stable rejection codes for a pair, or an empty tuple when compatible."""

    report = compare_campaigns([left, right], [dimension], as_of=as_of)
    return tuple(dict.fromkeys(item.reason_code for item in report.items if not item.comparable))
