"""Deterministic acceptance boundary for untrusted model fact proposals."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from katilim_analiz.contracts import (
    CleanDocument,
    EvidenceStatus,
    FeeValue,
    RateKind,
    RatePeriod,
    RewardBasis,
    RewardKind,
    RewardValue,
)
from katilim_analiz.domain import (
    normalize_money,
    normalize_rate,
    normalize_terms,
    normalize_validity,
)
from katilim_analiz.extraction.draft import BoundFact, CustomerSegmentFact, ExtractionDraft
from katilim_analiz.extraction.evidence import EvidenceBindingError, TextSpan, verify_span
from katilim_analiz.extraction.fees import FEE_MARKER, read_fee
from katilim_analiz.extraction.rules import (
    is_explicit_eligibility,
    is_explicit_new_customer_restriction,
    segment_key_for_text,
    supported_campaign_types,
    supported_product_families,
    supported_sales_channels,
)
from katilim_analiz.llm.contracts import (
    ModelExtractionOutcome,
    ModelExtractionResponse,
    ModelFactField,
    ModelFactProposal,
)
from katilim_analiz.llm.safety import is_obvious_prompt_injection


class ProposalRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _span(document: CleanDocument, proposal: ModelFactProposal) -> TextSpan:
    quote = proposal.proposed_quote
    requested_block_id = proposal.proposed_block_id
    matching_blocks = [
        block
        for block in document.blocks
        if requested_block_id is None or block.id == requested_block_id
    ]
    if requested_block_id is not None and not matching_blocks:
        raise ProposalRejected("source_block_missing")
    match: tuple[str, int] | None = None
    for block in matching_blocks:
        cursor = 0
        while True:
            start = block.text.find(quote, cursor)
            if start < 0:
                break
            if match is not None:
                raise ProposalRejected("evidence_quote_non_unique")
            match = (block.id, start)
            cursor = start + 1
    if match is None:
        raise ProposalRejected("evidence_quote_missing")
    block_id, start_char = match
    span = TextSpan(
        block_id=block_id,
        quote=quote,
        start_char=start_char,
        end_char=start_char + len(quote),
    )
    if proposal.legacy_offsets is not None and proposal.legacy_offsets != (
        span.start_char,
        span.end_char,
    ):
        raise ProposalRejected("legacy_offsets_contradict_quote")
    try:
        block = verify_span(document, span)
    except EvidenceBindingError as exc:
        raise ProposalRejected(exc.code) from exc
    if is_obvious_prompt_injection(block.text) or is_obvious_prompt_injection(span.quote):
        raise ProposalRejected("prompt_injection_evidence_rejected")
    return span


def _upsert_by_span[T](
    existing: tuple[BoundFact[T], ...],
    fact: BoundFact[T],
) -> tuple[BoundFact[T], ...]:
    key = (fact.span.block_id, fact.span.start_char, fact.span.end_char)
    without_same_span = tuple(
        item
        for item in existing
        if (item.span.block_id, item.span.start_char, item.span.end_char) != key
    )
    return (*without_same_span, fact)


def _fee(proposal: ModelFactProposal, span: TextSpan) -> FeeValue:
    text = span.quote
    if FEE_MARKER.search(text.casefold()) is None:
        raise ProposalRejected("fee_terminology_missing")
    # The same sentence must read the same way whether a rule or the model
    # brought it here; reading it twice is how the two paths came to disagree
    # about whether a waiver states a charge.
    fee = read_fee(text, status=EvidenceStatus.INFERRED)
    if fee is None:
        raise ProposalRejected("fee_value_not_supported")
    if proposal.fee_kind is not None and proposal.fee_kind is not fee.kind:
        raise ProposalRejected("fee_hint_contradicts_quote")
    if proposal.fee_basis is not None and proposal.fee_basis is not fee.basis:
        raise ProposalRejected("fee_hint_contradicts_quote")
    return fee


def _reward(proposal: ModelFactProposal, span: TextSpan) -> RewardValue:
    text = span.quote
    lowered = text.casefold()
    kind: RewardKind
    basis: RewardBasis
    if re.search(r"\b\d+(?:[.,]\d+)?\s*puan\b", lowered):
        kind = RewardKind.POINTS
        basis = RewardBasis.CAMPAIGN_TOTAL
    elif re.search(r"\b(?:nakit\s+iade|para\s+iadesi)\b", lowered):
        kind = RewardKind.MONEY
        basis = RewardBasis.CAMPAIGN_TOTAL
    elif "indirim" in lowered:
        kind = RewardKind.DISCOUNT
        basis = RewardBasis.PER_TRANSACTION
    elif re.search(r"\b(?:ücretsiz|ucretsiz|bedelsiz)\b", lowered):
        kind = RewardKind.FREE_SERVICE
        basis = RewardBasis.PER_TRANSACTION
    else:
        raise ProposalRejected("reward_terminology_missing")
    if proposal.reward_kind is not None and proposal.reward_kind is not kind:
        raise ProposalRejected("reward_hint_contradicts_quote")
    if proposal.reward_basis is not None and proposal.reward_basis is not basis:
        raise ProposalRejected("reward_hint_contradicts_quote")
    if kind is RewardKind.POINTS:
        matches = tuple(re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*puan\b", text, re.I))
        if len(matches) != 1:
            raise ProposalRejected("reward_points_not_exact")
        try:
            points = Decimal(matches[0].group(1).replace(",", "."))
        except InvalidOperation as exc:
            raise ProposalRejected("reward_points_invalid") from exc
        return RewardValue(
            raw=text,
            kind=kind,
            basis=basis,
            points=points,
            status=EvidenceStatus.INFERRED,
        )
    if kind is RewardKind.MONEY:
        if re.search(r"\b(?:nakit\s+iade|para\s+iadesi)\b", text, re.I) is None:
            raise ProposalRejected("money_reward_terminology_missing")
        money = normalize_money(text).value
        if money is None:
            raise ProposalRejected("money_reward_value_invalid")
        return RewardValue(
            raw=text,
            kind=kind,
            basis=basis,
            money=money,
            status=EvidenceStatus.INFERRED,
        )
    if kind is RewardKind.DISCOUNT:
        if re.search(r"\bindirim\b", text, re.I) is None:
            raise ProposalRejected("discount_terminology_missing")
        rate = normalize_rate(text, kind_hint=RateKind.DISCOUNT_RATE).value
        money = normalize_money(text).value
        if rate is None and money is None:
            raise ProposalRejected("discount_value_invalid")
        return RewardValue(
            raw=text,
            kind=kind,
            basis=basis,
            money=money,
            rate=rate,
            status=EvidenceStatus.INFERRED,
        )
    if kind is RewardKind.FREE_SERVICE:
        if re.search(r"\b(?:ücretsiz|ucretsiz|bedelsiz)\b", text, re.I) is None:
            raise ProposalRejected("free_service_terminology_missing")
        return RewardValue(
            raw=text,
            kind=kind,
            basis=basis,
            description=text,
            status=EvidenceStatus.INFERRED,
        )
    raise ProposalRejected("reward_value_not_supported")


def _apply(
    draft: ExtractionDraft,
    proposal: ModelFactProposal,
    document: CleanDocument,
) -> ExtractionDraft:
    span = _span(document, proposal)
    text = span.quote
    field = proposal.field

    if field is ModelFactField.TITLE:
        block = verify_span(document, span)
        if block.kind != "heading":
            raise ProposalRejected("title_must_be_heading")
        return replace(draft, title=BoundFact(text, span))
    if field is ModelFactField.SUMMARY:
        return replace(draft, summary=BoundFact(text, span))
    if field is ModelFactField.PRODUCT_FAMILY:
        supported = supported_product_families(text)
        if len(supported) != 1:
            raise ProposalRejected("product_family_not_supported_by_quote")
        product_family = next(iter(supported))
        if proposal.product_family is not None and proposal.product_family is not product_family:
            raise ProposalRejected("product_family_hint_contradicts_quote")
        return replace(
            draft,
            product_family=BoundFact(product_family, span, inferred=True),
        )
    if field is ModelFactField.CAMPAIGN_TYPE:
        supported_types = supported_campaign_types(text)
        if len(supported_types) != 1:
            raise ProposalRejected("campaign_type_not_supported_by_quote")
        campaign_type = next(iter(supported_types))
        if proposal.campaign_type is not None and proposal.campaign_type is not campaign_type:
            raise ProposalRejected("campaign_type_hint_contradicts_quote")
        return replace(
            draft,
            campaign_type=BoundFact(campaign_type, span, inferred=True),
        )
    if field is ModelFactField.RATE:
        rate = normalize_rate(text).value
        if rate is None:
            raise ProposalRejected("rate_not_deterministically_normalizable")
        if rate.kind is RateKind.UNKNOWN or rate.period is RatePeriod.UNSPECIFIED:
            raise ProposalRejected("rate_semantics_incomplete")
        if proposal.rate_kind is not None and proposal.rate_kind is not rate.kind:
            raise ProposalRejected("rate_hint_contradicts_quote")
        if proposal.rate_period is not None and proposal.rate_period is not rate.period:
            raise ProposalRejected("rate_hint_contradicts_quote")
        if (
            proposal.gross_net_basis is not None
            and proposal.gross_net_basis is not rate.gross_net_basis
        ):
            raise ProposalRejected("rate_hint_contradicts_quote")
        return replace(
            draft,
            rates=_upsert_by_span(
                draft.rates,
                BoundFact(rate, span, inferred=rate.status is EvidenceStatus.INFERRED),
            ),
        )
    if field is ModelFactField.FINANCING_AMOUNT:
        if re.search(r"\b(?:finansman|kullandırım|kullandirim|tutar)\b", text, re.I) is None:
            raise ProposalRejected("financing_amount_context_missing")
        money = normalize_money(text).value
        if money is None:
            raise ProposalRejected("money_not_deterministically_normalizable")
        return replace(
            draft,
            financing_amounts=_upsert_by_span(
                draft.financing_amounts,
                BoundFact(money, span, inferred=money.status is EvidenceStatus.INFERRED),
            ),
        )
    if field is ModelFactField.TERM:
        terms = normalize_terms(text).value
        if terms is None:
            raise ProposalRejected("term_not_deterministically_normalizable")
        updated = draft.terms
        for term in terms:
            updated = _upsert_by_span(updated, BoundFact(term, span))
        return replace(draft, terms=updated)
    if field is ModelFactField.FEE:
        return replace(
            draft, fees=_upsert_by_span(draft.fees, BoundFact(_fee(proposal, span), span, True))
        )
    if field is ModelFactField.REWARD:
        return replace(
            draft,
            rewards=_upsert_by_span(draft.rewards, BoundFact(_reward(proposal, span), span, True)),
        )
    if field is ModelFactField.VALIDITY:
        validity = normalize_validity(text, reference_date=document.cleaned_at.date()).value
        if validity is None:
            raise ProposalRejected("validity_not_deterministically_normalizable")
        return replace(
            draft,
            validity=BoundFact(
                validity,
                span,
                inferred=validity.status is EvidenceStatus.INFERRED,
            ),
        )
    if field is ModelFactField.CUSTOMER_SEGMENT:
        key = segment_key_for_text(text)
        if key is None or (
            key == "yeni_musteri" and not is_explicit_new_customer_restriction(text)
        ):
            raise ProposalRejected("customer_segment_not_supported_by_quote")
        if key in {segment.canonical_key for segment in draft.customer_segments}:
            return draft
        return replace(
            draft,
            customer_segments=(
                *draft.customer_segments,
                CustomerSegmentFact(text, key, span),
            ),
        )
    if field is ModelFactField.ELIGIBILITY_CONDITION:
        if not is_explicit_eligibility(text):
            raise ProposalRejected("eligibility_not_supported_by_quote")
        return replace(
            draft,
            eligibility_conditions=_upsert_by_span(
                draft.eligibility_conditions,
                BoundFact(text, span),
            ),
        )
    if field is ModelFactField.SALES_CHANNEL:
        supported_channels = supported_sales_channels(text)
        if len(supported_channels) != 1:
            raise ProposalRejected("sales_channel_not_supported_by_quote")
        sales_channel = next(iter(supported_channels))
        if proposal.sales_channel is not None and proposal.sales_channel is not sales_channel:
            raise ProposalRejected("sales_channel_hint_contradicts_quote")
        return replace(
            draft,
            sales_channel=BoundFact(sales_channel, span, inferred=True),
        )
    if field is ModelFactField.NEW_CUSTOMER_ONLY:
        if not is_explicit_new_customer_restriction(text):
            raise ProposalRejected("new_customer_restriction_not_explicit")
        if proposal.boolean_value is not None and proposal.boolean_value is not True:
            raise ProposalRejected("new_customer_hint_contradicts_quote")
        return replace(draft, new_customer_only=BoundFact(True, span, inferred=True))
    raise ProposalRejected("field_not_supported")


_SCALAR_FIELDS = frozenset(
    {
        ModelFactField.TITLE,
        ModelFactField.SUMMARY,
        ModelFactField.PRODUCT_FAMILY,
        ModelFactField.CAMPAIGN_TYPE,
        ModelFactField.VALIDITY,
        ModelFactField.SALES_CHANNEL,
        ModelFactField.NEW_CUSTOMER_ONLY,
    }
)


def _with_issue(draft: ExtractionDraft, issue: str) -> ExtractionDraft:
    if issue in draft.issues:
        return draft
    return replace(draft, issues=(*draft.issues, issue))


def _rejection_issue(proposal: ModelFactProposal, exc: Exception) -> str:
    code = (
        exc.code
        if isinstance(exc, (ProposalRejected, EvidenceBindingError))
        else "contract_invalid"
    )
    return f"model_fact_rejected:{proposal.field.value}:{code}"


def merge_model_response(
    draft: ExtractionDraft,
    response: ModelExtractionResponse,
    document: CleanDocument,
) -> tuple[ExtractionDraft, int]:
    """Accept facts independently; one bad suggestion cannot launder another."""

    merged = draft
    accepted = 0
    resolved: set[ModelFactField] = set()
    scalar_proposals: dict[ModelFactField, list[ModelFactProposal]] = {}
    list_proposals: list[ModelFactProposal] = []
    for proposal in response.facts:
        if proposal.field in _SCALAR_FIELDS:
            scalar_proposals.setdefault(proposal.field, []).append(proposal)
        else:
            list_proposals.append(proposal)

    for field in sorted(scalar_proposals, key=lambda item: item.value):
        valid: list[ModelFactProposal] = []
        for proposal in scalar_proposals[field]:
            try:
                _apply(draft, proposal, document)
            except (ProposalRejected, EvidenceBindingError, ValueError) as exc:
                merged = _with_issue(merged, _rejection_issue(proposal, exc))
            else:
                valid.append(proposal)
        if len(valid) > 1:
            merged = _with_issue(
                merged,
                f"model_scalar_conflict:{field.value}:multiple_accepted_proposals",
            )
            continue
        if not valid:
            continue
        merged = _apply(merged, valid[0], document)
        accepted += 1
        resolved.add(field)

    for proposal in list_proposals:
        try:
            updated = _apply(merged, proposal, document)
        except (ProposalRejected, EvidenceBindingError, ValueError) as exc:
            merged = _with_issue(merged, _rejection_issue(proposal, exc))
            continue
        merged = updated
        accepted += 1
        resolved.add(proposal.field)

    for abstention in response.abstentions:
        issue = f"model_abstained:{abstention[:450]}"
        merged = _with_issue(merged, issue)
    if response.outcome is not None and response.outcome is not ModelExtractionOutcome.EXTRACTED:
        issue = f"model_outcome:{response.outcome.value}"
        merged = _with_issue(merged, issue)
    if ModelFactField.RATE in resolved and any(
        rate.value.kind is RateKind.UNKNOWN or rate.value.period is RatePeriod.UNSPECIFIED
        for rate in merged.rates
    ):
        resolved.remove(ModelFactField.RATE)
        merged = _with_issue(merged, "model_field_incomplete:rate")
    return replace(
        merged,
        unresolved_fields=merged.unresolved_fields.difference(resolved),
    ), accepted


__all__ = ["ProposalRejected", "merge_model_response"]
