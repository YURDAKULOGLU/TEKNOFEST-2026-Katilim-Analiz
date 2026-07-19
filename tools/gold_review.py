"""Prepare and merge deterministic, offline two-human gold reviews."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_ID = "https://katilim-analiz.local/datasets/review-decision.schema.json"
SCHEMA_VERSION = "1.0"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DECISION_KEYS = {
    "$schema",
    "schema_version",
    "example_id",
    "candidate_fingerprint",
    "source_raw_sha256",
    "source_clean_sha256",
    "source_excerpt_sha256",
    "reviewer_id",
    "decision",
    "reviewed_at",
    "review_notes",
}
_SOURCE_HASH_FIELDS = (
    "source_raw_sha256",
    "source_clean_sha256",
    "source_excerpt_sha256",
)


class ReviewError(ValueError):
    """A review artifact failed a deterministic, fail-closed validation."""


@dataclass(frozen=True, slots=True)
class PrepareResult:
    output_dir: Path
    decision_paths: Mapping[str, Path]
    markdown_packet: Path
    html_packet: Path


@dataclass(frozen=True, slots=True)
class MergeSummary:
    candidate_set_sha256: str
    reviewer_ids: tuple[str, str]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _SubmittedReview:
    reviewer_id: str
    rows: Mapping[str, Mapping[str, Any]]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ReviewError(
            f"value cannot be represented as canonical JSON: {exc}"
        ) from exc


def candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Hash every candidate property except mutable human-review state."""

    payload = {key: value for key, value in candidate.items() if key != "human_review"}
    return _sha256_text(_canonical_json(payload))


def _candidate_set_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "candidate_fingerprint": candidate_fingerprint(row),
            "example_id": row["example_id"],
        }
        for row in rows
    ]
    return _sha256_text(_canonical_json(payload))


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ReviewError(f"{label} is not a readable file: {path}")
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise ReviewError(
                        f"{label} contains a blank row at line {line_number}"
                    )
                try:
                    row = json.loads(
                        line,
                        object_pairs_hook=_object_without_duplicate_keys,
                        parse_constant=_reject_json_constant,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ReviewError(
                        f"{label} contains malformed JSON at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise ReviewError(
                        f"{label} row {line_number} must be a JSON object"
                    )
                rows.append(row)
    except (OSError, UnicodeError) as exc:
        raise ReviewError(f"unable to read {label}: {exc}") from exc
    if not rows:
        raise ReviewError(f"{label} must contain at least one row")
    return rows


def _valid_identifier(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ReviewError(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ReviewError(
            f"{label} must not contain surrounding whitespace or control characters"
        )
    return value


def _valid_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ReviewError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_candidate_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for row_number, row in enumerate(rows, start=1):
        example_id = _valid_identifier(
            row.get("example_id"),
            label=f"candidate row {row_number} example_id",
            maximum=256,
        )
        if example_id in by_id:
            raise ReviewError(f"duplicate candidate example_id {example_id!r}")
        for field in _SOURCE_HASH_FIELDS:
            _valid_sha256(row.get(field), label=f"{example_id} {field}")
        excerpt = row.get("source_excerpt")
        if not isinstance(excerpt, str) or not excerpt:
            raise ReviewError(f"{example_id} source_excerpt must be a non-empty string")
        if _sha256_text(excerpt) != row["source_excerpt_sha256"]:
            raise ReviewError(
                f"{example_id} source_excerpt_sha256 does not match the excerpt"
            )
        for field in ("bank_id", "source_url"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ReviewError(f"{example_id} {field} must be a non-empty string")

        fields = row.get("fields")
        evidence = row.get("evidence")
        if not isinstance(fields, dict):
            raise ReviewError(f"{example_id} fields must be an object")
        if not isinstance(evidence, list):
            raise ReviewError(f"{example_id} evidence must be an array")
        evidenced_fields: set[str] = set()
        for evidence_number, binding in enumerate(evidence, start=1):
            if not isinstance(binding, dict):
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} must be an object"
                )
            pointer = binding.get("field_pointer")
            quote = binding.get("quote")
            start = binding.get("start_char")
            end = binding.get("end_char")
            if not isinstance(pointer, str) or pointer not in fields:
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} has an unknown field"
                )
            if not isinstance(quote, str) or not quote:
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} quote is invalid"
                )
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or start < 0
                or isinstance(end, bool)
                or not isinstance(end, int)
                or end <= start
                or end - start != len(quote)
            ):
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} span is invalid"
                )
            evidence_sha256 = _valid_sha256(
                binding.get("evidence_sha256"),
                label=f"{example_id} evidence {evidence_number} evidence_sha256",
            )
            if _sha256_text(quote) != evidence_sha256:
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} quote hash mismatch"
                )
            if not isinstance(binding.get("block_id"), str) or not binding["block_id"]:
                raise ReviewError(
                    f"{example_id} evidence {evidence_number} block_id is invalid"
                )
            evidenced_fields.add(pointer)
        if any(
            value is not None and pointer not in evidenced_fields
            for pointer, value in fields.items()
        ):
            raise ReviewError(f"{example_id} has a non-null field without evidence")

        review = row.get("human_review")
        if not isinstance(review, dict):
            raise ReviewError(f"{example_id} human_review must be an object")
        if review.get("status") != "pending":
            raise ReviewError(f"{example_id} is not an unreviewed pending candidate")
        if review.get("reviewer_ids") != [] or review.get("reviewed_at") is not None:
            raise ReviewError(f"{example_id} already contains human-review activity")
        if not isinstance(review.get("review_notes"), str):
            raise ReviewError(
                f"{example_id} human_review.review_notes must be a string"
            )
        by_id[example_id] = row
    return by_id


