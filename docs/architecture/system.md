# System Architecture

## Context

The product turns public participation-bank campaign and product pages into evidence-backed structured facts, comparable views, and grounded Turkish answers. It is an analysis aid, not financial advice and not a participation-principles compliance engine.

## Runtime topology

```text
Browser
  -> Kubernetes Service
       -> API Deployment (FastAPI + built React static files)
            -> PostgreSQL Service/managed endpoint
            -> private Ollama Service
       -> Worker Deployment (same application image)
            -> PostgreSQL durable job leases
            -> approved-source ingestion when explicitly enabled
PostgreSQL StatefulSet + PVC (local overlay)
Ollama Deployment + model PVC (local fully-contained overlay)
```

Development and reference deployment use a single-node Kubernetes cluster. The application remains a modular monolith in code while API and worker are separate process roles of the same immutable image. The institutional overlay can bind the same ports to managed PostgreSQL and an internal model service. No external network is required after images, model, and source corpus are prepared.

## Evidence-first flow

```text
allowlisted URL
  -> FetchArtifact(raw hash, policy result, time, status)
  -> CleanDocument(clean hash, text blocks, source offsets)
  -> deterministic candidates
  -> optional schema-constrained LLM candidates for unresolved fields
  -> evidence and semantic validation
  -> CampaignRecord + review issues
  -> versioned PostgreSQL facts/full-text index
  -> whitelisted comparison/query service
  -> dashboard/chat response with metadata-owned citations
```

The LLM cannot overwrite a deterministic fact silently. Conflicts become explicit review issues. Citations come from stored provenance, never from model-authored URLs or quotes.

## Module boundaries

- `domain`: immutable value objects, Turkish normalizers, comparability rules.
- `ingestion`: bank registry, crawl policy, fetch/import, cleaning, source snapshots.
- `extraction`: rule extractors, local-model adapter, candidate merge, evidence validation.
- `storage`: PostgreSQL/Alembic migrations, repositories, durable job leases, versioning, full-text search, and transactions.
- `application`: use cases, structured queries, comparison, grounded-answer assembly.
- `api`: OpenAPI HTTP boundary and static SPA serving.
- `web`: React dashboard, comparison workspace, assistant, coverage and evidence views.
- `notifications`, `auth`, `integrations`: post-V1 modules activated by later gates.

## Profiles

- `rules_only`: no model; deterministic extraction and fixture/demo operation.
- `laptop`: deterministic-first; the local 4B Q4 model is called only for unresolved
  fields and may abstain at the 120-second deadline.
- `workstation`: the same 4B quality profile is pinned in CPU memory. GPU execution
  is outside the current competition release. A larger model is only a future
  comparative eval, not a deployment dependency.

Profile claims remain benchmarks, not marketing assumptions.
The reusable base manifests fail closed to `rules_only`; the self-contained local
overlay explicitly enables `laptop` and keeps its CPU model warm. Institution
overlays must explicitly bind and enable their approved internal model service.

Dependency adoption and architectural stop-loss gates are defined in
[`dependency-strategy.md`](dependency-strategy.md). In particular, Kubernetes
packaging and later enhancements must not displace the verified source-to-answer
product chain from the critical path.

## Kubernetes platform contract

V1 is Kubernetes-first:

- one immutable OCI application image;
- environment/file-based configuration and secret injection;
- separate liveness and readiness endpoints;
- graceful termination and bounded job leases;
- versioned, idempotent migrations;
- persistent data/model volumes and no host-specific source paths;
- structured stdout logs with request, ingestion-run, and job correlation IDs;
- PostgreSQL/model/notification ports;
- versioned OpenAPI and domain-event contracts.

The local overlay supplies a single-replica PostgreSQL StatefulSet and an Ollama
Deployment, each with a PVC and conservative resource limits. The institutional
overlay expects managed/high-availability equivalents and can scale API/worker
roles independently. Domain modules are not split merely because process roles
run in Kubernetes.
