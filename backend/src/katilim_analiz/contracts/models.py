"""Canonical immutable contracts for evidence-backed campaign analysis."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from katilim_analiz.contracts.enums import (
    CampaignType,
    CitationStatus,
    ComparisonDimension,
    CoverageStatus,
    EvidenceStatus,
    ExtractionMethod,
    FeeBasis,
    FeeKind,
    FetchStatus,
    GrossNetBasis,
    ProductFamily,
    QueryIntent,
    RateKind,
    RatePeriod,
    RecordStatus,
    RewardBasis,
    RewardKind,
    SalesChannel,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
JsonPointer = Annotated[str, StringConstraints(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CanonicalKey = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_:-]{0,63}$"),
]
PersistenceKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=191,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,190}$",
    ),
]


class ContractModel(BaseModel):
    """Strict-at-the-boundary base without accepting unknown object properties."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        json_schema_serialization_defaults_required=True,
    )


def _timezone_required(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return value


class SourceBlock(ContractModel):
    id: NonEmptyStr
    ordinal: Annotated[int, Field(ge=0)]
    kind: Literal["heading", "paragraph", "list_item", "table", "metadata", "other"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=50_000)]
    locator: NonEmptyStr
    text_sha256: Sha256


class FetchArtifact(ContractModel):
    id: NonEmptyStr
    bank_id: NonEmptyStr
    requested_url: HttpUrl
    final_url: HttpUrl | None = None
    status: FetchStatus
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    fetched_at: datetime
    robots_allowed: bool
    content_type: ShortText | None = None
    etag: ShortText | None = None
    last_modified: ShortText | None = None
    raw_sha256: Sha256 | None = None
    raw_size_bytes: Annotated[int, Field(ge=0)] = 0
    private_raw_path: NonEmptyStr | None = None
    error_code: ShortText | None = None
    error_detail: ShortText | None = None

    _validate_fetched_at = field_validator("fetched_at")(_timezone_required)

    @model_validator(mode="after")
    def successful_fetch_has_content(self) -> FetchArtifact:
        if self.status is FetchStatus.SUCCESS and (
            self.raw_sha256 is None or self.final_url is None or self.raw_size_bytes == 0
        ):
            raise ValueError("successful fetch requires final URL, hash, and non-empty content")
        return self


