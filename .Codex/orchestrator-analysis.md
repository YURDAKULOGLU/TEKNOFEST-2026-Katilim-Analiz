# Orchestrator Analysis

- Complexity: **10/10**
- Risk: **high** — financial-domain semantics, third-party web data, local LLM uncertainty, public deliverables, and an end-to-end demo
- Execution model: pointer-first, evaluation-driven, staged delivery
- Root responsibility: requirements, domain contracts, ADRs, dependency boundaries, integration, and final acceptance
- Subagent responsibility: bounded `WP-*` implementation only

## Critical path

1. Freeze official scope and current bank registry.
2. Freeze evidence-first data contracts and evaluation gates.
3. Build deterministic ingestion and normalization.
4. Add constrained local-LLM fallback for unresolved fields.
5. Persist versioned facts and provenance in PostgreSQL under ADR-008.
6. Expose whitelisted comparison and grounded-chat services.
7. Build the dashboard and evidence views.
8. Establish ten-bank coverage and run the gold evaluation.
9. Package and benchmark the offline/laptop profile.
10. Pass V1 before starting enhancements.

## Explicit cuts from V1

No microservice mesh, Redis, Kafka, Celery, vector database, generic agent framework, arbitrary SQL generation, AD, SMTP, OCR, or fine-tuning is part of V1. Kubernetes and PostgreSQL are explicit user-directed V1 baseline decisions under `ADR-008`; they do not imply premature domain-service decomposition.
