# ADR-006: Version Boundaries

- Status: accepted
- Date: 2026-07-18

## Decision

Complete and freeze specification-aligned V1 before enhancements:

1. `V1.1`: content-hash/version change detection, affected-comparison refresh, durable in-app notification/outbox.
2. `V1.2`: local administrator authentication and protected ingestion/review actions. AD remains an interface seam, not a fake integration.
3. `V1.3`: institutional managed-service, identity, event-consumer, high-availability, policy, and observability integration over the V1 Kubernetes baseline.

“Institutionally integrable” means a stable Kubernetes workload boundary and tested contracts. It does not mean pre-emptively decomposing the domain code into networked microservices.
