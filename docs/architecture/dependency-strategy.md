# Dependency and Architecture Stop-Loss Strategy

Date: 2026-07-19

## Objective

The product must prove one end-to-end claim before platform breadth becomes the
critical path:

```text
official source
  -> evidence-aligned structured fact
  -> semantically valid comparison
  -> grounded UI/chat answer
  -> repeatable human-verified evaluation
```

Kubernetes, PostgreSQL, authentication, notifications, and institutional ports
are useful only when this chain remains correct. They must not conceal a weak
information-extraction product behind a large delivery surface.

## Default architectural position

| Area | Decision | Reason |
|---|---|---|
| PostgreSQL + Alembic | Keep | One transactional source of truth for facts, provenance, jobs, sessions, and outbox records. |
| Kubernetes overlays | Keep, bound the effort | Required deployment target and useful portability boundary; it is not a reason to split the codebase. |
| Modular monolith | Keep | API and worker can scale as separate process roles from one image without distributed-system overhead. |
| Evidence-first raw/clean/derived stages | Keep | Matches established data-engineering practice and makes every model claim auditable. |
| Rules-first extraction | Keep | High-precision deterministic parsing is cheap, explainable, and suitable for stable financial expressions. |
| One large all-fields model call | Replace | It couples retrieval, extraction, quotation, offset calculation, normalization, and classification in one failure domain. |
| PostgreSQL full-text and typed queries | Keep | Current questions are structured comparison and filtered lookup; vector retrieval has no proven gap yet. |
| Custom bounded source adapters | Keep for ten sources | A generic crawler adds lifecycle and operational cost without solving CAPTCHA, policy, or semantic extraction. |
| Runtime microservices | Defer | Process-role separation already supplies an integration boundary. Split only after an independently scalable or governed workload is measured. |

## Target information-extraction pattern

The release path uses a cascade instead of forcing a small model to solve every
subproblem at once:

1. Preserve the official source artifact, clean blocks, tables, locators, and hashes.
2. Run deterministic candidate and terminology rules.
3. Select only relevant sentences or compact windows for unresolved field families.
4. Ask an optional CPU-local extractor for values and verbatim source quotes, not offsets.
5. Align quotes to the preserved source deterministically and reject unsupported text.
6. Normalize values and infer comparison semantics in deterministic domain code.
7. Record `extracted`, `not_stated`, `ambiguous`, or `failed` explicitly.
8. Route conflicts and low-confidence cases to human review.

This makes model replacement reversible and keeps evidence validation independent
of the model provider.

## Buy, borrow, or build decisions

