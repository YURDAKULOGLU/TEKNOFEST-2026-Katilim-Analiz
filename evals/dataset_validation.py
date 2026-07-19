"""Integrity checks for public gold annotations and split manifests."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from evals.loading import DatasetError
from evals.splits import find_split_leakage


def validate_gold_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for row in rows:
        example_id = row.get("example_id")
        if not isinstance(example_id, str) or not example_id:
            raise DatasetError("gold example_id must be a non-empty string")
        if example_id in seen:
            raise DatasetError(f"duplicate gold example_id {example_id!r}")
        seen.add(example_id)
        excerpt = row.get("source_excerpt")
        if not isinstance(excerpt, str) or not excerpt:
            raise DatasetError(f"{example_id} has no short source excerpt")
        if hashlib.sha256(excerpt.encode()).hexdigest() != row.get(
            "source_excerpt_sha256"
        ):
            raise DatasetError(f"{example_id} source excerpt hash mismatch")
        fields = row.get("fields")
        if not isinstance(fields, Mapping):
            raise DatasetError(f"{example_id} fields must be an object")
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            raise DatasetError(f"{example_id} evidence must be an array")
        evidenced_fields: set[str] = set()
        for binding in evidence:
            if not isinstance(binding, Mapping):
                raise DatasetError(f"{example_id} evidence binding must be an object")
            pointer = binding.get("field_pointer")
            quote = binding.get("quote")
            start = binding.get("start_char")
            end = binding.get("end_char")
            if not isinstance(pointer, str) or pointer not in fields:
                raise DatasetError(
                    f"{example_id} evidence points outside annotated fields"
                )
            if (
                not isinstance(quote, str)
                or not quote
                or isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or end - start != len(quote)
            ):
                raise DatasetError(f"{example_id} has an invalid evidence span")
            if hashlib.sha256(quote.encode()).hexdigest() != binding.get(
                "evidence_sha256"
            ):
                raise DatasetError(f"{example_id} evidence quote hash mismatch")
            evidenced_fields.add(pointer)
        if any(
            value is not None and pointer not in evidenced_fields
            for pointer, value in fields.items()
        ):
            raise DatasetError(f"{example_id} has a non-null field without evidence")
        review = row.get("human_review")
        if not isinstance(review, Mapping):
            raise DatasetError(f"{example_id} human_review must be an object")
        status = review.get("status")
        reviewers = review.get("reviewer_ids")
        reviewed_at = review.get("reviewed_at")
        if status not in {"pending", "verified", "rejected"} or not isinstance(
            reviewers, list
        ):
            raise DatasetError(f"{example_id} has an invalid human-review state")
        if status == "verified" and (len(set(reviewers)) < 2 or reviewed_at is None):
            raise DatasetError(
                f"{example_id} verified status requires two reviewers and a timestamp"
            )


def validate_split_manifest(
    rows: Sequence[Mapping[str, Any]], split_manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    splits = split_manifest.get("splits")
    if not isinstance(splits, Mapping):
        raise DatasetError("split manifest must contain a splits object")
    assignments: dict[str, str] = {}
    for split_name in ("train", "development", "test"):
        identifiers = splits.get(split_name)
        if not isinstance(identifiers, list) or not all(
            isinstance(identifier, str) for identifier in identifiers
        ):
            raise DatasetError(f"split {split_name} must be a string array")
        for identifier in identifiers:
            if identifier in assignments:
                raise DatasetError(f"example {identifier!r} appears in multiple splits")
            assignments[identifier] = split_name
    expected = {str(row["example_id"]) for row in rows}
    if set(assignments) != expected:
        raise DatasetError("split assignments must exactly cover the gold examples")
    for row in rows:
        if assignments[str(row["example_id"])] != row.get("split"):
            raise DatasetError(f"split mismatch for {row['example_id']}")
    threshold = float(split_manifest.get("near_duplicate_threshold", 0.85))
    leaks = find_split_leakage(rows, threshold=threshold)
    serialized = tuple(
        {
            "left_id": leak.left_id,
            "right_id": leak.right_id,
            "left_split": leak.left_split,
            "right_split": leak.right_split,
            "similarity": leak.similarity,
            "reason_code": leak.reason_code,
        }
        for leak in leaks
    )
    if serialized:
        raise DatasetError(
            f"{len(serialized)} cross-split duplicate or near-duplicate leaks found"
        )
    return serialized
