"""Canonical candidate construction and unsupported-fact validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from katilim_analiz.contracts import (
    CampaignData,
    CampaignType,
    CleanDocument,
    ComparisonContext,
    EvidenceRef,
    EvidenceStatus,
    ExtractionCandidate,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    RateKind,
    RatePeriod,
    SalesChannel,
)
from katilim_analiz.domain import (
    normalize_money,
    normalize_rate,
    normalize_terms,
    normalize_validity,
)
from katilim_analiz.extraction.draft import BoundFact, ExtractionDraft
from katilim_analiz.extraction.evidence import bind_evidence, verify_evidence_ref
from katilim_analiz.extraction.rules import (
    segment_key_for_text,
    supported_campaign_types,
    supported_product_families,
    supported_sales_channels,
)


class CandidateValidationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def _status(fact: BoundFact[Any]) -> EvidenceStatus:
    return EvidenceStatus.INFERRED if fact.inferred else EvidenceStatus.STATED


def _canonical_payload(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _candidate_digest(
    source_document_id: str,
    data: CampaignData,
    evidence: list[EvidenceRef],
    metadata: ExtractionMetadata,
) -> str:
    identity = {
        "source_document_id": source_document_id,
        "data": data.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "method": metadata.method.value,
        "extractor_version": metadata.extractor_version,
        "schema_version": metadata.schema_version,
        "prompt_version": metadata.prompt_version,
        "model_id": metadata.model_id,
        "model_digest": metadata.model_digest,
    }
    return hashlib.sha256(_canonical_payload(identity)).hexdigest()


def build_candidate(
    draft: ExtractionDraft,
    document: CleanDocument,
    *,
    started_at: datetime,
    completed_at: datetime,
    extractor_version: str,
    model_id: str | None = None,
    model_digest: str | None = None,
    prompt_version: str | None = None,
    model_fact_count: int = 0,
) -> ExtractionCandidate:
    """Build a canonical candidate only when its mandatory title is source-bound."""

    if draft.bank_id != document.bank_id:
        raise CandidateValidationError("bank_mismatch", "draft bank differs from source document")
    if draft.title is None:
        raise CandidateValidationError("title_unresolved", "no evidence-backed title was found")
    if model_fact_count < 0:
        raise ValueError("model_fact_count must be non-negative")
    if model_fact_count and (model_id is None or model_digest is None or prompt_version is None):
        raise ValueError("accepted model facts require model identity, digest, and prompt version")

    family = ProductFamily.UNKNOWN if draft.product_family is None else draft.product_family.value
    campaign_type = (
        CampaignType.UNKNOWN if draft.campaign_type is None else draft.campaign_type.value
    )
    currencies = {fact.value.currency for fact in draft.financing_amounts}
    product_currency = next(iter(currencies)) if len(currencies) == 1 else None
    context = ComparisonContext(
        product_currency=product_currency,
        customer_segment_keys=[item.canonical_key for item in draft.customer_segments],
        sales_channel=(
            SalesChannel.UNKNOWN if draft.sales_channel is None else draft.sales_channel.value
        ),
        new_customer_only=(
            None if draft.new_customer_only is None else draft.new_customer_only.value
        ),
        product_mechanism=(
            None if draft.product_mechanism is None else draft.product_mechanism.value
        ),
        secured=None if draft.secured is None else draft.secured.value,
    )
    data = CampaignData(
        bank_id=draft.bank_id,
        title=draft.title.value,
        product_family=family,
        campaign_type=campaign_type,
        summary=None if draft.summary is None else draft.summary.value,
        rates=[fact.value for fact in draft.rates],
        financing_amounts=[fact.value for fact in draft.financing_amounts],
        terms=[fact.value for fact in draft.terms],
        fees=[fact.value for fact in draft.fees],
        rewards=[fact.value for fact in draft.rewards],
        validity=None if draft.validity is None else draft.validity.value,
        customer_segments=[item.display_value for item in draft.customer_segments],
        eligibility_conditions=[fact.value for fact in draft.eligibility_conditions],
        comparison_context=context,
    )

    evidence: list[EvidenceRef] = []

    def add(pointer: str, fact: BoundFact[Any]) -> None:
        evidence.append(bind_evidence(document, pointer, fact.span, status=_status(fact)))

    add("/data/title", draft.title)
    if draft.summary is not None:
        add("/data/summary", draft.summary)
    if draft.product_family is not None:
        add("/data/product_family", draft.product_family)
    if draft.campaign_type is not None:
        add("/data/campaign_type", draft.campaign_type)
    for index, rate_fact in enumerate(draft.rates):
        add(f"/data/rates/{index}", rate_fact)
    for index, amount_fact in enumerate(draft.financing_amounts):
        add(f"/data/financing_amounts/{index}", amount_fact)
    for index, term_fact in enumerate(draft.terms):
        add(f"/data/terms/{index}", term_fact)
    for index, fee_fact in enumerate(draft.fees):
        add(f"/data/fees/{index}", fee_fact)
    for index, reward_fact in enumerate(draft.rewards):
        add(f"/data/rewards/{index}", reward_fact)
    if draft.validity is not None:
        add("/data/validity", draft.validity)
    for index, segment in enumerate(draft.customer_segments):
        fact = BoundFact(segment.display_value, segment.span)
        add(f"/data/customer_segments/{index}", fact)
        add(f"/data/comparison_context/customer_segment_keys/{index}", fact)
    for index, condition_fact in enumerate(draft.eligibility_conditions):
        add(f"/data/eligibility_conditions/{index}", condition_fact)
    if product_currency is not None:
        add("/data/comparison_context/product_currency", draft.financing_amounts[0])
    if draft.sales_channel is not None:
        add("/data/comparison_context/sales_channel", draft.sales_channel)
    if draft.new_customer_only is not None:
        add("/data/comparison_context/new_customer_only", draft.new_customer_only)
    if draft.product_mechanism is not None:
        add("/data/comparison_context/product_mechanism", draft.product_mechanism)
    if draft.secured is not None:
        add("/data/comparison_context/secured", draft.secured)

    method = ExtractionMethod.HYBRID if model_fact_count else ExtractionMethod.RULE
    metadata = ExtractionMetadata(
        method=method,
        extractor_version=extractor_version,
        schema_version="campaign-data/1.0",
        prompt_version=prompt_version if model_fact_count else None,
        model_id=model_id if model_fact_count else None,
        model_digest=model_digest if model_fact_count else None,
        started_at=started_at,
        completed_at=completed_at,
    )
    issues = list(
        dict.fromkeys(
            (
                *draft.issues,
                *(
                    f"unresolved:{item.value}"
                    for item in sorted(draft.unresolved_fields, key=lambda value: value.value)
                ),
            )
        )
    )
    if len(issues) > 100:
        issues = [*issues[:99], "issues_truncated"]
    digest = _candidate_digest(document.id, data, evidence, metadata)
    candidate = ExtractionCandidate(
        id=f"candidate:{digest}",
        source_document_id=document.id,
        data=data,
        evidence=evidence,
        metadata=metadata,
        issues=issues,
    )
    validate_candidate(candidate, document)
    return candidate


def _expected_raw_values(data: CampaignData) -> dict[str, str | None]:
    expected: dict[str, str | None] = {
        "/data/title": data.title,
    }
    if data.summary is not None:
        expected["/data/summary"] = data.summary
    if data.product_family is not ProductFamily.UNKNOWN:
        expected["/data/product_family"] = None
    if data.campaign_type is not CampaignType.UNKNOWN:
        expected["/data/campaign_type"] = None
    for index, rate_value in enumerate(data.rates):
        expected[f"/data/rates/{index}"] = rate_value.raw
    for index, amount_value in enumerate(data.financing_amounts):
        expected[f"/data/financing_amounts/{index}"] = amount_value.raw
    for index, term_value in enumerate(data.terms):
        expected[f"/data/terms/{index}"] = term_value.raw
    for index, fee_value in enumerate(data.fees):
        expected[f"/data/fees/{index}"] = fee_value.raw
    for index, reward_value in enumerate(data.rewards):
        expected[f"/data/rewards/{index}"] = reward_value.raw
    if data.validity is not None:
        expected["/data/validity"] = data.validity.raw
    for index, segment_value in enumerate(data.customer_segments):
        expected[f"/data/customer_segments/{index}"] = segment_value
        expected[f"/data/comparison_context/customer_segment_keys/{index}"] = segment_value
    for index, condition_value in enumerate(data.eligibility_conditions):
        expected[f"/data/eligibility_conditions/{index}"] = condition_value
    context = data.comparison_context
    if context.product_currency is not None:
        expected["/data/comparison_context/product_currency"] = None
    if context.sales_channel is not SalesChannel.UNKNOWN:
        expected["/data/comparison_context/sales_channel"] = None
    if context.new_customer_only is not None:
        expected["/data/comparison_context/new_customer_only"] = None
    if context.product_mechanism is not None:
        expected["/data/comparison_context/product_mechanism"] = None
    if context.secured is not None:
        expected["/data/comparison_context/secured"] = None
    return expected


def validate_candidate(candidate: ExtractionCandidate, document: CleanDocument) -> None:
    """Recheck provenance and reject every non-null fact lacking locatable support."""

    if candidate.source_document_id != document.id:
        raise CandidateValidationError(
            "source_document_mismatch",
            "candidate belongs to a different document",
        )
    if candidate.data.bank_id != document.bank_id:
        raise CandidateValidationError("bank_mismatch", "candidate bank differs from document")
    expected = _expected_raw_values(candidate.data)
    by_pointer: dict[str, list[EvidenceRef]] = {}
    for evidence in candidate.evidence:
        try:
            verify_evidence_ref(document, evidence)
        except ValueError as exc:
            code = getattr(exc, "code", "evidence_invalid")
            raise CandidateValidationError(code, str(exc)) from exc
        if evidence.field_pointer not in expected:
            raise CandidateValidationError(
                "orphan_evidence_pointer",
                f"evidence points outside factual fields: {evidence.field_pointer}",
            )
        by_pointer.setdefault(evidence.field_pointer, []).append(evidence)

    for pointer, raw in expected.items():
        references = by_pointer.get(pointer, [])
        if not references:
            raise CandidateValidationError(
                "unsupported_fact",
                f"non-null fact has no evidence: {pointer}",
            )
        if raw is not None and not any(reference.quote == raw for reference in references):
            raise CandidateValidationError(
                "raw_evidence_mismatch",
                f"fact raw value is not its exact evidence quote: {pointer}",
            )

    family_refs = by_pointer.get("/data/product_family", [])
    if candidate.data.product_family is not ProductFamily.UNKNOWN and not any(
        candidate.data.product_family in supported_product_families(item.quote)
        for item in family_refs
    ):
        raise CandidateValidationError(
            "product_family_evidence_laundering",
            "product family label is not supported by its evidence",
        )
    type_refs = by_pointer.get("/data/campaign_type", [])
    if candidate.data.campaign_type is not CampaignType.UNKNOWN and not any(
        candidate.data.campaign_type in supported_campaign_types(item.quote) for item in type_refs
    ):
        raise CandidateValidationError(
            "campaign_type_evidence_laundering",
            "campaign type label is not supported by its evidence",
        )
    channel = candidate.data.comparison_context.sales_channel
    channel_refs = by_pointer.get("/data/comparison_context/sales_channel", [])
    if channel is not SalesChannel.UNKNOWN and not any(
        channel in supported_sales_channels(item.quote) for item in channel_refs
    ):
        raise CandidateValidationError(
            "sales_channel_evidence_laundering",
            "sales channel label is not supported by its evidence",
        )

    for index, rate in enumerate(candidate.data.rates):
        normalized_rate = normalize_rate(
            rate.raw,
            kind_hint=None if rate.kind is RateKind.UNKNOWN else rate.kind,
            period_hint=None if rate.period is RatePeriod.UNSPECIFIED else rate.period,
            gross_net_hint=rate.gross_net_basis,
            term_months_hint=rate.term_months,
            basis_label_hint=rate.basis_label,
        ).value
        if normalized_rate is None or (
            normalized_rate.value_percent != rate.value_percent
            or normalized_rate.kind is not rate.kind
            or normalized_rate.period is not rate.period
            or normalized_rate.gross_net_basis is not rate.gross_net_basis
            or normalized_rate.term_months != rate.term_months
            or normalized_rate.basis_label != rate.basis_label
        ):
            raise CandidateValidationError(
                "rate_normalization_mismatch",
                f"rate does not deterministically derive from raw evidence: {index}",
            )
    for index, money in enumerate(candidate.data.financing_amounts):
        normalized_money = normalize_money(money.raw).value
        if normalized_money is None or (
            normalized_money.amount != money.amount or normalized_money.currency != money.currency
        ):
            raise CandidateValidationError(
                "money_normalization_mismatch",
                f"money does not deterministically derive from raw evidence: {index}",
            )
    for index, term in enumerate(candidate.data.terms):
        normalized_terms = normalize_terms(term.raw).value
        if normalized_terms is None or not any(
            item.minimum_months == term.minimum_months
            and item.maximum_months == term.maximum_months
            for item in normalized_terms
        ):
            raise CandidateValidationError(
                "term_normalization_mismatch",
                f"term does not deterministically derive from raw evidence: {index}",
            )
    if candidate.data.validity is not None:
        normalized_validity = normalize_validity(
            candidate.data.validity.raw,
            reference_date=document.cleaned_at.date(),
        ).value
        if normalized_validity is None or (
            normalized_validity.starts_on != candidate.data.validity.starts_on
            or normalized_validity.ends_on != candidate.data.validity.ends_on
        ):
            raise CandidateValidationError(
                "validity_normalization_mismatch",
                "validity does not deterministically derive from raw evidence",
            )

    context = candidate.data.comparison_context
    currencies = {item.currency for item in candidate.data.financing_amounts}
    derived_currency = next(iter(currencies)) if len(currencies) == 1 else None
    if context.product_currency != derived_currency:
        raise CandidateValidationError(
            "product_currency_derivation_mismatch",
            "comparison currency must derive from financing amounts",
        )
    derived_segments = [segment_key_for_text(value) for value in candidate.data.customer_segments]
    if (
        any(value is None for value in derived_segments)
        or context.customer_segment_keys != derived_segments
    ):
        raise CandidateValidationError(
            "customer_segment_derivation_mismatch",
            "comparison segment keys must derive from evidence-backed segment text",
        )

    expected_digest = _candidate_digest(
        candidate.source_document_id,
        candidate.data,
        candidate.evidence,
        candidate.metadata,
    )
    if candidate.id != f"candidate:{expected_digest}":
        raise CandidateValidationError(
            "candidate_id_mismatch",
            "candidate ID is not the deterministic content digest",
        )


__all__ = [
    "CandidateValidationError",
    "build_candidate",
    "validate_candidate",
]
