from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from katilim_analiz.application.processing import (
    CleanOutcome,
    CleanResult,
    CollectionFetch,
    ExtractionProcessResult,
    ExtractionProcessState,
    IngestCampaignUseCase,
    JobDisposition,
    PersistenceResult,
    ProcessOutcome,
    ProcessSourceUseCase,
    SourceRequest,
)
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    CleanDocument,
    CoverageEntry,
    CoverageStatus,
    EvidenceRef,
    EvidenceStatus,
    ExtractionCandidate,
    ExtractionMetadata,
    ExtractionMethod,
    FetchArtifact,
    FetchStatus,
    ProductFamily,
    RecordStatus,
    SourceBlock,
)

NOW = datetime(2026, 7, 18, 19, 0, tzinfo=UTC)
SHA = "a" * 64
RAW = b"0123456789"
RAW_SHA = hashlib.sha256(RAW).hexdigest()
SOURCE = SourceRequest(
    bank_id="bank-a",
    bank_name="Banka A",
    source_url="https://example.test/campaign",
    campaign_key="bank-a:campaign",
    job_id="job:1",
)


def artifact(status: FetchStatus, *, error_code: str | None = None) -> FetchArtifact:
    success = status is FetchStatus.SUCCESS
    return FetchArtifact(
        id=f"fetch:{status.value}",
        bank_id="bank-a",
        requested_url="https://example.test/campaign",
        final_url="https://example.test/campaign" if success else None,
        status=status,
        http_status=200 if success else None,
        fetched_at=NOW,
        robots_allowed=status is not FetchStatus.BLOCKED,
        content_type="text/html" if success else None,
        raw_sha256=RAW_SHA if success else None,
        raw_size_bytes=10 if success else 0,
        private_raw_path="private/a.html" if success else None,
        error_code=error_code,
        error_detail=error_code,
    )


def document() -> CleanDocument:
    return CleanDocument(
        id="doc:a",
        fetch_artifact_id="fetch:success",
        bank_id="bank-a",
        canonical_url="https://example.test/campaign",
        cleaned_at=NOW,
        cleaner_version="test",
        clean_sha256=SHA,
        blocks=[
            SourceBlock(
                id="block:a",
                ordinal=0,
                kind="paragraph",
                text="Kampanya başlığı",
                locator="p",
                text_sha256=SHA,
            )
        ],
    )


def extraction(state: ExtractionProcessState) -> ExtractionProcessResult:
    metadata = ExtractionMetadata(
        method=ExtractionMethod.RULE,
        extractor_version="test",
        schema_version="1",
        started_at=NOW,
        completed_at=NOW,
    )
    quote = "Kampanya başlığı"
    evidence = EvidenceRef(
        id="evidence:a",
        field_pointer="/data/title",
        source_document_id="doc:a",
        block_id="block:a",
        quote=quote,
        start_char=0,
        end_char=len(quote),
        evidence_sha256=SHA,
        status=EvidenceStatus.STATED,
    )
    data = CampaignData(
        bank_id="bank-a",
        title="Kampanya başlığı",
        product_family=ProductFamily.UNKNOWN,
        campaign_type=CampaignType.UNKNOWN,
    )
    candidate = ExtractionCandidate(
        id="candidate:a",
        source_document_id="doc:a",
        data=data,
        evidence=[evidence],
        metadata=metadata,
    )
    record = CampaignRecord(
        id="record:a",
        version=1,
        source_document_id="doc:a",
        observed_at=NOW,
        data=data,
        evidence=[evidence],
        extraction=metadata,
        status=(
            RecordStatus.VALIDATED
            if state is ExtractionProcessState.VALIDATED
            else RecordStatus.NEEDS_REVIEW
        ),
        validation_issues=[] if state is ExtractionProcessState.VALIDATED else ["review"],
        record_sha256=SHA,
    )
    if state is ExtractionProcessState.ABSTAINED:
        return ExtractionProcessResult(state=state, issues=["title_unresolved"])
    return ExtractionProcessResult(
        state=state,
        candidate=candidate,
        record=record,
        issues=[] if state is ExtractionProcessState.VALIDATED else ["review"],
    )


