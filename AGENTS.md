# Pointer-First Collaboration Contract

This repository is coordinated by the root architect. Every implementation change must be traceable to a work-package pointer in `control/pointers.json`.

## Non-negotiable rules

1. Start each task by naming its `WP-*` pointer and reading its linked `REQ-*`, `ADR-*`, and `EVAL-*` pointers.
2. Edit only the paths listed in the work package's `owns` field. Shared contracts, architecture records, pointer files, dependency manifests, and database schemas are root-owned unless the assignment explicitly says otherwise.
3. Do not alter an `ADR-*` decision or a public schema implicitly. Raise the conflict to the root architect and wait for a new or superseding ADR.
4. Other agents work in the same checkout. Never revert, reset, overwrite, or reformat unrelated changes. Accommodate concurrent changes.
5. No fabricated bank data, campaign facts, scores, citations, benchmark results, or test results. Missing information is represented explicitly as `unknown`, `ambiguous`, `not_found`, or `unreachable`.
6. A non-null extracted fact needs field-level evidence that can be located in the cleaned source. Normalized values derive from that evidence; the model is never the source of a citation.
7. The LLM has no direct SQL, network, filesystem, or tool authority. It may only return a schema-constrained candidate that deterministic code validates.
8. Do not compare products with incompatible rate types, periods, product families, or eligibility contexts.
9. Never commit raw third-party HTML, private data, credentials, model weights, build output, or local databases.
10. Before handoff, run the work package's listed checks and report the exact command, result, changed files, known limitations, and pointer coverage.

## Completion evidence

An agent handoff is incomplete unless it includes:

- pointer ID;
- owned files changed;
- tests/checks run and their outcomes;
- requirements and evaluation gates covered;
- unresolved risks or assumptions;
- confirmation that unrelated files were not reverted.

## Version boundaries

- `V1` is the specification-aligned competition core.
- `V1.1` adds change detection and in-app notifications only after the V1 gate passes.
- `V1.2` adds local administration authentication only after V1.1 passes.
- `V1.3` adds institutional identity/event/managed-service integration contracts and high-availability hardening without prematurely splitting the modular monolith.
