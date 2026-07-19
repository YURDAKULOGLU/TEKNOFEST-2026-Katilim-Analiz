# Dataset provenance and licence boundary

This directory contains team-authored schemas, annotations, evaluation cases, and derived structured facts. Apache-2.0 covers only those team-authored parts.

Official bank text remains third-party material. The repository therefore stores only canonical URLs, observation timestamps, hashes, structured derived fields, and short evidence excerpts needed to audit an annotation. Full HTML and full cleaned text stay under ignored `data/private/` storage and are not redistributed.

## Published sets

| Path | Content | Review state |
|---|---|---|
| `coverage/2026-07-18.json` | One explicit live state for each BDDK-listed participation bank | Collector observation |
| `derived/v0.1/` | Rule-only structured outputs and URL/hash provenance; no raw page text | Pending human review |
| `gold/v0.1/` | Four proposed annotations with short evidence and original block offsets | 0 verified, 4 pending |
| `normalization/v1/` | Team-authored Turkish format cases | Verified synthetic cases |
| `comparison/v1/` | Team-authored semantic comparison cases | Verified synthetic cases |
| `security/v1/` | Team-authored indirect prompt-injection cases | Verified synthetic cases; no model result yet |

No personal data, credentials, model weights, full third-party pages, or search-engine snippets are included. A source that is inaccessible, blocked, or absent is recorded explicitly and never replaced with fabricated content.

## Human-review rule

A proposed bank annotation becomes `verified` only after two distinct reviewers confirm the semantic value and evidence binding and add a review timestamp. The evaluator excludes every `pending` example from extraction and classification scores.