class FakeCollection:
    def __init__(self, fetch: CollectionFetch, clean: CleanResult | None = None) -> None:
        self.fetch_result = fetch
        self.clean_result = clean
        self.clean_calls = 0

    async def fetch(self, source: SourceRequest) -> CollectionFetch:
        if self.fetch_result.artifact.bank_id != source.bank_id:
            return self.fetch_result.model_copy(
                update={
                    "artifact": self.fetch_result.artifact.model_copy(
                        update={
                            "id": f"fetch:{source.bank_id}",
                            "bank_id": source.bank_id,
                        }
                    )
                }
            )
        return self.fetch_result

    async def clean(self, fetched: CollectionFetch) -> CleanResult:
        self.clean_calls += 1
        assert self.clean_result is not None
        return self.clean_result


class FakeExtraction:
    def __init__(self, result: ExtractionProcessResult) -> None:
        self.result = result
        self.calls = 0

    async def extract_and_validate(
        self, source: SourceRequest, cleaned: CleanDocument
    ) -> ExtractionProcessResult:
        self.calls += 1
        return self.result


class FakeWrites:
    def __init__(self, *, record_created: bool | None = None, fail: bool = False) -> None:
        self.record_created = record_created
        self.fail = fail
        self.bundles = []

    async def persist(self, bundle):  # type: ignore[no-untyped-def]
        self.bundles.append(bundle)
        if self.fail:
            raise RuntimeError("persistence failed")
        observation = bundle.coverage_observation
        return PersistenceResult(
            coverage=CoverageEntry(
                bank_id=observation.bank_id,
                bank_name=observation.bank_name,
                observed_at=observation.observed_at,
                status=observation.status,
                source_count=1,
                campaign_count=0 if bundle.record is None else 1,
                reason=observation.reason,
            ),
            record_id=None if bundle.record is None else bundle.record.id,
            record_created=self.record_created,
        )


class FakeJobOutcomes:
    def __init__(self) -> None:
        self.results = []

    async def record(self, result):  # type: ignore[no-untyped-def]
        self.results.append(result)


def use_case(
    fetch: CollectionFetch,
    *,
    clean: CleanResult | None = None,
    extracted: ExtractionProcessResult | None = None,
    record_created: bool | None = None,
    persist_fail: bool = False,
):  # type: ignore[no-untyped-def]
    collection = FakeCollection(fetch, clean)
    extraction_port = FakeExtraction(extracted or extraction(ExtractionProcessState.ABSTAINED))
    writes = FakeWrites(record_created=record_created, fail=persist_fail)
    outcomes = FakeJobOutcomes()
    case = ProcessSourceUseCase(
        collection=collection,
        extraction=extraction_port,
        writes=writes,
        job_outcomes=outcomes,
    )
    return case, collection, extraction_port, writes, outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code", "outcome", "coverage", "job"),
    [
        (
            FetchStatus.BLOCKED,
            "robots_denied",
            ProcessOutcome.BLOCKED,
            CoverageStatus.BLOCKED,
            JobDisposition.SUCCEEDED,
        ),
        (
            FetchStatus.NOT_MODIFIED,
            None,
            ProcessOutcome.NOT_MODIFIED,
            CoverageStatus.SUCCESS,
            JobDisposition.SUCCEEDED,
        ),
        (
            FetchStatus.FAILED,
            "transport_error",
            ProcessOutcome.FETCH_FAILED,
            CoverageStatus.UNREACHABLE,
            JobDisposition.RETRY,
        ),
    ],
)
async def test_fetch_terminal_states_map_to_explicit_coverage_and_job_outcome(
    status: FetchStatus,
    error_code: str | None,
    outcome: ProcessOutcome,
    coverage: CoverageStatus,
    job: JobDisposition,
) -> None:
    case, collection, extraction_port, writes, job_outcomes = use_case(
        CollectionFetch(
            artifact=artifact(status, error_code=error_code),
            retryable=status is FetchStatus.FAILED,
        )
    )

    result = await case.execute(SOURCE)

    assert result.outcome is outcome
    assert result.job_disposition is job
    assert collection.clean_calls == 0
    assert extraction_port.calls == 0
    assert writes.bundles[0].coverage_observation.status is coverage
    assert job_outcomes.results == [result]


