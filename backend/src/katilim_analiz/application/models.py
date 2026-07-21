"""Typed application projections; persistence implementations must not leak ORM rows."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from katilim_analiz.contracts import (
    CampaignChangeEvent,
    CampaignRecord,
    CampaignType,
    CoverageEntry,
    EvidenceStatus,
    ExtractionCandidate,
    ExtractionMethod,
    ProductFamily,
    RecordStatus,
    SalesChannel,
    ValidityWindow,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


def _timezone_required(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return value


class ApplicationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        json_schema_serialization_defaults_required=True,
    )


class ValidityFilter(StrEnum):
    ACTIVE = "active"
    UPCOMING = "upcoming"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class CampaignCursor(ApplicationModel):
    """Keyset position for descending observed time and ascending stable ID."""

    observed_at: datetime
    campaign_id: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=191,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,190}$",
        ),
    ]

    _validate_observed_at = field_validator("observed_at")(_timezone_required)


class CampaignListFilters(ApplicationModel):
    bank_id: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
        ]
        | None
    ) = None
    product_family: ProductFamily | None = None
    currency: CurrencyCode | None = None
    customer_segment: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    sales_channel: SalesChannel | None = None
    validity: ValidityFilter | None = None


class FacetOption(ApplicationModel):
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    label: ShortText
    count: Annotated[int, Field(ge=0)]


class CampaignFacets(ApplicationModel):
    banks: Annotated[list[FacetOption], Field(max_length=100)] = Field(default_factory=list)
    product_families: Annotated[list[FacetOption], Field(max_length=100)] = Field(
        default_factory=list
    )
    currencies: Annotated[list[FacetOption], Field(max_length=100)] = Field(default_factory=list)
    customer_segments: Annotated[list[FacetOption], Field(max_length=100)] = Field(
        default_factory=list
    )
    sales_channels: Annotated[list[FacetOption], Field(max_length=100)] = Field(
        default_factory=list
    )


class CampaignProjection(ApplicationModel):
    """Complete canonical record with registry and immutable source provenance."""

    record: CampaignRecord
    campaign_key: NonEmpty | None = None
    bank_name: ShortText
    source_url: HttpUrl
    source_title: ShortText | None = None


class CampaignReadSlice(ApplicationModel):
    """Repository result for latest logical campaigns at an explicit as-of instant."""

    items: Annotated[list[CampaignProjection], Field(max_length=100)]
    has_more: bool
    facets: CampaignFacets


class PrimaryValue(ApplicationModel):
    label: ShortText
    value: ShortText


class CampaignSummary(ApplicationModel):
    id: NonEmpty
    campaign_key: NonEmpty
    version: Annotated[int, Field(ge=1)]
    bank_id: NonEmpty
    bank_name: ShortText
    title: ShortText
    product_family: ProductFamily
    campaign_type: CampaignType
    summary: ShortText | None = None
    currency: CurrencyCode | None = None
    customer_segments: Annotated[list[ShortText], Field(max_length=50)] = Field(
        default_factory=list
    )
    sales_channel: SalesChannel
    validity: ValidityWindow | None = None
    observed_at: datetime
    status: RecordStatus
    evidence_count: Annotated[int, Field(ge=0)]
    primary_value: PrimaryValue | None = None

    _validate_observed_at = field_validator("observed_at")(_timezone_required)


class NotificationCursor(ApplicationModel):
    """Ascending keyset position over the immutable, commit-safe outbox sequence."""

    feed_sequence: Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]


class NotificationReadItem(ApplicationModel):
    """Internal feed position paired with the unchanged public event contract."""

    feed_sequence: Annotated[int, Field(ge=1, le=9_223_372_036_854_775_807)]
    event: CampaignChangeEvent


class NotificationReadSlice(ApplicationModel):
    items: Annotated[list[NotificationReadItem], Field(max_length=100)]
    has_more: bool


class NotificationListResponse(ApplicationModel):
    items: Annotated[list[CampaignChangeEvent], Field(max_length=100)]
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None


class CampaignListResponse(ApplicationModel):
    items: Annotated[list[CampaignSummary], Field(max_length=100)]
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    facets: CampaignFacets
    as_of: datetime

    _validate_as_of = field_validator("as_of")(_timezone_required)


class EvidenceProjection(ApplicationModel):
    id: NonEmpty
    field_pointer: NonEmpty
    quote: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    status: EvidenceStatus
    block_id: NonEmpty


class ExtractionProjection(ApplicationModel):
    method: ExtractionMethod
    extractor_version: NonEmpty
    schema_version: NonEmpty
    model_id: NonEmpty | None = None


class PreviewExtractionOutput(ApplicationModel):
    """Adapter result before the public unverified-preview label is applied."""

    candidate: ExtractionCandidate | None = None
    issues: Annotated[list[ShortText], Field(max_length=100)] = Field(default_factory=list)
    model_attempted: bool
    accepted_model_facts: Annotated[int, Field(ge=0)]


class CampaignDetail(ApplicationModel):
    campaign: CampaignSummary
    source_document_id: NonEmpty
    source_url: HttpUrl
    source_title: ShortText | None = None
    record_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    extraction: ExtractionProjection
    evidence: Annotated[list[EvidenceProjection], Field(max_length=1_000)]
    validation_issues: Annotated[list[ShortText], Field(max_length=100)] = Field(
        default_factory=list
    )


class HealthChecks(ApplicationModel):
    database: Literal["ok", "failed"]
    migration: Literal["ok", "failed", "out_of_date"]
    model: Literal["ok", "failed", "not_required"]


class HealthResponse(ApplicationModel):
    status: Literal["ok", "degraded"]
    checks: HealthChecks


CoverageResponse = list[CoverageEntry]
