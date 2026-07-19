"""Strict, non-authoritative contracts for local model suggestions."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from katilim_analiz.contracts import (
    CampaignType,
    FeeBasis,
    FeeKind,
    GrossNetBasis,
    ProductFamily,
    RateKind,
    RatePeriod,
    RewardBasis,
    RewardKind,
    SalesChannel,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FactText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ModelFactField(StrEnum):
    """The complete set of facts a model is permitted to suggest."""

    TITLE = "title"
    SUMMARY = "summary"
    PRODUCT_FAMILY = "product_family"
    CAMPAIGN_TYPE = "campaign_type"
    RATE = "rate"
    FINANCING_AMOUNT = "financing_amount"
    TERM = "term"
    FEE = "fee"
    REWARD = "reward"
    VALIDITY = "validity"
    CUSTOMER_SEGMENT = "customer_segment"
    ELIGIBILITY_CONDITION = "eligibility_condition"
    SALES_CHANNEL = "sales_channel"
    NEW_CUSTOMER_ONLY = "new_customer_only"


class ModelExtractionOutcome(StrEnum):
    """Compact outcome for one bounded field-family request."""

    EXTRACTED = "extracted"
    NOT_STATED = "not_stated"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class ModelEvidenceSpan(_StrictModel):
    """A model-proposed quote; deterministic code derives its actual span.

    ``start_char`` and ``end_char`` are accepted only for compatibility with
    stored 1.0 responses.  The 1.1 generation schema does not expose them and
    the acceptance boundary never treats them as authoritative.
    """

    block_id: NonEmpty | None = None
    quote: FactText
    start_char: Annotated[int, Field(ge=0)] | None = None
    end_char: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def legacy_offsets_are_well_formed(self) -> ModelEvidenceSpan:
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("legacy evidence offsets must be supplied together")
        if self.start_char is None or self.end_char is None:
            return self
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        if self.end_char - self.start_char != len(self.quote):
            raise ValueError("span length must equal quote length")
        return self


class ModelFactProposal(_StrictModel):
    """One quote-only suggestion plus compatibility fields for stored 1.0 responses."""

    field: ModelFactField
    quote: FactText | None = None
    block_id: NonEmpty | None = None
    # Compatibility-only input for model-extraction/1.0.  New prompts do not
    # ask a small model to duplicate its exact quote into a second property.
    raw_text: FactText | None = None
    evidence: ModelEvidenceSpan | None = None
    product_family: ProductFamily | None = None
    campaign_type: CampaignType | None = None
    rate_kind: RateKind | None = None
    rate_period: RatePeriod | None = None
    gross_net_basis: GrossNetBasis | None = None
    fee_kind: FeeKind | None = None
    fee_basis: FeeBasis | None = None
    reward_kind: RewardKind | None = None
    reward_basis: RewardBasis | None = None
    sales_channel: SalesChannel | None = None
    boolean_value: bool | None = None

    @model_validator(mode="after")
    def hints_are_field_scoped(self) -> ModelFactProposal:
        proposed_quote = self.proposed_quote
        if (
            self.quote is not None
            and self.evidence is not None
            and self.quote != self.evidence.quote
        ):
            raise ValueError("quote contradicts legacy evidence quote")
        if (
            self.block_id is not None
            and self.evidence is not None
            and self.evidence.block_id is not None
            and self.block_id != self.evidence.block_id
        ):
            raise ValueError("block_id contradicts legacy evidence block_id")
        if self.raw_text is not None and self.raw_text != proposed_quote:
            raise ValueError("raw_text must be the exact evidence quote")

        supplied = {
            "product_family": self.product_family,
            "campaign_type": self.campaign_type,
            "rate_kind": self.rate_kind,
            "rate_period": self.rate_period,
            "gross_net_basis": self.gross_net_basis,
            "fee_kind": self.fee_kind,
            "fee_basis": self.fee_basis,
            "reward_kind": self.reward_kind,
            "reward_basis": self.reward_basis,
            "sales_channel": self.sales_channel,
            "boolean_value": self.boolean_value,
        }
        allowed: dict[ModelFactField, frozenset[str]] = {
            ModelFactField.PRODUCT_FAMILY: frozenset({"product_family"}),
            ModelFactField.CAMPAIGN_TYPE: frozenset({"campaign_type"}),
            ModelFactField.RATE: frozenset({"rate_kind", "rate_period", "gross_net_basis"}),
            ModelFactField.FEE: frozenset({"fee_kind", "fee_basis"}),
            ModelFactField.REWARD: frozenset({"reward_kind", "reward_basis"}),
            ModelFactField.SALES_CHANNEL: frozenset({"sales_channel"}),
            ModelFactField.NEW_CUSTOMER_ONLY: frozenset({"boolean_value"}),
        }
        present = {name for name, value in supplied.items() if value is not None}
        permitted = allowed.get(self.field, frozenset())
        if not present.issubset(permitted):
            raise ValueError("semantic hints are not permitted for this field")

        if self.product_family is ProductFamily.UNKNOWN:
            raise ValueError("unknown product_family is not a useful proposal")
        if self.campaign_type is CampaignType.UNKNOWN:
            raise ValueError("unknown campaign_type is not a useful proposal")
        if self.sales_channel is SalesChannel.UNKNOWN:
            raise ValueError("unknown sales_channel is not a useful proposal")
        if self.rate_kind is RateKind.UNKNOWN:
            raise ValueError("unknown rate_kind is not a useful proposal")
        if self.rate_period is RatePeriod.UNSPECIFIED:
            raise ValueError("unspecified rate_period is not a useful proposal")
        if self.gross_net_basis is GrossNetBasis.UNSPECIFIED:
            raise ValueError("unspecified gross_net_basis is not a useful proposal")
        if self.fee_kind is FeeKind.UNKNOWN:
            raise ValueError("unknown fee_kind is not a useful proposal")
        if self.fee_basis is FeeBasis.UNSPECIFIED:
            raise ValueError("unspecified fee_basis is not a useful proposal")
        return self

    @property
    def proposed_quote(self) -> str:
        if self.quote is not None:
            return self.quote
        if self.evidence is not None:
            return self.evidence.quote
        raise ValueError("a fact proposal requires an exact quote")

    @property
    def proposed_block_id(self) -> str | None:
        if self.block_id is not None:
            return self.block_id
        return self.evidence.block_id if self.evidence is not None else None

    @property
    def legacy_offsets(self) -> tuple[int, int] | None:
        if (
            self.evidence is None
            or self.evidence.start_char is None
            or self.evidence.end_char is None
        ):
            return None
        return (self.evidence.start_char, self.evidence.end_char)


class ModelExtractionResponse(_StrictModel):
    """Only accepted body of a structured local inference response."""

    schema_version: Literal["model-extraction/1.0", "model-extraction/1.1"]
    document_id: NonEmpty
    outcome: ModelExtractionOutcome | None = None
    facts: Annotated[list[ModelFactProposal], Field(max_length=64)] = Field(default_factory=list)
    # Compatibility-only.  The 1.1 output schema replaces unbounded prose with
    # one closed outcome token for the entire compact field-family request.
    abstentions: Annotated[list[FactText], Field(max_length=64)] = Field(default_factory=list)

    @model_validator(mode="after")
    def outcome_matches_facts(self) -> ModelExtractionResponse:
        if self.schema_version == "model-extraction/1.1":
            if self.outcome is not None or self.abstentions:
                raise ValueError("model-extraction/1.1 does not allow outcome or abstentions")
            if len(self.facts) > 1:
                raise ValueError("model-extraction/1.1 allows at most one fact")
            for fact in self.facts:
                legacy_or_optional_values = (
                    fact.block_id,
                    fact.raw_text,
                    fact.evidence,
                    fact.product_family,
                    fact.campaign_type,
                    fact.rate_kind,
                    fact.rate_period,
                    fact.gross_net_basis,
                    fact.fee_kind,
                    fact.fee_basis,
                    fact.reward_kind,
                    fact.reward_basis,
                    fact.sales_channel,
                    fact.boolean_value,
                )
                if fact.quote is None or any(
                    value is not None for value in legacy_or_optional_values
                ):
                    raise ValueError("model-extraction/1.1 facts must be quote-only")
                if len(fact.quote) > 120:
                    raise ValueError("model-extraction/1.1 quote exceeds live limit")
        if self.outcome is ModelExtractionOutcome.EXTRACTED and not self.facts:
            raise ValueError("extracted outcome requires at least one fact")
        if self.outcome not in {None, ModelExtractionOutcome.EXTRACTED} and self.facts:
            raise ValueError("non-extracted outcome cannot contain facts")
        return self


def model_extraction_output_schema(
    requested_fields: frozenset[ModelFactField],
) -> dict[str, object]:
    """Build the exact request schema enforced by Ollama's ``format`` field.

    Pydantic model validators deliberately remain the final authority.  This
    request-specific schema pins requested fields while keeping the small-model
    response limited to field labels and exact quotes.  Document identity and
    the compatibility schema version are trusted client-owned metadata, so the
    model is neither asked nor allowed to repeat them.
    """

    if not requested_fields:
        raise ValueError("at least one requested field is required")

    variants: list[dict[str, object]] = []
    for field in sorted(requested_fields, key=lambda item: item.value):
        properties: dict[str, object] = {
            "field": {"type": "string", "const": field.value},
            "quote": {"type": "string", "minLength": 1, "maxLength": 120},
        }
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": ["field", "quote"],
            }
        )

    fact_items: dict[str, object] = variants[0] if len(variants) == 1 else {"oneOf": variants}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "facts": {"type": "array", "maxItems": 1, "items": fact_items},
        },
        "required": ["facts"],
    }
