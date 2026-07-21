"""Bounded source-processing use cases; worker loops and adapters live outside this module."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, HttpUrl, StringConstraints, field_validator, model_validator

from katilim_analiz.application.models import ApplicationModel
from katilim_analiz.contracts import (
    CampaignRecord,
    CleanDocument,
    CoverageEntry,
    CoverageStatus,
    ExtractionCandidate,
    FetchArtifact,
    FetchStatus,
    RecordStatus,
)

if TYPE_CHECKING:
    from katilim_analiz.application.ports import (
        CampaignWritePort,
        CollectionPort,
        ExtractionPort,
        JobOutcomePort,
    )

IssueText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def campaign_observation_key(
    scan_run_id: str,
    campaign_key: str,
    canonical_url: str,
) -> str:
    """Hash the stable scan, logical campaign, and canonical target identities."""

    fields = (scan_run_id.strip(), campaign_key.strip(), canonical_url.strip())
    if any(not field for field in fields):
        raise ValueError("campaign observation identity fields must be non-empty")
    material = "\0".join(fields)
    return hashlib.sha256(material.encode()).hexdigest()


class CleanOutcome(StrEnum):
    DOCUMENT = "document"
    FAILED = "failed"


class ExtractionProcessState(StrEnum):
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    ABSTAINED = "abstained"


class ProcessStage(StrEnum):
    FETCH = "fetch"
    CLEAN = "clean"
    EXTRACT = "extract"
    PERSIST = "persist"


class ProcessOutcome(StrEnum):
    BLOCKED = "blocked"
    NOT_MODIFIED = "not_modified"
    FETCH_FAILED = "fetch_failed"
    SKIPPED = "skipped"
    CLEAN_FAILED = "clean_failed"
    EXTRACTION_ABSTAINED = "extraction_abstained"
    REVIEW_REQUIRED = "review_required"
    RECORD_CREATED = "record_created"
    RECORD_UNCHANGED = "record_unchanged"


class JobDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY = "retry"


class SourceRequest(ApplicationModel):
    """One campaign source whose bank must already exist in the seeded BDDK registry."""

    bank_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
    ]
    bank_name: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    source_url: HttpUrl
    campaign_key: Annotated[str, StringConstraints(min_length=1, max_length=191)]
    job_id: Annotated[str, StringConstraints(min_length=1, max_length=191)] | None = None
    scan_run_id: Annotated[str, StringConstraints(min_length=1, max_length=191)] | None = None
    observation_key: Sha256Text | None = None
    #: Issue #33: the owner-curated label of a registry static product page
    #: (``konut``/``tasit``/``ihtiyac``). Source metadata, not page content;
    #: extraction may use it only to break a classification ambiguity.
    static_page_label: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]
        | None
    ) = None

    @model_validator(mode="after")
    def supplied_observation_identity_is_consistent(self) -> SourceRequest:
        if self.observation_key is None:
            return self
        expected = campaign_observation_key(
            self.require_scan_run_id(),
            self.campaign_key,
            str(self.source_url),
        )
        if self.observation_key != expected:
            raise ValueError("observation_key differs from scan, campaign, and canonical URL")
        return self

    def require_scan_run_id(self) -> str:
        """Resolve the scan identity, using only the retry-stable durable job ID as fallback."""

        scan_run_id = self.scan_run_id or self.job_id
        if scan_run_id is None:
            raise ValueError("record persistence requires scan_run_id or durable job_id")
        return scan_run_id

    def require_observation_key(self) -> str:
        expected = campaign_observation_key(
            self.require_scan_run_id(),
            self.campaign_key,
            str(self.source_url),
        )
        if self.observation_key is not None and self.observation_key != expected:
            raise ValueError("observation_key differs from its deterministic identity")
        return expected


class CollectionFetch(ApplicationModel):
    artifact: FetchArtifact
    raw_content: Annotated[bytes, Field(max_length=25_000_000)] | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def content_matches_artifact(self) -> CollectionFetch:
        if self.artifact.status is FetchStatus.SUCCESS:
            if self.raw_content is None:
                raise ValueError("successful collection requires raw content")
            if len(self.raw_content) != self.artifact.raw_size_bytes:
                raise ValueError("raw content size differs from fetch artifact")
            if hashlib.sha256(self.raw_content).hexdigest() != self.artifact.raw_sha256:
                raise ValueError("raw content hash differs from fetch artifact")
        elif self.raw_content is not None:
            raise ValueError("non-successful collection cannot expose raw content")
        if self.retryable and self.artifact.status not in {FetchStatus.FAILED}:
            raise ValueError("only failed collection may be marked retryable")
        return self


class CleanResult(ApplicationModel):
    outcome: CleanOutcome
    document: CleanDocument | None = None
    error_code: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    detail: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def state_is_complete(self) -> CleanResult:
        if self.outcome == CleanOutcome.DOCUMENT:
            if (
                self.document is None
                or self.error_code is not None
                or self.detail is not None
                or self.retryable
            ):
                raise ValueError("document cleaning result has inconsistent fields")
        elif self.outcome == CleanOutcome.FAILED:
            if self.document is not None or self.error_code is None:
                raise ValueError("failed cleaning result requires an error code only")
        else:
            raise ValueError("unknown cleaning outcome")
        return self


class ExtractionProcessResult(ApplicationModel):
    state: ExtractionProcessState
    candidate: ExtractionCandidate | None = None
    record: CampaignRecord | None = None
    issues: Annotated[list[IssueText], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode="after")
    def state_is_complete(self) -> ExtractionProcessResult:
        if self.state == ExtractionProcessState.ABSTAINED:
            if self.candidate is not None or self.record is not None or not self.issues:
                raise ValueError("abstention requires issues and cannot contain a record")
            return self
        if self.state not in {
            ExtractionProcessState.VALIDATED,
            ExtractionProcessState.NEEDS_REVIEW,
        }:
            raise ValueError("unknown extraction process state")
        if self.candidate is None or self.record is None:
            raise ValueError("non-abstaining extraction requires candidate and record")
        expected_status = (
            RecordStatus.VALIDATED
            if self.state == ExtractionProcessState.VALIDATED
            else RecordStatus.NEEDS_REVIEW
        )
        if self.record.status is not expected_status:
            raise ValueError("record status differs from extraction process state")
        if (
            self.record.source_document_id != self.candidate.source_document_id
            or self.record.data != self.candidate.data
            or self.record.evidence != self.candidate.evidence
            or self.record.extraction != self.candidate.metadata
        ):
            raise ValueError("record does not derive from the validated extraction candidate")
        return self


class CoverageObservation(ApplicationModel):
    """A status observation; transactional storage derives truthful aggregate counts."""

    bank_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
    ]
    bank_name: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    observed_at: datetime
    status: CoverageStatus
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coverage observation time must include a timezone offset")
        return value


class PersistenceBundle(ApplicationModel):
    source: SourceRequest
    fetch_artifact: FetchArtifact
    clean_document: CleanDocument | None = None
    candidate: ExtractionCandidate | None = None
    record: CampaignRecord | None = None
    coverage_observation: CoverageObservation

    @model_validator(mode="after")
    def provenance_chain_is_consistent(self) -> PersistenceBundle:
        if self.fetch_artifact.bank_id != self.source.bank_id:
            raise ValueError("fetch artifact belongs to another bank")
        if self.fetch_artifact.requested_url != self.source.source_url:
            raise ValueError("fetch artifact belongs to another source URL")
        if self.coverage_observation.bank_id != self.source.bank_id:
            raise ValueError("coverage belongs to another bank")
        if self.coverage_observation.bank_name != self.source.bank_name:
            raise ValueError("coverage bank name differs from the seeded source request")
        if self.clean_document is not None:
            if self.clean_document.fetch_artifact_id != self.fetch_artifact.id:
                raise ValueError("clean document belongs to another fetch")
            if self.clean_document.bank_id != self.source.bank_id:
                raise ValueError("clean document belongs to another bank")
        if self.candidate is not None:
            if self.clean_document is None:
                raise ValueError("candidate requires a clean document")
            if self.candidate.source_document_id != self.clean_document.id:
                raise ValueError("candidate belongs to another document")
        if self.record is not None:
            if self.candidate is None or self.clean_document is None:
                raise ValueError("record requires candidate and clean document")
            if self.record.source_document_id != self.clean_document.id:
                raise ValueError("record belongs to another document")
        return self


class PersistenceResult(ApplicationModel):
    coverage: CoverageEntry
    record_id: Annotated[str, StringConstraints(min_length=1, max_length=191)] | None = None
    record_created: bool | None = None

    @model_validator(mode="after")
    def record_result_is_consistent(self) -> PersistenceResult:
        if (self.record_id is None) != (self.record_created is None):
            raise ValueError("record ID and created state must be returned together")
        return self


class ProcessSourceResult(ApplicationModel):
    job_id: str | None
    bank_id: str
    terminal_stage: ProcessStage
    outcome: ProcessOutcome
    job_disposition: JobDisposition
    coverage: CoverageEntry
    record_id: str | None = None
    issues: list[IssueText] = Field(default_factory=list, max_length=100)


class BatchProcessResult(ApplicationModel):
    results: list[ProcessSourceResult] = Field(max_length=100)


def _bounded_reason(code: str, detail: str | None = None) -> str:
    value = code if not detail else f"{code}:{detail}"
    return value[:500]


def _observation(
    source: SourceRequest,
    fetched: FetchArtifact,
    *,
    status: CoverageStatus,
    reason: str | None,
) -> CoverageObservation:
    return CoverageObservation(
        bank_id=source.bank_id,
        bank_name=source.bank_name,
        observed_at=fetched.fetched_at,
        status=status,
        reason=reason,
    )


class ProcessSourceUseCase:
    def __init__(
        self,
        *,
        collection: CollectionPort,
        extraction: ExtractionPort,
        writes: CampaignWritePort,
        job_outcomes: JobOutcomePort,
    ) -> None:
        self._collection = collection
        self._extraction = extraction
        self._writes = writes
        self._job_outcomes = job_outcomes

    async def execute(self, source: SourceRequest) -> ProcessSourceResult:
        fetched = await self._collection.fetch(source)
        artifact = fetched.artifact
        if artifact.status is not FetchStatus.SUCCESS:
            return await self._finish_fetch_state(source, fetched)

        cleaned = await self._collection.clean(fetched)
        if cleaned.outcome == CleanOutcome.FAILED:
            coverage = _observation(
                source,
                artifact,
                status=CoverageStatus.PARTIAL,
                reason=_bounded_reason(cleaned.error_code or "clean_failed"),
            )
            return await self._persist_and_record(
                source=source,
                fetched=artifact,
                clean_document=None,
                extracted=None,
                coverage=coverage,
                terminal_stage=ProcessStage.CLEAN,
                outcome=ProcessOutcome.CLEAN_FAILED,
                disposition=(JobDisposition.RETRY if cleaned.retryable else JobDisposition.FAILED),
                issues=(() if cleaned.detail is None else (cleaned.detail,)),
            )

        document = cleaned.document
        if document is None:
            raise ValueError("collection adapter returned document state without a document")
        if document.fetch_artifact_id != artifact.id:
            raise ValueError("collection adapter returned a document for another fetch")
        extracted = await self._extraction.extract_and_validate(source, document)
        if extracted.state == ExtractionProcessState.ABSTAINED:
            coverage = _observation(
                source,
                artifact,
                status=CoverageStatus.PARTIAL,
                reason=_bounded_reason("extraction_abstained", extracted.issues[0]),
            )
            return await self._persist_and_record(
                source=source,
                fetched=artifact,
                clean_document=document,
                extracted=extracted,
                coverage=coverage,
                terminal_stage=ProcessStage.EXTRACT,
                outcome=ProcessOutcome.EXTRACTION_ABSTAINED,
                disposition=JobDisposition.SUCCEEDED,
                issues=tuple(extracted.issues),
            )

        if extracted.record is None or extracted.candidate is None:
            raise ValueError("extraction adapter returned an incomplete record result")
        needs_review = extracted.state == ExtractionProcessState.NEEDS_REVIEW
        coverage = _observation(
            source,
            artifact,
            status=CoverageStatus.PARTIAL if needs_review else CoverageStatus.SUCCESS,
            reason="review_required" if needs_review else None,
        )
        return await self._persist_and_record(
            source=source,
            fetched=artifact,
            clean_document=document,
            extracted=extracted,
            coverage=coverage,
            terminal_stage=ProcessStage.PERSIST,
            outcome=(
                ProcessOutcome.REVIEW_REQUIRED if needs_review else ProcessOutcome.RECORD_CREATED
            ),
            disposition=JobDisposition.SUCCEEDED,
            issues=tuple(extracted.issues),
        )

    async def _finish_fetch_state(
        self,
        source: SourceRequest,
        fetched: CollectionFetch,
    ) -> ProcessSourceResult:
        artifact = fetched.artifact
        status_map: dict[
            FetchStatus,
            tuple[ProcessOutcome, CoverageStatus, JobDisposition],
        ] = {
            FetchStatus.BLOCKED: (
                ProcessOutcome.BLOCKED,
                CoverageStatus.BLOCKED,
                JobDisposition.SUCCEEDED,
            ),
            FetchStatus.NOT_MODIFIED: (
                ProcessOutcome.NOT_MODIFIED,
                CoverageStatus.SUCCESS,
                JobDisposition.SUCCEEDED,
            ),
            FetchStatus.FAILED: (
                ProcessOutcome.FETCH_FAILED,
                CoverageStatus.UNREACHABLE,
                JobDisposition.RETRY if fetched.retryable else JobDisposition.FAILED,
            ),
            FetchStatus.SKIPPED: (
                ProcessOutcome.SKIPPED,
                CoverageStatus.NOT_FOUND,
                JobDisposition.SUCCEEDED,
            ),
        }
        mapping = status_map.get(artifact.status)
        if mapping is None:
            raise ValueError(f"unsupported terminal fetch status: {artifact.status.value}")
        outcome, coverage_status, disposition = mapping
        reason = artifact.error_code or artifact.status.value
        coverage = _observation(
            source,
            artifact,
            status=coverage_status,
            reason=reason,
        )
        return await self._persist_and_record(
            source=source,
            fetched=artifact,
            clean_document=None,
            extracted=None,
            coverage=coverage,
            terminal_stage=ProcessStage.FETCH,
            outcome=outcome,
            disposition=disposition,
        )

    async def _persist_and_record(
        self,
        *,
        source: SourceRequest,
        fetched: FetchArtifact,
        clean_document: CleanDocument | None,
        extracted: ExtractionProcessResult | None,
        coverage: CoverageObservation,
        terminal_stage: ProcessStage,
        outcome: ProcessOutcome,
        disposition: JobDisposition,
        issues: tuple[str, ...] = (),
    ) -> ProcessSourceResult:
        candidate = None if extracted is None else extracted.candidate
        record = None if extracted is None else extracted.record
        persisted = await self._writes.persist(
            PersistenceBundle(
                source=source,
                fetch_artifact=fetched,
                clean_document=clean_document,
                candidate=candidate,
                record=record,
                coverage_observation=coverage,
            )
        )
        if record is not None and persisted.record_created is None:
            raise ValueError("write adapter omitted record idempotency state")
        if record is not None and persisted.record_created is False:
            outcome = ProcessOutcome.RECORD_UNCHANGED
        result = ProcessSourceResult(
            job_id=source.job_id,
            bank_id=source.bank_id,
            terminal_stage=terminal_stage,
            outcome=outcome,
            job_disposition=disposition,
            coverage=persisted.coverage,
            record_id=persisted.record_id,
            issues=list(issues),
        )
        await self._job_outcomes.record(result)
        return result


class IngestCampaignUseCase:
    """Process one bounded deterministic batch; scheduling and retries remain worker-owned."""

    def __init__(self, process_source: ProcessSourceUseCase) -> None:
        self._process_source = process_source

    async def execute(self, sources: list[SourceRequest]) -> BatchProcessResult:
        if len(sources) > 100:
            raise ValueError("one ingestion batch may contain at most 100 sources")
        results: list[ProcessSourceResult] = []
        for source in sources:
            results.append(await self._process_source.execute(source))
        return BatchProcessResult(results=results)
