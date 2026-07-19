"""Evidence-first deterministic and hybrid campaign extraction."""

from katilim_analiz.extraction.candidate import (
    CandidateValidationError,
    build_candidate,
    validate_candidate,
)
from katilim_analiz.extraction.evidence import (
    EvidenceBindingError,
    TextSpan,
    bind_evidence,
    verify_document_blocks,
    verify_evidence_ref,
    verify_span,
)
from katilim_analiz.extraction.pipeline import (
    EXTRACTOR_VERSION,
    ExtractionOutcome,
    ExtractionPipeline,
    ExtractionResult,
    pipeline_from_settings,
)
from katilim_analiz.extraction.rules import extract_rules

__all__ = [
    "EXTRACTOR_VERSION",
    "CandidateValidationError",
    "EvidenceBindingError",
    "ExtractionOutcome",
    "ExtractionPipeline",
    "ExtractionResult",
    "TextSpan",
    "bind_evidence",
    "build_candidate",
    "extract_rules",
    "pipeline_from_settings",
    "validate_candidate",
    "verify_document_blocks",
    "verify_evidence_ref",
    "verify_span",
]
