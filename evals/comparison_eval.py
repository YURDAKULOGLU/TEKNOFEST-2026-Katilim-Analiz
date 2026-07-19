"""Run concise golden comparison cases through the public pure domain API."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    ComparisonContext,
    ComparisonDimension,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    GrossNetBasis,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    ValidityWindow,
)
from katilim_analiz.domain import compare_campaigns

from evals.runner import EvaluationGate, GateStatus


def _evidence(record_id: str, raw: str) -> EvidenceRef:
    return EvidenceRef(
        id=f"evidence:{record_id}",
        field_pointer="/data/rates/0",
        source_document_id=f"document:{record_id}",
        block_id=f"block:{record_id}",
        quote=raw,
        start_char=0,
        end_char=len(raw),
        evidence_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        status=EvidenceStatus.STATED,
    )


def _record(spec: Mapping[str, Any]) -> CampaignRecord:
    record_id = str(spec["id"])
    family = ProductFamily(spec.get("product_family", "financing"))
    rate_spec = dict(spec.get("rate", {}))
    raw = f"%{rate_spec.get('value_percent', '1.50')} aylık kâr payı"
    rate = RateValue(
        raw=raw,
        value_percent=Decimal(str(rate_spec.get("value_percent", "1.50"))),
        kind=RateKind(rate_spec.get("kind", "financing_profit_rate")),
        period=RatePeriod(rate_spec.get("period", "monthly")),
        gross_net_basis=GrossNetBasis.UNSPECIFIED,
        term_months=12,
        basis_label="kampanya_aylik_kar_payi",
        status=EvidenceStatus.STATED,
    )
    segments = list(spec.get("segments", ["bireysel"]))
    context = ComparisonContext(
        product_currency="TRY",
        customer_segment_keys=segments,
        sales_channel=SalesChannel.MOBILE,
        new_customer_only=False,
        product_mechanism="ihtiyac_finansmani"
        if family is ProductFamily.FINANCING
        else "kart",
        secured=False if family is ProductFamily.FINANCING else None,
    )
    observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
    extraction = ExtractionMetadata(
        method=ExtractionMethod.RULE,
        extractor_version="comparison-fixture/1.0",
        schema_version="1.0",
        started_at=observed_at,
        completed_at=observed_at,
    )
    evidence = [_evidence(record_id, raw)] if spec.get("rate_evidence", True) else []
    return CampaignRecord(
        id=record_id,
        version=1,
        source_document_id=f"document:{record_id}",
        observed_at=observed_at,
        data=CampaignData(
            bank_id=f"bank-{record_id}",
            title=record_id,
            product_family=family,
            campaign_type=CampaignType.FINANCING_RATE,
            rates=[rate],
            validity=ValidityWindow(
                raw="1-31 Temmuz 2026",
                starts_on=date(2026, 7, 1),
                ends_on=date(2026, 7, 31),
            ),
            customer_segments=segments,
            comparison_context=context,
        ),
        evidence=evidence,
        extraction=extraction,
        status=RecordStatus.VALIDATED,
        record_sha256=hashlib.sha256(record_id.encode()).hexdigest(),
    )


def _run_case(case: Mapping[str, Any]) -> dict[str, Any]:
    records = [_record(spec) for spec in case.get("records", [])]
    dimension = ComparisonDimension(case.get("dimension", "rate"))
    report = compare_campaigns(
        records,
        [dimension],
        as_of=date.fromisoformat(str(case["as_of"])),
    )
    expected = dict(case.get("expected", {}))
    comparable = all(item.comparable for item in report.items)
    reasons = sorted({item.reason_code for item in report.items if not item.comparable})
    ranks = {
        item.campaign_id: item.rank for item in report.items if item.rank is not None
    }
    passed = comparable == expected.get("all_comparable")
    if "reason_codes" in expected:
        passed = passed and reasons == sorted(expected["reason_codes"])
    if "ranks" in expected:
        passed = passed and ranks == expected["ranks"]
    return {
        "case_id": case.get("case_id"),
        "passed": passed,
        "actual": {
            "all_comparable": comparable,
            "reason_codes": reasons,
            "ranks": ranks,
        },
        "expected": expected,
        "canonical_sha256": report.canonical_sha256,
    }


def evaluate_comparisons(
    cases: Sequence[Mapping[str, Any]], *, minimum_cases: int = 5
) -> EvaluationGate:
    verified = [case for case in cases if case.get("review_status") == "verified"]
    outcomes = [_run_case(case) for case in verified]
    details: dict[str, Any] = {
        "verified_cases": len(verified),
        "minimum_cases": minimum_cases,
        "passed_cases": sum(1 for outcome in outcomes if outcome["passed"]),
        "accuracy": (
            sum(1 for outcome in outcomes if outcome["passed"]) / len(outcomes)
            if outcomes
            else 0.0
        ),
        "outcomes": outcomes,
    }
    if len(verified) < minimum_cases:
        return EvaluationGate(
            "EVAL-007",
            GateStatus.INSUFFICIENT_DATA,
            "Comparison case coverage is below the declared minimum.",
            details,
        )
    passed = all(outcome["passed"] for outcome in outcomes)
    return EvaluationGate(
        "EVAL-007",
        GateStatus.PASS if passed else GateStatus.FAIL,
        "All golden comparison cases passed."
        if passed
        else "A golden comparison case failed.",
        details,
    )
