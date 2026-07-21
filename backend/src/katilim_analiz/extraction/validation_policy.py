"""Product-family-aware machine-validation gate for extraction candidates.

Issue #3: the previous gate required every one of the 13 extractable fields to
resolve before a record could become ``validated``, which no real campaign can
satisfy (a cashback card campaign has no financing amount, term, or rate by
nature). This module is the single source of truth for the replacement policy:

    validated = (all fields REQUIRED for the record's product family and
                 campaign type are resolved)
                AND (no blocking issue is present)

Design rules:

- Fields that are genuinely optional for a product (validity window, customer
  segment, sales channel, financing ceiling, ...) never block validation.
- ADR-002 stays intact: a missing field stays missing; it is never coerced to
  zero, and the record simply remains ``needs_review`` when it is required.
- Verbatim grounding is untouched: this policy only reads the issue list that
  the deterministic evidence validators produced; it never relaxes them.
- Blocking issues are ambiguity on a required classification, prompt-injection
  quarantine, rejected (ungrounded) model facts, and any unknown issue code.
  Model bookkeeping annotations that merely explain why an optional field
  stayed unresolved are not blocking; that includes the model being skipped
  or falling back to rules — a required field the model would have filled is
  still blocked by its own ``unresolved:<field>`` marker (issue #22).
- Machine validation is separate from human validation: ``human_verified``
  bookkeeping is intentionally not derived from this status.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from katilim_analiz.contracts import CampaignType, ProductFamily, RecordStatus
from katilim_analiz.llm.contracts import ModelFactField

UNRESOLVED_ISSUE_PREFIX = "unresolved:"

# Every machine-validated record must at least know what it is about.
BASE_REQUIRED_FIELDS: frozenset[ModelFactField] = frozenset(
    {
        ModelFactField.TITLE,
        ModelFactField.PRODUCT_FAMILY,
        ModelFactField.CAMPAIGN_TYPE,
    }
)


@dataclass(frozen=True, slots=True)
class FieldRequirement:
    """Declarative requirement: every ``all_of`` and at least one ``any_of``."""

    all_of: frozenset[ModelFactField] = frozenset()
    any_of: frozenset[ModelFactField] = frozenset()


# A financing offer is meaningless without its rate and term; a participation
# account campaign is about its profit-share rate. Card and investment
# campaigns carry their substance in the campaign type instead.
FAMILY_REQUIRED_FIELDS: Mapping[ProductFamily, FieldRequirement | None] = {
    ProductFamily.FINANCING: FieldRequirement(
        all_of=frozenset({ModelFactField.RATE, ModelFactField.TERM})
    ),
    ProductFamily.CARD: FieldRequirement(),
    ProductFamily.PARTICIPATION_ACCOUNT: FieldRequirement(all_of=frozenset({ModelFactField.RATE})),
    ProductFamily.INVESTMENT: FieldRequirement(),
    # An unclassifiable product can never be machine-validated.
    ProductFamily.OTHER: None,
    ProductFamily.UNKNOWN: None,
}

# The campaign type names the fact that makes the campaign an offer at all:
# a rate campaign needs its rate, a fee campaign its fee status, a reward
# campaign its reward. A welcome campaign may lead with either a reward or a
# fee waiver, so it needs at least one of the two.
CAMPAIGN_TYPE_REQUIRED_FIELDS: Mapping[CampaignType, FieldRequirement | None] = {
    CampaignType.FINANCING_RATE: FieldRequirement(all_of=frozenset({ModelFactField.RATE})),
    CampaignType.PROFIT_SHARE: FieldRequirement(all_of=frozenset({ModelFactField.RATE})),
    CampaignType.INSTALLMENT: FieldRequirement(all_of=frozenset({ModelFactField.TERM})),
    CampaignType.CASHBACK: FieldRequirement(all_of=frozenset({ModelFactField.REWARD})),
    CampaignType.DISCOUNT: FieldRequirement(all_of=frozenset({ModelFactField.REWARD})),
    CampaignType.POINTS: FieldRequirement(all_of=frozenset({ModelFactField.REWARD})),
    CampaignType.FEE_WAIVER: FieldRequirement(all_of=frozenset({ModelFactField.FEE})),
    CampaignType.WELCOME: FieldRequirement(
        any_of=frozenset({ModelFactField.REWARD, ModelFactField.FEE})
    ),
    CampaignType.OTHER: None,
    CampaignType.UNKNOWN: None,
}

# Ambiguity issues are field-scoped: they block only when the ambiguous field
# is required for this record; ambiguity on an optional field leaves that
# field unknown without disqualifying the rest of the evidence-backed record.
_FIELD_AMBIGUITY_ISSUES: Mapping[str, ModelFactField] = {
    "product_family_ambiguous": ModelFactField.PRODUCT_FAMILY,
    "campaign_type_ambiguous": ModelFactField.CAMPAIGN_TYPE,
    "sales_channel_ambiguous": ModelFactField.SALES_CHANNEL,
}

# Model bookkeeping that only records why the model contributed nothing for a
# field. The affected fields stay unresolved and are judged by the matrix; the
# rule-extracted facts in the record remain verbatim-grounded regardless.
#
# ``model_skipped:*`` (model disabled/unavailable/skipped) and
# ``*:rules_fallback`` (model inference failed, rules kept) are bookkeeping
# too (issue #22): they can only leave fields unresolved, never invent facts.
# If the skipped model left a REQUIRED field unresolved, the record is still
# blocked by its ``unresolved:<field>`` marker — so treating these codes as
# non-blocking only unblocks records whose required fields were fully
# rule-resolved.
_NON_BLOCKING_ISSUE_PREFIXES: tuple[str, ...] = (
    "model_abstained:",
    "model_outcome:",
    "model_field_incomplete:",
    "model_skipped:",
    # ``registry_hint:*`` (issue #33) annotates a classification that was
    # inferred from the curated static-page registry label; it explains a
    # resolved fact's provenance and never hides a missing one. Its blocking
    # counterpart, ``registry_page_family_conflict``, carries no prefix and is
    # therefore treated as an unknown (blocking) issue by design.
    "registry_hint:",
)

_NON_BLOCKING_ISSUE_SUFFIXES: tuple[str, ...] = (":rules_fallback",)


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    """Explainable gate outcome: the status plus every blocking reason."""

    status: RecordStatus
    missing_required_fields: frozenset[ModelFactField] = frozenset()
    blocking_issues: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default=())


def required_fields(
    product_family: ProductFamily,
    campaign_type: CampaignType,
) -> FieldRequirement | None:
    """Combine the family and campaign-type matrices; ``None`` = unvalidatable."""

    family_requirement = FAMILY_REQUIRED_FIELDS[product_family]
    type_requirement = CAMPAIGN_TYPE_REQUIRED_FIELDS[campaign_type]
    if family_requirement is None or type_requirement is None:
        return None
    return FieldRequirement(
        all_of=BASE_REQUIRED_FIELDS | family_requirement.all_of | type_requirement.all_of,
        any_of=family_requirement.any_of | type_requirement.any_of,
    )


def _unresolved_field(issue: str) -> ModelFactField | None:
    name = issue.removeprefix(UNRESOLVED_ISSUE_PREFIX)
    try:
        return ModelFactField(name)
    except ValueError:
        return None


def evaluate_validation(
    product_family: ProductFamily,
    campaign_type: CampaignType,
    issues: Sequence[str],
) -> ValidationDecision:
    """Decide machine validation from the requirement matrix and issue list."""

    requirement = required_fields(product_family, campaign_type)
    if requirement is None:
        return ValidationDecision(
            status=RecordStatus.NEEDS_REVIEW,
            reasons=("classification_not_validatable",),
        )

    unresolved: set[ModelFactField] = set()
    blocking: list[str] = []
    for issue in issues:
        if issue.startswith(UNRESOLVED_ISSUE_PREFIX):
            unresolved_field = _unresolved_field(issue)
            if unresolved_field is None:
                # An unknown unresolved marker is treated conservatively.
                blocking.append(issue)
            else:
                unresolved.add(unresolved_field)
            continue
        if issue.startswith(_NON_BLOCKING_ISSUE_PREFIXES):
            continue
        if issue.endswith(_NON_BLOCKING_ISSUE_SUFFIXES):
            continue
        ambiguous_field = _FIELD_AMBIGUITY_ISSUES.get(issue)
        if ambiguous_field is not None and ambiguous_field not in (
            requirement.all_of | requirement.any_of
        ):
            continue
        blocking.append(issue)

    missing = frozenset(unresolved & requirement.all_of)
    reasons: list[str] = []
    if missing:
        reasons.append("required_field_unresolved")
    if requirement.any_of and requirement.any_of <= unresolved:
        missing = missing | requirement.any_of
        reasons.append("required_field_group_unresolved")
    if blocking:
        reasons.append("blocking_issue_present")

    if missing or blocking:
        return ValidationDecision(
            status=RecordStatus.NEEDS_REVIEW,
            missing_required_fields=missing,
            blocking_issues=tuple(blocking),
            reasons=tuple(reasons),
        )
    return ValidationDecision(status=RecordStatus.VALIDATED)


def decide_record_status(
    product_family: ProductFamily,
    campaign_type: CampaignType,
    issues: Sequence[str],
) -> RecordStatus:
    """Return the machine status for one extraction candidate."""

    return evaluate_validation(product_family, campaign_type, issues).status


__all__ = [
    "BASE_REQUIRED_FIELDS",
    "CAMPAIGN_TYPE_REQUIRED_FIELDS",
    "FAMILY_REQUIRED_FIELDS",
    "UNRESOLVED_ISSUE_PREFIX",
    "FieldRequirement",
    "ValidationDecision",
    "decide_record_status",
    "evaluate_validation",
    "required_fields",
]
