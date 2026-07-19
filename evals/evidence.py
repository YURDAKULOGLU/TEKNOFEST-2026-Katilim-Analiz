"""Independent evidence-span verification for evaluation results."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    valid: bool
    reason_code: str


def verify_evidence_binding(
    field_pointer: str,
    evidence: Mapping[str, object],
    blocks: Mapping[str, str],
) -> EvidenceCheck:
    """Verify pointer, block, exact UTF-8 quote hash, and Python character offsets."""

    if evidence.get("field_pointer") != field_pointer:
        return EvidenceCheck(False, "evidence_pointer_mismatch")
    block_id = evidence.get("block_id")
    if not isinstance(block_id, str) or block_id not in blocks:
        return EvidenceCheck(False, "evidence_block_missing")
    quote = evidence.get("quote")
    start = evidence.get("start_char")
    end = evidence.get("end_char")
    digest = evidence.get("evidence_sha256")
    if (
        not isinstance(quote, str)
        or not quote
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end - start != len(quote)
    ):
        return EvidenceCheck(False, "evidence_span_invalid")
    if hashlib.sha256(quote.encode("utf-8")).hexdigest() != digest:
        return EvidenceCheck(False, "evidence_hash_mismatch")
    if blocks[block_id][start:end] != quote:
        return EvidenceCheck(False, "evidence_quote_mismatch")
    return EvidenceCheck(True, "evidence_valid")
