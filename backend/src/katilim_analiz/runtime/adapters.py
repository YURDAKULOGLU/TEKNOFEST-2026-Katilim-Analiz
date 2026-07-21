"""Concrete ingestion, extraction, versioning, outcome, and model-health adapters."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from katilim_analiz.application.models import PreviewExtractionOutput
from katilim_analiz.application.processing import (
    CleanOutcome,
    CleanResult,
    CollectionFetch,
    ExtractionProcessResult,
    ExtractionProcessState,
    ProcessSourceResult,
    SourceRequest,
)
from katilim_analiz.contracts import CampaignRecord, CleanDocument, FetchStatus, RecordStatus
from katilim_analiz.extraction import (
    ExtractionOutcome,
    ExtractionPipeline,
    decide_record_status,
    validate_candidate,
)
from katilim_analiz.ingestion import CleaningError, HttpIngestor, clean_html
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.serialization import canonical_sha256

_RETRYABLE_FETCH_ERRORS = frozenset(
    {
        "body_read_error",
        "request_failed",
        "retry_exhausted",
        "transport_error",
        "http_408",
        "http_425",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
    }
)
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class HttpCollectionAdapter:
    def __init__(self, ingestor: HttpIngestor) -> None:
        self._ingestor = ingestor

    async def fetch(self, source: SourceRequest) -> CollectionFetch:
        fetched = await self._ingestor.fetch(source.bank_id, str(source.source_url))
        artifact = fetched.artifact
        return CollectionFetch(
            artifact=artifact,
            raw_content=fetched.raw_content,
            retryable=(
                artifact.status is FetchStatus.FAILED
                and artifact.error_code in _RETRYABLE_FETCH_ERRORS
            ),
        )

    async def clean(self, fetched: CollectionFetch) -> CleanResult:
        if fetched.raw_content is None:
            raise ValueError("successful collection result omitted raw content")
        try:
            document = clean_html(
                fetched.artifact,
                fetched.raw_content,
                cleaned_at=fetched.artifact.fetched_at,
            )
        except CleaningError as exc:
            return CleanResult(
                outcome=CleanOutcome.FAILED,
                error_code="cleaning_error",
                detail=str(exc)[:500],
            )
        return CleanResult(outcome=CleanOutcome.DOCUMENT, document=document)


@dataclass(frozen=True, slots=True)
class RecordIdentity:
    id: str
    version: int


class CampaignVersionPort(Protocol):
    async def resolve(self, campaign_key: str, record_sha256: str) -> RecordIdentity: ...


class PostgresCampaignVersionStore:
    """Provide only an explicit provisional identity; PostgreSQL writer allocates versions."""

    def __init__(self, database: Database) -> None:
        # Retain the V1 composition signature while removing the pre-transaction read race.
        del database

    async def resolve(self, campaign_key: str, record_sha256: str) -> RecordIdentity:
        digest = hashlib.sha256(f"{campaign_key}\0{record_sha256}".encode()).hexdigest()
        return RecordIdentity(id=f"provisional-record:{digest}", version=1)


class PipelineExtractionAdapter:
    def __init__(self, pipeline: ExtractionPipeline, versions: CampaignVersionPort) -> None:
        self._pipeline = pipeline
        self._versions = versions

    async def extract_and_validate(
        self,
        source: SourceRequest,
        cleaned: CleanDocument,
    ) -> ExtractionProcessResult:
        extracted = await self._pipeline.extract(
            cleaned,
            static_page_label=source.static_page_label,
        )
        if extracted.outcome is ExtractionOutcome.ABSTAINED or extracted.candidate is None:
            issues = list(extracted.issues) or ["extraction_abstained"]
            return ExtractionProcessResult(
                state=ExtractionProcessState.ABSTAINED,
                issues=issues,
            )

        # A retried durable job must reproduce the same immutable candidate payload.
        # The cleaned document timestamp is content-identity-bound in this adapter.
        metadata = extracted.candidate.metadata.model_copy(
            update={
                "started_at": cleaned.cleaned_at,
                "completed_at": cleaned.cleaned_at,
            }
        )
        candidate = extracted.candidate.model_copy(update={"metadata": metadata})
        validate_candidate(candidate, cleaned)
        issues = list(candidate.issues)
        # Issue #3: machine validation is product-family aware. A record is
        # validated when every field required for its family and campaign type
        # resolved and no blocking issue remains; genuinely optional fields
        # (validity, ceiling, segment, ...) never gate validation.
        status = decide_record_status(
            candidate.data.product_family,
            candidate.data.campaign_type,
            issues,
        )
        record_sha256 = canonical_sha256(
            {
                "campaign_key": source.campaign_key,
                "candidate_id": candidate.id,
                "status": status,
                "validation_issues": issues,
            }
        )
        identity = await self._versions.resolve(source.campaign_key, record_sha256)
        record = CampaignRecord(
            id=identity.id,
            version=identity.version,
            source_document_id=cleaned.id,
            observed_at=cleaned.cleaned_at,
            data=candidate.data,
            evidence=candidate.evidence,
            extraction=candidate.metadata,
            status=status,
            validation_issues=issues,
            record_sha256=record_sha256,
        )
        return ExtractionProcessResult(
            state=(
                ExtractionProcessState.VALIDATED
                if status is RecordStatus.VALIDATED
                else ExtractionProcessState.NEEDS_REVIEW
            ),
            candidate=candidate,
            record=record,
            issues=issues,
        )


class PipelinePreviewAdapter:
    """Expose candidate extraction without versioning, persistence, or record status."""

    def __init__(self, pipeline: ExtractionPipeline) -> None:
        self._pipeline = pipeline

    async def extract(self, document: CleanDocument) -> PreviewExtractionOutput:
        extracted = await self._pipeline.extract(document)
        return PreviewExtractionOutput(
            candidate=extracted.candidate,
            issues=list(extracted.issues),
            model_attempted=extracted.model_attempted,
            accepted_model_facts=extracted.accepted_model_facts,
        )


class StructuredJobOutcomeLogger:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("katilim_analiz.worker.outcome")

    async def record(self, result: ProcessSourceResult) -> None:
        self._logger.info(
            "source processing reached a persisted terminal outcome",
            extra={
                "event": "source_processing_outcome",
                "job_id": result.job_id,
                "bank_id": result.bank_id,
            },
        )


class _OllamaModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    name: str
    model: str
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _OllamaTags(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    models: list[_OllamaModel]


class OllamaModelHealth:
    """Readiness probe against Ollama's documented GET /api/tags endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        expected_digest: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("model health timeout must be positive")
        if _SHA256_DIGEST.fullmatch(expected_digest) is None:
            raise ValueError("expected model digest must be a lowercase SHA-256 digest")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._expected_digest = expected_digest
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    async def ping(self) -> bool:
        try:
            response = await self._client.get(
                f"{self._base_url}/api/tags",
                timeout=self._timeout,
            )
            response.raise_for_status()
            tags = _OllamaTags.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError):
            return False
        return any(
            self._model in {item.name, item.model} and item.digest == self._expected_digest
            for item in tags.models
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
