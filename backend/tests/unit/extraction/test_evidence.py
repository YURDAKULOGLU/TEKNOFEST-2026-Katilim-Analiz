from __future__ import annotations

import hashlib

import pytest

from katilim_analiz.contracts import CleanDocument, EvidenceStatus
from katilim_analiz.extraction import (
    EvidenceBindingError,
    TextSpan,
    bind_evidence,
    verify_document_blocks,
    verify_evidence_ref,
    verify_span,
)


def test_exact_unicode_span_is_bound_and_reverified(campaign_document: CleanDocument) -> None:
    block = campaign_document.blocks[1]
    quote = "%1,89"
    start = block.text.index(quote)
    span = TextSpan(block.id, quote, start, start + len(quote))

    reference = bind_evidence(
        campaign_document,
        "/data/rates/0",
        span,
        status=EvidenceStatus.STATED,
    )

    assert reference.evidence_sha256 == hashlib.sha256(quote.encode()).hexdigest()
    assert reference.id.startswith("evidence:")
    verify_evidence_ref(campaign_document, reference)


def test_quote_offset_mismatch_is_rejected(campaign_document: CleanDocument) -> None:
    block = campaign_document.blocks[1]
    quote = "%1,89"
    start = block.text.index(quote)
    shifted = TextSpan(block.id, quote, start - 1, start - 1 + len(quote))

    with pytest.raises(EvidenceBindingError, match="does not equal") as error:
        verify_span(campaign_document, shifted)

    assert error.value.code == "evidence_quote_mismatch"


def test_source_block_hash_tampering_is_rejected(campaign_document: CleanDocument) -> None:
    original = campaign_document.blocks[1]
    tampered = original.model_copy(update={"text": f"{original.text} değiştirilmiş"})
    document = campaign_document.model_copy(
        update={"blocks": [campaign_document.blocks[0], tampered, *campaign_document.blocks[2:]]}
    )

    with pytest.raises(EvidenceBindingError) as error:
        verify_document_blocks(document)

    assert error.value.code == "source_block_hash_mismatch"


@pytest.mark.parametrize("field", ["evidence_sha256", "id"])
def test_persisted_evidence_hash_or_id_tampering_is_rejected(
    campaign_document: CleanDocument,
    field: str,
) -> None:
    block = campaign_document.blocks[0]
    span = TextSpan(block.id, block.text, 0, len(block.text))
    reference = bind_evidence(
        campaign_document,
        "/data/title",
        span,
        status=EvidenceStatus.STATED,
    )
    replacement = "0" * 64 if field == "evidence_sha256" else "evidence:tampered"
    tampered = reference.model_copy(update={field: replacement})

    with pytest.raises(EvidenceBindingError):
        verify_evidence_ref(campaign_document, tampered)
