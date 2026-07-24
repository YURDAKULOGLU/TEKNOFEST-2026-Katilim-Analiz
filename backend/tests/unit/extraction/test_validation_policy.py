"""Family-aware machine-validation gate (issue #3)."""

from __future__ import annotations

import pytest

from katilim_analiz.contracts import CampaignType, ProductFamily, RecordStatus
from katilim_analiz.extraction.validation_policy import (
    BASE_REQUIRED_FIELDS,
    CAMPAIGN_TYPE_REQUIRED_FIELDS,
    FAMILY_REQUIRED_FIELDS,
    decide_record_status,
    evaluate_validation,
    required_fields,
)
from katilim_analiz.llm.contracts import ModelFactField

# Optional context that real campaign pages routinely leave unstated. Under the
# old 13-field conjunction each of these alone forced needs_review forever.
_OPTIONAL_UNRESOLVED = [
    "unresolved:validity",
    "unresolved:customer_segment",
    "unresolved:eligibility_condition",
    "unresolved:sales_channel",
    "unresolved:new_customer_only",
]


def test_matrix_covers_every_family_and_campaign_type() -> None:
    assert set(FAMILY_REQUIRED_FIELDS) == set(ProductFamily)
    assert set(CAMPAIGN_TYPE_REQUIRED_FIELDS) == set(CampaignType)


def test_required_fields_combine_base_family_and_type() -> None:
    requirement = required_fields(ProductFamily.FINANCING, CampaignType.FINANCING_RATE)

    assert requirement is not None
    assert requirement.all_of == BASE_REQUIRED_FIELDS | {
        ModelFactField.RATE,
        ModelFactField.TERM,
    }
    assert requirement.any_of == frozenset()
    assert required_fields(ProductFamily.UNKNOWN, CampaignType.CASHBACK) is None
    assert required_fields(ProductFamily.CARD, CampaignType.UNKNOWN) is None


@pytest.mark.parametrize(
    ("bank", "family", "campaign_type", "issues"),
    [
        # Kuveyt Turk: housing financing campaign - rate + term resolved;
        # amount, fee, reward, and validity are not stated on the page.
        (
            "kuveyt-turk",
            ProductFamily.FINANCING,
            CampaignType.FINANCING_RATE,
            [
                "unresolved:financing_amount",
                "unresolved:fee",
                "unresolved:reward",
                *_OPTIONAL_UNRESOLVED,
            ],
        ),
        # Albaraka: fee-waiver financing campaign - fee status resolved;
        # a reward is absent by the product's nature.
        (
            "albaraka",
            ProductFamily.FINANCING,
            CampaignType.FEE_WAIVER,
            [
                "unresolved:financing_amount",
                "unresolved:reward",
                *_OPTIONAL_UNRESOLVED,
            ],
        ),
        # Turkiye Finans: card cashback campaign - reward resolved; rate,
        # term, and financing amount are absent by a card campaign's nature.
        (
            "turkiye-finans",
            ProductFamily.CARD,
            CampaignType.CASHBACK,
            [
                "unresolved:rate",
                "unresolved:term",
                "unresolved:financing_amount",
                "unresolved:fee",
                *_OPTIONAL_UNRESOLVED,
            ],
        ),
    ],
)
def test_sartname_scenario_records_validate_under_family_gate(
    bank: str,
    family: ProductFamily,
    campaign_type: CampaignType,
    issues: list[str],
) -> None:
    decision = evaluate_validation(family, campaign_type, issues)

    assert decision.status is RecordStatus.VALIDATED, (bank, decision)
    assert decision.missing_required_fields == frozenset()
    assert decision.blocking_issues == ()


def test_record_with_no_issues_validates() -> None:
    assert (
        decide_record_status(ProductFamily.FINANCING, CampaignType.FINANCING_RATE, [])
        is RecordStatus.VALIDATED
    )