@pytest.mark.asyncio
async def test_clean_failure_is_persisted_as_partial_without_extraction() -> None:
    case, _, extraction_port, writes, _ = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.SUCCESS), raw_content=RAW),
        clean=CleanResult(
            outcome=CleanOutcome.FAILED,
            error_code="empty_document",
            detail="cleaned document has no blocks",
        ),
    )

    result = await case.execute(SOURCE)

    assert result.outcome is ProcessOutcome.CLEAN_FAILED
    assert result.job_disposition is JobDisposition.FAILED
    assert extraction_port.calls == 0
    assert writes.bundles[0].coverage_observation.status is CoverageStatus.PARTIAL


@pytest.mark.asyncio
async def test_extraction_abstention_is_honest_partial_result() -> None:
    case, _, _, writes, _ = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.SUCCESS), raw_content=RAW),
        clean=CleanResult(outcome=CleanOutcome.DOCUMENT, document=document()),
        extracted=extraction(ExtractionProcessState.ABSTAINED),
    )

    result = await case.execute(SOURCE)

    assert result.outcome is ProcessOutcome.EXTRACTION_ABSTAINED
    assert result.job_disposition is JobDisposition.SUCCEEDED
    assert writes.bundles[0].candidate is None
    assert writes.bundles[0].coverage_observation.status is CoverageStatus.PARTIAL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created", "expected"),
    [(True, ProcessOutcome.RECORD_CREATED), (False, ProcessOutcome.RECORD_UNCHANGED)],
)
async def test_validated_record_maps_created_and_idempotent_persistence(
    created: bool, expected: ProcessOutcome
) -> None:
    case, _, _, writes, _ = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.SUCCESS), raw_content=RAW),
        clean=CleanResult(outcome=CleanOutcome.DOCUMENT, document=document()),
        extracted=extraction(ExtractionProcessState.VALIDATED),
        record_created=created,
    )

    result = await case.execute(SOURCE)

    assert result.outcome is expected
    assert result.job_disposition is JobDisposition.SUCCEEDED
    assert writes.bundles[0].coverage_observation.status is CoverageStatus.SUCCESS
    assert result.coverage.campaign_count == 1


@pytest.mark.asyncio
async def test_needs_review_replay_is_reported_as_unchanged() -> None:
    case, _, _, writes, _ = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.SUCCESS), raw_content=RAW),
        clean=CleanResult(outcome=CleanOutcome.DOCUMENT, document=document()),
        extracted=extraction(ExtractionProcessState.NEEDS_REVIEW),
        record_created=False,
    )

    result = await case.execute(SOURCE)

    assert result.outcome is ProcessOutcome.RECORD_UNCHANGED
    assert result.job_disposition is JobDisposition.SUCCEEDED
    assert writes.bundles[0].coverage_observation.status is CoverageStatus.PARTIAL
    assert writes.bundles[0].coverage_observation.reason == "review_required"


@pytest.mark.asyncio
async def test_unexpected_persistence_error_propagates_and_job_is_not_falsely_completed() -> None:
    case, _, _, _, outcomes = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.SUCCESS), raw_content=RAW),
        clean=CleanResult(outcome=CleanOutcome.DOCUMENT, document=document()),
        extracted=extraction(ExtractionProcessState.VALIDATED),
        persist_fail=True,
    )

    with pytest.raises(RuntimeError, match="persistence failed"):
        await case.execute(SOURCE)

    assert outcomes.results == []


@pytest.mark.asyncio
async def test_batch_use_case_preserves_source_order() -> None:
    case, *_ = use_case(
        CollectionFetch(artifact=artifact(FetchStatus.NOT_MODIFIED)),
    )
    second = SOURCE.model_copy(update={"bank_id": "bank-b", "bank_name": "Banka B"})

    result = await IngestCampaignUseCase(case).execute([SOURCE, second])

    assert [item.bank_id for item in result.results] == ["bank-a", "bank-b"]
