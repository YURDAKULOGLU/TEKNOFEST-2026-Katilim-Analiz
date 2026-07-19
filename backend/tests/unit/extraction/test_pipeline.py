from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from _factories import make_document

from katilim_analiz.contracts import (
    CampaignType,
    CleanDocument,
    ExtractionMethod,
)
from katilim_analiz.extraction import ExtractionOutcome, ExtractionPipeline
from katilim_analiz.extraction.model_merge import merge_model_response
from katilim_analiz.extraction.rules import extract_rules
from katilim_analiz.llm import (
    ModelEvidenceSpan,
    ModelExtractionResponse,
    ModelFactField,
    ModelFactProposal,
    ModelFailureCode,
    ModelInferenceError,
    ModelInferenceSkipped,
)
from katilim_analiz.llm.contracts import ModelExtractionOutcome


@dataclass
class FakeModel:
    response: ModelExtractionResponse | None = None
    error: ModelInferenceError | ModelInferenceSkipped | None = None
    model_id: str = "qwen3.5:4b"
    model_digest: str = "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    requested: frozenset[ModelFactField] | None = None

    async def extract(
        self,
        document: CleanDocument,
        requested_fields: frozenset[ModelFactField],
    ) -> ModelExtractionResponse:
        self.requested = requested_fields
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _clock() -> datetime:
    return datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_pipeline_calls_model_only_for_unresolved_fields_and_accepts_supported_fact() -> None:
    document = make_document(
        ("heading", "Kart Kampanyası"),
        ("paragraph", "Seçili alışverişlerde indirim ve taksit fırsatı sunulur."),
    )
    block = document.blocks[1]
    quote = "indirim"
    start = block.text.index(quote)
    proposal = ModelFactProposal(
        field=ModelFactField.CAMPAIGN_TYPE,
        raw_text=quote,
        evidence=ModelEvidenceSpan(
            block_id=block.id,
            quote=quote,
            start_char=start,
            end_char=start + len(quote),
        ),
        campaign_type=CampaignType.DISCOUNT,
    )
    model = FakeModel(
        response=ModelExtractionResponse(
            schema_version="model-extraction/1.0",
            document_id=document.id,
            facts=[proposal],
        )
    )
    pipeline = ExtractionPipeline(model=model, model_enabled=True, clock=_clock)

    result = await pipeline.extract(document)

    assert result.outcome is ExtractionOutcome.CANDIDATE
    assert result.candidate is not None
    assert result.candidate.data.campaign_type is CampaignType.DISCOUNT
    assert result.candidate.metadata.method is ExtractionMethod.HYBRID
    assert result.candidate.metadata.model_id == "qwen3.5:4b"
    assert result.candidate.metadata.model_digest == model.model_digest
    assert result.accepted_model_facts == 1
    assert model.requested is not None
    assert ModelFactField.CAMPAIGN_TYPE in model.requested


@pytest.mark.asyncio
async def test_timeout_is_honestly_reported_and_rules_candidate_survives(
    campaign_document: CleanDocument,
) -> None:
    model = FakeModel(error=ModelInferenceError(ModelFailureCode.TIMEOUT, "120 second timeout"))
    pipeline = ExtractionPipeline(model=model, model_enabled=True, clock=_clock)

    result = await pipeline.extract(campaign_document)

    assert result.outcome is ExtractionOutcome.CANDIDATE
    assert result.candidate is not None
    assert result.candidate.metadata.method is ExtractionMethod.RULE
    assert "model_timeout:rules_fallback" in result.issues
    assert result.accepted_model_facts == 0


@pytest.mark.asyncio
async def test_typed_model_skip_does_not_claim_physical_inference(
    campaign_document: CleanDocument,
) -> None:
    model = FakeModel(error=ModelInferenceSkipped("no_relevant_source_signal"))

    result = await ExtractionPipeline(model=model, model_enabled=True, clock=_clock).extract(
        campaign_document
    )

    assert result.candidate is not None
    assert result.model_attempted is False
    assert "model_skipped:no_relevant_source_signal" in result.issues


@pytest.mark.asyncio
async def test_valid_span_cannot_launder_unsupported_classification() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Bu fırsat tüm müşterilere sunulur."),
    )
    block = document.blocks[1]
    quote = "fırsat"
    start = block.text.index(quote)
    proposal = ModelFactProposal(
        field=ModelFactField.CAMPAIGN_TYPE,
        raw_text=quote,
        evidence=ModelEvidenceSpan(
            block_id=block.id,
            quote=quote,
            start_char=start,
            end_char=start + len(quote),
        ),
        campaign_type=CampaignType.CASHBACK,
    )
    model = FakeModel(
        response=ModelExtractionResponse(
            schema_version="model-extraction/1.0",
            document_id=document.id,
            facts=[proposal],
        )
    )

    result = await ExtractionPipeline(
        model=model,
        model_enabled=True,
        clock=_clock,
    ).extract(document)

    assert result.candidate is not None
    assert result.candidate.data.campaign_type is CampaignType.UNKNOWN
    assert result.accepted_model_facts == 0
    assert any("campaign_type_not_supported_by_quote" in issue for issue in result.issues)


