"""Internal evidence-bound draft used before the canonical candidate is constructed."""

from __future__ import annotations

from dataclasses import dataclass, field

from katilim_analiz.contracts import (
    CampaignType,
    FeeValue,
    MoneyValue,
    ProductFamily,
    RateValue,
    RewardValue,
    SalesChannel,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.extraction.evidence import TextSpan
from katilim_analiz.llm.contracts import ModelFactField


@dataclass(frozen=True, slots=True)
class BoundFact[T]:
    value: T
    span: TextSpan
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class CustomerSegmentFact:
    display_value: str
    canonical_key: str
    span: TextSpan


@dataclass(frozen=True, slots=True)
class ExtractionDraft:
    bank_id: str
    title: BoundFact[str] | None = None
    summary: BoundFact[str] | None = None
    product_family: BoundFact[ProductFamily] | None = None
    campaign_type: BoundFact[CampaignType] | None = None
    rates: tuple[BoundFact[RateValue], ...] = ()
    financing_amounts: tuple[BoundFact[MoneyValue], ...] = ()
    terms: tuple[BoundFact[TermRange], ...] = ()
    fees: tuple[BoundFact[FeeValue], ...] = ()
    rewards: tuple[BoundFact[RewardValue], ...] = ()
    validity: BoundFact[ValidityWindow] | None = None
    customer_segments: tuple[CustomerSegmentFact, ...] = ()
    eligibility_conditions: tuple[BoundFact[str], ...] = ()
    sales_channel: BoundFact[SalesChannel] | None = None
    new_customer_only: BoundFact[bool] | None = None
    product_mechanism: BoundFact[str] | None = None
    secured: BoundFact[bool] | None = None
    issues: tuple[str, ...] = ()
    unresolved_fields: frozenset[ModelFactField] = field(default_factory=frozenset)
