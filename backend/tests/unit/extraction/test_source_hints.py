"""Issue #33: curated registry static-page labels break product-sheet ambiguity."""

from __future__ import annotations

import pytest
from _factories import make_document

from katilim_analiz.contracts import (
    CampaignType,
    CleanDocument,
    EvidenceStatus,
    ProductFamily,
    RateKind,
    RecordStatus,
)
from katilim_analiz.extraction import ExtractionOutcome, ExtractionPipeline, evaluate_validation
from katilim_analiz.extraction.rules import extract_rules
from katilim_analiz.extraction.source_hints import (
    REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE,
    apply_registry_static_page_hint,
)
from katilim_analiz.llm.contracts import ModelFactField


def _product_sheet_document() -> CleanDocument:
    """The live vakif-katilim "Tasit Finansmani" shape: a financing-dominated
    product sheet whose only competing family signal is one cross-product
    teaser mentioning an investment purpose."""

    return make_document(
        ("heading", "Taşıt Finansmanı"),
        (
            "paragraph",
            "Taşıt Finansmanı desteği ile 0 km ya da ikinci el otomobil "
            "alımlarınızı uygun finansman oranlarıyla gerçekleştirin.",
        ),
        ("paragraph", "Taşıt finansmanı başvurunuzu hemen yapın."),
        ("table", "Tutar | Vade | Kar Oranı"),
        ("table", "100.000 TL | 48 Ay | %3,50"),
        # Cross-product teaser card at the page bottom leaks a weak
        # investment signal into an otherwise financing-only page.
        (
            "list_item",
            "Konut Finansmanı hayalinizdeki eve kavuşmanız için yatırım "
            "amaçlı konut alımlarında yanınızda.",
        ),
    )


def test_diagnosed_conflict_produces_family_ambiguity_without_the_hint() -> None:
    draft = extract_rules(_product_sheet_document())

    assert draft.product_family is None
    assert "product_family_ambiguous" in draft.issues
    assert draft.campaign_type is None
    assert ModelFactField.CAMPAIGN_TYPE in draft.unresolved_fields


def test_registry_label_breaks_weak_cross_link_family_ambiguity() -> None:
    document = _product_sheet_document()
    draft = apply_registry_static_page_hint(extract_rules(document), document, "tasit")

    assert draft.product_family is not None
    assert draft.product_family.value is ProductFamily.FINANCING
    assert draft.product_family.inferred is True
    assert "product_family_ambiguous" not in draft.issues
    assert "registry_hint:product_family:financing:tasit" in draft.issues
    assert REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE not in draft.issues
    assert ModelFactField.PRODUCT_FAMILY not in draft.unresolved_fields


def test_registry_label_resolves_campaign_type_from_the_rate_table() -> None:
    document = _product_sheet_document()
    draft = apply_registry_static_page_hint(extract_rules(document), document, "tasit")

    assert any(rate.value.kind is RateKind.FINANCING_PROFIT_RATE for rate in draft.rates)
    assert draft.campaign_type is not None
    assert draft.campaign_type.value is CampaignType.FINANCING_RATE
    assert draft.campaign_type.inferred is True
    assert "registry_hint:campaign_type:financing_rate:tasit" in draft.issues
    assert ModelFactField.CAMPAIGN_TYPE not in draft.unresolved_fields


def test_registry_label_without_a_priced_rate_leaves_campaign_type_open() -> None:
    document = make_document(
        ("heading", "Taşıt Finansmanı"),
        ("paragraph", "Taşıt Finansmanı başvurularınız şubelerimizde alınır."),
        ("list_item", "Yatırım hesabı ürünlerimizi de inceleyin."),
    )
    draft = apply_registry_static_page_hint(extract_rules(document), document, "tasit")

    assert draft.product_family is not None
    assert draft.product_family.value is ProductFamily.FINANCING
    assert draft.campaign_type is None
    assert ModelFactField.CAMPAIGN_TYPE in draft.unresolved_fields