@pytest.mark.parametrize(
    ("family", "campaign_type", "issues"),
    [
        # Empty page: nothing but the heading resolved.
        (
            ProductFamily.UNKNOWN,
            CampaignType.UNKNOWN,
            [
                "unresolved:product_family",
                "unresolved:campaign_type",
                "unresolved:rate",
                "unresolved:financing_amount",
                "unresolved:term",
                "unresolved:fee",
                "unresolved:reward",
                *_OPTIONAL_UNRESOLVED,
            ],
        ),
        # Financing campaign without its rate.
        (
            ProductFamily.FINANCING,
            CampaignType.FINANCING_RATE,
            ["unresolved:rate", *_OPTIONAL_UNRESOLVED],
        ),
        # Financing campaign without its term.
        (
            ProductFamily.FINANCING,
            CampaignType.FINANCING_RATE,
            ["unresolved:term"],
        ),
        # Fee campaign without a fee status.
        (ProductFamily.CARD, CampaignType.FEE_WAIVER, ["unresolved:fee"]),
        # Reward campaign without a reward.
        (ProductFamily.CARD, CampaignType.CASHBACK, ["unresolved:reward"]),
        # Welcome campaign with neither a reward nor a fee waiver.
        (
            ProductFamily.CARD,
            CampaignType.WELCOME,
            ["unresolved:reward", "unresolved:fee"],
        ),
        # Participation account campaign without a profit-share rate.
        (
            ProductFamily.PARTICIPATION_ACCOUNT,
            CampaignType.PROFIT_SHARE,
            ["unresolved:rate"],
        ),
        # Unclassifiable products are never machine-validated.
        (ProductFamily.OTHER, CampaignType.DISCOUNT, []),
        (ProductFamily.CARD, CampaignType.OTHER, []),
    ],
)
def test_missing_family_critical_fields_do_not_validate(
    family: ProductFamily,
    campaign_type: CampaignType,
    issues: list[str],
) -> None:
    assert decide_record_status(family, campaign_type, issues) is RecordStatus.NEEDS_REVIEW


def test_welcome_campaign_validates_with_either_reward_or_fee() -> None:
    with_fee_only = decide_record_status(
        ProductFamily.CARD,
        CampaignType.WELCOME,
        ["unresolved:reward"],
    )
    with_reward_only = decide_record_status(
        ProductFamily.CARD,
        CampaignType.WELCOME,
        ["unresolved:fee"],
    )

    assert with_fee_only is RecordStatus.VALIDATED
    assert with_reward_only is RecordStatus.VALIDATED


@pytest.mark.parametrize(
    "blocking_issue",
    [
        "quarantined_prompt_injection_block:block-7",
        "model_fact_rejected:rate:raw_evidence_mismatch",
        "issues_truncated",
        "some_future_issue_code",
        "unresolved:not_a_known_field",
    ],
)
def test_blocking_issues_prevent_validation_even_with_all_fields(blocking_issue: str) -> None:
    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [blocking_issue],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == (blocking_issue,)


def test_model_bookkeeping_annotations_do_not_block() -> None:
    status = decide_record_status(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [
            "model_abstained:kaynakta yer almıyor",
            "model_outcome:not_stated",
            "model_field_incomplete:rate",
            "unresolved:reward",
        ],
    )

    assert status is RecordStatus.VALIDATED


def test_model_offline_with_required_fields_rule_resolved_validates() -> None:
    """Issue #22: a model outage must not gate rule-complete records."""

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [
            "model_skipped:model_disabled",
            "model_skipped:no_relevant_source_signal",
            "model_timeout:rules_fallback",
            "model_unavailable:rules_fallback",
            # Only optional fields were left unresolved by the skipped model.
            "unresolved:reward",
            *_OPTIONAL_UNRESOLVED,
        ],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.blocking_issues == ()


