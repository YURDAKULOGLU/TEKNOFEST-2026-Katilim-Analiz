"""Non-persistent, evidence-bound extraction preview for operator-supplied text."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from katilim_analiz.application.ports import PreviewExtractionPort
from katilim_analiz.contracts import (
    CleanDocument,
    ExtractionCandidate,
    ExtractionPreviewRequest,
    ExtractionPreviewResponse,
    SourceBlock,
)
from katilim_analiz.ingestion.registry import BankRegistry

_PREVIEW_ORIGIN = "https://preview.invalid"
_CLEANER_VERSION = "text-preview/1.0"


class PreviewBankNotAllowedError(ValueError):
    code = "invalid_preview_bank"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_preview_document(
    request: ExtractionPreviewRequest,
    *,
    cleaned_at: datetime,
) -> tuple[CleanDocument, str]:
    input_sha256 = _sha256(request.text)
    blocks: list[SourceBlock] = []
    for line_number, raw_line in enumerate(request.text.splitlines(), start=1):
        text = raw_line.strip()
        if not text:
            continue
        ordinal = len(blocks)
        text_sha256 = _sha256(text)
        blocks.append(
            SourceBlock(
                id=f"preview-block:{ordinal}:{text_sha256[:24]}",
                ordinal=ordinal,
                kind="heading" if ordinal == 0 else "paragraph",
                text=text,
                locator=f"text-input:line-{line_number}",
                text_sha256=text_sha256,
            )
        )

    # Request validation guarantees at least one non-empty line.
    assert blocks
    clean_sha256 = _sha256("\n".join(block.text for block in blocks))
    document = CleanDocument(
        id=f"preview-document:{input_sha256}",
        fetch_artifact_id=f"preview-input:{input_sha256}",
        bank_id=request.bank_id,
        canonical_url=f"{_PREVIEW_ORIGIN}/{input_sha256}",
        title=blocks[0].text,
        cleaned_at=cleaned_at,
        cleaner_version=_CLEANER_VERSION,
        clean_sha256=clean_sha256,
        blocks=blocks,
    )
    return document, input_sha256


def _uses_exact_preview_heading(
    candidate: ExtractionCandidate,
    document: CleanDocument,
) -> bool:
    heading = document.blocks[0]
    if candidate.data.title != heading.text:
        return False
    return any(
        evidence.field_pointer == "/data/title"
        and evidence.block_id == heading.id
        and evidence.quote == heading.text
        and evidence.start_char == 0
        and evidence.end_char == len(heading.text)
        for evidence in candidate.evidence
    )


class ExtractionPreviewService:
    """Create review-only candidates without collection, storage, or query access."""

    def __init__(
        self,
        *,
        registry: BankRegistry,
        extractor: PreviewExtractionPort,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._registry = registry
        self._extractor = extractor
        self._clock = clock

    async def preview(self, request: ExtractionPreviewRequest) -> ExtractionPreviewResponse:
        try:
            self._registry.bank(request.bank_id)
        except KeyError as exc:
            raise PreviewBankNotAllowedError(
                "bank_id is not present in the active BDDK registry"
            ) from exc

        cleaned_at = self._clock()
        if cleaned_at.tzinfo is None or cleaned_at.utcoffset() is None:
            raise ValueError("preview clock must be timezone-aware")
        document, input_sha256 = _build_preview_document(request, cleaned_at=cleaned_at)
        extracted = await self._extractor.extract(document)
        candidate = extracted.candidate
        if candidate is not None and (
            candidate.source_document_id != document.id or candidate.data.bank_id != request.bank_id
        ):
            raise RuntimeError("preview extractor returned a cross-document candidate")

        issues = list(extracted.issues)
        accepted_model_facts = extracted.accepted_model_facts
        if candidate is not None and not _uses_exact_preview_heading(candidate, document):
            candidate = None
            accepted_model_facts = 0
            issues.insert(0, "preview_heading_evidence_required")

        bounded_issues = list(dict.fromkeys(issues))[:100]

        return ExtractionPreviewResponse(
            status="abstained" if candidate is None else "needs_review",
            input_sha256=input_sha256,
            candidate=candidate,
            issues=bounded_issues,
            model_attempted=extracted.model_attempted,
            accepted_model_facts=accepted_model_facts,
        )


__all__ = [
    "ExtractionPreviewService",
    "PreviewBankNotAllowedError",
]
