# ADR-005: Data and Licensing Boundary

- Status: accepted
- Date: 2026-07-18

## Decision

Keep fetched third-party HTML and full cleaned text in ignored, access-restricted local storage with retention controls. Publish only team-created code/schema/annotations and a derived dataset containing normalized facts, canonical source URLs, observation timestamps, hashes, status, and short necessary evidence snippets.

Apache-2.0 applies to team-owned project material, not to bank text or model weights. Each dependency, model, source, and dataset has a separate licence/provenance inventory. The collector observes `robots.txt`, domain terms, low per-host rates, caching, and access controls; it never bypasses authentication or CAPTCHA.

## Basis

The competition permits scraping as a collection method but does not transfer third-party copyright, database, website-term, personal-data, or access-control rights.