def _validate_reviewer_ids(reviewer_ids: Sequence[str]) -> tuple[str, str]:
    if len(reviewer_ids) != 2:
        raise ReviewError("exactly two reviewer IDs are required")
    validated = tuple(
        _valid_identifier(value, label="reviewer_id", maximum=128)
        for value in reviewer_ids
    )
    if validated[0] == validated[1]:
        raise ReviewError("the two reviewer IDs must be distinct")
    first, second = sorted(validated)
    return first, second


def _decision_template(
    candidate: Mapping[str, Any], reviewer_id: str
) -> dict[str, Any]:
    return {
        "$schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "example_id": candidate["example_id"],
        "candidate_fingerprint": candidate_fingerprint(candidate),
        "source_raw_sha256": candidate["source_raw_sha256"],
        "source_clean_sha256": candidate["source_clean_sha256"],
        "source_excerpt_sha256": candidate["source_excerpt_sha256"],
        "reviewer_id": reviewer_id,
        "decision": None,
        "reviewed_at": None,
        "review_notes": "",
    }


def _render_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(f"{_canonical_json(row)}\n" for row in rows)


def _indented_markdown(value: str) -> str:
    escaped = html.escape(value, quote=True)
    return "\n".join(f"    {line}" for line in escaped.splitlines() or [""])


