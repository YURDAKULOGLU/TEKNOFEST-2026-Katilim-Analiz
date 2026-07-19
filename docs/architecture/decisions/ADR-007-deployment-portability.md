# ADR-007: Kubernetes-Compatible, Not Kubernetes-First

- Status: superseded by ADR-008
- Date: 2026-07-18

## Decision

This record originally kept Kubernetes outside the V1 baseline. The portability contracts remain useful, but the deployment decision was superseded by the user's explicit Kubernetes-first direction in ADR-008.

The original SQLite warning no longer applies because ADR-008 selects PostgreSQL from the start.

## Migration triggers

This former trigger list is retained as historical rationale only:

- an institution supplies a real cluster integration requirement;
- multi-node high availability is required;
- measured sustained concurrency exceeds the single-host profile;
- a separate operational team requires cluster-native rollout, policy, secret, and observability integration.

ADR-008 implements PostgreSQL and multi-worker-safe job leases in V1 instead of deferring them.

## V1.3 proof

V1 now proves the Kubernetes workload. V1.3 hardens the institutional overlay and managed-service integrations.
