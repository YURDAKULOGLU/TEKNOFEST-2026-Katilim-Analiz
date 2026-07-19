"""Typed repositories over caller-owned, short SQLAlchemy transactions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, desc, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from katilim_analiz.contracts import (
    CampaignRecord,
    CleanDocument,
    ComparisonRequest,
    ComparisonResponse,
    CoverageEntry,
    EvidenceRef,
    ExtractionCandidate,
    FetchArtifact,
)
from katilim_analiz.storage.models import (
    CampaignObservationRow,
    CampaignRecordRow,
    CleanDocumentRow,
    ComparisonRecordLink,
    ComparisonSnapshot,
    CoverageEntryRow,
    DurableJob,
    EvidenceRefRow,
    ExtractionCandidateRow,
    ExtractionEvidenceLink,
    FetchArtifactRow,
    FetchDocumentLink,
    MonitoredCampaignTargetRow,
    MonitoredSourceStateRow,
    OutboxEvent,
    RecordEvidenceLink,
    Source,
    SourceBlockRow,
)
from katilim_analiz.storage.serialization import canonical_sha256, json_value

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OUTBOX_FEED_LOCK = text("SELECT pg_advisory_xact_lock(1262572617, 1330992194)")
_OUTBOX_FEED_NEXTVAL = text("nextval('outbox_feed_sequence'::regclass)")


class StorageError(RuntimeError):
    """Base class for persistence invariants and safe worker failures."""


class ImmutableConflictError(StorageError):
    """A caller tried to reuse an immutable identity for different content."""


class ObservationConflictError(ImmutableConflictError):
    """A scan observation identity was replayed with different semantic content."""


class StaleSourceVersionError(StorageError):
    """A source registry update would move a source backwards in time."""


class EvidenceIntegrityError(StorageError):
    """Evidence does not resolve exactly to its claimed source block span."""


class LeaseLostError(StorageError):
    """The lease expired, was reclaimed, or its fencing token is stale."""


@dataclass(frozen=True, slots=True)
class PersistResult:
    id: str
    created: bool


@dataclass(frozen=True, slots=True)
class JobLease:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    worker_id: str
    token: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxLease:
    id: UUID
    topic: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempt: int
    worker_id: str
    token: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MonitoredSourceObservationResult:
    previous_content_sha256: str | None
    current_content_sha256: str | None
    source_index_changed: bool


@dataclass(frozen=True, slots=True)
class RecordSummary:
    rate_min: Decimal | None
    rate_max: Decimal | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    term_min: int | None
    term_max: int | None


def summarize_record(record: CampaignRecord) -> RecordSummary:
    """Extract queryable numeric bounds while retaining complete JSONB data."""

    rates = [rate.value_percent for rate in record.data.rates]
    amounts = [amount.amount for amount in record.data.financing_amounts]
    term_minimums = [
        term.minimum_months for term in record.data.terms if term.minimum_months is not None
    ]
    term_maximums = [term.maximum_months for term in record.data.terms]
    return RecordSummary(
        rate_min=min(rates) if rates else None,
        rate_max=max(rates) if rates else None,
        amount_min=min(amounts) if amounts else None,
        amount_max=max(amounts) if amounts else None,
        term_min=min(term_minimums) if term_minimums else None,
        term_max=max(term_maximums) if term_maximums else None,
    )


def comparison_request_sha256(
    request: ComparisonRequest,
    *,
    ruleset_version: str,
    generated_at: datetime | None = None,
) -> str:
    """Return the version-aware lookup key used by comparison snapshots."""

    effective_as_of = request.as_of or generated_at
    if effective_as_of is None:
        raise ValueError("generated_at is required when a comparison has no explicit as_of")
    return canonical_sha256(
        {
            "request": request,
            "ruleset_version": ruleset_version,
            "effective_as_of": effective_as_of,
        }
    )


def _positive_duration(value: timedelta, field: str) -> None:
    if value <= timedelta(0) or value > timedelta(days=1):
        raise ValueError(f"{field} must be greater than zero and at most one day")


def _clean_text(value: str, field: str, *, maximum: int = 191) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{field} must contain 1..{maximum} characters")
    return cleaned


def _aware_datetime(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


def monitored_campaign_key(bank_id: str, canonical_url: str) -> str:
    """Return the stable logical campaign key for one verified bank detail URL."""

    bank_id = _clean_text(bank_id, "bank_id", maximum=64)
    canonical_url = _clean_text(canonical_url, "canonical_url", maximum=2_048)
    digest = hashlib.sha256(f"{bank_id}\0{canonical_url}".encode()).hexdigest()
    return f"{bank_id}:{digest}"


class MonitoredCampaignTargetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_seen(
        self,
        *,
        bank_id: str,
        campaign_key: str,
        canonical_url: str,
        discovered_from: str,
        registry_version: str,
        observed_at: datetime,
    ) -> tuple[MonitoredCampaignTargetRow, bool]:
        bank_id = _clean_text(bank_id, "bank_id", maximum=64)
        canonical_url = _clean_text(canonical_url, "canonical_url", maximum=2_048)
        discovered_from = _clean_text(discovered_from, "discovered_from", maximum=2_048)
        registry_version = _clean_text(registry_version, "registry_version", maximum=32)
        observed_at = _aware_datetime(observed_at, "observed_at")
        expected_campaign_key = monitored_campaign_key(bank_id, canonical_url)
        if campaign_key != expected_campaign_key:
            raise ValueError("campaign_key must derive from bank_id and canonical_url")
        identity_digest = hashlib.sha256(f"{bank_id}\0{canonical_url}".encode()).hexdigest()
        identifier = f"target:{identity_digest}"
        values = {
            "id": identifier,
            "bank_id": bank_id,
            "campaign_key": campaign_key,
            "canonical_url": canonical_url,
            "discovered_from": discovered_from,
            "registry_version": registry_version,
            "active": True,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
        }
        inserted = (
            await self.session.execute(
                insert(MonitoredCampaignTargetRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        MonitoredCampaignTargetRow.bank_id,
                        MonitoredCampaignTargetRow.canonical_url,
                    ]
                )
                .returning(MonitoredCampaignTargetRow.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            row = await self.session.get(MonitoredCampaignTargetRow, inserted)
            if row is None:  # pragma: no cover - RETURNING and same-session lookup are atomic
                raise StorageError("inserted monitored target could not be reloaded")
            return row, True

        row = (
            await self.session.execute(
                update(MonitoredCampaignTargetRow)
                .where(
                    MonitoredCampaignTargetRow.bank_id == bank_id,
                    MonitoredCampaignTargetRow.canonical_url == canonical_url,
                    MonitoredCampaignTargetRow.id == identifier,
                    MonitoredCampaignTargetRow.campaign_key == campaign_key,
                )
                .values(
                    discovered_from=discovered_from,
                    registry_version=registry_version,
                    active=True,
                    last_seen_at=func.greatest(
                        MonitoredCampaignTargetRow.last_seen_at,
                        observed_at,
                    ),
                )
                .returning(MonitoredCampaignTargetRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ImmutableConflictError(
                "monitored target identity conflicts with an existing campaign target"
            )
        return row, False

    async def list_active(self, bank_id: str) -> tuple[MonitoredCampaignTargetRow, ...]:
        bank_id = _clean_text(bank_id, "bank_id", maximum=64)
        rows = (
            (
                await self.session.execute(
                    select(MonitoredCampaignTargetRow)
                    .where(
                        MonitoredCampaignTargetRow.bank_id == bank_id,
                        MonitoredCampaignTargetRow.active.is_(True),
                    )
                    .order_by(
                        MonitoredCampaignTargetRow.canonical_url,
                        MonitoredCampaignTargetRow.campaign_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(rows)


class MonitoredSourceStateRepository:
    _STATUSES = frozenset({"success", "not_modified", "blocked", "failed"})

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_observation(
        self,
        *,
        bank_id: str,
        index_url: str,
        registry_version: str,
        status: str,
        observed_at: datetime,
        content_sha256: str | None,
    ) -> MonitoredSourceObservationResult:
        bank_id = _clean_text(bank_id, "bank_id", maximum=64)
        index_url = _clean_text(index_url, "index_url", maximum=2_048)
        registry_version = _clean_text(registry_version, "registry_version", maximum=32)
        status = _clean_text(status, "status", maximum=32)
        observed_at = _aware_datetime(observed_at, "observed_at")
        if status not in self._STATUSES:
            raise ValueError("status must be success, not_modified, blocked, or failed")
        if content_sha256 is not None and _SHA256.fullmatch(content_sha256) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if status == "success" and content_sha256 is None:
            raise ValueError("successful source observation requires content_sha256")

        initial_content = content_sha256 if status == "success" else None
        inserted = (
            await self.session.execute(
                insert(MonitoredSourceStateRow)
                .values(
                    bank_id=bank_id,
                    index_url=index_url,
                    registry_version=registry_version,
                    last_content_sha256=initial_content,
                    last_status=status,
                    last_observed_at=observed_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        MonitoredSourceStateRow.bank_id,
                        MonitoredSourceStateRow.index_url,
                    ]
                )
                .returning(MonitoredSourceStateRow.bank_id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return MonitoredSourceObservationResult(
                previous_content_sha256=None,
                current_content_sha256=initial_content,
                source_index_changed=False,
            )

        current = (
            await self.session.execute(
                select(MonitoredSourceStateRow)
                .where(
                    MonitoredSourceStateRow.bank_id == bank_id,
                    MonitoredSourceStateRow.index_url == index_url,
                )
                .with_for_update()
            )
        ).scalar_one()
        previous_content = current.last_content_sha256
        next_content = content_sha256 if status == "success" else previous_content
        changed = (
            status == "success"
            and previous_content is not None
            and next_content != previous_content
        )
        current.registry_version = registry_version
        current.last_content_sha256 = next_content
        current.last_status = status
        current.last_observed_at = observed_at
        current.updated_at = cast(datetime, func.now())
        await self.session.flush()
        return MonitoredSourceObservationResult(
            previous_content_sha256=previous_content,
            current_content_sha256=next_content,
            source_index_changed=changed,
        )


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        source_id: str,
        registry_version: str,
        listing_order: int,
        legal_name: str,
        homepage_url: str,
        allowed_hosts: list[str],
        digital_bank: bool,
        active: bool = True,
    ) -> bool:
        """Insert or advance a source; equal registry versions are immutable."""

        values = {
            "id": source_id,
            "registry_version": registry_version,
            "listing_order": listing_order,
            "legal_name": legal_name,
            "homepage_url": homepage_url,
            "allowed_hosts": allowed_hosts,
            "digital_bank": digital_bank,
            "active": active,
        }
        source_insert = insert(Source).values(**values)
        statement = source_insert.on_conflict_do_update(
            index_elements=[Source.id],
            set_={
                "registry_version": source_insert.excluded.registry_version,
                "listing_order": source_insert.excluded.listing_order,
                "legal_name": source_insert.excluded.legal_name,
                "homepage_url": source_insert.excluded.homepage_url,
                "allowed_hosts": source_insert.excluded.allowed_hosts,
                "digital_bank": source_insert.excluded.digital_bank,
                "active": source_insert.excluded.active,
                "updated_at": func.now(),
            },
            where=Source.registry_version < source_insert.excluded.registry_version,
        ).returning(Source.id)
        changed = (await self.session.execute(statement)).scalar_one_or_none()
        if changed is not None:
            return True

        existing = (
            await self.session.execute(select(Source).where(Source.id == source_id))
        ).scalar_one()
        if existing.registry_version > registry_version:
            raise StaleSourceVersionError(
                f"source {source_id!r} is already at {existing.registry_version}"
            )
        persisted = {
            "id": existing.id,
            "registry_version": existing.registry_version,
            "listing_order": existing.listing_order,
            "legal_name": existing.legal_name,
            "homepage_url": existing.homepage_url,
            "allowed_hosts": existing.allowed_hosts,
            "digital_bank": existing.digital_bank,
            "active": existing.active,
        }
        if persisted != values:
            raise ImmutableConflictError(
                f"source {source_id!r} changed without a new registry version"
            )
        return False


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_fetch(self, artifact: FetchArtifact) -> PersistResult:
        payload = artifact.model_dump(mode="json", exclude={"id"})
        artifact_hash = canonical_sha256(payload)
        values = {
            "id": artifact.id,
            "source_id": artifact.bank_id,
            "requested_url": str(artifact.requested_url),
            "final_url": None if artifact.final_url is None else str(artifact.final_url),
            "status": artifact.status.value,
            "http_status": artifact.http_status,
            "fetched_at": artifact.fetched_at,
            "robots_allowed": artifact.robots_allowed,
            "content_type": artifact.content_type,
            "etag": artifact.etag,
            "last_modified": artifact.last_modified,
            "raw_sha256": artifact.raw_sha256,
            "raw_size_bytes": artifact.raw_size_bytes,
            "private_raw_path": artifact.private_raw_path,
            "error_code": artifact.error_code,
            "error_detail": artifact.error_detail,
            "artifact_sha256": artifact_hash,
        }
        statement = (
            insert(FetchArtifactRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(FetchArtifactRow.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return PersistResult(id=inserted, created=True)
        existing = (
            (
                await self.session.execute(
                    select(FetchArtifactRow).where(
                        or_(
                            FetchArtifactRow.id == artifact.id,
                            FetchArtifactRow.artifact_sha256 == artifact_hash,
                        )
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is None or existing.artifact_sha256 != artifact_hash:
            raise ImmutableConflictError(f"fetch identity {artifact.id!r} has different content")
        return PersistResult(id=existing.id, created=False)

    async def add_document(self, document: CleanDocument) -> PersistResult:
        """Persist canonical content and attach this particular fetch observation."""

        values = {
            "id": document.id,
            "source_id": document.bank_id,
            "canonical_url": str(document.canonical_url),
            "title": document.title,
            "clean_sha256": document.clean_sha256,
            "language": document.language,
        }
        statement = (
            insert(CleanDocumentRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(CleanDocumentRow.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        created = inserted is not None
        if not created:
            existing = (
                (
                    await self.session.execute(
                        select(CleanDocumentRow).where(
                            or_(
                                CleanDocumentRow.id == document.id,
                                CleanDocumentRow.clean_sha256 == document.clean_sha256,
                            )
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is None:
                raise ImmutableConflictError("clean document uniqueness conflict is unresolved")
            expected = (
                document.bank_id,
                str(document.canonical_url),
                document.title,
                document.clean_sha256,
                document.language,
            )
            actual = (
                existing.source_id,
                existing.canonical_url,
                existing.title,
                existing.clean_sha256,
                existing.language,
            )
            if existing.id != document.id or actual != expected:
                raise ImmutableConflictError(
                    f"clean document identity {document.id!r} has different content"
                )

        if created:
            await self.session.execute(
                insert(SourceBlockRow),
                [
                    {
                        "id": block.id,
                        "document_id": document.id,
                        "ordinal": block.ordinal,
                        "kind": block.kind,
                        "text": block.text,
                        "locator": block.locator,
                        "text_sha256": block.text_sha256,
                    }
                    for block in document.blocks
                ],
            )
        else:
            rows = (
                (
                    await self.session.execute(
                        select(SourceBlockRow)
                        .where(SourceBlockRow.document_id == document.id)
                        .order_by(SourceBlockRow.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            expected_blocks = [
                (block.id, block.ordinal, block.kind, block.text, block.locator, block.text_sha256)
                for block in document.blocks
            ]
            actual_blocks = [
                (row.id, row.ordinal, row.kind, row.text, row.locator, row.text_sha256)
                for row in rows
            ]
            if actual_blocks != expected_blocks:
                raise ImmutableConflictError(
                    f"clean document {document.id!r} has non-canonical blocks"
                )

        await self.session.execute(
            insert(FetchDocumentLink)
            .values(
                fetch_artifact_id=document.fetch_artifact_id,
                document_id=document.id,
                cleaned_at=document.cleaned_at,
                cleaner_version=document.cleaner_version,
            )
            .on_conflict_do_nothing()
        )
        return PersistResult(id=document.id, created=created)


class EvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def put(self, evidence: EvidenceRef) -> PersistResult:
        """Validate the exact block span, then content-address the evidence reference."""

        block = (
            await self.session.execute(
                select(SourceBlockRow).where(SourceBlockRow.id == evidence.block_id)
            )
        ).scalar_one_or_none()
        if block is None or block.document_id != evidence.source_document_id:
            raise EvidenceIntegrityError("evidence block does not belong to the source document")
        if block.text[evidence.start_char : evidence.end_char] != evidence.quote:
            raise EvidenceIntegrityError("evidence quote does not match the claimed block span")
        quote_hash = hashlib.sha256(evidence.quote.encode()).hexdigest()
        if quote_hash != evidence.evidence_sha256:
            raise EvidenceIntegrityError("evidence quote does not match evidence_sha256")

        ref_hash = canonical_sha256(evidence.model_dump(mode="json", exclude={"id"}))
        values = {
            "id": evidence.id,
            "field_pointer": evidence.field_pointer,
            "source_document_id": evidence.source_document_id,
            "block_id": evidence.block_id,
            "quote": evidence.quote,
            "start_char": evidence.start_char,
            "end_char": evidence.end_char,
            "evidence_sha256": evidence.evidence_sha256,
            "status": evidence.status.value,
            "ref_sha256": ref_hash,
        }
        statement = (
            insert(EvidenceRefRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(EvidenceRefRow.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return PersistResult(id=inserted, created=True)
        existing = (
            (
                await self.session.execute(
                    select(EvidenceRefRow).where(
                        or_(EvidenceRefRow.id == evidence.id, EvidenceRefRow.ref_sha256 == ref_hash)
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is None or existing.ref_sha256 != ref_hash:
            raise ImmutableConflictError(f"evidence identity {evidence.id!r} has different content")
        return PersistResult(id=existing.id, created=False)


class ExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.evidence = EvidenceRepository(session)

    async def add_candidate(self, candidate: ExtractionCandidate) -> PersistResult:
        candidate_hash = candidate_identity_sha256(candidate)
        metadata = candidate.metadata
        values = {
            "id": candidate.id,
            "source_document_id": candidate.source_document_id,
            "bank_id": candidate.data.bank_id,
            "data": json_value(candidate.data),
            "method": metadata.method.value,
            "extractor_version": metadata.extractor_version,
            "schema_version": metadata.schema_version,
            "prompt_version": metadata.prompt_version,
            "model_id": metadata.model_id,
            "model_digest": metadata.model_digest,
            "started_at": metadata.started_at,
            "completed_at": metadata.completed_at,
            "issues": list(candidate.issues),
            "candidate_sha256": candidate_hash,
        }
        statement = (
            insert(ExtractionCandidateRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ExtractionCandidateRow.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            conflicts = (
                (
                    await self.session.execute(
                        select(ExtractionCandidateRow).where(
                            or_(
                                ExtractionCandidateRow.id == candidate.id,
                                ExtractionCandidateRow.candidate_sha256 == candidate_hash,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            if (
                len(conflicts) != 1
                or conflicts[0].id != candidate.id
                or conflicts[0].candidate_sha256 != candidate_hash
            ):
                raise ImmutableConflictError(
                    f"extraction identity {candidate.id!r} has different content"
                )
            candidate_id = conflicts[0].id
            created = False
        else:
            candidate_id = inserted
            created = True

        for evidence in candidate.evidence:
            evidence_result = await self.evidence.put(evidence)
            await self.session.execute(
                insert(ExtractionEvidenceLink)
                .values(extraction_id=candidate_id, evidence_id=evidence_result.id)
                .on_conflict_do_nothing()
            )
        return PersistResult(id=candidate_id, created=created)


def candidate_identity_sha256(candidate: ExtractionCandidate) -> str:
    """Hash exactly the semantic fields used by the deterministic candidate ID."""

    metadata = candidate.metadata
    return canonical_sha256(
        {
            "source_document_id": candidate.source_document_id,
            "data": candidate.data,
            "evidence": candidate.evidence,
            "method": metadata.method.value,
            "extractor_version": metadata.extractor_version,
            "schema_version": metadata.schema_version,
            "prompt_version": metadata.prompt_version,
            "model_id": metadata.model_id,
            "model_digest": metadata.model_digest,
        }
    )


class CampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.evidence = EvidenceRepository(session)

    async def add_record(self, campaign_key: str, record: CampaignRecord) -> PersistResult:
        campaign_key = _clean_text(campaign_key, "campaign_key")
        summary = summarize_record(record)
        payload_hash = canonical_sha256(
            {
                "source_document_id": record.source_document_id,
                "data": record.data,
                "evidence": record.evidence,
                "extraction": record.extraction,
                "status": record.status,
                "validation_issues": record.validation_issues,
            }
        )
        validity = record.data.validity
        values = {
            "id": record.id,
            "campaign_key": campaign_key,
            "version": record.version,
            "source_document_id": record.source_document_id,
            "bank_id": record.data.bank_id,
            "observed_at": record.observed_at,
            "title": record.data.title,
            "summary": record.data.summary,
            "product_family": record.data.product_family.value,
            "campaign_type": record.data.campaign_type.value,
            "product_currency": record.data.comparison_context.product_currency,
            "valid_from": None if validity is None else validity.starts_on,
            "valid_to": None if validity is None else validity.ends_on,
            "rate_min": summary.rate_min,
            "rate_max": summary.rate_max,
            "amount_min": summary.amount_min,
            "amount_max": summary.amount_max,
            "term_min": summary.term_min,
            "term_max": summary.term_max,
            "data": json_value(record.data),
            "extraction": json_value(record.extraction),
            "status": record.status.value,
            "validation_issues": list(record.validation_issues),
            "record_sha256": record.record_sha256,
            "payload_sha256": payload_hash,
        }
        statement = (
            insert(CampaignRecordRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(CampaignRecordRow.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            conflicts = (
                (
                    await self.session.execute(
                        select(CampaignRecordRow).where(
                            or_(
                                CampaignRecordRow.id == record.id,
                                and_(
                                    CampaignRecordRow.campaign_key == campaign_key,
                                    CampaignRecordRow.version == record.version,
                                ),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            if (
                len(conflicts) != 1
                or conflicts[0].id != record.id
                or conflicts[0].campaign_key != campaign_key
                or conflicts[0].version != record.version
                or conflicts[0].record_sha256 != record.record_sha256
                or conflicts[0].payload_sha256 != payload_hash
            ):
                raise ImmutableConflictError(
                    f"campaign record {record.id!r} conflicts with an existing version"
                )
            record_id = conflicts[0].id
            created = False
        else:
            record_id = inserted
            created = True

        for evidence in record.evidence:
            evidence_result = await self.evidence.put(evidence)
            await self.session.execute(
                insert(RecordEvidenceLink)
                .values(record_id=record_id, evidence_id=evidence_result.id)
                .on_conflict_do_nothing()
            )
        return PersistResult(id=record_id, created=created)

    async def latest(self, campaign_key: str) -> CampaignRecordRow | None:
        statement = (
            select(CampaignRecordRow)
            .where(CampaignRecordRow.campaign_key == campaign_key)
            .order_by(desc(CampaignRecordRow.version))
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def search(
        self,
        query: str,
        *,
        bank_id: str | None = None,
        product_family: str | None = None,
        limit: int = 20,
    ) -> list[CampaignRecordRow]:
        """Run a fixed, parameterized Turkish full-text query; no free-form SQL enters."""

        query = _clean_text(query, "query", maximum=500)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        ts_query = func.websearch_to_tsquery("turkish", func.immutable_unaccent(query))
        statement = select(CampaignRecordRow).where(
            CampaignRecordRow.search_vector.op("@@")(ts_query)
        )
        if bank_id is not None:
            statement = statement.where(CampaignRecordRow.bank_id == bank_id)
        if product_family is not None:
            statement = statement.where(CampaignRecordRow.product_family == product_family)
        statement = statement.order_by(
            desc(func.ts_rank_cd(CampaignRecordRow.search_vector, ts_query)),
            desc(CampaignRecordRow.observed_at),
        ).limit(limit)
        return list((await self.session.execute(statement)).scalars().all())


class CampaignObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self,
        campaign_key: str,
        observation_key: str,
    ) -> CampaignObservationRow | None:
        campaign_key = _clean_text(campaign_key, "campaign_key")
        if _SHA256.fullmatch(observation_key) is None:
            raise ValueError("observation_key must be a lowercase SHA-256 digest")
        return (
            await self.session.execute(
                select(CampaignObservationRow).where(
                    CampaignObservationRow.campaign_key == campaign_key,
                    CampaignObservationRow.observation_key == observation_key,
                )
            )
        ).scalar_one_or_none()

    async def add(
        self,
        *,
        campaign_key: str,
        observation_key: str,
        scan_run_id: str,
        record_id: str,
        clean_sha256: str,
        record_sha256: str,
        observed_at: datetime,
    ) -> tuple[CampaignObservationRow, bool]:
        campaign_key = _clean_text(campaign_key, "campaign_key")
        scan_run_id = _clean_text(scan_run_id, "scan_run_id")
        record_id = _clean_text(record_id, "record_id")
        observed_at = _aware_datetime(observed_at, "observed_at")
        digests = {
            "observation_key": observation_key,
            "clean_sha256": clean_sha256,
            "record_sha256": record_sha256,
        }
        if any(_SHA256.fullmatch(value) is None for value in digests.values()):
            raise ValueError("observation and content hashes must be lowercase SHA-256 digests")
        values = {
            "campaign_key": campaign_key,
            "observation_key": observation_key,
            "scan_run_id": scan_run_id,
            "record_id": record_id,
            "clean_sha256": clean_sha256,
            "record_sha256": record_sha256,
            "observed_at": observed_at,
        }
        inserted_id = (
            await self.session.execute(
                insert(CampaignObservationRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        CampaignObservationRow.campaign_key,
                        CampaignObservationRow.observation_key,
                    ]
                )
                .returning(CampaignObservationRow.id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            inserted = await self.session.get(CampaignObservationRow, inserted_id)
            if inserted is None:  # pragma: no cover - RETURNING and lookup share one transaction
                raise StorageError("inserted campaign observation could not be reloaded")
            return inserted, True

        existing = await self.get(campaign_key, observation_key)
        if (
            existing is None
            or existing.scan_run_id != scan_run_id
            or existing.record_id != record_id
            or existing.clean_sha256 != clean_sha256
            or existing.record_sha256 != record_sha256
        ):
            raise ObservationConflictError(
                f"campaign observation {campaign_key!r}/{observation_key!r} changed content"
            )
        return existing, False


class CoverageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: CoverageEntry) -> bool:
        entry_hash = canonical_sha256(entry)
        statement = (
            insert(CoverageEntryRow)
            .values(
                source_id=entry.bank_id,
                bank_name=entry.bank_name,
                observed_at=entry.observed_at,
                status=entry.status.value,
                source_count=entry.source_count,
                campaign_count=entry.campaign_count,
                reason=entry.reason,
                entry_sha256=entry_hash,
            )
            .on_conflict_do_nothing(index_elements=[CoverageEntryRow.entry_sha256])
            .returning(CoverageEntryRow.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        available_at: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        job_id: UUID | None = None,
    ) -> tuple[UUID, bool]:
        kind = _clean_text(kind, "kind", maximum=100)
        if not -32_768 <= priority <= 32_767:
            raise ValueError("priority is outside the smallint range")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if dedupe_key is not None:
            dedupe_key = _clean_text(dedupe_key, "dedupe_key")
        identifier = job_id or uuid4()
        statement = (
            insert(DurableJob)
            .values(
                id=identifier,
                kind=kind,
                payload=json_value(payload),
                dedupe_key=dedupe_key,
                status="queued",
                priority=priority,
                available_at=func.now() if available_at is None else available_at,
                attempts=0,
                max_attempts=max_attempts,
            )
            .on_conflict_do_nothing()
            .returning(DurableJob.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = (
            await self.session.execute(
                select(DurableJob.id).where(
                    or_(
                        DurableJob.id == identifier,
                        and_(DurableJob.kind == kind, DurableJob.dedupe_key == dedupe_key),
                    )
                )
            )
        ).scalar_one()
        return existing, False

    async def claim_next(
        self, worker_id: str, *, lease_for: timedelta = timedelta(minutes=2)
    ) -> JobLease | None:
        worker_id = _clean_text(worker_id, "worker_id")
        _positive_duration(lease_for, "lease_for")
        await self.session.execute(
            update(DurableJob)
            .where(
                DurableJob.status == "running",
                DurableJob.lease_expires_at <= func.now(),
                DurableJob.attempts >= DurableJob.max_attempts,
            )
            .values(
                status="dead",
                leased_by=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=func.now(),
                finished_at=func.now(),
            )
        )
        statement = (
            select(DurableJob)
            .where(
                # PostgreSQL can only prove a partial-index predicate from the
                # query text at planning time. Keep this trusted literal in the
                # statement so a generic prepared plan remains eligible for
                # ix_durable_jobs_claim; the status-specific branch below still
                # controls lease semantics.
                text("durable_jobs.status IN ('queued','running')"),
                DurableJob.available_at <= func.now(),
                DurableJob.attempts < DurableJob.max_attempts,
                or_(
                    DurableJob.status == "queued",
                    and_(
                        DurableJob.status == "running",
                        DurableJob.lease_expires_at <= func.now(),
                    ),
                ),
            )
            .order_by(desc(DurableJob.priority), DurableJob.available_at, DurableJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self.session.execute(statement)).scalar_one_or_none()
        if job is None:
            return None
        token = uuid4()
        job.status = "running"
        job.attempts += 1
        job.leased_by = worker_id
        job.lease_token = token
        job.lease_expires_at = cast(datetime, func.now() + lease_for)
        job.updated_at = cast(datetime, func.now())
        await self.session.flush()
        await self.session.refresh(job)
        if job.lease_expires_at is None:
            raise StorageError("database did not assign a job lease expiry")
        return JobLease(
            id=job.id,
            kind=job.kind,
            payload=job.payload,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            worker_id=worker_id,
            token=token,
            expires_at=job.lease_expires_at,
        )

    async def renew(self, lease: JobLease, *, lease_for: timedelta) -> datetime:
        _positive_duration(lease_for, "lease_for")
        statement = (
            update(DurableJob)
            .where(
                DurableJob.id == lease.id,
                DurableJob.status == "running",
                DurableJob.leased_by == lease.worker_id,
                DurableJob.lease_token == lease.token,
                DurableJob.lease_expires_at > func.now(),
            )
            .values(lease_expires_at=func.now() + lease_for, updated_at=func.now())
            .returning(DurableJob.lease_expires_at)
        )
        expiry = (await self.session.execute(statement)).scalar_one_or_none()
        if expiry is None:
            raise LeaseLostError(f"job lease {lease.id} is no longer owned")
        return expiry

    async def complete(self, lease: JobLease, result: dict[str, Any] | None = None) -> None:
        statement = (
            update(DurableJob)
            .where(
                DurableJob.id == lease.id,
                DurableJob.status == "running",
                DurableJob.leased_by == lease.worker_id,
                DurableJob.lease_token == lease.token,
                DurableJob.lease_expires_at > func.now(),
            )
            .values(
                status="succeeded",
                result=None if result is None else json_value(result),
                leased_by=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=func.now(),
                finished_at=func.now(),
            )
            .returning(DurableJob.id)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise LeaseLostError(f"job lease {lease.id} is no longer owned")

    async def fail(
        self,
        lease: JobLease,
        error: str,
        *,
        retry_after: timedelta = timedelta(seconds=30),
    ) -> bool:
        _positive_duration(retry_after, "retry_after")
        error = _clean_text(error, "error", maximum=10_000)
        statement = (
            select(DurableJob)
            .where(
                DurableJob.id == lease.id,
                DurableJob.status == "running",
                DurableJob.leased_by == lease.worker_id,
                DurableJob.lease_token == lease.token,
                DurableJob.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
        job = (await self.session.execute(statement)).scalar_one_or_none()
        if job is None:
            raise LeaseLostError(f"job lease {lease.id} is no longer owned")
        retrying = job.attempts < job.max_attempts
        job.status = "queued" if retrying else "dead"
        job.available_at = cast(datetime, func.now() + retry_after)
        job.last_error = error
        job.leased_by = None
        job.lease_token = None
        job.lease_expires_at = None
        job.updated_at = cast(datetime, func.now())
        if not retrying:
            job.finished_at = cast(datetime, func.now())
        await self.session.flush()
        return retrying


class OutboxRepository:
    """Transactional outbox; ``add`` must be the caller's final lock-taking write."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        topic: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        dedupe_key: str,
        occurred_at: datetime | None = None,
        available_at: datetime | None = None,
        event_id: UUID | None = None,
        max_attempts: int = 10,
    ) -> tuple[UUID, bool]:
        now = datetime.now(UTC)
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        identifier = event_id or uuid4()
        values = {
            "id": identifier,
            "feed_sequence": _OUTBOX_FEED_NEXTVAL,
            "topic": _clean_text(topic, "topic"),
            "event_type": _clean_text(event_type, "event_type"),
            "aggregate_type": _clean_text(aggregate_type, "aggregate_type", maximum=100),
            "aggregate_id": _clean_text(aggregate_id, "aggregate_id"),
            "payload": json_value(payload),
            "dedupe_key": _clean_text(dedupe_key, "dedupe_key"),
            "occurred_at": occurred_at or now,
            "available_at": func.now() if available_at is None else available_at,
            "attempts": 0,
            "max_attempts": max_attempts,
        }
        statement = (
            insert(OutboxEvent).values(**values).on_conflict_do_nothing().returning(OutboxEvent.id)
        )
        # Sequence allocation alone is not commit ordered. This fixed two-int
        # namespace cannot collide with the one-bigint campaign lock namespace.
        # The xact lock stays held through commit/rollback, so a later position
        # can never become visible before an earlier committed position.
        await self.session.flush()
        await self.session.execute(_OUTBOX_FEED_LOCK)
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = (
            await self.session.execute(
                select(OutboxEvent.id).where(
                    or_(
                        OutboxEvent.id == identifier,
                        OutboxEvent.dedupe_key == values["dedupe_key"],
                    )
                )
            )
        ).scalar_one()
        return existing, False

    async def claim_next(
        self, worker_id: str, *, lease_for: timedelta = timedelta(minutes=2)
    ) -> OutboxLease | None:
        worker_id = _clean_text(worker_id, "worker_id")
        _positive_duration(lease_for, "lease_for")
        await self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.lease_expires_at <= func.now(),
                OutboxEvent.attempts >= OutboxEvent.max_attempts,
            )
            .values(leased_by=None, lease_token=None, lease_expires_at=None)
        )
        statement = (
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.available_at <= func.now(),
                OutboxEvent.attempts < OutboxEvent.max_attempts,
                or_(
                    OutboxEvent.lease_token.is_(None),
                    OutboxEvent.lease_expires_at <= func.now(),
                ),
            )
            .order_by(OutboxEvent.available_at, OutboxEvent.feed_sequence)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        event = (await self.session.execute(statement)).scalar_one_or_none()
        if event is None:
            return None
        token = uuid4()
        event.attempts += 1
        event.leased_by = worker_id
        event.lease_token = token
        event.lease_expires_at = cast(datetime, func.now() + lease_for)
        await self.session.flush()
        await self.session.refresh(event)
        if event.lease_expires_at is None:
            raise StorageError("database did not assign an outbox lease expiry")
        return OutboxLease(
            id=event.id,
            topic=event.topic,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
            attempt=event.attempts,
            worker_id=worker_id,
            token=token,
            expires_at=event.lease_expires_at,
        )

    async def mark_published(self, lease: OutboxLease) -> None:
        statement = (
            update(OutboxEvent)
            .where(
                OutboxEvent.id == lease.id,
                OutboxEvent.published_at.is_(None),
                OutboxEvent.leased_by == lease.worker_id,
                OutboxEvent.lease_token == lease.token,
                OutboxEvent.lease_expires_at > func.now(),
            )
            .values(
                published_at=func.now(),
                leased_by=None,
                lease_token=None,
                lease_expires_at=None,
            )
            .returning(OutboxEvent.id)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise LeaseLostError(f"outbox lease {lease.id} is no longer owned")

    async def release(
        self,
        lease: OutboxLease,
        error: str,
        *,
        retry_after: timedelta = timedelta(seconds=30),
    ) -> None:
        _positive_duration(retry_after, "retry_after")
        statement = (
            update(OutboxEvent)
            .where(
                OutboxEvent.id == lease.id,
                OutboxEvent.published_at.is_(None),
                OutboxEvent.leased_by == lease.worker_id,
                OutboxEvent.lease_token == lease.token,
                OutboxEvent.lease_expires_at > func.now(),
            )
            .values(
                available_at=func.now() + retry_after,
                last_error=_clean_text(error, "error", maximum=10_000),
                leased_by=None,
                lease_token=None,
                lease_expires_at=None,
            )
            .returning(OutboxEvent.id)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise LeaseLostError(f"outbox lease {lease.id} is no longer owned")


class ComparisonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def put(
        self,
        request: ComparisonRequest,
        response: ComparisonResponse,
        *,
        record_ids: list[str],
    ) -> tuple[UUID, bool]:
        request_hash = comparison_request_sha256(
            request,
            ruleset_version=response.ruleset_version,
            generated_at=response.generated_at,
        )
        identifier = uuid4()
        statement = (
            insert(ComparisonSnapshot)
            .values(
                id=identifier,
                request_sha256=request_hash,
                request=json_value(request),
                ruleset_version=response.ruleset_version,
                generated_at=response.generated_at,
                response=json_value(response),
                canonical_sha256=response.canonical_sha256,
            )
            .on_conflict_do_nothing()
            .returning(ComparisonSnapshot.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            existing = (
                await self.session.execute(
                    select(ComparisonSnapshot).where(
                        ComparisonSnapshot.request_sha256 == request_hash
                    )
                )
            ).scalar_one_or_none()
            if existing is None or existing.canonical_sha256 != response.canonical_sha256:
                raise ImmutableConflictError("comparison request has a different response")
            comparison_id = existing.id
            created = False
        else:
            comparison_id = inserted
            created = True
        for record_id in set(record_ids):
            await self.session.execute(
                insert(ComparisonRecordLink)
                .values(comparison_id=comparison_id, record_id=record_id)
                .on_conflict_do_nothing()
            )
        return comparison_id, created

    async def by_request_hash(self, request_hash: str) -> ComparisonSnapshot | None:
        if not _SHA256.fullmatch(request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        return (
            await self.session.execute(
                select(ComparisonSnapshot).where(ComparisonSnapshot.request_sha256 == request_hash)
            )
        ).scalar_one_or_none()


class AuthRepository:
    """Deprecated storage import shim for the pre-V1.2 persistence seam.

    New auth code imports ``katilim_analiz.auth.repository.AuthRepository``.
    The implementation import stays lazy because ``storage.__init__`` exports
    this compatibility name while the auth repository itself maps storage
    models.
    """

    def __init__(self, session: AsyncSession) -> None:
        from katilim_analiz.auth.repository import AuthRepository as AuthRepositoryImpl

        self._delegate = AuthRepositoryImpl(session)

    async def create_user(
        self, username: str, password_hash: str, *, roles: list[str] | None = None
    ) -> UUID:
        return await self._delegate.create_user(username, password_hash, roles=roles)

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        csrf_hash: str,
        expires_at: datetime,
        client_context: dict[str, Any] | None = None,
    ) -> UUID:
        return await self._delegate.create_session(
            user_id=user_id,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            expires_at=expires_at,
            client_context=client_context,
        )

    async def revoke_session(self, session_id: UUID) -> bool:
        return await self._delegate.revoke_session(session_id)