class CleanDocument(ContractModel):
    id: NonEmptyStr
    fetch_artifact_id: NonEmptyStr
    bank_id: NonEmptyStr
    canonical_url: HttpUrl
    title: ShortText | None = None
    cleaned_at: datetime
    cleaner_version: NonEmptyStr
    clean_sha256: Sha256
    language: Literal["tr", "unknown"] = "tr"
    blocks: Annotated[list[SourceBlock], Field(min_length=1, max_length=10_000)]

    _validate_cleaned_at = field_validator("cleaned_at")(_timezone_required)

    @model_validator(mode="after")
    def unique_ordered_blocks(self) -> CleanDocument:
        ids = [block.id for block in self.blocks]
        ordinals = [block.ordinal for block in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("block IDs must be unique")
        if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
            raise ValueError("block ordinals must be unique and ordered")
        return self


class EvidenceRef(ContractModel):
    id: NonEmptyStr
    field_pointer: JsonPointer
    source_document_id: NonEmptyStr
    block_id: NonEmptyStr
    quote: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    start_char: Annotated[int, Field(ge=0)]
    end_char: Annotated[int, Field(gt=0)]
    evidence_sha256: Sha256
    status: EvidenceStatus

    @model_validator(mode="after")
    def valid_span(self) -> EvidenceRef:
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        if self.end_char - self.start_char != len(self.quote):
            raise ValueError("evidence span length must equal quote length")
        return self


class RateValue(ContractModel):
    raw: ShortText
    value_percent: Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1000"))]
    kind: RateKind
    period: RatePeriod
    gross_net_basis: GrossNetBasis = GrossNetBasis.UNSPECIFIED
    reference_starts_on: date | None = None
    reference_ends_on: date | None = None
    term_months: Annotated[int, Field(ge=0, le=1_200)] | None = None
    basis_label: ShortText | None = None
    status: EvidenceStatus = EvidenceStatus.STATED

    @model_validator(mode="after")
    def ordered_reference_window(self) -> RateValue:
        if (
            self.reference_starts_on
            and self.reference_ends_on
            and self.reference_starts_on > self.reference_ends_on
        ):
            raise ValueError("reference_starts_on cannot be after reference_ends_on")
        return self


class MoneyValue(ContractModel):
    raw: ShortText
    amount: Annotated[Decimal, Field(ge=Decimal("0"))]
    currency: CurrencyCode
    status: EvidenceStatus = EvidenceStatus.STATED


class FeeValue(ContractModel):
    raw: ShortText
    money: MoneyValue | None = None
    rate: RateValue | None = None
    kind: FeeKind
    basis: FeeBasis
    description: ShortText | None = None
    status: EvidenceStatus = EvidenceStatus.STATED

    @model_validator(mode="after")
    def concrete_fee_value(self) -> FeeValue:
        if self.money is None and self.rate is None and self.description is None:
            raise ValueError("fee requires money, rate, or an explicit description")
        if self.basis is FeeBasis.PERCENT_OF_AMOUNT and self.rate is None:
            raise ValueError("percentage fee basis requires a rate")
        return self


class TermRange(ContractModel):
    raw: ShortText
    minimum_months: Annotated[int, Field(ge=0, le=1_200)] | None = None
    maximum_months: Annotated[int, Field(ge=0, le=1_200)]
    status: EvidenceStatus = EvidenceStatus.STATED

    @model_validator(mode="after")
    def ordered_range(self) -> TermRange:
        if self.minimum_months is not None and self.minimum_months > self.maximum_months:
            raise ValueError("minimum_months cannot exceed maximum_months")
        return self


class RewardValue(ContractModel):
    raw: ShortText
    kind: RewardKind
    basis: RewardBasis = RewardBasis.UNSPECIFIED
    program_name: ShortText | None = None
    money: MoneyValue | None = None
    rate: RateValue | None = None
    points: Annotated[Decimal, Field(ge=Decimal("0"))] | None = None
    minimum_spend: MoneyValue | None = None
    maximum_money: MoneyValue | None = None
    maximum_points: Annotated[Decimal, Field(ge=Decimal("0"))] | None = None
    description: ShortText | None = None
    status: EvidenceStatus = EvidenceStatus.STATED

    @model_validator(mode="after")
    def value_matches_kind(self) -> RewardValue:
        values = (self.money, self.rate, self.points, self.description)
        present = sum(value is not None for value in values)
        if present == 0:
            raise ValueError("reward needs at least one concrete value or description")
        if self.kind is RewardKind.MONEY and self.money is None:
            raise ValueError("money reward requires money")
        if self.kind is RewardKind.POINTS and self.points is None:
            raise ValueError("points reward requires points")
        if self.kind is RewardKind.DISCOUNT and self.rate is None and self.money is None:
            raise ValueError("discount reward requires a rate or money value")
        return self


class ValidityWindow(ContractModel):
    raw: ShortText
    starts_on: date | None = None
    ends_on: date | None = None
    status: EvidenceStatus = EvidenceStatus.STATED

    @model_validator(mode="after")
    def ordered_dates(self) -> ValidityWindow:
        if self.starts_on and self.ends_on and self.starts_on > self.ends_on:
            raise ValueError("starts_on cannot be after ends_on")
        return self


class ComparisonContext(ContractModel):
    product_currency: CurrencyCode | None = None
    customer_segment_keys: Annotated[list[CanonicalKey], Field(max_length=30)] = Field(
        default_factory=list
    )
    sales_channel: SalesChannel = SalesChannel.UNKNOWN
    new_customer_only: bool | None = None
    product_mechanism: CanonicalKey | None = None
    secured: bool | None = None

    @field_validator("customer_segment_keys")
    @classmethod
    def unique_segments(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("customer segment keys must be unique")
        return value


class CampaignData(ContractModel):
    bank_id: NonEmptyStr
    title: ShortText
    product_family: ProductFamily
    campaign_type: CampaignType
    summary: ShortText | None = None
    rates: Annotated[list[RateValue], Field(max_length=20)] = Field(default_factory=list)
    financing_amounts: Annotated[list[MoneyValue], Field(max_length=20)] = Field(
        default_factory=list
    )
    terms: Annotated[list[TermRange], Field(max_length=20)] = Field(default_factory=list)
    fees: Annotated[list[FeeValue], Field(max_length=20)] = Field(default_factory=list)
    rewards: Annotated[list[RewardValue], Field(max_length=20)] = Field(default_factory=list)
    validity: ValidityWindow | None = None
    customer_segments: Annotated[list[ShortText], Field(max_length=50)] = Field(
        default_factory=list
    )
    eligibility_conditions: Annotated[list[ShortText], Field(max_length=100)] = Field(
        default_factory=list
    )
    comparison_context: ComparisonContext = Field(default_factory=ComparisonContext)


class ExtractionMetadata(ContractModel):
    method: ExtractionMethod
    extractor_version: NonEmptyStr
    schema_version: NonEmptyStr
    prompt_version: NonEmptyStr | None = None
    model_id: NonEmptyStr | None = None
    model_digest: Sha256 | None = None
    started_at: datetime
    completed_at: datetime

    _validate_started_at = field_validator("started_at")(_timezone_required)
    _validate_completed_at = field_validator("completed_at")(_timezone_required)

    @model_validator(mode="after")
    def ordered_times_and_model_metadata(self) -> ExtractionMetadata:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be before started_at")
        if self.method in {ExtractionMethod.LLM, ExtractionMethod.HYBRID} and not self.model_id:
            raise ValueError("LLM and hybrid extraction require model_id")
        return self


class ExtractionCandidate(ContractModel):
    id: NonEmptyStr
    source_document_id: NonEmptyStr
    data: CampaignData
    evidence: Annotated[list[EvidenceRef], Field(max_length=1_000)] = Field(default_factory=list)
    metadata: ExtractionMetadata
    issues: Annotated[list[ShortText], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_ids_unique(self) -> ExtractionCandidate:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        return self


class ExtractionPreviewRequest(ContractModel):
    """Bounded operator text; no URL or execution authority crosses this boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        json_schema_serialization_defaults_required=True,
    )

    bank_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
    ]
    text: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]

    @field_validator("text")
    @classmethod
    def has_bounded_evidence_heading(cls, value: str) -> str:
        first_non_empty = next((line.strip() for line in value.splitlines() if line.strip()), None)
        if first_non_empty is None:
            raise ValueError("text must contain a non-empty evidence heading")
        if len(first_non_empty) > 500:
            raise ValueError("the first non-empty line must be at most 500 characters")
        return value


class ExtractionPreviewResponse(ContractModel):
    """Non-persistent candidate output that can never represent human validation."""

    scope: Literal["unverified_preview"] = "unverified_preview"
    human_verified: Literal[False] = False
    persisted: Literal[False] = False
    status: Literal["needs_review", "abstained"]
    input_sha256: Sha256
    candidate: ExtractionCandidate | None = None
    issues: Annotated[list[ShortText], Field(max_length=100)] = Field(default_factory=list)
    model_attempted: bool
    accepted_model_facts: Annotated[int, Field(ge=0)]


class CampaignRecord(ContractModel):
    id: NonEmptyStr
    version: Annotated[int, Field(ge=1)]
    source_document_id: NonEmptyStr
    observed_at: datetime
    data: CampaignData
    evidence: Annotated[list[EvidenceRef], Field(max_length=1_000)]
    extraction: ExtractionMetadata
    status: RecordStatus
    validation_issues: Annotated[list[ShortText], Field(max_length=100)] = Field(
        default_factory=list
    )
    record_sha256: Sha256

    _validate_observed_at = field_validator("observed_at")(_timezone_required)


class CampaignChangeEvent(ContractModel):
    """Public envelope for one strictly validated campaign-change outbox event."""

    id: UUID
    campaign_key: PersistenceKey
    record_id: PersistenceKey
    record_version: Annotated[int, Field(strict=True, ge=1)]
    change_kind: Literal["created", "updated"]
    record_status: RecordStatus
    previous_record_id: PersistenceKey | None
    observed_at: datetime
    created_at: datetime

    _validate_observed_at = field_validator("observed_at")(_timezone_required)
    _validate_created_at = field_validator("created_at")(_timezone_required)

    @model_validator(mode="after")
    def change_metadata_matches_version(self) -> CampaignChangeEvent:
        if self.change_kind == "created":
            if self.record_version != 1 or self.previous_record_id is not None:
                raise ValueError("created events require version 1 and no previous record")
        elif self.record_version < 2 or self.previous_record_id is None:
            raise ValueError("updated events require version 2 or later and a previous record")
        return self


class CoverageEntry(ContractModel):
    bank_id: NonEmptyStr
    bank_name: NonEmptyStr
    observed_at: datetime
    status: CoverageStatus
    source_count: Annotated[int, Field(ge=0)] = 0
    campaign_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "En güncel, insan tarafından doğrulanmış kampanya kaydı sayısı; "
                "çıkarım adaylarını içermez."
            ),
        ),
    ] = 0
    reason: ShortText | None = None

    _validate_observed_at = field_validator("observed_at")(_timezone_required)


class ComparisonRequest(ContractModel):
    campaign_ids: Annotated[list[NonEmptyStr], Field(min_length=2, max_length=10)]
    dimensions: Annotated[list[ComparisonDimension], Field(min_length=1, max_length=5)]
    as_of: datetime | None = None

    @field_validator("campaign_ids")
    @classmethod
    def campaign_ids_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("campaign_ids must be unique")
        return value

    @field_validator("as_of")
    @classmethod
    def as_of_has_timezone(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _timezone_required(value)


class ComparisonItem(ContractModel):
    campaign_id: NonEmptyStr
    dimension: ComparisonDimension
    rank: Annotated[int, Field(ge=1)] | None = None
    display_value: ShortText
    comparable: bool
    reason_code: NonEmptyStr
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)


class ComparisonResponse(ContractModel):
    ruleset_version: NonEmptyStr
    generated_at: datetime
    items: Annotated[list[ComparisonItem], Field(max_length=100)]
    warnings: list[ShortText] = Field(default_factory=list)
    canonical_sha256: Sha256

    _validate_generated_at = field_validator("generated_at")(_timezone_required)


class ChatRequest(ContractModel):
    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=1_000),
    ]
    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def chat_as_of_has_timezone(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _timezone_required(value)


class ChatQueryPlan(ContractModel):
    intent: QueryIntent
    bank_ids: Annotated[list[NonEmptyStr], Field(max_length=10)] = Field(default_factory=list)
    product_family: ProductFamily | None = None
    campaign_type: CampaignType | None = None
    comparison_dimensions: Annotated[list[ComparisonDimension], Field(max_length=5)] = Field(
        default_factory=list
    )
    keywords: Annotated[list[ShortText], Field(max_length=20)] = Field(default_factory=list)
    limit: Annotated[int, Field(ge=1, le=20)] = 5


class AnswerCitation(ContractModel):
    id: NonEmptyStr
    source_document_id: NonEmptyStr
    block_id: NonEmptyStr
    source_url: HttpUrl
    source_title: ShortText | None = None
    quote: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    status: CitationStatus


class ChatAnswer(ContractModel):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000)]
    plan: ChatQueryPlan
    citations: Annotated[list[AnswerCitation], Field(max_length=20)] = Field(default_factory=list)
    insufficient_evidence: bool
    warnings: list[ShortText] = Field(default_factory=list)

    @model_validator(mode="after")
    def sufficient_answers_need_verified_citations(self) -> ChatAnswer:
        if not self.insufficient_evidence and not any(
            citation.status is CitationStatus.VERIFIED for citation in self.citations
        ):
            raise ValueError("factual answer requires at least one verified citation")
        return self


class ProblemDetail(ContractModel):
    type: str = "about:blank"
    title: NonEmptyStr
    status: Annotated[int, Field(ge=400, le=599)]
    detail: ShortText | None = None
    instance: str | None = None
    code: NonEmptyStr
    correlation_id: NonEmptyStr | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