def test_title_hint_is_provenance_bookkeeping_not_a_block() -> None:
    """Issue #2: a heading-disambiguated card installment campaign validates."""

    decision = evaluate_validation(
        ProductFamily.CARD,
        CampaignType.INSTALLMENT,
        [
            "title_hint:campaign_type",
            "unresolved:term",
            "unresolved:rate",
            "unresolved:reward",
        ],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.blocking_issues == ()


def test_model_value_grounding_is_provenance_bookkeeping_not_a_block() -> None:
    """A model-proposed, rules-grounded campaign type validates like a title hint."""

    decision = evaluate_validation(
        ProductFamily.CARD,
        CampaignType.INSTALLMENT,
        [
            "campaign_type_ambiguous",
            "model_value_grounded:campaign_type",
            "unresolved:term",
            "unresolved:rate",
            "unresolved:reward",
        ],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.blocking_issues == ()


def test_model_offline_never_rescues_a_missing_required_field() -> None:
    """Issue #22 safety: the unresolved:<field> marker still blocks alone."""

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [
            "model_skipped:model_disabled",
            "model_timeout:rules_fallback",
            "unresolved:rate",
        ],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert ModelFactField.RATE in decision.missing_required_fields
    assert decision.reasons == ("required_field_unresolved",)


def test_rejected_model_fact_on_optional_field_does_not_block() -> None:
    """A discarded proposal on an optional field is bookkeeping, not quarantine."""

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [
            # Rate and term are rule-resolved; the model's fee suggestion
            # failed grounding and was discarded. Fee is optional here.
            "model_fact_rejected:fee:fee_value_not_supported",
            "unresolved:fee",
            "unresolved:reward",
            *_OPTIONAL_UNRESOLVED,
        ],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.blocking_issues == ()


def test_rejected_model_fact_on_required_field_still_blocks() -> None:
    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        ["model_fact_rejected:rate:raw_evidence_mismatch"],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == ("model_fact_rejected:rate:raw_evidence_mismatch",)


def test_rejected_model_fact_with_unknown_field_blocks_conservatively() -> None:
    issue = "model_fact_rejected:not_a_field:whatever"

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [issue],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == (issue,)


def test_prompt_injection_quarantine_blocks_despite_optional_rejection() -> None:
    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [
            "quarantined_prompt_injection_block:block-7",
            "model_fact_rejected:fee:fee_value_not_supported",
        ],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == ("quarantined_prompt_injection_block:block-7",)


@pytest.mark.parametrize(
    ("ambiguity_issue", "unresolved_marker"),
    [
        ("campaign_type_ambiguous", "unresolved:campaign_type"),
        ("product_family_ambiguous", "unresolved:product_family"),
    ],
)
def test_unresolved_required_ambiguity_still_blocks(
    ambiguity_issue: str,
    unresolved_marker: str,
) -> None:
    """An ambiguous classification nobody resolved keeps the record in review."""

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.FINANCING_RATE,
        [ambiguity_issue, unresolved_marker],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == (ambiguity_issue,)


@pytest.mark.parametrize(
    "ambiguity_issue",
    ["campaign_type_ambiguous", "product_family_ambiguous"],
)
def test_resolved_required_ambiguity_is_bookkeeping(ambiguity_issue: str) -> None:
    """Rule-side ambiguity that a grounded fact later settled must not block.

    The rules saw two candidate classifications and abstained; the model (or a
    registry hint) then filled the field with a verbatim-grounded fact, so the
    ``unresolved:<field>`` marker is gone. The stale ambiguity annotation only
    explains why the rules alone could not decide.
    """

    decision = evaluate_validation(
        ProductFamily.CARD,
        CampaignType.DISCOUNT,
        [ambiguity_issue, "unresolved:rate", "unresolved:term", *_OPTIONAL_UNRESOLVED],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.blocking_issues == ()


def test_ambiguity_on_an_optional_field_does_not_block() -> None:
    status = decide_record_status(
        ProductFamily.CARD,
        CampaignType.POINTS,
        ["sales_channel_ambiguous", "unresolved:sales_channel"],
    )

    assert status is RecordStatus.VALIDATED


def test_card_installment_without_term_validates() -> None:
    """ "N taksit" is an installment count, not a term in months; the domain's
    installment_count_is_not_term refusal stands, so card+installment must not
    demand a TERM the source can never state as months."""

    requirement = required_fields(ProductFamily.CARD, CampaignType.INSTALLMENT)
    assert requirement is not None
    assert requirement.all_of == BASE_REQUIRED_FIELDS
    assert requirement.any_of == frozenset()

    decision = evaluate_validation(
        ProductFamily.CARD,
        CampaignType.INSTALLMENT,
        [
            "unresolved:rate",
            "unresolved:term",
            "unresolved:financing_amount",
            "unresolved:fee",
            "unresolved:reward",
            *_OPTIONAL_UNRESOLVED,
        ],
    )

    assert decision.status is RecordStatus.VALIDATED
    assert decision.missing_required_fields == frozenset()
    assert decision.blocking_issues == ()


def test_financing_installment_still_requires_term() -> None:
    """The relaxation is pair-scoped: a financing installment offer without a
    month-denominated term stays needs_review."""

    requirement = required_fields(ProductFamily.FINANCING, CampaignType.INSTALLMENT)
    assert requirement is not None
    assert ModelFactField.TERM in requirement.all_of

    decision = evaluate_validation(
        ProductFamily.FINANCING,
        CampaignType.INSTALLMENT,
        ["unresolved:term"],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert ModelFactField.TERM in decision.missing_required_fields


def test_card_installment_blocking_issues_still_block() -> None:
    """Dropping the TERM requirement never relaxes quarantine or grounding."""

    decision = evaluate_validation(
        ProductFamily.CARD,
        CampaignType.INSTALLMENT,
        ["quarantined_prompt_injection_block:block-3", "unresolved:term"],
    )

    assert decision.status is RecordStatus.NEEDS_REVIEW
    assert decision.blocking_issues == ("quarantined_prompt_injection_block:block-3",)
