# ADR-001: Modular Monolith

- Status: superseded by ADR-008
- Date: 2026-07-18

## Decision

This record originally selected Python/FastAPI/Pydantic, React/Vite/TypeScript, SQLite WAL/FTS5, and Ollama. The modular-monolith and UI/model boundaries remain valid; the SQLite and non-Kubernetes deployment decisions were superseded by the user's explicit Kubernetes/PostgreSQL direction in ADR-008.

## Why

This topology satisfies the on-premise and laptop goals with minimal operational parts while preserving explicit module contracts. OpenAPI 3.1 and JSON Schema 2020-12 define service and model-output contracts.

## Rejected for V1

Redis/Celery, Kafka, a vector database, Next.js/SSR, and generic agent frameworks remain rejected for V1. Kubernetes and PostgreSQL are now accepted by ADR-008.
