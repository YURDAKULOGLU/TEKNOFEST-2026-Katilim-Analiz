"""Deterministically replay a saved collection from verified private raw artifacts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from katilim_analiz.contracts import FetchStatus
from katilim_analiz.extraction import (
    EXTRACTOR_VERSION,
    ExtractionOutcome,
    ExtractionPipeline,
)
from katilim_analiz.ingestion import CleaningError, clean_html
from katilim_analiz.ingestion.artifacts import (
    ArtifactStoreError,
    content_key,
    create_fetch_artifact,
)

from evals.live_collection import _derived_row
from evals.loading import DatasetError, load_json

REPLAY_VERSION = "offline-coverage-replay/1.0"
_COVERAGE_STATUSES = frozenset({"success", "not_found", "unreachable", "blocked"})


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DatasetError(f"{label} must be an object")
    return value


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, label)


def _timestamp(value: object, label: str) -> tuple[str, datetime]:
    raw = _non_empty_string(value, label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetError(f"{label} must include a timezone offset")
    return raw, parsed


def _constant_clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _successful_attempt(bank: Mapping[str, Any]) -> Mapping[str, Any] | None:
    bank_id = _non_empty_string(bank.get("bank_id"), "coverage bank_id")
    status = _non_empty_string(bank.get("status"), f"{bank_id} status")
    if status not in _COVERAGE_STATUSES:
        raise DatasetError(f"{bank_id} has unsupported coverage status {status!r}")
    attempts_value = bank.get("attempts")
    if not isinstance(attempts_value, list) or not attempts_value:
        raise DatasetError(f"{bank_id} attempts must be a non-empty array")

    successful: list[Mapping[str, Any]] = []
    for index, attempt_value in enumerate(attempts_value):
        attempt = _mapping(attempt_value, f"{bank_id} attempt {index}")
        fetch_success = attempt.get("fetch_status") == FetchStatus.SUCCESS.value
        coverage_success = attempt.get("coverage_status") == "success"
        if fetch_success != coverage_success:
            raise DatasetError(
                f"{bank_id} attempt {index} has inconsistent successful statuses"
            )
        if fetch_success:
            successful.append(attempt)

    if len(successful) > 1:
        raise DatasetError(f"{bank_id} has multiple successful attempts")
    if status == "success" and not successful:
        raise DatasetError(f"{bank_id} is successful but has no successful attempt")
    if status != "success" and successful:
        raise DatasetError(f"{bank_id} is {status!r} but contains a successful attempt")
    return successful[0] if successful else None


def _read_verified_raw(
    *,
    private_root: Path,
    bank_id: str,
    raw_sha256: str,
    raw_size_bytes: int,
) -> tuple[bytes, str]:
    try:
        key = content_key(raw_sha256, ".html")
    except ArtifactStoreError as exc:
        raise DatasetError(f"{bank_id} has invalid raw_sha256: {exc}") from exc

    root = private_root.resolve()
    if not root.is_dir():
        raise DatasetError(f"private root is not a directory: {private_root}")
    path = root / Path(key)
    resolved_parent = path.parent.resolve()
    if root != resolved_parent and root not in resolved_parent.parents:
        raise DatasetError(f"{bank_id} raw artifact escapes the private root")
    if path.is_symlink() or not path.is_file():
        raise DatasetError(f"{bank_id} missing raw artifact: {key}")
    try:
        raw_html = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{bank_id} cannot read raw artifact {key}: {exc}") from exc
    actual_sha256 = hashlib.sha256(raw_html).hexdigest()
    if actual_sha256 != raw_sha256:
        raise DatasetError(
            f"{bank_id} raw artifact SHA-256 mismatch: expected {raw_sha256}, "
            f"observed {actual_sha256}"
        )
    if len(raw_html) != raw_size_bytes:
        raise DatasetError(
            f"{bank_id} raw artifact size mismatch: expected {raw_size_bytes}, "
            f"observed {len(raw_html)}"
        )
    return raw_html, key


def _manifest(
    coverage: Mapping[str, Any],
    rows: list[dict[str, Any]],
    *,
    coverage_sha256: str,
    successful_attempt_count: int,
) -> dict[str, Any]:
    completed_at, _ = _timestamp(coverage.get("completed_at"), "coverage completed_at")
    registry_version = _non_empty_string(
        coverage.get("registry_version"), "coverage registry_version"
    )
    collector_profile = _mapping(
        coverage.get("collector_profile"), "coverage collector_profile"
    )
    collector_version = _non_empty_string(
        collector_profile.get("collector_version"),
        "coverage collector_profile.collector_version",
    )
    review_counts = {
        status: sum(1 for row in rows if row.get("human_review_status") == status)
        for status in ("pending", "verified", "rejected")
    }
    banks = coverage.get("banks")
    assert isinstance(banks, list)
    blocked_count = sum(
        1
        for value in banks
        if isinstance(value, dict) and value.get("status") == "blocked"
    )
    return {
        "schema_version": "1.0",
        "dataset_id": "katilim-campaign-derived",
        "dataset_version": "0.1.0",
        "generated_at": completed_at,
        "source_registry_version": registry_version,
        "collector_profile": collector_version,
        "extractor_profile": f"rules-only:{EXTRACTOR_VERSION}",
        "replay_profile": REPLAY_VERSION,
        "source_coverage_sha256": coverage_sha256,
        "replayed_raw_artifact_count": successful_attempt_count,
        "record_count": len(rows),
        "human_review_counts": review_counts,
        "contains_raw_html": False,
        "contains_full_cleaned_text": False,
        "licence_boundary": (
            "Structured team-authored derivations are Apache-2.0; source URLs and hashes "
            "identify third-party official bank pages without transferring rights."
        ),
        "known_limitations": [
            f"{blocked_count} coverage entries were blocked; offline replay does not fetch "
            "or bypass them.",
            "TOM listing segmentation is bound to the observed repeated DOM structure and "
            "abstains when that structure is not recognized.",
            "Rules-only candidates retain unresolved semantic fields and require review.",
            "No derived row is eligible as gold until the two-reviewer workflow is complete.",
        ],
    }


async def replay_collection(
    *,
    coverage_path: Path,
    private_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay exactly one successful saved attempt per successful coverage entry."""

    coverage = _mapping(load_json(coverage_path), "coverage")
    if coverage.get("schema_version") != "1.0":
        raise DatasetError("coverage schema_version must be '1.0'")
    _timestamp(coverage.get("started_at"), "coverage started_at")
    _timestamp(coverage.get("completed_at"), "coverage completed_at")
    _non_empty_string(
        coverage.get("registry_source_url"), "coverage registry_source_url"
    )
    banks_value = coverage.get("banks")
    if not isinstance(banks_value, list) or not banks_value:
        raise DatasetError("coverage banks must be a non-empty array")

    coverage_canonical = json.dumps(
        coverage, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    coverage_sha256 = hashlib.sha256(coverage_canonical).hexdigest()
    banks: list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None]] = []
    seen_bank_ids: set[str] = set()
    for index, bank_value in enumerate(banks_value):
        bank = _mapping(bank_value, f"coverage bank {index}")
        bank_id = _non_empty_string(
            bank.get("bank_id"), f"coverage bank {index} bank_id"
        )
        if bank_id in seen_bank_ids:
            raise DatasetError(f"duplicate coverage bank_id {bank_id!r}")
        seen_bank_ids.add(bank_id)
        banks.append((bank_id, bank, _successful_attempt(bank)))

    rows: list[dict[str, Any]] = []
    successful_attempt_count = 0
    for bank_id, _bank, attempt in sorted(banks, key=lambda item: item[0]):
        if attempt is None:
            continue
        successful_attempt_count += 1
        observed_at, observed_datetime = _timestamp(
            attempt.get("observed_at"), f"{bank_id} attempt observed_at"
        )
        requested_url = _non_empty_string(
            attempt.get("requested_url"), f"{bank_id} requested_url"
        )
        final_url = _non_empty_string(attempt.get("final_url"), f"{bank_id} final_url")
        raw_sha256 = _non_empty_string(
            attempt.get("raw_sha256"), f"{bank_id} raw_sha256"
        )
        raw_size_bytes = attempt.get("raw_size_bytes")
        if (
            isinstance(raw_size_bytes, bool)
            or not isinstance(raw_size_bytes, int)
            or raw_size_bytes <= 0
        ):
            raise DatasetError(f"{bank_id} raw_size_bytes must be a positive integer")
        http_status = attempt.get("http_status")
        if isinstance(http_status, bool) or not isinstance(http_status, int):
            raise DatasetError(f"{bank_id} http_status must be an integer")
        if attempt.get("robots_allowed") is not True:
            raise DatasetError(f"{bank_id} successful attempt must be robots_allowed")
        if attempt.get("error_code") is not None:
            raise DatasetError(
                f"{bank_id} successful attempt cannot contain an error_code"
            )

        raw_html, private_raw_path = _read_verified_raw(
            private_root=private_root,
            bank_id=bank_id,
            raw_sha256=raw_sha256,
            raw_size_bytes=raw_size_bytes,
        )
        try:
            artifact = create_fetch_artifact(
                bank_id=bank_id,
                requested_url=requested_url,
                final_url=final_url,
                status=FetchStatus.SUCCESS,
                http_status=http_status,
                fetched_at=observed_datetime,
                robots_allowed=True,
                content_type=_optional_string(
                    attempt.get("content_type"), f"{bank_id} content_type"
                ),
                etag=_optional_string(attempt.get("etag"), f"{bank_id} etag"),
                last_modified=_optional_string(
                    attempt.get("last_modified"), f"{bank_id} last_modified"
                ),
                raw_sha256=raw_sha256,
                raw_size_bytes=raw_size_bytes,
                private_raw_path=private_raw_path,
            )
            document = clean_html(
                artifact,
                raw_html,
                cleaned_at=observed_datetime,
            )
        except (CleaningError, ValueError) as exc:
            raise DatasetError(f"{bank_id} replay cleaning failed: {exc}") from exc
        pipeline = ExtractionPipeline(
            model_enabled=False,
            clock=_constant_clock(observed_datetime),
        )
        for extraction in await pipeline.extract_many(document):
            if extraction.model_attempted:
                raise DatasetError(
                    f"{bank_id} offline replay attempted model inference"
                )
            if (
                extraction.outcome is not ExtractionOutcome.CANDIDATE
                or extraction.candidate is None
            ):
                continue
            rows.append(
                _derived_row(
                    bank_id=bank_id,
                    source_url=str(document.canonical_url),
                    raw_sha256=raw_sha256,
                    clean_sha256=document.clean_sha256,
                    observed_at=observed_at,
                    candidate=extraction.candidate,
                )
            )

    rows.sort(key=lambda row: (row["bank_id"], row["source_url"], row["derived_id"]))
    return rows, _manifest(
        coverage,
        rows,
        coverage_sha256=coverage_sha256,
        successful_attempt_count=successful_attempt_count,
    )


