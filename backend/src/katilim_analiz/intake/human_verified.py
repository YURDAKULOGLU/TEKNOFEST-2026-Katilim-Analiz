"""Ingest human-attested campaign facts through the demo-seed persistence path.

Four registry banks cannot be scraped (CAPTCHA, access challenges, or missing
pages) and some fetched pages genuinely lack required facts, so those records
can never be machine-validated.  This module gives a human operator an
evidence-first path: every attested fact must carry a verbatim quote copied by
the human from the official source, quotes are bound to synthetic source
blocks exactly like the demo evidence replay, and the resulting record is
persisted through the same ``PostgresCampaignWriteAdapter`` bundle the demo
seed uses.

Human verification is modeled as ``ExtractionMethod.MANUAL`` plus
``RecordStatus.VALIDATED`` with a ``human_verified`` bookkeeping issue; it is
never derived from - and never counted as - the machine-validation gate in
``extraction.validation_policy`` (whose docstring keeps the two separate by
design).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from katilim_analiz.application.processing import (
    CoverageObservation,
    PersistenceBundle,
    SourceRequest,
)
from katilim_analiz.contracts import (
    CampaignRecord,
    CampaignType,
    CleanDocument,
    CoverageStatus,
    ExtractionMethod,
    FeeBasis,
    FeeKind,
    FeeValue,
    FetchArtifact,
    FetchStatus,
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
    SourceBlock,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.domain import (
    normalize_money,
    normalize_rate,
    normalize_terms,
    normalize_validity,
)
from katilim_analiz.extraction.candidate import build_candidate
from katilim_analiz.extraction.draft import BoundFact, ExtractionDraft
from katilim_analiz.extraction.evidence import TextSpan
from katilim_analiz.extraction.rules import (
    supported_campaign_types,
    supported_product_families,
)
from katilim_analiz.runtime.paths import resolve_runtime_path
from katilim_analiz.runtime.registry import load_runtime_registry, sync_source_registry
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.serialization import canonical_sha256
from katilim_analiz.storage.write_adapter import PostgresCampaignWriteAdapter

MAX_INTAKE_BYTES = 1_048_576
INTAKE_EXTRACTOR_VERSION = "human-verified-intake/1.0.0"
HUMAN_VERIFIED_ISSUE = "human_verified"

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$"),
]
Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]
ShortLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _IntakeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HumanRateFact(_IntakeModel):
    """One rate the human attests, with its verbatim source quote."""

    quote: Quote
    kind: RateKind
    period: RatePeriod
    gross_net_basis: GrossNetBasis = GrossNetBasis.UNSPECIFIED
    term_months: Annotated[int, Field(ge=0, le=1_200)] | None = None
    basis_label: ShortLabel | None = None

    def to_value(self) -> RateValue:
        result = normalize_rate(
            self.quote,
            kind_hint=None if self.kind is RateKind.UNKNOWN else self.kind,
            period_hint=None if self.period is RatePeriod.UNSPECIFIED else self.period,
            gross_net_hint=self.gross_net_basis,
            term_months_hint=self.term_months,
            basis_label_hint=self.basis_label,
        )
        if result.value is None:
            raise ValueError(
                f"rate quote is not deterministically parseable ({result.reason_code}): "
                f"{self.quote!r}"
            )
        return result.value

    @model_validator(mode="after")
    def quote_is_parseable(self) -> HumanRateFact:
        self.to_value()
        return self


class HumanTermFact(_IntakeModel):
    quote: Quote

    def to_values(self) -> tuple[TermRange, ...]:
        result = normalize_terms(self.quote)
        if result.value is None:
            raise ValueError(
                f"term quote is not deterministically parseable ({result.reason_code}): "
                f"{self.quote!r}"
            )
        return result.value

    @model_validator(mode="after")
    def quote_is_parseable(self) -> HumanTermFact:
        self.to_values()
        return self


class HumanAmountFact(_IntakeModel):
    quote: Quote
    currency_hint: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None

    def to_value(self) -> MoneyValue:
        result = normalize_money(self.quote, currency_hint=self.currency_hint)
        if result.value is None:
            raise ValueError(
                f"amount quote is not deterministically parseable ({result.reason_code}): "
                f"{self.quote!r}"
            )
        return result.value

    @model_validator(mode="after")
    def quote_is_parseable(self) -> HumanAmountFact:
        self.to_value()
        return self


class HumanFeeFact(_IntakeModel):
    quote: Quote
    kind: FeeKind
    basis: FeeBasis
    waived: bool = False
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
        | None
    ) = None

    def to_value(self) -> FeeValue:
        return FeeValue(
            raw=self.quote,
            kind=self.kind,
            basis=self.basis,
            waived=self.waived,
            description=self.description,
        )

    @model_validator(mode="after")
    def fee_is_concrete(self) -> HumanFeeFact:
        self.to_value()
        return self


class HumanRewardFact(_IntakeModel):
    quote: Quote
    kind: RewardKind
    basis: RewardBasis = RewardBasis.UNSPECIFIED
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
        | None
    ) = None
    points: Annotated[Decimal, Field(ge=Decimal("0"))] | None = None
    amount: Annotated[Decimal, Field(ge=Decimal("0"))] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None

    def to_value(self) -> RewardValue:
        money = None
        if self.amount is not None and self.currency is not None:
            money = MoneyValue(raw=self.quote, amount=self.amount, currency=self.currency)
        return RewardValue(
            raw=self.quote,
            kind=self.kind,
            basis=self.basis,
            description=self.description,
            points=self.points,
            money=money,
        )

    @model_validator(mode="after")
    def reward_is_concrete(self) -> HumanRewardFact:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("reward money requires both amount and currency")
        self.to_value()
        return self


class HumanValidityFact(_IntakeModel):
    quote: Quote

    def to_value(self, reference_date: date) -> ValidityWindow:
        result = normalize_validity(self.quote, reference_date=reference_date)
        if result.value is None:
            raise ValueError(
                f"validity quote is not deterministically parseable ({result.reason_code}): "
                f"{self.quote!r}"
            )
        return result.value


class HumanVerifiedCampaign(_IntakeModel):
    """One campaign a named human attests from an official source page."""

    intake_id: Identifier
    bank_id: Identifier
    source_url: HttpUrl
    observed_at: datetime
    attested_by: ShortLabel
    attested_on: date
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    title_quote: Quote
    product_family: ProductFamily = ProductFamily.UNKNOWN
    product_family_quote: Quote | None = None
    campaign_type: CampaignType = CampaignType.UNKNOWN
    campaign_type_quote: Quote | None = None
    rates: Annotated[list[HumanRateFact], Field(max_length=20)] = Field(default_factory=list)
    financing_amounts: Annotated[list[HumanAmountFact], Field(max_length=20)] = Field(
        default_factory=list
    )
    terms: Annotated[list[HumanTermFact], Field(max_length=20)] = Field(default_factory=list)
    fees: Annotated[list[HumanFeeFact], Field(max_length=20)] = Field(default_factory=list)
    rewards: Annotated[list[HumanRewardFact], Field(max_length=20)] = Field(default_factory=list)
    validity: HumanValidityFact | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def attested_facts_are_evidence_first(self) -> HumanVerifiedCampaign:
        if self.title_quote != self.title:
            raise ValueError("title_quote must equal the attested title verbatim")
        if self.product_family is not ProductFamily.UNKNOWN:
            if self.product_family_quote is None:
                raise ValueError("a stated product family requires product_family_quote")
            if self.product_family not in supported_product_families(self.product_family_quote):
                raise ValueError(
                    "product_family_quote does not name the attested product family; "
                    "copy the exact phrase from the source page"
                )
        elif self.product_family_quote is not None:
            raise ValueError("product_family_quote requires a stated product family")
        if self.campaign_type is not CampaignType.UNKNOWN:
            if self.campaign_type_quote is None:
                raise ValueError("a stated campaign type requires campaign_type_quote")
            if self.campaign_type not in supported_campaign_types(self.campaign_type_quote):
                raise ValueError(
                    "campaign_type_quote does not name the attested campaign type; "
                    "copy the exact phrase from the source page"
                )
        elif self.campaign_type_quote is not None:
            raise ValueError("campaign_type_quote requires a stated campaign type")
        if self.validity is not None:
            self.validity.to_value(self.observed_at.date())
        return self


class HumanVerifiedIntake(_IntakeModel):
    schema_version: Literal["1.0"]
    dataset_id: Literal["human-verified-intake"]
    intake_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    campaigns: Annotated[list[HumanVerifiedCampaign], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def intake_ids_are_unique(self) -> HumanVerifiedIntake:
        identifiers = [campaign.intake_id for campaign in self.campaigns]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("intake IDs must be unique")
        return self


class HumanVerifiedIntakeResult(_IntakeModel):
    intake_version: str
    registry_version: str
    campaign_count: int
    campaigns_created: int
    human_verified_count: int
    machine_validated_count: Literal[0] = 0
    status: Literal["ingested"] = "ingested"


def load_human_verified_intake(path: str | Path) -> HumanVerifiedIntake:
    """Load one bounded strict JSON intake file; unknown fields fail closed."""

    resolved = resolve_runtime_path(path)
    size = resolved.stat().st_size
    if size > MAX_INTAKE_BYTES:
        raise ValueError(f"intake file exceeds the {MAX_INTAKE_BYTES}-byte parsing limit: {size}")
    return HumanVerifiedIntake.model_validate_json(resolved.read_bytes())


class _BlockBuilder:
    """Deduplicated synthetic source blocks built from human-copied quotes."""

    def __init__(self, intake: HumanVerifiedIntake, campaign: HumanVerifiedCampaign) -> None:
        self._intake = intake
        self._campaign = campaign
        self._blocks: list[SourceBlock] = []
        self._by_quote: dict[str, SourceBlock] = {}

    @property
    def blocks(self) -> list[SourceBlock]:
        return self._blocks

    def span(self, quote: str) -> TextSpan:
        block = self._by_quote.get(quote)
        if block is None:
            digest = canonical_sha256(
                {
                    "intake_version": self._intake.intake_version,
                    "intake_id": self._campaign.intake_id,
                    "quote": quote,
                }
            )
            block = SourceBlock(
                id=f"block:human:{digest}",
                ordinal=len(self._blocks),
                kind="other",
                text=quote,
                locator=(
                    f"human-verified-intake:{self._intake.intake_version}"
                    f"#campaign={self._campaign.intake_id};block={len(self._blocks)}"
                ),
                text_sha256=hashlib.sha256(quote.encode()).hexdigest(),
            )
            self._blocks.append(block)
            self._by_quote[quote] = block
        return TextSpan(block.id, quote, 0, len(quote))


def _document_and_fetch(
    intake: HumanVerifiedIntake,
    campaign: HumanVerifiedCampaign,
    builder: _BlockBuilder,
) -> tuple[FetchArtifact, CleanDocument]:
    blocks = builder.blocks
    replay_bytes = "\n".join(block.text for block in blocks).encode()
    replay_sha256 = hashlib.sha256(replay_bytes).hexdigest()
    fetch_digest = canonical_sha256(
        {
            "intake_version": intake.intake_version,
            "intake_id": campaign.intake_id,
            "source_url": str(campaign.source_url),
            "observed_at": campaign.observed_at,
            "replay_sha256": replay_sha256,
        }
    )
    fetched = FetchArtifact(
        id=f"fetch:human:{fetch_digest}",
        bank_id=campaign.bank_id,
        requested_url=campaign.source_url,
        final_url=campaign.source_url,
        status=FetchStatus.SUCCESS,
        fetched_at=campaign.observed_at,
        robots_allowed=True,
        content_type="application/vnd.katilim-analiz.human-attestation+text",
        raw_sha256=replay_sha256,
        raw_size_bytes=len(replay_bytes),
        private_raw_path=(
            f"human-verified-intake:{intake.intake_version}"
            f"#campaign={campaign.intake_id};attestation"
        ),
    )
    clean_sha256 = canonical_sha256(
        {
            "intake_version": intake.intake_version,
            "intake_id": campaign.intake_id,
            "blocks": [block.model_dump(mode="json") for block in blocks],
        }
    )
    document_digest = canonical_sha256(
        {
            "fetch_artifact_id": fetched.id,
            "cleaner_version": INTAKE_EXTRACTOR_VERSION,
            "clean_sha256": clean_sha256,
        }
    )
    document = CleanDocument(
        id=f"clean:human:{document_digest}",
        fetch_artifact_id=fetched.id,
        bank_id=campaign.bank_id,
        canonical_url=campaign.source_url,
        title=campaign.title,
        cleaned_at=campaign.observed_at,
        cleaner_version=INTAKE_EXTRACTOR_VERSION,
        clean_sha256=clean_sha256,
        language="tr",
        blocks=blocks,
    )
    return fetched, document


def _draft(campaign: HumanVerifiedCampaign, builder: _BlockBuilder) -> ExtractionDraft:
    rates: list[BoundFact[RateValue]] = [
        BoundFact(fact.to_value(), builder.span(fact.quote)) for fact in campaign.rates
    ]
    amounts: list[BoundFact[MoneyValue]] = [
        BoundFact(fact.to_value(), builder.span(fact.quote)) for fact in campaign.financing_amounts
    ]
    terms: list[BoundFact[TermRange]] = [
        BoundFact(term, builder.span(fact.quote))
        for fact in campaign.terms
        for term in fact.to_values()
    ]
    fees: list[BoundFact[FeeValue]] = [
        BoundFact(fact.to_value(), builder.span(fact.quote)) for fact in campaign.fees
    ]
    rewards: list[BoundFact[RewardValue]] = [
        BoundFact(fact.to_value(), builder.span(fact.quote)) for fact in campaign.rewards
    ]
    return ExtractionDraft(
        bank_id=campaign.bank_id,
        title=BoundFact(campaign.title, builder.span(campaign.title_quote)),
        product_family=(
            None
            if campaign.product_family is ProductFamily.UNKNOWN
            or campaign.product_family_quote is None
            else BoundFact(
                campaign.product_family,
                builder.span(campaign.product_family_quote),
                inferred=True,
            )
        ),
        campaign_type=(
            None
            if campaign.campaign_type is CampaignType.UNKNOWN
            or campaign.campaign_type_quote is None
            else BoundFact(
                campaign.campaign_type,
                builder.span(campaign.campaign_type_quote),
                inferred=True,
            )
        ),
        rates=tuple(rates),
        financing_amounts=tuple(amounts),
        terms=tuple(terms),
        fees=tuple(fees),
        rewards=tuple(rewards),
        validity=(
            None
            if campaign.validity is None
            else BoundFact(
                campaign.validity.to_value(campaign.observed_at.date()),
                builder.span(campaign.validity.quote),
            )
        ),
        issues=(
            HUMAN_VERIFIED_ISSUE,
            f"attested_by:{campaign.attested_by}",
            f"attested_on:{campaign.attested_on.isoformat()}",
        ),
    )


def materialize_human_verified_campaign(
    intake: HumanVerifiedIntake,
    campaign: HumanVerifiedCampaign,
    *,
    bank_name: str,
    record_id: str,
    version: int,
) -> PersistenceBundle:
    """Build the same immutable, human-verified bundle on every replay."""

    builder = _BlockBuilder(intake, campaign)
    draft = _draft(campaign, builder)
    fetched, document = _document_and_fetch(intake, campaign, builder)
    candidate = build_candidate(
        draft,
        document,
        started_at=campaign.observed_at,
        completed_at=campaign.observed_at,
        extractor_version=INTAKE_EXTRACTOR_VERSION,
        method=ExtractionMethod.MANUAL,
    )
    campaign_key = f"human:{campaign.intake_id}"
    record_sha256 = canonical_sha256(
        {
            "campaign_key": campaign_key,
            "candidate_id": candidate.id,
            "status": RecordStatus.VALIDATED,
            "validation_issues": candidate.issues,
        }
    )
    record = CampaignRecord(
        id=record_id,
        version=version,
        source_document_id=document.id,
        observed_at=campaign.observed_at,
        data=candidate.data,
        evidence=candidate.evidence,
        extraction=candidate.metadata,
        status=RecordStatus.VALIDATED,
        validation_issues=candidate.issues,
        record_sha256=record_sha256,
    )
    return PersistenceBundle(
        source=SourceRequest(
            bank_id=campaign.bank_id,
            bank_name=bank_name,
            source_url=campaign.source_url,
            campaign_key=campaign_key,
            scan_run_id=f"human-intake:{intake.dataset_id}:{intake.intake_version}",
        ),
        fetch_artifact=fetched,
        clean_document=document,
        candidate=candidate,
        record=record,
        coverage_observation=CoverageObservation(
            bank_id=campaign.bank_id,
            bank_name=bank_name,
            observed_at=campaign.observed_at,
            status=CoverageStatus.SUCCESS,
            reason="human_verified_manual_intake",
        ),
    )


async def ingest_human_verified(
    database: Database,
    *,
    intake_path: str | Path,
    registry_path: str | Path | None = None,
) -> HumanVerifiedIntakeResult:
    """Idempotently upsert human-attested records through the seed storage path."""

    intake = load_human_verified_intake(intake_path)
    registry = load_runtime_registry(registry_path)
    await sync_source_registry(database, registry)

    writer = PostgresCampaignWriteAdapter(database.session_factory)
    created = 0
    for campaign in intake.campaigns:
        bank = registry.bank(campaign.bank_id)
        bundle = materialize_human_verified_campaign(
            intake,
            campaign,
            bank_name=bank.legal_name,
            record_id="record:human:provisional",
            version=1,
        )
        persisted = await writer.persist(bundle)
        created += int(persisted.record_created is True)

    return HumanVerifiedIntakeResult(
        intake_version=intake.intake_version,
        registry_version=registry.registry_version,
        campaign_count=len(intake.campaigns),
        campaigns_created=created,
        human_verified_count=len(intake.campaigns),
    )
