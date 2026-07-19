# Data and Licence Boundary

The competition permits web scraping as a collection method; that permission is not a transfer of bank-site copyright, database rights, trademark rights, personal-data authority, or permission to bypass technical controls.

## Public repository

The repository may publish:

- team-authored code, schema, tests, documentation, and annotations under Apache-2.0;
- normalized campaign facts and classifications;
- canonical source URLs, retrieval timestamps, HTTP/coverage status, and content hashes;
- short evidence snippets only to the extent needed to verify a fact;
- synthetic or manually authored test fixtures clearly labelled as such.

## Private runtime storage

Full fetched HTML, full cleaned text, cache headers, and processing artefacts remain in ignored private storage/PVCs. They are not covered by the project licence and are excluded from the public dataset. Retention, deletion, and takedown procedures apply.

## Collection rules

- Official allowlisted public domains only.
- Respect RFC 9309 decisions, site terms, per-host delay, `Retry-After`, conditional requests, and byte/document quotas.
- Revalidate every redirect and block private, loopback, link-local, metadata, and non-HTTP(S) targets.
- Never bypass authentication, CAPTCHA, robots restrictions, or another technical control.
- Exclude unnecessary staff names, personal contact details, testimonials, and tracking identifiers.
- A missing or inaccessible campaign becomes an explicit coverage state; it is never fabricated.

## Primary references

- [Scenario 2 specification](https://cdn.teknofest.org/media/upload/userFormUpload/2026_TYDA_SARTNAME_Ikinci_Senaryo_TR_1_1IAJb.pdf)
- [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html)
- [KVKK processing principles](https://www.kvkk.gov.tr/Icerik/4189/Kisisel-Verilerin-Islenmesine-Iliskin-Temel-Ilkeler)
- [Fikir ve Sanat Eserleri Kanunu no. 5846](https://telifhaklari.ktb.gov.tr/Eklenti/106879%2Cfikir-ve-sanat-eserleri-kanunupdf.pdf?0=)
