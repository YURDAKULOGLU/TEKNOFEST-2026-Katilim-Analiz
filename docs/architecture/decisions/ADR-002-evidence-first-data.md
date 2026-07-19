# ADR-002: Evidence-First Data

- Status: accepted
- Date: 2026-07-18

## Decision

Represent the pipeline as immutable, hash-addressed source and cleaning artefacts followed by candidate and independently validated records. Every extracted field stores raw value, normalized value/unit, source quote and offsets, method, confidence/status, and extractor/model/prompt/schema version.

A cleaned document's identity is derived only from its canonical cleaned-content
hash. The individual fetch that observed that content, the cleaning time, and the
cleaner version remain immutable observation metadata on the fetch-to-document
link. Re-observing identical content therefore records a new observation without
creating a second semantic document. A campaign version is reused only when its
validated semantic record hash is also unchanged; a legitimate review-status or
extractor-version change remains versionable even over the same source content.

Campaigns are versioned by source and observation time. Missing is never converted to zero. Explicit “no fee” may normalize to zero only with evidence. Historical and current validity are not conflated.

## Consequences

- Review and regression failures are reproducible.
- The UI can expose freshness, ambiguity, and evidence.
- Storage is larger than a flat campaign table, but remains modest for the
  PostgreSQL deployment selected by ADR-008.
- W3C PROV concepts guide naming; RDF and a graph database are unnecessary.