def _render_markdown_packet(
    rows: Sequence[Mapping[str, Any]], candidate_set_sha256: str
) -> str:
    sections = [
        "# Gold review packet",
        "",
        "This packet contains public short excerpts and proposed field-level evidence only.",
        "Reviewers record decisions independently in their assigned JSONL files.",
        "",
        f"Candidate set SHA-256: `{candidate_set_sha256}`",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        sections.extend(
            [
                f"## {index}. {html.escape(str(row['example_id']), quote=True)}",
                "",
                f"- Candidate fingerprint: `{candidate_fingerprint(row)}`",
                f"- Bank: {html.escape(str(row['bank_id']), quote=True)}",
                f"- Source URL: {html.escape(str(row['source_url']), quote=True)}",
                f"- Raw SHA-256: `{row['source_raw_sha256']}`",
                f"- Clean SHA-256: `{row['source_clean_sha256']}`",
                f"- Excerpt SHA-256: `{row['source_excerpt_sha256']}`",
                "",
                "### Public source excerpt",
                "",
                _indented_markdown(str(row["source_excerpt"])),
                "",
                "### Proposed fields",
                "",
                _indented_markdown(
                    json.dumps(
                        row["fields"], ensure_ascii=False, indent=2, sort_keys=True
                    )
                ),
                "",
                "### Evidence bindings",
                "",
            ]
        )
        for evidence in row["evidence"]:
            sections.extend(
                [
                    f"- Field: `{html.escape(str(evidence['field_pointer']), quote=True)}`",
                    f"  - Block: `{html.escape(str(evidence['block_id']), quote=True)}`",
                    f"  - Span: `{evidence['start_char']}:{evidence['end_char']}`",
                    f"  - Quote SHA-256: `{evidence['evidence_sha256']}`",
                    "  - Quote:",
                    _indented_markdown(str(evidence["quote"])),
                ]
            )
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _render_html_packet(
    rows: Sequence[Mapping[str, Any]], candidate_set_sha256: str
) -> str:
    candidates: list[str] = []
    for index, row in enumerate(rows, start=1):
        evidence_items = "".join(
            "<li>"
            f"<strong>{html.escape(str(binding['field_pointer']), quote=True)}</strong> "
            f"({binding['start_char']}:{binding['end_char']}, "
            f"<code>{binding['evidence_sha256']}</code>)"
            f"<pre>{html.escape(str(binding['quote']), quote=True)}</pre>"
            "</li>"
            for binding in row["evidence"]
        )
        fields_json = json.dumps(
            row["fields"], ensure_ascii=False, indent=2, sort_keys=True
        )
        candidates.append(
            "<article>"
            f"<h2>{index}. {html.escape(str(row['example_id']), quote=True)}</h2>"
            "<dl>"
            f"<dt>Candidate fingerprint</dt><dd><code>{candidate_fingerprint(row)}</code></dd>"
            f"<dt>Bank</dt><dd>{html.escape(str(row['bank_id']), quote=True)}</dd>"
            f"<dt>Source URL</dt><dd>{html.escape(str(row['source_url']), quote=True)}</dd>"
            f"<dt>Raw SHA-256</dt><dd><code>{row['source_raw_sha256']}</code></dd>"
            f"<dt>Clean SHA-256</dt><dd><code>{row['source_clean_sha256']}</code></dd>"
            f"<dt>Excerpt SHA-256</dt><dd><code>{row['source_excerpt_sha256']}</code></dd>"
            "</dl>"
            "<h3>Public source excerpt</h3>"
            f"<pre>{html.escape(str(row['source_excerpt']), quote=True)}</pre>"
            "<h3>Proposed fields</h3>"
            f"<pre>{html.escape(fields_json, quote=True)}</pre>"
            f"<h3>Evidence bindings</h3><ul>{evidence_items}</ul>"
            "</article>"
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Gold review packet</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:72rem;margin:auto;padding:2rem;}"
        "article{border-top:1px solid #bbb;margin-top:2rem;padding-top:1rem;}"
        "pre{white-space:pre-wrap;background:#f5f5f5;padding:1rem;overflow-wrap:anywhere;}"
        "code{overflow-wrap:anywhere;}dt{font-weight:700;}dd{margin-bottom:.5rem;}</style>"
        "</head><body><h1>Gold review packet</h1>"
        "<p>This packet contains public short excerpts and proposed field-level evidence only. "
        "Reviewers record decisions independently in their assigned JSONL files.</p>"
        f"<p>Candidate set SHA-256: <code>{candidate_set_sha256}</code></p>"
        f"{''.join(candidates)}</body></html>\n"
    )


def _review_filename(reviewer_id: str) -> str:
    return f"review-decisions-{_sha256_text(reviewer_id)[:16]}.jsonl"


def prepare_review(
    candidates_path: Path,
    output_dir: Path,
    reviewer_ids: Sequence[str],
) -> PrepareResult:
    """Create two independent decision templates and human-readable packets."""

    reviewers = _validate_reviewer_ids(reviewer_ids)
    rows = _load_jsonl(candidates_path, label="candidate gold JSONL")
    _validate_candidate_rows(rows)
    candidate_set_sha256 = _candidate_set_sha256(rows)
    decision_paths = {
        reviewer_id: output_dir / _review_filename(reviewer_id)
        for reviewer_id in reviewers
    }
    if len(set(decision_paths.values())) != 2:
        raise ReviewError("reviewer template filename collision")
    markdown_packet = output_dir / "review-packet.md"
    html_packet = output_dir / "review-packet.html"
    artifacts: dict[Path, str] = {
        decision_paths[reviewer_id]: _render_jsonl(
            [_decision_template(row, reviewer_id) for row in rows]
        )
        for reviewer_id in reviewers
    }
    artifacts[markdown_packet] = _render_markdown_packet(rows, candidate_set_sha256)
    artifacts[html_packet] = _render_html_packet(rows, candidate_set_sha256)

    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ReviewError(
            f"review output directory must be new: {output_dir}: {exc}"
        ) from exc
    created: list[Path] = []
    try:
        for path in sorted(artifacts, key=lambda item: item.name):
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                created.append(path)
                handle.write(artifacts[path])
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        with suppress(OSError):
            output_dir.rmdir()
        raise ReviewError(f"unable to create review artifacts: {exc}") from exc
    return PrepareResult(output_dir, decision_paths, markdown_packet, html_packet)


