# Offline two-human gold review

This WP-070 workflow turns pending machine-proposed annotations into auditable
human decisions. It is a local artifact process, not a runtime service. It uses
only the public short excerpts, proposed fields, evidence bindings, and source
hashes already present in the candidate JSONL. It never reads private HTML,
contacts a model, or changes the current gold examples or manifest.

## Invariants

- Exactly two explicitly named, distinct people review every candidate.
- Reviewers receive independent JSONL files and do not share decisions before
  merge.
- A decision is exactly `approve` or `reject` and has an RFC 3339 timestamp with
  an explicit timezone.
- Candidate fingerprints cover the whole candidate row except mutable
  `human_review` state. Raw, clean, and excerpt hashes are also repeated in each
  decision row.
- Merge recomputes and compares every fingerprint and source hash. Missing,
  extra, duplicate, malformed, stale, or partially completed rows fail the
  whole merge.
- Only two approvals of the identical candidate fingerprint produce
  `verified`. Two rejections produce `rejected`; disagreement remains
  `pending`.
- Input and existing output files are never overwritten. A successful merge
  creates a new candidate gold JSONL and a separate audit JSON.
- `prepare` accepts only unreviewed `pending` rows. Re-review or adjudication
  starts from a deliberately created new candidate file rather than erasing
  earlier reviewer activity.

Reviewer IDs are supplied by the operator and copied exactly. The tool does not
derive identity from a filename, account, environment variable, or workstation.
Reviewer and example IDs must not contain control characters or surrounding
whitespace.

## 1. Prepare an independent batch

Run from the repository root:

```powershell
python tools/gold_review.py prepare `
  --candidates datasets/gold/v0.1/examples.jsonl `
  --output-dir artifacts/gold-review/batch-001 `
  --reviewer-id reviewer-a `
  --reviewer-id reviewer-b
```

The output directory must not already exist. It contains:

- two `review-decisions-<reviewer-id-hash>.jsonl` templates;
- `review-packet.md`;
- `review-packet.html`.

The hash in a template filename prevents unsafe or ambiguous path construction;
the authoritative identity is the exact `reviewer_id` inside every row. Both
packets contain the same public review material and no decisions. The HTML is
self-contained and has no scripts or external resources.

Give each reviewer only their assigned decision JSONL plus either packet. For
every row, the reviewer changes:

```json
{
  "decision": "approve",
  "reviewed_at": "2026-07-19T14:30:00+03:00",
  "review_notes": "The proposed value and exact quote agree."
}
```

The snippet shows only editable properties, not a complete row. Do not change
`reviewer_id`, `example_id`, fingerprints, source hashes, schema fields, or row
coverage. Approve only when every proposed non-null field has the correct
semantic value and exact evidence binding. Otherwise reject and explain the
specific defect in `review_notes`.

## 2. Merge the completed reviews

```powershell
python tools/gold_review.py merge `
  --candidates datasets/gold/v0.1/examples.jsonl `
  --decision artifacts/gold-review/batch-001/review-decisions-REVIEWER_A_HASH.jsonl `
  --decision artifacts/gold-review/batch-001/review-decisions-REVIEWER_B_HASH.jsonl `
  --output artifacts/gold-review/batch-001/gold-reviewed.jsonl `
  --audit artifacts/gold-review/batch-001/gold-review-audit.json
```

Replace the example filenames with those printed by `prepare`. Both output paths
must be new and distinct from every input path. Validation completes before
either output is written. If an I/O error occurs while creating the pair, the
tool removes any output it created in that failed attempt.

The audit JSON records the candidate-set digest, output digest, exact reviewer
IDs, per-row fingerprints and source hashes, both submitted decisions and
timestamps, and aggregate `verified`, `pending`, and `rejected` counts. It has no
generated timestamp or machine-specific path, so identical inputs produce
identical outputs.

Updating a versioned gold manifest or promoting the new JSONL is a separate,
explicit release step. This tool intentionally does neither.

## Contract and checks

Decision rows follow
`datasets/schemas/review-decision.schema.json`. The merge command performs the
same security-relevant checks with the Python standard library and does not rely
on optional JSON Schema format validation.

Focused check:

```powershell
python -m pytest tools/tests/test_gold_review.py
```