@pytest.mark.asyncio
async def test_missing_evidence_backed_title_abstains() -> None:
    document = make_document(("paragraph", "31 Ağustos 2026 tarihine kadar geçerlidir."))
    pipeline = ExtractionPipeline(model_enabled=False, clock=_clock)

    result = await pipeline.extract(document)

    assert result.outcome is ExtractionOutcome.ABSTAINED
    assert result.candidate is None
    assert "candidate_abstained:title_unresolved" in result.issues


@pytest.mark.asyncio
async def test_tom_listing_campaigns_are_extracted_separately_without_cross_contamination() -> None:
    document = make_document(
        ("heading", "Kampanyalar"),
        ("heading", "Restoran Harcamalarında %10 Nakit İade"),
        ("paragraph", "Hafta sonu restoran harcamalarında %10 nakit iade kazan."),
        ("heading", "Kampanya Koşulları"),
        (
            "list_item",
            "İlk kampanyadan yararlanmak için Katıl butonuna basılması gerekmektedir.",
        ),
        ("heading", "Özel Okul Ödemelerinde 10 Taksit"),
        ("paragraph", "Özel okul ödemelerinde vade farksız 10 taksit fırsatı."),
        ("heading", "Kampanya Koşulları"),
        (
            "list_item",
            "İkinci kampanya sadece Hadi Black Kredi Kartı ile geçerlidir.",
        ),
    )
    prefix = "html > body > main > section > div > div"
    locators = [
        f"{prefix} > h4",
        f"{prefix} > div:nth-of-type(1) > div > h5",
        f"{prefix} > div:nth-of-type(1) > div > p",
        f"{prefix} > section:nth-of-type(1) > div > h5",
        f"{prefix} > section:nth-of-type(1) > div > ul > li",
        f"{prefix} > div:nth-of-type(2) > div > h5",
        f"{prefix} > div:nth-of-type(2) > div > p",
        f"{prefix} > section:nth-of-type(2) > div > h5",
        f"{prefix} > section:nth-of-type(2) > div > ul > li",
    ]
    document = document.model_copy(
        update={
            "bank_id": "tom-katilim",
            "canonical_url": "https://www.tombank.com.tr/kampanyalar.html",
            "blocks": [
                block.model_copy(update={"locator": locator})
                for block, locator in zip(document.blocks, locators, strict=True)
            ],
        }
    )
    pipeline = ExtractionPipeline(model_enabled=False, clock=_clock)

    merged = await pipeline.extract(document)
    separated = await pipeline.extract_many(document)

    assert merged.outcome is ExtractionOutcome.ABSTAINED
    assert merged.candidate is None
    assert "multiple_campaigns_require_extract_many" in merged.issues
    assert [result.candidate.data.title for result in separated if result.candidate] == [
        "Restoran Harcamalarında %10 Nakit İade",
        "Özel Okul Ödemelerinde 10 Taksit",
    ]
    first, second = (result.candidate for result in separated)
    assert first is not None and second is not None
    assert first.data.eligibility_conditions == [
        "İlk kampanyadan yararlanmak için Katıl butonuna basılması gerekmektedir."
    ]
    assert second.data.eligibility_conditions == [
        "İkinci kampanya sadece Hadi Black Kredi Kartı ile geçerlidir."
    ]

    unrecognized = make_document(
        ("heading", "Kampanyalar"),
        ("heading", "Birinci Kampanya"),
        ("heading", "İkinci Kampanya"),
    ).model_copy(
        update={
            "bank_id": "tom-katilim",
            "canonical_url": "https://www.tombank.com.tr/kampanyalar.html",
        }
    )
    unresolved = await pipeline.extract_many(unrecognized)

    assert unresolved[0].outcome is ExtractionOutcome.ABSTAINED
    assert unresolved[0].issues == ("campaign_listing_segmentation_unresolved",)


def test_quote_only_model_fact_derives_unique_offsets_deterministically() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Seçili harcamalarda indirim uygulanır."),
    )
    quote = "indirim"
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.1",
        document_id=document.id,
        facts=[ModelFactProposal(field=ModelFactField.CAMPAIGN_TYPE, quote=quote)],
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 1
    assert merged.campaign_type is not None
    assert merged.campaign_type.span.block_id == document.blocks[1].id
    assert merged.campaign_type.span.start_char == document.blocks[1].text.index(quote)
    assert merged.campaign_type.span.end_char == (document.blocks[1].text.index(quote) + len(quote))


@pytest.mark.parametrize(
    ("paragraph", "quote", "expected_code"),
    [
        ("indirim yanında ikinci bir indirim bulunur.", "indirim", "evidence_quote_non_unique"),
        ("Kaynakta yalnız kampanya bilgisi vardır.", "indirim", "evidence_quote_missing"),
    ],
)
def test_quote_alignment_rejects_non_unique_or_missing_evidence(
    paragraph: str,
    quote: str,
    expected_code: str,
) -> None:
    document = make_document(("heading", "Kampanya"), ("paragraph", paragraph))
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.1",
        document_id=document.id,
        facts=[
            ModelFactProposal(
                field=ModelFactField.CAMPAIGN_TYPE,
                quote=quote,
            )
        ],
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 0
    assert any(expected_code in issue for issue in merged.issues)


