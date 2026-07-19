from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from katilim_analiz.contracts import (
    AnswerCitation,
    ChatAnswer,
    ChatQueryPlan,
    CitationStatus,
    ComparisonContext,
    EvidenceRef,
    EvidenceStatus,
    FeeBasis,
    FeeKind,
    FeeValue,
    MoneyValue,
    QueryIntent,
    RateKind,
    RatePeriod,
    RateValue,
)

SHA256 = "0" * 64


def test_money_currency_is_explicit() -> None:
    with pytest.raises(ValidationError):
        MoneyValue.model_validate({"raw": "100", "amount": "100"})

    value = MoneyValue(raw="100 TL", amount=Decimal("100"), currency="TRY")
    assert value.currency == "TRY"


def test_rate_reference_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="reference_starts_on"):
        RateValue(
            raw="geçmiş getiri %40",
            value_percent=Decimal("40"),
            kind=RateKind.HISTORICAL_RETURN_RATE,
            period=RatePeriod.ANNUAL,
            reference_starts_on=date(2026, 12, 31),
            reference_ends_on=date(2026, 1, 1),
        )


def test_percentage_fee_requires_rate_value() -> None:
    with pytest.raises(ValidationError, match="percentage fee"):
        FeeValue(
            raw="tahsis ücreti",
            money=MoneyValue(raw="100 TL", amount=Decimal("100"), currency="TRY"),
            kind=FeeKind.ALLOCATION,
            basis=FeeBasis.PERCENT_OF_AMOUNT,
        )


def test_canonical_comparison_segments_are_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ComparisonContext(customer_segment_keys=["new_customer", "new_customer"])


def test_evidence_span_must_match_quote_length() -> None:
    with pytest.raises(ValidationError, match="span length"):
        EvidenceRef(
            id="evidence-1",
            field_pointer="/data/rates/0/value_percent",
            source_document_id="doc-1",
            block_id="block-1",
            quote="%1,49",
            start_char=10,
            end_char=20,
            evidence_sha256=SHA256,
            status=EvidenceStatus.STATED,
        )


def test_factual_chat_answer_requires_verified_citation() -> None:
    plan = ChatQueryPlan(intent=QueryIntent.DETAIL)
    with pytest.raises(ValidationError, match="verified citation"):
        ChatAnswer(answer="Oran %1,49.", plan=plan, insufficient_evidence=False)

    answer = ChatAnswer(
        answer="Oran %1,49.",
        plan=plan,
        insufficient_evidence=False,
        citations=[
            AnswerCitation(
                id="citation-1",
                source_document_id="doc-1",
                block_id="block-1",
                source_url="https://example.com/campaign",
                quote="Kâr payı oranı %1,49",
                status=CitationStatus.VERIFIED,
            )
        ],
    )
    assert answer.citations[0].status is CitationStatus.VERIFIED
