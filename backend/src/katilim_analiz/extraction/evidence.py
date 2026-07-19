"""Evidence span verification and deterministic reference construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from katilim_analiz.contracts import CleanDocument, EvidenceRef, EvidenceStatus, SourceBlock


class EvidenceBindingError(ValueError):
    """A proposed or stored evidence span does not resolve exactly."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class TextSpan:
    block_id: str
    quote: str
    start_char: int
    end_char: int

    def __post_init__(self) -> None:
        if not self.quote or len(self.quote) > 1_000:
            raise ValueError("evidence quote must contain 1..1000 characters")
        if self.start_char < 0 or self.end_char <= self.start_char:
            raise ValueError("invalid evidence offsets")
        if self.end_char - self.start_char != len(self.quote):
            raise ValueError("evidence offsets must match quote length")


def _block_map(document: CleanDocument) -> dict[str, SourceBlock]:
    return {block.id: block for block in document.blocks}


def verify_document_blocks(document: CleanDocument) -> None:
    """Fail if any in-memory block changed after the cleaning contract was built."""

    for block in document.blocks:
        actual = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        if actual != block.text_sha256:
            raise EvidenceBindingError(
                "source_block_hash_mismatch",
                f"source block hash mismatch: {block.id}",
            )


def verify_span(document: CleanDocument, span: TextSpan) -> SourceBlock:
    verify_document_blocks(document)
    block = _block_map(document).get(span.block_id)
    if block is None:
        raise EvidenceBindingError("source_block_missing", "evidence block is not in document")
    if span.end_char > len(block.text):
        raise EvidenceBindingError("evidence_offset_out_of_bounds", "evidence span exceeds block")
    if block.text[span.start_char : span.end_char] != span.quote:
        raise EvidenceBindingError(
            "evidence_quote_mismatch",
            "evidence quote does not equal the source block slice",
        )
    return block


def bind_evidence(
    document: CleanDocument,
    field_pointer: str,
    span: TextSpan,
    *,
    status: EvidenceStatus,
) -> EvidenceRef:
    """Turn a verified source slice into a hash-addressed canonical reference."""

    verify_span(document, span)
    evidence_sha256 = hashlib.sha256(span.quote.encode("utf-8")).hexdigest()
    identity = {
        "field_pointer": field_pointer,
        "source_document_id": document.id,
        "block_id": span.block_id,
        "quote": span.quote,
        "start_char": span.start_char,
        "end_char": span.end_char,
        "evidence_sha256": evidence_sha256,
        "status": status.value,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EvidenceRef(id=f"evidence:{digest}", **identity)


def verify_evidence_ref(document: CleanDocument, evidence: EvidenceRef) -> None:
    """Re-resolve a persisted reference, including its quote hash and deterministic ID."""

    if evidence.source_document_id != document.id:
        raise EvidenceBindingError(
            "evidence_document_mismatch",
            "evidence belongs to a different source document",
        )
    span = TextSpan(
        block_id=evidence.block_id,
        quote=evidence.quote,
        start_char=evidence.start_char,
        end_char=evidence.end_char,
    )
    expected = bind_evidence(
        document,
        evidence.field_pointer,
        span,
        status=evidence.status,
    )
    if evidence.evidence_sha256 != expected.evidence_sha256:
        raise EvidenceBindingError("evidence_hash_mismatch", "evidence quote hash is invalid")
    if evidence.id != expected.id:
        raise EvidenceBindingError("evidence_id_mismatch", "evidence ID is not deterministic")
