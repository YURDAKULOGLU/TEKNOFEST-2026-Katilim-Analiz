"""Atomic persistence adapter for one validated processing bundle."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from katilim_analiz.application.ports import CampaignWritePort
from katilim_analiz.application.processing import PersistenceBundle, PersistenceResult
from katilim_analiz.contracts import CoverageEntry, RecordStatus
from katilim_analiz.storage.models import (
    CampaignRecordRow,
    FetchArtifactRow,
    Source,
)
from katilim_analiz.storage.repositories import (
    ArtifactRepository,
    CampaignObservationRepository,
    CampaignRepository,
    CoverageRepository,
    ExtractionRepository,
    ObservationConflictError,
    OutboxRepository,
    StorageError,
)


class RegistrySourceMissingError(StorageError):
    """A worker attempted persistence before the versioned registry was seeded."""


class RegistrySourceMismatchError(StorageError):
    """A bundle disagrees with its pre-seeded source registry identity or allowlist."""


@dataclass(frozen=True, slots=True)
class _PendingCampaignChange:
    aggregate_id: str
    payload: dict[str, Any]
    dedupe_key: str
    occurred_at: datetime


class PostgresCampaignWriteAdapter(CampaignWritePort):
    """Persist fetch-to-coverage provenance atomically after registry verification."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def persist(self, bundle: PersistenceBundle) -> PersistenceResult:
        async with self._sessions.begin() as session:
            source = await self._verified_source(session, bundle)
            artifacts = ArtifactRepository(session)
            await artifacts.add_fetch(bundle.fetch_artifact)
            if bundle.clean_document is not None:
                await artifacts.add_document(bundle.clean_document)

            record_id: str | None = None
            record_created: bool | None = None
            pending_change: _PendingCampaignChange | None = None
            if bundle.record is not None:
                (
                    record_id,
                    record_created,
                    pending_change,
                ) = await self._persist_campaign_observation(
                    session,
                    bundle,
                )
            elif bundle.candidate is not None:
                await ExtractionRepository(session).add_candidate(bundle.candidate)
            source_count, campaign_count = await self._coverage_counts(session, source.id)
            observation = bundle.coverage_observation
            coverage = CoverageEntry(
                bank_id=observation.bank_id,
                bank_name=observation.bank_name,
                observed_at=observation.observed_at,
                status=observation.status,
                source_count=source_count,
                campaign_count=campaign_count,
                reason=observation.reason,
            )
            await CoverageRepository(session).add(coverage)
            if pending_change is not None:
                _, outbox_created = await OutboxRepository(session).add(
                    topic="notifications.campaigns.v1",
                    event_type="campaign_record.changed.v1",
                    aggregate_type="campaign",
                    aggregate_id=pending_change.aggregate_id,
                    payload=pending_change.payload,
                    dedupe_key=pending_change.dedupe_key,
                    occurred_at=pending_change.occurred_at,
                )
                if not outbox_created:
                    raise StorageError("new campaign version reused an existing outbox dedupe key")
        return PersistenceResult(
            coverage=coverage,
            record_id=record_id,
            record_created=record_created,
        )

    async def _persist_campaign_observation(
        self,
        session: AsyncSession,
        bundle: PersistenceBundle,
    ) -> tuple[str, bool, _PendingCampaignChange | None]:
        """Allocate and persist one observation-safe campaign version under an xact lock."""

        record = bundle.record
        document = bundle.clean_document
        candidate = bundle.candidate
        if record is None or document is None or candidate is None:
            raise ValueError("campaign observation requires record, candidate, and clean document")
        campaign_key = bundle.source.campaign_key
        scan_run_id = bundle.source.require_scan_run_id()
        observation_key = bundle.source.require_observation_key()
        await session.execute(select(func.pg_advisory_xact_lock(_campaign_lock_key(campaign_key))))

        # Candidate equality intentionally ignores runtime timestamps, matching its ID.
        await ExtractionRepository(session).add_candidate(candidate)
        observations = CampaignObservationRepository(session)
        existing_observation = await observations.get(campaign_key, observation_key)
        if existing_observation is not None:
            if (
                existing_observation.scan_run_id != scan_run_id
                or existing_observation.clean_sha256 != document.clean_sha256
                or existing_observation.record_sha256 != record.record_sha256
            ):
                raise ObservationConflictError(
                    f"campaign observation {campaign_key!r}/{observation_key!r} changed content"
                )
            return existing_observation.record_id, False, None

        campaigns = CampaignRepository(session)
        latest = await campaigns.latest(campaign_key)
        if latest is not None and latest.record_sha256 == record.record_sha256:
            await observations.add(
                campaign_key=campaign_key,
                observation_key=observation_key,
                scan_run_id=scan_run_id,
                record_id=latest.id,
                clean_sha256=document.clean_sha256,
                record_sha256=record.record_sha256,
                observed_at=record.observed_at,
            )
            return latest.id, False, None

        version = 1 if latest is None else latest.version + 1
        record_id = _record_id(campaign_key, observation_key, record.record_sha256)
        canonical_record = record.model_copy(update={"id": record_id, "version": version})
        persisted = await campaigns.add_record(campaign_key, canonical_record)
        if not persisted.created:
            raise StorageError("new campaign observation reused an existing record identity")
        await observations.add(
            campaign_key=campaign_key,
            observation_key=observation_key,
            scan_run_id=scan_run_id,
            record_id=record_id,
            clean_sha256=document.clean_sha256,
            record_sha256=record.record_sha256,
            observed_at=record.observed_at,
        )
        change_kind = "created" if latest is None else "updated"
        pending_change = _PendingCampaignChange(
            aggregate_id=campaign_key,
            payload={
                "campaign_key": campaign_key,
                "record_id": record_id,
                "record_version": version,
                "change_kind": change_kind,
                "record_status": canonical_record.status.value,
                "previous_record_id": None if latest is None else latest.id,
                "observed_at": canonical_record.observed_at.isoformat(),
            },
            dedupe_key=f"campaign-change:{record_id}",
            occurred_at=canonical_record.observed_at,
        )
        return record_id, True, pending_change

    async def _verified_source(self, session: AsyncSession, bundle: PersistenceBundle) -> Source:
        source = (
            await session.execute(
                select(Source).where(Source.id == bundle.source.bank_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if source is None or not source.active:
            raise RegistrySourceMissingError(
                f"source {bundle.source.bank_id!r} is not active in the seeded registry"
            )
        if source.legal_name != bundle.source.bank_name:
            raise RegistrySourceMismatchError(
                f"source {bundle.source.bank_id!r} legal name differs from the registry"
            )
        if source.legal_name != bundle.coverage_observation.bank_name:
            raise RegistrySourceMismatchError(
                f"coverage for {bundle.source.bank_id!r} uses a non-registry bank name"
            )

        urls = [str(bundle.source.source_url), str(bundle.fetch_artifact.requested_url)]
        if bundle.fetch_artifact.final_url is not None:
            urls.append(str(bundle.fetch_artifact.final_url))
        if bundle.clean_document is not None:
            urls.append(str(bundle.clean_document.canonical_url))
        allowed_hosts = {host.casefold().rstrip(".") for host in source.allowed_hosts}
        if any(_url_host(url) not in allowed_hosts for url in urls):
            raise RegistrySourceMismatchError(
                f"bundle for {source.id!r} contains a URL outside the registry allowlist"
            )
        if bundle.candidate is not None and bundle.candidate.data.bank_id != source.id:
            raise RegistrySourceMismatchError("candidate belongs to another source bank")
        if bundle.record is not None and bundle.record.data.bank_id != source.id:
            raise RegistrySourceMismatchError("record belongs to another source bank")
        return source

    async def _coverage_counts(self, session: AsyncSession, source_id: str) -> tuple[int, int]:
        """Derive current aggregates from facts visible in this transaction."""

        source_count = int(
            (
                await session.execute(
                    select(func.count(func.distinct(FetchArtifactRow.requested_url))).where(
                        FetchArtifactRow.source_id == source_id
                    )
                )
            ).scalar_one()
        )
        ranked = (
            select(
                CampaignRecordRow.status.label("status"),
                func.row_number()
                .over(
                    partition_by=CampaignRecordRow.campaign_key,
                    order_by=(
                        CampaignRecordRow.observed_at.desc(),
                        CampaignRecordRow.version.desc(),
                        CampaignRecordRow.id.asc(),
                    ),
                )
                .label("version_rank"),
            )
            .where(CampaignRecordRow.bank_id == source_id)
            .subquery("ranked_coverage_campaigns")
        )
        campaign_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ranked)
                    .where(
                        ranked.c.version_rank == 1,
                        ranked.c.status == RecordStatus.VALIDATED.value,
                    )
                )
            ).scalar_one()
        )
        return source_count, campaign_count


def _url_host(value: str) -> str:
    host = urlsplit(value).hostname
    return "" if host is None else host.casefold().rstrip(".")


def _campaign_lock_key(campaign_key: str) -> int:
    digest = hashlib.sha256(campaign_key.encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _record_id(campaign_key: str, observation_key: str, record_sha256: str) -> str:
    material = f"{campaign_key}\0{observation_key}\0{record_sha256}"
    return f"record:{hashlib.sha256(material.encode()).hexdigest()}"
