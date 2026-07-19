from __future__ import annotations

import hashlib

from evals.evidence import verify_evidence_binding


def _binding(quote: str, start: int, end: int) -> dict[str, object]:
    return {
        "field_pointer": "/data/rewards/0",
        "block_id": "block-1",
        "quote": quote,
        "start_char": start,
        "end_char": end,
        "evidence_sha256": hashlib.sha256(quote.encode()).hexdigest(),
    }


def test_evidence_binding_requires_exact_quote_offsets_and_hash() -> None:
    text = "İlk alışverişe 500 TL nakit iade verilir."
    quote = "500 TL nakit iade"
    start = text.index(quote)

    result = verify_evidence_binding(
        "/data/rewards/0",
        _binding(quote, start, start + len(quote)),
        {"block-1": text},
    )

    assert result.valid is True
    assert result.reason_code == "evidence_valid"


def test_evidence_binding_rejects_shifted_span_even_when_quote_exists_elsewhere() -> (
    None
):
    text = "500 TL nakit iade; tekrar 500 TL nakit iade."
    quote = "500 TL nakit iade"

    result = verify_evidence_binding(
        "/data/rewards/0",
        _binding(quote, 1, 1 + len(quote)),
        {"block-1": text},
    )

    assert result.valid is False
    assert result.reason_code == "evidence_quote_mismatch"
