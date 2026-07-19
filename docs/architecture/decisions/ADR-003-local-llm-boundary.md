# ADR-003: Rule-First Local LLM Boundary

- Status: accepted
- Date: 2026-07-18

## Decision

Apply deterministic Turkish parsers and terminology rules first. A local LLM may fill unresolved candidate fields or create a constrained chat query plan. It receives clearly delimited untrusted content and returns only strict JSON matching a Pydantic/JSON Schema contract. Deterministic code validates schema, enum values, lengths, evidence presence, offsets, rate semantics, and allowlisted query operations.

The model has no tools, SQL, network, filesystem, or citation-authoring authority. Failure, timeout, malformed JSON, ambiguity, or unsupported evidence yields abstention/review—not a guessed value.

## Model profiles

The default laptop path is deterministic first and may call the configured local
model only for unresolved fields. The current quality profile uses the quantized
`qwen3.5:4b` model through Ollama, pins it in memory with `keep_alive=-1`, and
enforces a 120-second application deadline. A timeout produces an explicit
missing/ambiguous result; it never silently changes or invents data. Model names
remain configuration rather than domain logic. Smaller variants and fine-tuning
may be selected only through the versioned evaluation gates in
`docs/evaluation/acceptance-plan.md`.

## Evidence-aligned extraction contract amendment (2026-07-19)

Model size is not a remedy for an oversized or internally inconsistent output
contract. For unresolved extraction fields, the model proposes the field label
and a verbatim source quote. Deterministic code locates the quote, derives
character offsets, normalizes the value, and rejects missing, non-unique, or
contradictory alignment. Fuzzy alignment may suggest a human-review candidate
but cannot create accepted evidence.

Requests are split by field family and relevant sentence/block windows. The
model is not asked to repeat raw text, count Unicode offsets, or populate every
unresolved field in one response. Outcomes distinguish `extracted`,
`not_stated`, `ambiguous`, and `failed`; absence in the source is not treated as
an endless retry signal.

This adopts the source-grounding and deterministic alignment patterns described
by [Google LangExtract](https://github.com/google/langextract) without adding it
as a runtime framework. The current `qwen3.5:4b` remains the baseline until the
same human-audited Turkish banking set compares it against:

- an Apache-2.0 multilingual
  [GLiNER v2.1](https://huggingface.co/urchade/gliner_multi-v2.1) span/classification
  path on CPU; and
- the Apache-2.0 [NuExtract3](https://huggingface.co/numind/NuExtract3)
  schema-extraction path, with every result re-aligned and re-normalized by the
  existing evidence boundary.

GLiNER2 is not a release candidate until a Turkish-capable checkpoint or direct
gold-set evidence exists; its published multilingual checkpoint currently lists
French, English, Spanish, German, Italian, and Portuguese rather than Turkish.
Upstream or vendor benchmark numbers do not substitute for the project gold
set. A larger general model is considered only after contract, alignment, and
prompt defects are removed and residual semantic errors remain.

## Security basis

Scraped HTML is an indirect-prompt-injection source under OWASP LLM01. Separation, least privilege, structured output, provenance verification, resource limits, quarantine, logging, and adversarial regression reduce impact; the project does not claim prompt injection is “solved.”
