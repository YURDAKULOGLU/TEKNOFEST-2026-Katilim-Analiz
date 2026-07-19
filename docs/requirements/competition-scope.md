# Competition Scope and Source of Truth

Observed first on **18 July 2026** and rechecked on **19 July 2026**. This
document separates official requirements, project acceptance targets, and
team-requested enhancements.

## Controlling official sources

- [TEKNOFEST 2026 AI Language Agents competition page](https://www.teknofest.org/tr/yarismalar/yapay-zeka-dil-ajanlari-yarismasi/)
- [TEKNOFEST extension announcement dated 13 July 2026](https://www.teknofest.org/tr/duyurular/teknofest-2026-yapay-zeka-dil-ajanlari-yarismasi-basvurulari-devam-ediyor/)
- [Scenario 2 official technical specification](https://cdn.teknofest.org/media/upload/userFormUpload/2026_TYDA_SARTNAME_Ikinci_Senaryo_TR_1_1IAJb.pdf)
- [Live BDDK participation-bank list](https://www.bddk.org.tr/Kurulus/Liste/77)
- [TKBB participation-finance glossary](https://tkbb.org.tr/katilim-sozluk)
- [Current BDDK participation-principles regulations index](https://www.bddk.gov.tr/Mevzuat/Liste/50)

The user's current KYS acceptance and official competition communication govern
operational dates. A direct recheck on 19 July showed **17 July 2026** on both
the live competition page and the official 13 July extension announcement. The
current 22-page Scenario 2 PDF also says 17 July in its schedule table, but its
participation section still says applications close on **12 July 2026**. The PDF
places preliminary results on **18 July**, while the live page says **19 July**;
the competition index also continued to label the event **Başvuru Aşaması**.
Therefore the live page/KYS must be rechecked before every operational deadline;
dates are not hard-coded into product logic.

## Official V1 scope

V1 covers all `REQ-*` pointers. In summary: collect public official content for every bank on the current BDDK list; clean Turkish campaign/product text; extract, classify, normalize, and structure facts; compare like with like; expose both dashboard and chatbot; operate fully locally with open-source components; and deliver reproducible code, derived data, documentation, slides, and demo material.

The current PDF also requires an accessible open-source GitHub repository,
Apache License 2.0 at competition end, complete dependency/run/data instructions,
the exact project marker `BilisimVadisi2026`, PDF and PPTX slides, a maximum
five-minute product video, and a four-minute jury presentation plus one-minute
demo-video cut. These are release artefacts, not optional polish.

The current observed bank list contains ten institutions. A bank remains represented even when no qualifying content is found or access is unavailable. The system records that state; it never invents a campaign.

## Evaluation weights

| Official criterion | Weight |
|---|---:|
| Model success and semantic understanding | 30% |
| Functionality and scope | 20% |
| Technical implementation and architecture | 20% |
| On-premise capability | 20% |
| Innovation and creativity | 10% |

## Not official V1 requirements

Login, notifications, AD, SMTP, OpenShift, Kubernetes, a microservice decomposition, a vector database, and fine-tuning are not stated V1 requirements. The team has nevertheless selected Kubernetes and PostgreSQL as explicit engineering baseline decisions in `ADR-008`. Login, notification, and additional institutional integration remain tracked separately by `ENH-*` pointers.

## Terminology guardrails

- Do not mechanically replace *faiz* with *kâr payı*.
- Separate financing price/profit rate from participation-account distributed profit.
- Never present a historical distributed-profit rate as a guaranteed future return.
- Preserve source term, canonical term, product mechanism, rate kind, period, and evidence.
- Report what the bank states; do not issue an independent participation-principles compliance verdict.

## Schedule and deliverable ambiguities

The public competition page and PDF contain inconsistent dates; the PDF also mentions both a maximum five-minute demo and a one-minute final demo. The release must therefore prepare a full five-minute cut and a one-minute cut, mark the schedule as KYS-controlled, and seek written clarification through official channels.
