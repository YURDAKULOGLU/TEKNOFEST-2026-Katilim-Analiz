from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError

from katilim_analiz.contracts import CampaignType, ProductFamily
from katilim_analiz.llm import (
    ModelEvidenceSpan,
    ModelFactField,
    ModelFactProposal,
    model_extraction_output_schema,
)


def _span() -> ModelEvidenceSpan:
    return ModelEvidenceSpan(
        block_id="block-1",
        quote="finansman",
        start_char=0,
        end_char=9,
    )


def test_model_fact_contract_rejects_extra_properties() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelFactProposal.model_validate(
            {
                "field": "product_family",
                "quote": "finansman",
                "sql": "DROP TABLE campaigns",
            }
        )


def test_model_fact_contract_rejects_evidence_laundering_raw_text() -> None:
    with pytest.raises(ValidationError, match="exact evidence quote"):
        ModelFactProposal(
            field=ModelFactField.PRODUCT_FAMILY,
            raw_text="kart",
            evidence=_span(),
            product_family=ProductFamily.FINANCING,
        )


def test_semantic_hints_cannot_cross_field_boundaries() -> None:
    with pytest.raises(ValidationError, match="not permitted"):
        ModelFactProposal(
            field=ModelFactField.PRODUCT_FAMILY,
            raw_text="finansman",
            evidence=_span(),
            product_family=ProductFamily.FINANCING,
            campaign_type=CampaignType.FINANCING_RATE,
        )


def test_live_request_schema_is_compact_quote_only_and_pins_field() -> None:
    schema = model_extraction_output_schema(frozenset({ModelFactField.PRODUCT_FAMILY}))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    proposal: dict[str, Any] = {
        "facts": [
            {
                "field": "product_family",
                "quote": "finansman",
            }
        ],
    }

    assert list(validator.iter_errors(proposal)) == []
    fact_schema = schema["properties"]["facts"]["items"]
    assert set(fact_schema["required"]) == {"field", "quote"}
    assert "raw_text" not in fact_schema["properties"]
    assert "evidence" not in fact_schema["properties"]
    assert "block_id" not in fact_schema["properties"]
    assert "abstentions" not in schema["properties"]
    assert "outcome" not in schema["properties"]
    assert "schema_version" not in schema["properties"]
    assert "document_id" not in schema["properties"]
    assert schema["properties"]["facts"]["maxItems"] == 1
    assert fact_schema["properties"]["quote"]["maxLength"] == 120
    assert list(validator.iter_errors({**proposal, "facts": []})) == []

    without_quote = {
        **proposal,
        "facts": [{key: value for key, value in proposal["facts"][0].items() if key != "quote"}],
    }
    unrequested_field = {
        **proposal,
        "facts": [{**proposal["facts"][0], "field": "campaign_type"}],
    }
    assert list(validator.iter_errors(without_quote))
    assert list(validator.iter_errors(unrequested_field))
    for forbidden_property in ("schema_version", "document_id", "outcome"):
        assert list(validator.iter_errors({**proposal, forbidden_property: "forbidden"}))


def test_quote_only_proposal_does_not_require_offsets_raw_text_or_hints() -> None:
    proposal = ModelFactProposal(
        field=ModelFactField.PRODUCT_FAMILY,
        quote="finansman",
    )

    assert proposal.proposed_quote == "finansman"
    assert proposal.proposed_block_id is None


def test_top_level_and_legacy_block_identity_cannot_conflict_without_top_level_quote() -> None:
    with pytest.raises(ValidationError, match="block_id contradicts"):
        ModelFactProposal(
            field=ModelFactField.PRODUCT_FAMILY,
            block_id="block-top",
            evidence=ModelEvidenceSpan(block_id="block-legacy", quote="finansman"),
        )