def test_strongly_contradicting_page_signal_keeps_ambiguity_and_flags_conflict() -> None:
    document = make_document(
        ("heading", "Kredi Kartı Dünyası"),
        ("paragraph", "Kredi kartı başvurusu ve kart aidatı bilgileri."),
        ("paragraph", "Kartınız ile tüm alışverişlerde geçerli."),
        ("paragraph", "Finansman ürünlerimize de göz atın."),
    )
    draft = apply_registry_static_page_hint(extract_rules(document), document, "tasit")

    assert draft.product_family is None
    assert "product_family_ambiguous" in draft.issues
    assert REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE in draft.issues
    decision = evaluate_validation(
        ProductFamily.UNKNOWN,
        CampaignType.UNKNOWN,
        draft.issues,
    )
    assert decision.status is RecordStatus.NEEDS_REVIEW


def test_page_resolved_foreign_family_is_never_overridden() -> None:
    document = make_document(
        ("heading", "Kredi Kartı Kampanyası"),
        ("paragraph", "Kredi kartı sahiplerine özel fırsatlar sunulur."),
    )
    draft = apply_registry_static_page_hint(extract_rules(document), document, "konut")

    assert draft.product_family is not None
    assert draft.product_family.value is ProductFamily.CARD
    assert REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE in draft.issues


def test_unknown_label_asserts_nothing() -> None:
    document = _product_sheet_document()
    baseline = extract_rules(document)

    assert apply_registry_static_page_hint(baseline, document, None) == baseline
    assert apply_registry_static_page_hint(baseline, document, "kampanya") == baseline


def test_hint_is_idempotent() -> None:
    document = _product_sheet_document()
    once = apply_registry_static_page_hint(extract_rules(document), document, "tasit")
    twice = apply_registry_static_page_hint(once, document, "tasit")

    assert twice == once


@pytest.mark.asyncio
async def test_financing_product_sheet_with_rate_and_term_now_validates() -> None:
    """End to end through the rules pipeline: the record that was permanently
    needs_review (issue #33) validates once the registry hint is applied."""

    document = _product_sheet_document()
    pipeline = ExtractionPipeline(model_enabled=False)

    result = await pipeline.extract(document, static_page_label="tasit")

    assert result.outcome is ExtractionOutcome.CANDIDATE
    assert result.candidate is not None
    candidate = result.candidate
    assert candidate.data.product_family is ProductFamily.FINANCING
    assert candidate.data.campaign_type is CampaignType.FINANCING_RATE
    family_evidence = [
        item for item in candidate.evidence if item.field_pointer == "/data/product_family"
    ]
    type_evidence = [
        item for item in candidate.evidence if item.field_pointer == "/data/campaign_type"
    ]
    assert family_evidence and family_evidence[0].status is EvidenceStatus.INFERRED
    assert type_evidence and type_evidence[0].status is EvidenceStatus.INFERRED
    assert "unresolved:campaign_type" not in candidate.issues
    assert "product_family_ambiguous" not in candidate.issues

    decision = evaluate_validation(
        candidate.data.product_family,
        candidate.data.campaign_type,
        candidate.issues,
    )
    assert decision.status is RecordStatus.VALIDATED


@pytest.mark.asyncio
async def test_without_the_label_the_same_sheet_still_needs_review() -> None:
    document = _product_sheet_document()
    pipeline = ExtractionPipeline(model_enabled=False)

    result = await pipeline.extract(document)

    assert result.candidate is not None
    decision = evaluate_validation(
        result.candidate.data.product_family,
        result.candidate.data.campaign_type,
        result.candidate.issues,
    )
    assert decision.status is RecordStatus.NEEDS_REVIEW
    # The ambiguous family leaves the record unclassifiable for the gate.
    assert result.candidate.data.product_family is ProductFamily.UNKNOWN
    assert "product_family_ambiguous" in result.candidate.issues
    assert "classification_not_validatable" in decision.reasons
