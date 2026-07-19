from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    CleanDocument,
    ComparisonContext,
    EvidenceRef,
    EvidenceStatus,
    ExtractionCandidate,
    ExtractionMetadata,
    ExtractionMethod,
    FetchArtifact,
    FetchStatus,
    MoneyValue,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SourceBlock,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.storage.serialization import canonical_sha256

NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
BLOCK_TEXT = "Aylık kâr payı oranı %1,99 ve finansman tutarı 100.000 TL."
QUOTE = "%1,99"


def fetch_artifact(*, suffix: str = "1", fetched_at: datetime = NOW) -> FetchArtifact:
    raw_hash = hashlib.sha256(BLOCK_TEXT.encode()).hexdigest()
    return FetchArtifact(
        id=f"fetch:{suffix}",
        bank_id="bank-a",
        requested_url="https://bank.example/kampanya",
        final_url="https://bank.example/kampanya",
        status=FetchStatus.SUCCESS,
        http_status=200,
        fetched_at=fetched_at,
        robots_allowed=True,
        content_type="text/html",
        raw_sha256=raw_hash,
        raw_size_bytes=len(BLOCK_TEXT.encode()),
        private_raw_path=f"{raw_hash}.html",
    )


def clean_document(fetch_id: str = "fetch:1") -> CleanDocument:
    text_hash = hashlib.sha256(BLOCK_TEXT.encode()).hexdigest()
    return CleanDocument(
        id=f"clean:{'a' * 64}",
        fetch_artifact_id=fetch_id,
        bank_id="bank-a",
        canonical_url="https://bank.example/kampanya",
        title="Kâr Payı Kampanyası",
        cleaned_at=NOW + timedelta(seconds=1),
        cleaner_version="html-blocks/1.0",
        clean_sha256="a" * 64,
        language="tr",
        blocks=[
            SourceBlock(
                id="block:1",
                ordinal=0,
                kind="paragraph",
                text=BLOCK_TEXT,
                locator="html > body > p",
                text_sha256=text_hash,
            )
        ],
    )


def evidence(*, quote: str = QUOTE) -> EvidenceRef:
    start = BLOCK_TEXT.index(QUOTE)
    return EvidenceRef(
        id="evidence:1",
        field_pointer="/rates/0/value_percent",
        source_document_id=f"clean:{'a' * 64}",
        block_id="block:1",
        quote=quote,
        start_char=start,
        end_char=start + len(quote),
        evidence_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        status=EvidenceStatus.STATED,
    )


def campaign_data() -> CampaignData:
    return CampaignData(
        bank_id="bank-a",
        title="Kâr Payı Kampanyası",
        summary="Uygun müşterilere finansman fırsatı",
        product_family=ProductFamily.FINANCING,
        campaign_type=CampaignType.FINANCING_RATE,
        rates=[
            RateValue(
                raw="%1,99",
                value_percent=Decimal("1.990000"),
                kind=RateKind.FINANCING_PROFIT_RATE,
                period=RatePeriod.MONTHLY,
            )
        ],
        financing_amounts=[MoneyValue(raw="100.000 TL", amount=Decimal("100000"), currency="TRY")],
        terms=[TermRange(raw="3-12 ay", minimum_months=3, maximum_months=12)],
        validity=ValidityWindow(
            raw="18-31 Temmuz 2026",
            starts_on=date(2026, 7, 18),
            ends_on=date(2026, 7, 31),
        ),
        comparison_context=ComparisonContext(product_currency="TRY"),
    )


def extraction_metadata() -> ExtractionMetadata:
    return ExtractionMetadata(
        method=ExtractionMethod.RULE,
        extractor_version="rules/1",
        schema_version="1.0",
        started_at=NOW + timedelta(seconds=2),
        completed_at=NOW + timedelta(seconds=3),
    )


def candidate() -> ExtractionCandidate:
    return ExtractionCandidate(
        id="candidate:1",
        source_document_id=f"clean:{'a' * 64}",
        data=campaign_data(),
        evidence=[evidence()],
        metadata=extraction_metadata(),
    )


def record() -> CampaignRecord:
    data = campaign_data()
    metadata = extraction_metadata()
    record_hash = canonical_sha256(
        {"source_document_id": f"clean:{'a' * 64}", "data": data, "extraction": metadata}
    )
    return CampaignRecord(
        id="record:1",
        version=1,
        source_document_id=f"clean:{'a' * 64}",
        observed_at=NOW + timedelta(seconds=4),
        data=data,
        evidence=[evidence()],
        extraction=metadata,
        status=RecordStatus.VALIDATED,
        record_sha256=record_hash,
    )
