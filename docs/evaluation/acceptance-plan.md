# Evaluation-Driven Acceptance Plan

The numerical thresholds below are **project acceptance goals**, not claims that NIST, OWASP, BDDK, or TEKNOFEST mandate those numbers.

## Dataset split

- Gold examples are human-audited and versioned.
- Train/development/test splits are separated by bank, domain, and time where possible.
- Near-duplicate campaign variants cannot cross splits.
- A fact is correct only when both its semantic value and its evidence binding are correct.

## V1 blocking gates

| Pointer | Measure | Pass condition |
|---|---|---|
| EVAL-001 | Bank coverage | All ten observed BDDK banks have `success`, `not_found`, or `unreachable` status with timestamp and reason. |
| EVAL-002 | Collection policy | Allowlist, robots, per-host rate limit, retry/backoff, cache/hash, size limit, and no-control-bypass tests pass. |
| EVAL-003 | Deterministic normalization | Curated Turkish money, percentage, date, and term variants pass 100%. |
| EVAL-004 | Core extraction | Field-level macro precision, recall, and F1 are each at least 0.90 on held-out gold data. |
| EVAL-005 | Evidence and schema | Schema validity 100%; every non-null core fact has locatable evidence; unsupported factual assertions 0. |
| EVAL-006 | Classification | Campaign/product classification macro-F1 at least 0.90. |
| EVAL-007 | Comparison | Golden cases pass 100%; incompatible rate kinds/periods/families are rejected. |
| EVAL-008 | Grounded chat | Query plans are allowlisted; every factual answer citation resolves; correct abstention at least 0.95. |
| EVAL-009 | API/UI | OpenAPI contract, filters, compare flow, chat flow, evidence view, keyboard path, and error states pass. |
| EVAL-010 | On-premise | A single-node Kubernetes cluster works with outbound network disabled after images/models are prepared; no paid/cloud API is called. |
| EVAL-012 | Licensing | Dependency/model/data inventory and SBOM have no unresolved incompatible or unknown runtime licence. |
| EVAL-013 | AI security | Versioned indirect-prompt-injection suite yields zero authority escape and zero unsupported fact acceptance. |
| EVAL-014 | Reproduction | A clean checkout can install, test, seed sample data, build, and run using documented commands. |

## Performance characterization

`EVAL-011` targets a 16 GB, CPU-only, single-node Kubernetes laptop profile. Initial targets are total steady-state RSS at or below 12 GiB and extraction p95 at or below 60 seconds per document. This is initially non-blocking because the result must be measured on controlled hardware; the release reports cluster runtime, CPU, RAM, model quantization, input size, p50/p95, and peak memory without generalizing beyond evidence.

### Current runtime evidence (not an EVAL-011 pass)

On 19 July 2026 the local Kind profile ran the pinned
`qwen3.5:4b@2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`
model through the deployed FastAPI → Ollama path on CPU. Ollama reported a
3.2 GB loaded model, 4096-token context, and an unlimited keep-alive.

- Positive staged case: the unresolved source span `12 ay vade` produced one
  accepted `term` fact with the exact quote and source offsets in 25.598 s.
- Negative selector case: `Vade Farksız 3 Taksit` was classified as an
  installment campaign without a model call in 0.088 s; installment count was
  not misrepresented as a financing term.
- The earlier verbose live response contract took 111.007 s for the same
  positive case. The minimal quote-only wire contract reduced one successful
  response from 124 generated tokens to 39 in a direct warm smoke and the final
  deployed API call completed in 25.598 s.

These are controlled regression smokes, not a representative sample. Hardware
inventory, frozen inputs, repeated runs, p50/p95, and peak process/cluster
memory remain required before changing `EVAL-011` from `proposed`.

## Fine-tuning decision gate

Fine-tuning is not a default work item. First compare the contract-corrected
rules plus `qwen3.5:4b` baseline, a multilingual GLiNER v2.1 CPU span classifier,
and a NuExtract3 structured-extraction challenger on the same held-out Turkish
banking gold set. A larger general model is an optional fourth arm only when the
smaller paths retain repeated semantic errors and the laptop resource budget
still holds. Selection uses project evidence-bound F1, unsupported-fact count,
invalid-output rate, p95 latency, and peak RSS—not upstream benchmark rank.

The 20 human-verified examples required to unblock the initial evaluation are
not, by themselves, authorization or sufficient evidence for fine-tuning. A
LoRA experiment is authorized only when a separately versioned train set and a
bank/time-isolated held-out test set are large enough to measure the targeted
residual error, and those errors are repeated semantic failures—not collection,
cleaning, parsing, evidence, alignment, or prompt defects—and prompt/rule
improvements have plateaued.

A tuned artefact can replace the base only if all hold:

1. held-out macro-F1 improves by at least `+0.03`;
2. the paired bootstrap 95% confidence-interval lower bound is above zero;
3. critical-field precision does not decline;
4. unsupported-assertion and successful-injection counts remain zero;
5. p95 latency and peak RAM regress by no more than 20%;
6. the final quantized artefact reruns the complete quality and security suite;
7. base model, adapter, dataset, settings, and licences are versioned and reversible.

## Enhancement gates

- `EVAL-015`: one content change creates exactly one durable notification, updates affected comparisons, and does not duplicate on an unchanged scan.
- `EVAL-016`: pinned Argon2id parameters, NFC/code-point password policy,
  whole-value blocklist, session rotation, CSRF, authorization, real Chromium
  Secure cookies on canonical localhost, concurrent rate limiting, lockout,
  redacted audit logging, restart persistence, create-once bootstrap and logout
  tests pass. This does not assert NIST AAL/FIPS certification.
- `EVAL-017`: the institutional overlay passes local health/readiness, OpenAPI
  webhook validation, CloudEvents 1.0.2 structured-mode contracts, exact retry
  identity, rolling restart, PostgreSQL persistence, least-privilege
  NetworkPolicy and offline startup. A real managed service, identity provider,
  endpoint and OpenShift cluster remain `not evaluated` until their actual
  contracts are supplied. This does not require a microservice decomposition.

## Primary control references

- [NIST AI RMF 1.0](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST Generative AI Profile AI 600-1](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)
- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/)
- [OWASP LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [OWASP AISVS](https://github.com/OWASP/AISVS)
- [JSON Schema 2020-12](https://json-schema.org/draft/2020-12)
- [OpenAPI 3.1.0](https://spec.openapis.org/oas/v3.1.0.html)
- [W3C PROV-O](https://www.w3.org/TR/prov-o/)
