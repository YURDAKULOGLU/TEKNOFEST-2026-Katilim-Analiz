"""Model value-grounding for campaign_type (extractor 1.11).

Measured live, the local model usually names the right campaign type but fails
verbatim quote grounding (block_not_sent, evidence_quote_non_unique,
campaign_type_not_supported_by_quote).  These tests pin the narrow repair: the
model's VALUE survives only when the rules locate that exact type's own
deterministic marker in the document, and the rules' span becomes the
evidence.  A type the page never signals stays rejected, and a bound
classification is never overridden.
"""

from __future__ import annotations

from _factories import make_document

from katilim_analiz.contracts import CampaignType
from katilim_analiz.extraction.model_merge import merge_model_response
from katilim_analiz.extraction.rules import campaign_type_evidence_span, extract_rules
from katilim_analiz.llm import ModelExtractionResponse, ModelFactField, ModelFactProposal


def _response(
    document_id: str,
    *facts: ModelFactProposal,
    schema_version: str = "model-extraction/1.1",
) -> ModelExtractionResponse:
    return ModelExtractionResponse(
        schema_version=schema_version,
        document_id=document_id,
        facts=list(facts),
    )


def _ambiguous_document():
    """Body names two mechanics; the heading resolves neither (issue #2 stands)."""

    return make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Alışverişlerinizi taksit imkanıyla ödeyin."),
        ("paragraph", "Seçili ürünlerde indirim uygulanır."),
    )


def test_quote_rejected_value_binds_to_the_rules_own_marker_span() -> None:
    """Ambiguous page: the quote fails grounding, the rules find the value's span."""

    document = _ambiguous_document()
    draft = extract_rules(document)
    assert draft.campaign_type is None
    assert "campaign_type_ambiguous" in draft.issues
    response = _response(
        document.id,
        # The quote names exactly DISCOUNT but is not verbatim on the page,
        # so quote grounding fails with evidence_quote_missing.
        ModelFactProposal(
            field=ModelFactField.CAMPAIGN_TYPE,
            quote="kampanyada harika indirim var",
        ),
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert merged.campaign_type is not None
    assert merged.campaign_type.value is CampaignType.DISCOUNT
    assert merged.campaign_type.inferred is True
    # The evidence is the document's own deterministic "indirim" marker.
    assert merged.campaign_type.span.block_id == document.blocks[2].id
    assert merged.campaign_type.span.quote == "indirim"
    assert "model_value_grounded:campaign_type" in merged.issues
    assert not any(issue.startswith("model_fact_rejected:campaign_type") for issue in merged.issues)
    assert ModelFactField.CAMPAIGN_TYPE not in merged.unresolved_fields


def test_non_unique_quote_grounds_via_the_quote_named_type() -> None:
    """A verbatim but non-unique quote still grounds its single named type."""

    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Alışverişlerinizi taksit imkanıyla ödeyin."),
        ("paragraph", "Seçili ürünlerde indirim var; her sepette indirim uygulanır."),
    )
    draft = extract_rules(document)
    assert draft.campaign_type is None
    response = _response(
        document.id,
        # Verbatim twice in its block: grounding fails with
        # evidence_quote_non_unique, yet the quote names exactly DISCOUNT.
        ModelFactProposal(field=ModelFactField.CAMPAIGN_TYPE, quote="indirim"),
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert merged.campaign_type is not None
    assert merged.campaign_type.value is CampaignType.DISCOUNT
    assert merged.campaign_type.span == campaign_type_evidence_span(
        document, CampaignType.DISCOUNT
    )
    assert "model_value_grounded:campaign_type" in merged.issues
    assert not any(issue.startswith("model_fact_rejected:campaign_type") for issue in merged.issues)


def test_legacy_typed_hint_grounds_when_the_quote_names_no_type() -> None:
    """A stored 1.0 response's typed hint is the value when the quote reads blank."""

    document = _ambiguous_document()
    draft = extract_rules(document)
    assert draft.campaign_type is None
    response = _response(
        document.id,
        ModelFactProposal(
            field=ModelFactField.CAMPAIGN_TYPE,
            quote="kampanya kosullari sayfada listelenmistir",  # names no type
            campaign_type=CampaignType.DISCOUNT,
        ),
        schema_version="model-extraction/1.0",
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert merged.campaign_type is not None
    assert merged.campaign_type.value is CampaignType.DISCOUNT
    assert merged.campaign_type.span.quote == "indirim"
    assert "model_value_grounded:campaign_type" in merged.issues


def test_type_the_page_never_signals_stays_rejected() -> None:
    """The model may not invent a type without its marker on the page."""

    document = _ambiguous_document()
    draft = extract_rules(document)
    assert campaign_type_evidence_span(document, CampaignType.CASHBACK) is None
    response = _response(
        document.id,
        # Names CASHBACK, but no "nakit iade" marker exists anywhere on the page.
        ModelFactProposal(
            field=ModelFactField.CAMPAIGN_TYPE,
            quote="tüm harcamalara nakit iade",
        ),
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 0
    assert merged.campaign_type is None
    assert "model_fact_rejected:campaign_type:evidence_quote_missing" in merged.issues
    assert "model_value_grounded:campaign_type" not in merged.issues


def test_value_grounding_never_overrides_a_bound_campaign_type() -> None:
    """A classification the rules (here: title hint) bound stays bound."""

    document = make_document(
        ("heading", "A101'de 6 Taksit"),
        ("paragraph", "Alışverişlerinizi taksit imkanıyla ödeyin."),
        ("paragraph", "Seçili ürünlerde indirim uygulanır."),
    )
    draft = extract_rules(document)
    assert draft.campaign_type is not None
    assert draft.campaign_type.value is CampaignType.INSTALLMENT
    response = _response(
        document.id,
        ModelFactProposal(
            field=ModelFactField.CAMPAIGN_TYPE,
            quote="kampanyada harika indirim var",  # not verbatim on the page
        ),
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 0
    assert merged.campaign_type is not None
    assert merged.campaign_type.value is CampaignType.INSTALLMENT
    assert "model_value_grounded:campaign_type" not in merged.issues
    assert "model_fact_rejected:campaign_type:evidence_quote_missing" in merged.issues


def test_fully_grounded_proposal_for_the_field_outranks_value_grounding() -> None:
    """When one proposal grounds verbatim, the broken one only logs a rejection."""

    document = _ambiguous_document()
    draft = extract_rules(document)
    assert draft.campaign_type is None
    response = _response(
        document.id,
        ModelFactProposal(field=ModelFactField.CAMPAIGN_TYPE, quote="indirim uygulanır"),
        ModelFactProposal(
            field=ModelFactField.CAMPAIGN_TYPE,
            quote="kampanyada harika indirim var",  # not verbatim on the page
        ),
        schema_version="model-extraction/1.0",
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert merged.campaign_type is not None
    assert merged.campaign_type.value is CampaignType.DISCOUNT
    # The verbatim quote is the evidence, not the rules' marker span.
    assert merged.campaign_type.span.quote == "indirim uygulanır"
    assert "model_value_grounded:campaign_type" not in merged.issues
    assert "model_fact_rejected:campaign_type:evidence_quote_missing" in merged.issues
