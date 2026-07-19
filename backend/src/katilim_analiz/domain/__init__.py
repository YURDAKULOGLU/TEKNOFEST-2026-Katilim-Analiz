"""Pure domain rules for Turkish normalization and campaign comparison."""

from katilim_analiz.domain.comparison import (
    RULESET_VERSION,
    ComparisonOutcome,
    ComparisonReport,
    compare_campaigns,
    compatibility_reasons,
)
from katilim_analiz.domain.normalization import (
    NormalizationResult,
    NormalizationStatus,
    canonicalize_turkish_text,
    normalize_date,
    normalize_money,
    normalize_rate,
    normalize_term,
    normalize_terms,
    normalize_validity,
)

__all__ = [
    "RULESET_VERSION",
    "ComparisonOutcome",
    "ComparisonReport",
    "NormalizationResult",
    "NormalizationStatus",
    "canonicalize_turkish_text",
    "compare_campaigns",
    "compatibility_reasons",
    "normalize_date",
    "normalize_money",
    "normalize_rate",
    "normalize_term",
    "normalize_terms",
    "normalize_validity",
]
