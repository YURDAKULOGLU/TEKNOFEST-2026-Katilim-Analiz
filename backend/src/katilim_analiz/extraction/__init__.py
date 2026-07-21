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
from katilim_analiz.extraction.validation_policy import (
    FieldRequirement,
    ValidationDecision,
    decide_record_status,
    evaluate_validation,
    required_fields,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "CandidateValidationError",
    "EvidenceBindingError",
    "ExtractionOutcome",
    "ExtractionPipeline",
    "ExtractionResult",
    "FieldRequirement",
    "TextSpan",
    "ValidationDecision",
    "bind_evidence",
    "build_candidate",
    "decide_record_status",
    "evaluate_validation",
    "extract_rules",
    "pipeline_from_settings",
    "required_fields",
    "validate_candidate",
    "verify_document_blocks",
    "verify_evidence_ref",
    "verify_span",
]