def _parse_rfc3339(value: object, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _RFC3339_PATTERN.fullmatch(value) is None:
        raise ReviewError(
            f"{label} must be an RFC 3339 timestamp with an explicit offset"
        )
    try:
        parsed = datetime.fromisoformat(
            value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        )
    except ValueError as exc:
        raise ReviewError(f"{label} is not a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewError(f"{label} must include a timezone offset")
    return value, parsed.astimezone(UTC)


def _validate_submitted_review(
    rows: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    *,
    label: str,
) -> _SubmittedReview:
    by_id: dict[str, Mapping[str, Any]] = {}
    reviewer_id: str | None = None
    for row_number, row in enumerate(rows, start=1):
        if set(row) != _DECISION_KEYS:
            raise ReviewError(
                f"{label} row {row_number} has missing or unexpected properties"
            )
        if (
            row.get("$schema") != SCHEMA_ID
            or row.get("schema_version") != SCHEMA_VERSION
        ):
            raise ReviewError(f"{label} row {row_number} has an unknown review schema")
        example_id = _valid_identifier(
            row.get("example_id"),
            label=f"{label} row {row_number} example_id",
            maximum=256,
        )
        if example_id in by_id:
            raise ReviewError(f"{label} contains duplicate example_id {example_id!r}")
        candidate = candidates.get(example_id)
        if candidate is None:
            raise ReviewError(f"{label} contains unknown example_id {example_id!r}")
        expected_fingerprint = candidate_fingerprint(candidate)
        if row.get("candidate_fingerprint") != expected_fingerprint:
            raise ReviewError(f"{label} {example_id} candidate fingerprint mismatch")
        for field in _SOURCE_HASH_FIELDS:
            _valid_sha256(row.get(field), label=f"{label} {example_id} {field}")
            if row[field] != candidate[field]:
                raise ReviewError(f"{label} {example_id} {field} mismatch")
        current_reviewer = _valid_identifier(
            row.get("reviewer_id"),
            label=f"{label} row {row_number} reviewer_id",
            maximum=128,
        )
        if reviewer_id is None:
            reviewer_id = current_reviewer
        elif current_reviewer != reviewer_id:
            raise ReviewError(f"{label} mixes more than one reviewer identity")
        if row.get("decision") not in {"approve", "reject"}:
            raise ReviewError(
                f"{label} {example_id} decision must be exactly approve or reject"
            )
        _parse_rfc3339(
            row.get("reviewed_at"), label=f"{label} {example_id} reviewed_at"
        )
        notes = row.get("review_notes")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ReviewError(
                f"{label} {example_id} review_notes must be at most 2000 characters"
            )
        by_id[example_id] = row
    if set(by_id) != set(candidates):
        missing = sorted(set(candidates) - set(by_id))
        extra = sorted(set(by_id) - set(candidates))
        raise ReviewError(
            f"{label} does not exactly cover candidates; missing={missing}, extra={extra}"
        )
    if reviewer_id is None:
        raise ReviewError(f"{label} has no reviewer identity")
    return _SubmittedReview(reviewer_id, by_id)


def _latest_reviewed_at(decisions: Sequence[Mapping[str, Any]]) -> str:
    timestamps = [
        _parse_rfc3339(
            decision["reviewed_at"], label=f"{decision['example_id']} reviewed_at"
        )
        for decision in decisions
    ]
    return max(timestamps, key=lambda item: (item[1], item[0]))[0]


def _output_paths_are_safe(inputs: Sequence[Path], outputs: Sequence[Path]) -> None:
    resolved_inputs = {path.resolve(strict=False) for path in inputs}
    resolved_outputs = [path.resolve(strict=False) for path in outputs]
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise ReviewError("merge output paths must be distinct")
    if any(path in resolved_inputs for path in resolved_outputs):
        raise ReviewError("merge output paths must not overwrite an input")
    for path in outputs:
        if path.exists():
            raise ReviewError(f"merge output already exists: {path}")


def _write_new_outputs(artifacts: Sequence[tuple[Path, str]]) -> None:
    for path, _ in artifacts:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReviewError(
                f"unable to create output directory for {path}: {exc}"
            ) from exc
    created: list[Path] = []
    try:
        for path, content in artifacts:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                created.append(path)
                handle.write(content)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise ReviewError(f"unable to create merge outputs: {exc}") from exc


def merge_reviews(
    candidates_path: Path,
    decision_paths: Sequence[Path],
    output_path: Path,
    audit_path: Path,
) -> MergeSummary:
    """Merge exactly two complete independent reviews into new, immutable outputs."""

    if len(decision_paths) != 2:
        raise ReviewError("merge requires exactly two decision files")
    if decision_paths[0].resolve(strict=False) == decision_paths[1].resolve(
        strict=False
    ):
        raise ReviewError("merge requires two distinct decision files")
    _output_paths_are_safe(
        (candidates_path, *decision_paths),
        (output_path, audit_path),
    )
    candidate_rows = _load_jsonl(candidates_path, label="candidate gold JSONL")
    candidates = _validate_candidate_rows(candidate_rows)
    submitted = tuple(
        _validate_submitted_review(
            _load_jsonl(path, label=f"decision JSONL {index}"),
            candidates,
            label=f"decision JSONL {index}",
        )
        for index, path in enumerate(decision_paths, start=1)
    )
    first_reviewer, second_reviewer = sorted(review.reviewer_id for review in submitted)
    reviewer_ids = (first_reviewer, second_reviewer)
    if reviewer_ids[0] == reviewer_ids[1]:
        raise ReviewError("merge requires two distinct reviewer IDs")

    counts = {"verified": 0, "pending": 0, "rejected": 0}
    merged_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    for candidate in candidate_rows:
        example_id = candidate["example_id"]
        decisions = [review.rows[example_id] for review in submitted]
        decision_values = [decision["decision"] for decision in decisions]
        if decision_values == ["approve", "approve"]:
            status = "verified"
            outcome = "verified"
            note = "Two independent reviewers approved the candidate and its evidence."
        elif decision_values == ["reject", "reject"]:
            status = "rejected"
            outcome = "rejected"
            note = "Two independent reviewers rejected the candidate or its evidence."
        else:
            status = "pending"
            outcome = "disagreement"
            note = "Independent reviewers disagreed; the candidate remains pending."
        counts[status] += 1
        merged = copy.deepcopy(candidate)
        merged["human_review"] = {
            "review_notes": note,
            "reviewed_at": _latest_reviewed_at(decisions),
            "reviewer_ids": list(reviewer_ids),
            "status": status,
        }
        merged_rows.append(merged)
        audit_rows.append(
            {
                "candidate_fingerprint": candidate_fingerprint(candidate),
                "decisions": sorted(
                    (
                        {
                            "decision": decision["decision"],
                            "review_notes": decision["review_notes"],
                            "reviewed_at": decision["reviewed_at"],
                            "reviewer_id": decision["reviewer_id"],
                        }
                        for decision in decisions
                    ),
                    key=lambda item: item["reviewer_id"],
                ),
                "example_id": example_id,
                "outcome": outcome,
                "source_hashes": {
                    field: candidate[field] for field in _SOURCE_HASH_FIELDS
                },
            }
        )

    output_content = _render_jsonl(merged_rows)
    candidate_set_sha256 = _candidate_set_sha256(candidate_rows)
    audit = {
        "schema_version": SCHEMA_VERSION,
        "candidate_gold_sha256": _sha256_text(output_content),
        "candidate_set_sha256": candidate_set_sha256,
        "counts": counts,
        "example_count": len(candidate_rows),
        "reviewer_ids": list(reviewer_ids),
        "rows": audit_rows,
    }
    audit_content = (
        json.dumps(audit, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    _write_new_outputs(((output_path, output_content), (audit_path, audit_content)))
    return MergeSummary(candidate_set_sha256, reviewer_ids, counts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="create independent review templates")
    prepare.add_argument("--candidates", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    prepare.add_argument("--reviewer-id", required=True, action="append")
    merge = commands.add_parser("merge", help="merge two completed review files")
    merge.add_argument("--candidates", required=True, type=Path)
    merge.add_argument("--decision", required=True, action="append", type=Path)
    merge.add_argument("--output", required=True, type=Path)
    merge.add_argument("--audit", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepared = prepare_review(
                arguments.candidates,
                arguments.output_dir,
                arguments.reviewer_id,
            )
            payload = {
                "decision_files": {
                    reviewer_id: str(path)
                    for reviewer_id, path in prepared.decision_paths.items()
                },
                "html_packet": str(prepared.html_packet),
                "markdown_packet": str(prepared.markdown_packet),
            }
        else:
            summary = merge_reviews(
                arguments.candidates,
                arguments.decision,
                arguments.output,
                arguments.audit,
            )
            payload = {
                "candidate_set_sha256": summary.candidate_set_sha256,
                "counts": summary.counts,
                "reviewer_ids": list(summary.reviewer_ids),
            }
    except ReviewError as exc:
        print(f"gold-review error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