def test_legacy_offsets_that_contradict_deterministic_alignment_are_rejected() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Seçili harcamalarda indirim uygulanır."),
    )
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.0",
        document_id=document.id,
        facts=[
            ModelFactProposal(
                field=ModelFactField.CAMPAIGN_TYPE,
                raw_text="indirim",
                evidence=ModelEvidenceSpan(
                    block_id=document.blocks[1].id,
                    quote="indirim",
                    start_char=0,
                    end_char=7,
                ),
                campaign_type=CampaignType.DISCOUNT,
            )
        ],
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 0
    assert any("legacy_offsets_contradict_quote" in issue for issue in merged.issues)


def test_legacy_not_stated_outcome_adds_one_compact_issue() -> None:
    document = make_document(("heading", "Kampanya"), ("paragraph", "Genel açıklama."))
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.0",
        document_id=document.id,
        outcome=ModelExtractionOutcome.NOT_STATED,
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 0
    assert merged.issues.count("model_outcome:not_stated") == 1
    assert not any(issue.startswith("model_abstained:") for issue in merged.issues)


def test_rate_quote_requires_complete_deterministic_kind_and_period() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Kampanya oranı %1,99 olarak açıklanmıştır."),
    )
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.1",
        document_id=document.id,
        facts=[
            ModelFactProposal(
                field=ModelFactField.RATE,
                quote="oranı %1,99",
            )
        ],
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 0
    assert ModelFactField.RATE in merged.unresolved_fields
    assert any("rate_semantics_incomplete" in issue for issue in merged.issues)


def test_complete_model_rate_does_not_hide_an_incomplete_rule_rate() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Kampanya oranı %1,50 olarak açıklanmıştır."),
        ("paragraph", "Finansman kâr payı oranı aylık %1,99 olarak uygulanır."),
    )
    draft = extract_rules(document)
    assert ModelFactField.RATE in draft.unresolved_fields
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.1",
        document_id=document.id,
        facts=[
            ModelFactProposal(
                field=ModelFactField.RATE,
                quote="Finansman kâr payı oranı aylık %1,99",
            )
        ],
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert ModelFactField.RATE in merged.unresolved_fields
    assert "model_field_incomplete:rate" in merged.issues


def test_model_can_resolve_representable_term_from_ambiguous_source_sentence() -> None:
    document = make_document(
        ("heading", "Eğitim Kampanyası"),
        ("paragraph", "Ödemelerde 3 gün erteleme sonrasında 12 ay vade uygulanır."),
    )
    draft = extract_rules(document)
    assert not draft.terms
    assert ModelFactField.TERM in draft.unresolved_fields
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.1",
        document_id=document.id,
        facts=[ModelFactProposal(field=ModelFactField.TERM, quote="12 ay vade")],
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 1
    assert [term.value.maximum_months for term in merged.terms] == [12]
    assert ModelFactField.TERM not in merged.unresolved_fields


@pytest.mark.parametrize(
    "quotes",
    [
        ("indirim", "indirim"),
        ("indirim", "nakit iade"),
        ("nakit iade", "indirim"),
    ],
)
def test_multiple_valid_scalar_proposals_are_order_independent_conflicts(
    quotes: tuple[str, str],
) -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "Kampanya indirim ve nakit iade sunar."),
    )
    draft = extract_rules(document)
    assert draft.campaign_type is None
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.0",
        document_id=document.id,
        facts=[
            ModelFactProposal(field=ModelFactField.CAMPAIGN_TYPE, quote=quote) for quote in quotes
        ],
    )

    merged, accepted = merge_model_response(draft, response, document)

    assert accepted == 0
    assert merged.campaign_type is None
    assert ModelFactField.CAMPAIGN_TYPE in merged.unresolved_fields
    assert "model_scalar_conflict:campaign_type:multiple_accepted_proposals" in merged.issues


def test_list_valued_model_proposals_remain_independently_acceptable() -> None:
    document = make_document(
        ("heading", "Kampanya"),
        ("paragraph", "bireysel"),
        ("paragraph", "KOBİ"),
    )
    response = ModelExtractionResponse(
        schema_version="model-extraction/1.0",
        document_id=document.id,
        facts=[
            ModelFactProposal(field=ModelFactField.CUSTOMER_SEGMENT, quote="bireysel"),
            ModelFactProposal(field=ModelFactField.CUSTOMER_SEGMENT, quote="KOBİ"),
        ],
    )

    merged, accepted = merge_model_response(extract_rules(document), response, document)

    assert accepted == 2
    assert {segment.canonical_key for segment in merged.customer_segments} == {
        "bireysel",
        "kobi",
    }
