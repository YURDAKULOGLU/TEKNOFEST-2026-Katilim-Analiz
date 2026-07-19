# ADR-008: Kubernetes and PostgreSQL V1 Baseline

- Status: accepted
- Date: 2026-07-18
- Supersedes: ADR-001 storage/deployment portions and ADR-007 deployment decision

## Context

The team explicitly chose to establish the institutional deployment foundation at project start instead of migrating persistence and orchestration after V1.

## Decision

Kubernetes is the primary V1 runtime and PostgreSQL is the only application database. The code remains a modular monolith, packaged once and executed in distinct roles:

- `api` Deployment: FastAPI, OpenAPI, and compiled React assets;
- `worker` Deployment: durable ingestion/extraction/outbox jobs from the same image;
- migration Job: Alembic schema migration under a PostgreSQL advisory lock;
- PostgreSQL StatefulSet and PVC in the local overlay;
- Ollama Deployment and model PVC in the fully-contained local overlay;
- Services, ConfigMaps, Secrets, NetworkPolicies, security contexts, probes, quotas, and resource requests/limits.

Persistence uses SQLAlchemy 2 async APIs, asyncpg, Alembic, explicit transactions, numeric/JSONB types, PostgreSQL full-text search, and `FOR UPDATE SKIP LOCKED` job leases. Arbitrary model-generated SQL is forbidden.

## Overlays

- `local`: single-node, conservative resources, single PostgreSQL/Ollama replicas, local PVCs, and loopback/port-forward access; designed for a laptop cluster.
- `institution`: independently scalable API and worker roles after their storage prerequisites are met, externally provided PostgreSQL/model endpoints or approved operators, externally managed Secrets, stricter NetworkPolicies, disruption budgets, and observability hooks. The initial overlay keeps one worker because private raw storage is `ReadWriteOnce`; multiple workers require a reviewed RWX or internal object-storage adapter.

No production credential is committed. Local secrets are generated from ignored environment files or explicitly marked development-only literals.

## Domain and process boundaries

Kubernetes does not dictate domain decomposition. API and worker are process roles around the same application/domain modules. A module becomes a separately versioned network service only when independent scaling, release cadence, security boundary, or ownership is demonstrated and recorded in a later ADR.

## Laptop/on-premise constraint

The local profile is a real Kubernetes deployment, not a claim of enterprise high availability. It uses one node and bounded resources. Offline operation requires preloaded image archives, a checksummed model artefact/PVC, and a cached/imported source corpus. Connected scraping remains an explicitly enabled mode.

## Consequences

- Database and orchestration contracts are established once rather than migrated later.
- V1 carries additional operational work: cluster bootstrap, manifests, migrations, secrets, backups, restore tests, and image/model packaging.
- PostgreSQL enables safe concurrent worker leases and future replicas.
- The project must prove that the Kubernetes profile remains affordable on the declared laptop hardware; unmeasured portability claims are prohibited.