| Capability | Current decision | Adoption gate |
|---|---|---|
| Human annotation/review | Start with a small versioned two-reviewer packet; trial [Label Studio](https://github.com/HumanSignal/label-studio) as a development-only profile when review volume makes the packet cumbersome. | Twenty verified examples only unblock the initial evaluator. Target 60-100 stratified records for a defensible model decision. Adopt the UI when it demonstrably reduces review errors or time; never make it a release runtime dependency. |
| Source-grounded extraction | Borrow the quote-first and deterministic-alignment patterns from [LangExtract](https://github.com/google/langextract); do not adopt the entire framework by default. | Adopt code or a dependency only if it preserves exact locators and improves the verified-gold result. Fuzzy matches may create review candidates but cannot auto-authorize evidence. |
| CPU span extraction | Evaluate [GLiNER multi-v2.1](https://huggingface.co/urchade/gliner_multi-v2.1) as a challenger. | Must beat the rules-only baseline on Turkish bank gold data under the laptop memory and latency budget. |
| Schema-focused extraction | Evaluate [NuExtract 3](https://huggingface.co/numind/NuExtract3) as a challenger after the staged contract is implemented. | Must add enough recall to justify its larger CPU cost; all claims are re-aligned to source evidence. |
| Generic web cleaning | Keep the current locator/table-preserving cleaner; evaluate [Trafilatura](https://github.com/adbar/trafilatura) only as a recall/noise challenger. | Must improve cleaned-text quality without losing tables, DOM location, source hashes, or evidence offsets. |
| Robots policy parsing | Evaluate [Protego](https://github.com/scrapy/protego) against the existing policy fixtures before extending the custom parser. | Adopt only if it passes all fail-closed policy cases and removes meaningful maintenance code without weakening source controls. |
| Turkish date parsing | Keep domain date rules; evaluate [dateparser](https://github.com/scrapinghub/dateparser) only on spans already identified as dates. | Strict settings must improve verified recall without introducing false positives or changing financial semantics. |
| Browser automation | Add a Playwright source adapter only for an allowlisted, policy-compliant JavaScript-only source. | It must unlock a real source that HTTP retrieval cannot access. It is never used to bypass CAPTCHA or access controls. |
| Generic crawling | Do not add Scrapy now. | Reconsider only if the monitored source count or crawl scheduling requirements exceed the bounded registry/adapters. |
| Experiment tracking | Keep versioned JSON profiles/results in Git; do not add MLflow yet. | Reconsider when concurrent experiments and artifact lineage can no longer be reviewed reliably in the repository. |
| Dataset orchestration | Keep JSON Schema, Pydantic validation, manifests, and Git-versioned derived/gold data; do not add DVC or Great Expectations yet. | Reconsider when datasets outgrow Git or cross-table quality rules exceed the current validators. |
| Agent/RAG frameworks | Do not add LangChain, LlamaIndex, a vector database, or an autonomous tool loop. | A specific failed acceptance test must first prove that typed query planning and grounded answer assembly cannot meet the requirement. |
| Chat intent semantics | Build a verified Turkish query set first; evaluate a small multilingual encoder such as [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) for intent/example matching on CPU. | Adopt only if it materially beats the keyword/rule baseline while producing the same typed query plans. It does not authorize free-form SQL or replace evidence-grounded answers. |
| Distributed messaging | Do not add Kafka, RabbitMQ, Redis, or Celery. | PostgreSQL jobs and transactional outbox remain sufficient until measured throughput or isolation requirements disprove that assumption. |

Model challengers are evaluation dependencies, not accepted runtime dependencies.
No model or model framework enters the release image merely because its upstream
benchmark is strong.

## CPU-only model policy

- The competition baseline never requires a GPU.
- The local model service may remain warm in CPU RAM, with one loaded model and
  one concurrent request.
- Rules-only operation remains a complete, explicit fallback rather than a hidden
  degraded state.
- A larger generic model is not the first response to extraction failures. First
  reduce the contract, improve context selection, and create verified gold data.
- Fine-tuning is considered only after the error taxonomy shows a repeated,
  learnable gap and the verified dataset is large enough to evaluate without
  contaminating the test split.

## Stop-loss gates

1. Do not declare V1 complete without human-verified extraction, classification,
   comparison, and unsupported-claim metrics.
2. Do not add another runtime service without naming the failed acceptance test it
   fixes and the simpler alternatives already measured.
3. Do not split a module into a microservice merely to satisfy an integration
   narrative; OpenAPI, CloudEvents, configuration, and deployment boundaries are
   the integration contract.
4. Do not auto-accept fuzzy evidence alignment or model-authored URLs/offsets.
5. Do not tune or replace the model before separating context selection,
   extraction, alignment, normalization, and semantic comparison failures.
6. Platform enhancements may proceed in bounded parallel work, but no new
   enhancement becomes the critical path while the core product gate is red.
7. Every adopted dependency must pass license inventory, offline installation,
   SBOM, clean-clone, resource-budget, and regression gates.

## Current critical path

1. Correct the known real-source extraction failures.
2. Build a representative review packet and obtain two independent human reviews.
3. Run the rules-only baseline and categorize errors by pipeline stage.
4. Implement the staged evidence-aligned model contract.
5. Evaluate CPU challengers on the same frozen split without using the GPU.
6. Build a verified Turkish intent/query set and benchmark typed semantic routing.
7. Adopt only the smallest component that produces a material, repeatable gain.
8. Finish notification, authentication, and institutional acceptance evidence after
   the core chain has a defensible score.
