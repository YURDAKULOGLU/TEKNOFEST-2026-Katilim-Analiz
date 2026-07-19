"""Bounded live collection for the versioned ten-bank coverage snapshot."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from katilim_analiz.contracts import FetchStatus
from katilim_analiz.extraction import (
    EXTRACTOR_VERSION,
    ExtractionOutcome,
    ExtractionPipeline,
)
from katilim_analiz.ingestion import (
    HostPolicy,
    HttpIngestor,
    InMemoryResponseCache,
    PrivateFileArtifactStore,
    StaticHostPolicyProvider,
    CleaningError,
    clean_html,
    load_registry,
)

from evals.loading import DatasetError, load_json

_POLICY_BLOCK_CODES = frozenset(
    {
        "access_challenge",
        "authentication_required",
        "host_not_allowed",
        "redirect_policy_violation",
        "robots_disallowed",
        "robots_redirect_limit",
        "robots_redirect_without_location",
        "scheme_not_allowed",
    }
)
COLLECTOR_VERSION = "bounded-live-collector/1.1"


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def _coverage_status(fetch_status: FetchStatus, error_code: str | None) -> str:
    if fetch_status is FetchStatus.SUCCESS:
        return "success"
    if fetch_status is FetchStatus.BLOCKED or error_code in _POLICY_BLOCK_CODES:
        return "blocked"
    if error_code == "http_404":
        return "not_found"
    return "unreachable"


def _reason(status: str, error_code: str | None, error_detail: str | None) -> str:
    if status == "success":
        return "official_candidate_fetched_and_cleaned"
    detail = (error_detail or "no additional detail").replace("\n", " ").strip()
    return f"{error_code or 'collection_failed'}: {detail}"[:500]


def _derived_row(
    *,
    bank_id: str,
    source_url: str,
    raw_sha256: str,
    clean_sha256: str,
    observed_at: str,
    candidate: Any,
) -> dict[str, Any]:
    evidence_pointers = sorted({item.field_pointer for item in candidate.evidence})
    data = _strip_source_raw(candidate.data.model_dump(mode="json"))
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "schema_version": "1.0",
        "derived_id": f"derived:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "bank_id": bank_id,
        "source_url": source_url,
        "observed_at": observed_at,
        "raw_sha256": raw_sha256,
        "clean_sha256": clean_sha256,
        "extractor_version": candidate.metadata.extractor_version,
        "human_review_status": "pending",
        "data": data,
        "evidence_field_pointers": evidence_pointers,
        "evidence_count": len(candidate.evidence),
        "issues": list(candidate.issues),
    }


def _strip_source_raw(value: Any) -> Any:
    """Remove source-form `raw` members while retaining canonical structured facts."""

    if isinstance(value, dict):
        return {
            key: _strip_source_raw(item) for key, item in value.items() if key != "raw"
        }
    if isinstance(value, list):
        return [_strip_source_raw(item) for item in value]
    return value


def _derived_manifest(
    coverage: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    review_counts = {
        status: sum(1 for row in rows if row.get("human_review_status") == status)
        for status in ("pending", "verified", "rejected")
    }
    return {
        "schema_version": "1.0",
        "dataset_id": "katilim-campaign-derived",
        "dataset_version": "0.1.0",
        "generated_at": coverage["completed_at"],
        "source_registry_version": coverage["registry_version"],
        "collector_profile": COLLECTOR_VERSION,
        "extractor_profile": f"rules-only:{EXTRACTOR_VERSION}",
        "record_count": len(rows),
        "human_review_counts": review_counts,
        "contains_raw_html": False,
        "contains_full_cleaned_text": False,
        "licence_boundary": (
            "Structured team-authored derivations are Apache-2.0; source URLs and hashes "
            "identify third-party official bank pages without transferring rights."
        ),
        "known_limitations": [
            "Four official sites remain blocked by access challenges; no bypass was attempted.",
            "TOM listing segmentation is bound to the observed repeated DOM structure and "
            "abstains when that structure is not recognized.",
            "Rules-only candidates retain unresolved semantic fields and require review.",
            "No derived row is eligible as gold until the two-reviewer workflow is complete.",
        ],
    }


async def collect(
    *,
    registry_path: Path,
    discovery_path: Path,
    private_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = load_registry(registry_path)
    discovery = load_json(discovery_path)
    if not isinstance(discovery, dict) or not isinstance(discovery.get("banks"), list):
        raise DatasetError("discovery manifest must contain a banks array")
    discovered = {entry.get("bank_id"): entry for entry in discovery["banks"]}
    if set(discovered) != {bank.id for bank in registry.banks}:
        raise DatasetError("discovery bank IDs must exactly match the source registry")

    policy = HostPolicy(
        min_interval_seconds=2.0,
        request_timeout_seconds=20.0,
        max_attempts=2,
        backoff_base_seconds=1.0,
        backoff_cap_seconds=5.0,
        max_retry_after_seconds=30.0,
        max_response_bytes=2_000_000,
        max_redirects=5,
        user_agent="KatilimAnalizBot/0.1",
    )
    started_at = _now()
    coverage_entries: list[dict[str, Any]] = []
    derived_rows: list[dict[str, Any]] = []
    async with HttpIngestor(
        registry=registry,
        artifact_store=PrivateFileArtifactStore(private_root),
        response_cache=InMemoryResponseCache(),
        policy_provider=StaticHostPolicyProvider(default=policy),
    ) as ingestor:
        pipeline = ExtractionPipeline(model_enabled=False)
        for bank in registry.banks:
            bank_started = _now()
            attempts: list[dict[str, Any]] = []
            final_status = "not_found"
            final_reason = "no_candidate_succeeded"
            source_url = bank.listed_homepage_url
            source_count = 0
            campaign_count = 0
            candidates = discovered[bank.id].get("candidates", [])
            if not isinstance(candidates, list) or not candidates:
                raise DatasetError(f"{bank.id} has no discovery candidate")
            for candidate_spec in candidates:
                requested_url = candidate_spec.get("url")
                if not isinstance(requested_url, str):
                    raise DatasetError(f"{bank.id} candidate URL must be a string")
                source_url = requested_url
                observed_at = _now().isoformat(timespec="seconds")
                result = await ingestor.fetch(bank.id, requested_url)
                artifact = result.artifact
                status = _coverage_status(artifact.status, artifact.error_code)
                reason = _reason(status, artifact.error_code, artifact.error_detail)
                attempt: dict[str, Any] = {
                    "requested_url": requested_url,
                    "candidate_kind": candidate_spec.get("kind"),
                    "observed_at": observed_at,
                    "fetch_status": artifact.status.value,
                    "coverage_status": status,
                    "http_status": artifact.http_status,
                    "final_url": str(artifact.final_url)
                    if artifact.final_url
                    else None,
                    "robots_allowed": artifact.robots_allowed,
                    "error_code": artifact.error_code,
                    "reason": reason,
                    "raw_sha256": artifact.raw_sha256,
                    "raw_size_bytes": artifact.raw_size_bytes,
                }
                attempts.append(attempt)
                final_status = status
                final_reason = reason
                if (
                    artifact.status is not FetchStatus.SUCCESS
                    or result.raw_content is None
                ):
                    continue
                source_count += 1
                try:
                    document = clean_html(
                        artifact,
                        result.raw_content,
                        cleaned_at=_now(),
                    )
                except CleaningError as exc:
                    attempt["cleaning_error"] = str(exc)
                    final_status = "unreachable"
                    final_reason = f"cleaning_failed: {exc}"[:500]
                    continue
                attempt["clean_document_id"] = document.id
                attempt["clean_sha256"] = document.clean_sha256
                attempt["clean_block_count"] = len(document.blocks)
                extractions = await pipeline.extract_many(document)
                outcomes = {extraction.outcome.value for extraction in extractions}
                attempt["extraction_segment_count"] = len(extractions)
                attempt["extraction_outcome"] = (
                    next(iter(outcomes)) if len(outcomes) == 1 else "mixed"
                )
                attempt["extraction_issues"] = list(
                    dict.fromkeys(
                        issue
                        for extraction in extractions
                        for issue in extraction.issues
                    )
                )
                for extraction in extractions:
                    if (
                        extraction.outcome is not ExtractionOutcome.CANDIDATE
                        or extraction.candidate is None
                    ):
                        continue
                    campaign_count += 1
                    derived_rows.append(
                        _derived_row(
                            bank_id=bank.id,
                            source_url=str(document.canonical_url),
                            raw_sha256=artifact.raw_sha256 or "",
                            clean_sha256=document.clean_sha256,
                            observed_at=observed_at,
                            candidate=extraction.candidate,
                        )
                    )
                if candidate_spec.get("kind") == "homepage" and campaign_count == 0:
                    final_status = "not_found"
                    final_reason = (
                        "official_site_reachable_no_public_campaign_detail_discovered"
                    )
                else:
                    final_status = "success"
                    final_reason = "official_candidate_fetched_and_cleaned"
                break
            coverage_entries.append(
                {
                    "bank_id": bank.id,
                    "bank_name": bank.legal_name,
                    "observed_at": bank_started.isoformat(timespec="seconds"),
                    "source_url": source_url,
                    "status": final_status,
                    "reason": final_reason,
                    "source_count": source_count,
                    "campaign_count": campaign_count,
                    "attempts": attempts,
                }
            )
    completed_at = _now()
    coverage = {
        "$schema": "../schemas/coverage.schema.json",
        "schema_version": "1.0",
        "registry_version": registry.registry_version,
        "registry_source_url": registry.source_url,
        "started_at": started_at.isoformat(timespec="seconds"),
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "collector_profile": {
            "collector_version": COLLECTOR_VERSION,
            "user_agent": policy.user_agent,
            "min_interval_seconds": policy.min_interval_seconds,
            "request_timeout_seconds": policy.request_timeout_seconds,
            "max_attempts": policy.max_attempts,
            "max_response_bytes": policy.max_response_bytes,
            "max_redirects": policy.max_redirects,
            "robots": "required_fail_closed",
            "redirect_revalidation": True,
            "authentication_or_captcha_bypass": False,
        },
        "banks": coverage_entries,
    }
    return coverage, sorted(
        derived_rows, key=lambda row: (row["bank_id"], row["derived_id"])
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/registry/bddk-participation-banks-2026-07-18.json"),
    )
    parser.add_argument(
        "--discovery",
        type=Path,
        default=Path("datasets/discovery/candidates-2026-07-18.json"),
    )
    parser.add_argument(
        "--private-root", type=Path, default=Path("data/private/wp070/raw")
    )
    parser.add_argument(
        "--coverage-output",
        type=Path,
        default=Path("datasets/coverage/2026-07-18.json"),
    )
    parser.add_argument(
        "--derived-output",
        type=Path,
        default=Path("datasets/derived/v0.1/campaigns.jsonl"),
    )
    parser.add_argument(
        "--derived-manifest-output",
        type=Path,
        default=Path("datasets/derived/v0.1/manifest.json"),
    )
    arguments = parser.parse_args()
    coverage, derived = asyncio.run(
        collect(
            registry_path=arguments.registry,
            discovery_path=arguments.discovery,
            private_root=arguments.private_root,
        )
    )
    _write_json(arguments.coverage_output, coverage)
    _write_jsonl(arguments.derived_output, derived)
    _write_json(arguments.derived_manifest_output, _derived_manifest(coverage, derived))
    print(
        json.dumps(
            {
                "coverage_output": str(arguments.coverage_output),
                "derived_output": str(arguments.derived_output),
                "derived_manifest_output": str(arguments.derived_manifest_output),
                "status_counts": {
                    status: sum(
                        1 for row in coverage["banks"] if row["status"] == status
                    )
                    for status in ("success", "not_found", "unreachable", "blocked")
                },
                "derived_records": len(derived),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