def _output_payloads(
    rows: list[dict[str, Any]], manifest: Mapping[str, Any]
) -> tuple[bytes, bytes]:
    try:
        derived = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ).encode()
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
    except (TypeError, ValueError) as exc:
        raise DatasetError(f"replay output is not JSON serializable: {exc}") from exc
    return derived, manifest_bytes


def write_replay_outputs(
    *,
    derived_output: Path,
    manifest_output: Path,
    rows: list[dict[str, Any]],
    manifest: Mapping[str, Any],
    force: bool = False,
) -> None:
    """Write only caller-selected paths and refuse implicit overwrites."""

    paths = (Path(derived_output), Path(manifest_output))
    normalized = [os.path.normcase(str(path.resolve())) for path in paths]
    if normalized[0] == normalized[1]:
        raise DatasetError("derived and manifest output paths must be distinct")
    for path in paths:
        if path.is_symlink():
            raise DatasetError(f"output path cannot be a symbolic link: {path}")
        if path.exists() and not force:
            raise DatasetError(f"output already exists (use --force): {path}")
        if path.exists() and not path.is_file():
            raise DatasetError(f"output path is not a file: {path}")

    payloads = _output_payloads(rows, manifest)
    created: list[Path] = []
    try:
        for path, payload in zip(paths, payloads, strict=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "wb" if force else "xb"
            with path.open(mode) as output:
                output.write(payload)
            if not force:
                created.append(path)
    except FileExistsError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise DatasetError(
            f"output already exists (use --force): {exc.filename}"
        ) from exc
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise DatasetError(f"cannot write replay outputs: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--derived-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    try:
        rows, manifest = asyncio.run(
            replay_collection(
                coverage_path=arguments.coverage,
                private_root=arguments.private_root,
            )
        )
        write_replay_outputs(
            derived_output=arguments.derived_output,
            manifest_output=arguments.manifest_output,
            rows=rows,
            manifest=manifest,
            force=arguments.force,
        )
    except DatasetError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "derived_output": str(arguments.derived_output),
                "manifest_output": str(arguments.manifest_output),
                "derived_records": len(rows),
                "replay_profile": REPLAY_VERSION,
                "source_coverage_sha256": manifest["source_coverage_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
