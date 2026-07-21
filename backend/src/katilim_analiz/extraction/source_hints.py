"""Curated registry static-page hints for financing product sheets (issue #33).

The monitored-campaign-source registry lists a handful of stable financing
product pages per bank, each with an owner-curated label (``konut``, ``tasit``,
``ihtiyac``).  Those pages are product sheets, not campaigns: their body is
dominated by one financing product, but cross-product teasers and in-content
menus leak weak signals of other families (a "yatirim amacli" phrase in a
related-product card, a "Pratik Finansman Kart" sibling link), which makes the
one-hit-per-family classifier land on ``product_family_ambiguous``.  The pages
also never name a campaign type, so ``campaign_type`` stays unresolved and the
validation gate can never pass.

This module applies the registry label as a source-metadata hint:

- The hint may only BREAK ambiguity.  It selects the financing family when the
  page's own financing signal is at least as dominant (block-hit count) as any
  competing family signal; the resulting fact is INFERRED and bound to a real
  financing span from the page, with a ``registry_hint:...`` annotation naming
  the curated label as its origin.
- The hint never overrides a strong contradicting page signal.  When another
  family strictly dominates the page text, or the page resolved itself to a
  non-financing family, the classification is left untouched and the blocking
  issue ``registry_page_family_conflict`` is surfaced for review.
- A campaign type is only inferred when the sheet actually prices the product:
  a rule- or model-resolved financing profit rate makes the sheet a
  ``financing_rate`` offer for comparison purposes (ADR-004 stays honest — the
  sheet's substance is its rate table, and the validation matrix demands
  rate + term exactly like any financing-rate record).
"""

from __future__ import annotations

from dataclasses import replace

from katilim_analiz.contracts import CampaignType, CleanDocument, ProductFamily, RateKind
from katilim_analiz.extraction.draft import BoundFact, ExtractionDraft
from katilim_analiz.extraction.rules import product_family_block_spans
from katilim_analiz.llm.contracts import ModelFactField

#: Annotation prefix for facts asserted from the curated registry label. The
#: validation gate treats this prefix as non-blocking bookkeeping.
REGISTRY_HINT_ISSUE_PREFIX = "registry_hint:"
#: Blocking issue: the page's own dominant family signal contradicts the
#: curated registry label; the record must stay in review.
REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE = "registry_page_family_conflict"

_PRODUCT_FAMILY_AMBIGUOUS_ISSUE = "product_family_ambiguous"
_CAMPAIGN_TYPE_AMBIGUOUS_ISSUE = "campaign_type_ambiguous"

#: Registry labels the owner curates for financing product sheets. Any other
#: label asserts nothing.
_FINANCING_PAGE_LABELS = frozenset({"konut", "tasit", "ihtiyac"})


def _with_issue(draft: ExtractionDraft, issue: str) -> ExtractionDraft:
    if issue in draft.issues:
        return draft
    return replace(draft, issues=(*draft.issues, issue))


def apply_registry_static_page_hint(
    draft: ExtractionDraft,
    document: CleanDocument,
    label: str | None,
) -> ExtractionDraft:
    """Apply one curated static-page label to a rules/model draft, conservatively.

    The function is idempotent, so the pipeline may apply it both before the
    model call (to unblock classification early) and after the model merge
    (a financing profit rate the model resolved can complete the campaign
    type inference).
    """

    if label is None or label not in _FINANCING_PAGE_LABELS:
        return draft
    draft = _apply_family_hint(draft, document, label)
    return _apply_campaign_type_hint(draft, label)


def _apply_family_hint(
    draft: ExtractionDraft,
    document: CleanDocument,
    label: str,
) -> ExtractionDraft:
    if draft.product_family is not None:
        if draft.product_family.value is not ProductFamily.FINANCING:
            # The page resolved itself unambiguously to another family; the
            # curated label never overrides the page's own dominant signal.
            return _with_issue(draft, REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE)
        return draft
    if _PRODUCT_FAMILY_AMBIGUOUS_ISSUE not in draft.issues:
        # No family signal at all: the registry label has no page span to bind
        # evidence to, so nothing can be asserted.
        return draft
    spans = product_family_block_spans(document)
    financing_spans = spans.get(ProductFamily.FINANCING, ())
    if not financing_spans:
        return draft
    if any(
        len(family_spans) > len(financing_spans)
        for family, family_spans in spans.items()
        if family is not ProductFamily.FINANCING
    ):
        # A competing family dominates the page text: strong contradiction.
        return _with_issue(draft, REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE)
    issues = tuple(issue for issue in draft.issues if issue != _PRODUCT_FAMILY_AMBIGUOUS_ISSUE)
    draft = replace(
        draft,
        product_family=BoundFact(ProductFamily.FINANCING, financing_spans[0], inferred=True),
        issues=issues,
        unresolved_fields=draft.unresolved_fields - {ModelFactField.PRODUCT_FAMILY},
    )
    return _with_issue(
        draft,
        f"{REGISTRY_HINT_ISSUE_PREFIX}product_family:financing:{label}",
    )


def _apply_campaign_type_hint(draft: ExtractionDraft, label: str) -> ExtractionDraft:
    if draft.campaign_type is not None or _CAMPAIGN_TYPE_AMBIGUOUS_ISSUE in draft.issues:
        return draft
    family = draft.product_family
    if family is None or family.value is not ProductFamily.FINANCING:
        return draft
    if not any(rate.value.kind is RateKind.FINANCING_PROFIT_RATE for rate in draft.rates):
        # A product sheet without a resolved financing profit rate states no
        # offer; the type question stays open.
        return draft
    draft = replace(
        draft,
        campaign_type=BoundFact(CampaignType.FINANCING_RATE, family.span, inferred=True),
        unresolved_fields=draft.unresolved_fields - {ModelFactField.CAMPAIGN_TYPE},
    )
    return _with_issue(
        draft,
        f"{REGISTRY_HINT_ISSUE_PREFIX}campaign_type:financing_rate:{label}",
    )


__all__ = [
    "REGISTRY_HINT_ISSUE_PREFIX",
    "REGISTRY_PAGE_FAMILY_CONFLICT_ISSUE",
    "apply_registry_static_page_hint",
]
