"""Focused stdlib tests for the offline two-human gold review workflow."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools" / "gold_review.py"
SCHEMA_PATH = ROOT / "datasets" / "schemas" / "review-decision.schema.json"

SPEC = importlib.util.spec_from_file_location("gold_review", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {TOOL_PATH}")
gold_review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gold_review
SPEC.loader.exec_module(gold_review)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(example_id: str, *, excerpt: str | None = None) -> dict[str, Any]:
    source_excerpt = excerpt or f"{example_id} için %10 nakit iade"
    quote = "%10 nakit iade"
    return {
        "bank_id": "ornek-katilim",
        "duplicate_group": f"duplicate-{example_id}",
        "evidence": [
            {
                "block_id": f"block:{_sha256(example_id)}",
                "end_char": len(quote),
                "evidence_sha256": _sha256(quote),
                "field_pointer": "/data/campaign_type",
                "quote": quote,
                "start_char": 0,
            }
        ],
        "example_id": example_id,
        "fields": {"/data/campaign_type": "cashback"},
        "human_review": {
            "review_notes": "Machine proposal awaiting two independent reviewers.",
            "reviewed_at": None,
            "reviewer_ids": [],
            "status": "pending",
        },
        "observed_at": "2026-07-18T22:23:08+03:00",
        "source_clean_sha256": _sha256(f"clean:{example_id}"),
        "source_excerpt": source_excerpt,
        "source_excerpt_sha256": _sha256(source_excerpt),
        "source_raw_sha256": _sha256(f"raw:{example_id}"),
        "source_url": f"https://example.invalid/{example_id}",
        "split": "development",
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            f"{json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _submit(
    path: Path,
    decisions: dict[str, str],
    *,
    reviewed_at: str,
    reviewer_id: str | None = None,
) -> None:
    rows = _read_jsonl(path)
    for row in rows:
        row["decision"] = decisions[row["example_id"]]
        row["reviewed_at"] = reviewed_at
        row["review_notes"] = f"Reviewed {row['example_id']}"
        if reviewer_id is not None:
            row["reviewer_id"] = reviewer_id
    _write_jsonl(path, rows)


class GoldReviewTests(unittest.TestCase):
    def _prepare(
        self,
        directory: Path,
        rows: list[dict[str, Any]],
    ) -> tuple[Path, Any]:
        candidates = directory / "candidates.jsonl"
        _write_jsonl(candidates, rows)
        prepared = gold_review.prepare_review(
            candidates,
            directory / "prepared",
            ("reviewer-b", "reviewer-a"),
        )
        return candidates, prepared

    def test_prepare_is_deterministic_independent_and_escapes_untrusted_text(
        self,
    ) -> None:
        malicious_excerpt = "Kampanya <script>alert('x')</script> %10 nakit iade"
        rows = [_candidate("example-1", excerpt=malicious_excerpt)]
        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first = Path(first_raw)
            second = Path(second_raw)
            first_candidates = first / "candidates.jsonl"
            second_candidates = second / "candidates.jsonl"
            _write_jsonl(first_candidates, rows)
            _write_jsonl(second_candidates, rows)
            original = first_candidates.read_bytes()

            first_result = gold_review.prepare_review(
                first_candidates,
                first / "prepared",
                ("reviewer-b", "reviewer-a"),
            )
            second_result = gold_review.prepare_review(
                second_candidates,
                second / "prepared",
                ("reviewer-b", "reviewer-a"),
            )

            first_files = {
                path.relative_to(first_result.output_dir): path.read_bytes()
                for path in first_result.output_dir.iterdir()
            }
            second_files = {
                path.relative_to(second_result.output_dir): path.read_bytes()
                for path in second_result.output_dir.iterdir()
            }
            self.assertEqual(first_files, second_files)
            self.assertEqual(first_candidates.read_bytes(), original)
            self.assertEqual(
                set(first_result.decision_paths), {"reviewer-a", "reviewer-b"}
            )

            reviewer_rows = {
                reviewer_id: _read_jsonl(path)
                for reviewer_id, path in first_result.decision_paths.items()
            }
            for reviewer_id, decision_rows in reviewer_rows.items():
                self.assertEqual(decision_rows[0]["reviewer_id"], reviewer_id)
                self.assertIsNone(decision_rows[0]["decision"])
                self.assertIsNone(decision_rows[0]["reviewed_at"])
                self.assertEqual(
                    decision_rows[0]["candidate_fingerprint"],
                    gold_review.candidate_fingerprint(rows[0]),
                )
            self.assertNotEqual(
                first_result.decision_paths["reviewer-a"].read_bytes(),
                first_result.decision_paths["reviewer-b"].read_bytes(),
            )

            html_packet = first_result.html_packet.read_text(encoding="utf-8")
            markdown_packet = first_result.markdown_packet.read_text(encoding="utf-8")
            self.assertNotIn("<script>", html_packet)
            self.assertNotIn("<script>", markdown_packet)
            self.assertIn("&lt;script&gt;", html_packet)
            self.assertIn("&lt;script&gt;", markdown_packet)
            self.assertIn("/data/campaign_type", html_packet)

            with self.assertRaises(gold_review.ReviewError):
                gold_review.prepare_review(
                    first_candidates,
                    first_result.output_dir,
                    ("reviewer-b", "reviewer-a"),
                )

    def test_two_approvals_verify_without_overwriting_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            rows = [_candidate("example-1"), _candidate("example-2")]
            candidates, prepared = self._prepare(directory, rows)
            first = prepared.decision_paths["reviewer-a"]
            second = prepared.decision_paths["reviewer-b"]
            approvals = {row["example_id"]: "approve" for row in rows}
            _submit(first, approvals, reviewed_at="2026-07-19T10:00:00Z")
            _submit(second, approvals, reviewed_at="2026-07-19T14:00:00+03:00")
            original_inputs = {
                path: path.read_bytes() for path in (candidates, first, second)
            }
            output = directory / "gold-reviewed.jsonl"
            audit = directory / "gold-review-audit.json"

            summary = gold_review.merge_reviews(
                candidates,
                (second, first),
                output,
                audit,
            )
            ordered_output = directory / "gold-reviewed-ordered.jsonl"
            ordered_audit = directory / "gold-review-audit-ordered.json"
            gold_review.merge_reviews(
                candidates,
                (first, second),
                ordered_output,
                ordered_audit,
            )

            merged = _read_jsonl(output)
            self.assertEqual(
                [row["example_id"] for row in merged], ["example-1", "example-2"]
            )
            for row in merged:
                self.assertEqual(row["human_review"]["status"], "verified")
                self.assertEqual(
                    row["human_review"]["reviewer_ids"],
                    ["reviewer-a", "reviewer-b"],
                )
                self.assertEqual(
                    row["human_review"]["reviewed_at"],
                    "2026-07-19T14:00:00+03:00",
                )
            self.assertEqual(
                summary.counts, {"verified": 2, "pending": 0, "rejected": 0}
            )
            audit_payload = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(audit_payload["counts"], summary.counts)
            self.assertEqual(
                audit_payload["reviewer_ids"], ["reviewer-a", "reviewer-b"]
            )
            self.assertTrue(
                all(item["outcome"] == "verified" for item in audit_payload["rows"])
            )
            self.assertEqual(output.read_bytes(), ordered_output.read_bytes())
            self.assertEqual(audit.read_bytes(), ordered_audit.read_bytes())
            for path, content in original_inputs.items():
                self.assertEqual(path.read_bytes(), content)

    def test_disagreement_stays_pending_and_two_rejections_reject(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            rows = [_candidate("disagreement"), _candidate("rejected")]
            candidates, prepared = self._prepare(directory, rows)
            first = prepared.decision_paths["reviewer-a"]
            second = prepared.decision_paths["reviewer-b"]
            _submit(
                first,
                {"disagreement": "approve", "rejected": "reject"},
                reviewed_at="2026-07-19T10:00:00Z",
            )
            _submit(
                second,
                {"disagreement": "reject", "rejected": "reject"},
                reviewed_at="2026-07-19T11:00:00Z",
            )
            output = directory / "reviewed.jsonl"
            audit = directory / "audit.json"

            gold_review.merge_reviews(candidates, (first, second), output, audit)

            merged = {row["example_id"]: row for row in _read_jsonl(output)}
            self.assertEqual(
                merged["disagreement"]["human_review"]["status"], "pending"
            )
            self.assertEqual(merged["rejected"]["human_review"]["status"], "rejected")
            outcomes = {
                row["example_id"]: row["outcome"]
                for row in json.loads(audit.read_text(encoding="utf-8"))["rows"]
            }
            self.assertEqual(
                outcomes, {"disagreement": "disagreement", "rejected": "rejected"}
            )

    def test_merge_fails_closed_on_missing_or_malformed_rows(self) -> None:
        for case_name in ("missing", "malformed", "invalid-decision", "invalid-time"):
            with (
                self.subTest(case=case_name),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = Path(raw_directory)
                rows = [_candidate("example-1"), _candidate("example-2")]
                candidates, prepared = self._prepare(directory, rows)
                first = prepared.decision_paths["reviewer-a"]
                second = prepared.decision_paths["reviewer-b"]
                approvals = {row["example_id"]: "approve" for row in rows}
                _submit(first, approvals, reviewed_at="2026-07-19T10:00:00Z")
                _submit(second, approvals, reviewed_at="2026-07-19T11:00:00Z")
                if case_name == "missing":
                    _write_jsonl(second, _read_jsonl(second)[:1])
                elif case_name == "malformed":
                    second.write_text("{not-json}\n", encoding="utf-8")
                else:
                    decision_rows = _read_jsonl(second)
                    if case_name == "invalid-decision":
                        decision_rows[0]["decision"] = "Approve"
                    else:
                        decision_rows[0]["reviewed_at"] = "2026-07-19 11:00:00"
                    _write_jsonl(second, decision_rows)
                output = directory / "reviewed.jsonl"
                audit = directory / "audit.json"

                with self.assertRaises(gold_review.ReviewError):
                    gold_review.merge_reviews(
                        candidates, (first, second), output, audit
                    )
                self.assertFalse(output.exists())
                self.assertFalse(audit.exists())

    def test_merge_rejects_tampering_duplicate_identity_and_existing_output(
        self,
    ) -> None:
        for case_name in ("fingerprint", "source-hash", "identity", "existing-output"):
            with (
                self.subTest(case=case_name),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = Path(raw_directory)
                rows = [_candidate("example-1")]
                candidates, prepared = self._prepare(directory, rows)
                first = prepared.decision_paths["reviewer-a"]
                second = prepared.decision_paths["reviewer-b"]
                approvals = {"example-1": "approve"}
                _submit(first, approvals, reviewed_at="2026-07-19T10:00:00Z")
                _submit(second, approvals, reviewed_at="2026-07-19T11:00:00Z")
                if case_name in {"fingerprint", "source-hash"}:
                    decision_rows = _read_jsonl(second)
                    key = (
                        "candidate_fingerprint"
                        if case_name == "fingerprint"
                        else "source_clean_sha256"
                    )
                    decision_rows[0][key] = "0" * 64
                    _write_jsonl(second, decision_rows)
                elif case_name == "identity":
                    _submit(
                        second,
                        approvals,
                        reviewed_at="2026-07-19T11:00:00Z",
                        reviewer_id="reviewer-a",
                    )
                output = directory / "reviewed.jsonl"
                audit = directory / "audit.json"
                if case_name == "existing-output":
                    output.write_text("sentinel", encoding="utf-8")

                with self.assertRaises(gold_review.ReviewError):
                    gold_review.merge_reviews(
                        candidates, (first, second), output, audit
                    )
                if case_name == "existing-output":
                    self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")
                else:
                    self.assertFalse(output.exists())
                self.assertFalse(audit.exists())

    def test_prepare_rejects_non_pending_candidates_and_duplicate_reviewers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            row = _candidate("example-1")
            row["human_review"]["status"] = "verified"
            row["human_review"]["reviewer_ids"] = ["old-a", "old-b"]
            row["human_review"]["reviewed_at"] = "2026-07-18T12:00:00Z"
            candidates = directory / "candidates.jsonl"
            _write_jsonl(candidates, [row])

            with self.assertRaises(gold_review.ReviewError):
                gold_review.prepare_review(
                    candidates,
                    directory / "non-pending",
                    ("reviewer-a", "reviewer-b"),
                )
            with self.assertRaises(gold_review.ReviewError):
                gold_review.prepare_review(
                    directory / "missing.jsonl",
                    directory / "duplicate-reviewers",
                    ("reviewer-a", "reviewer-a"),
                )

    def test_review_decision_schema_has_template_and_submitted_states(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["decision"]["enum"],
            [None, "approve", "reject"],
        )
        self.assertIn("pattern", schema["properties"]["example_id"])
        self.assertIn("pattern", schema["properties"]["reviewer_id"])
        self.assertEqual(len(schema["oneOf"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
