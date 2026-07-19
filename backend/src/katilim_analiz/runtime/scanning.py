"""Repeatable campaign scans with scan-scoped durable-job idempotency."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import HttpUrl

from katilim_analiz.application.processing import SourceRequest, campaign_observation_key
from katilim_analiz.config import Settings
from katilim_analiz.contracts import FetchArtifact, FetchStatus
from katilim_analiz.ingestion import (
    HostPolicy,
    HttpIngestor,
    InMemoryResponseCache,
    PrivateFileArtifactStore,
    StaticHostPolicyProvider,
)
from katilim_analiz.ingestion.artifacts import FetchResult
from katilim_analiz.ingestion.discovery import discover_campaign_index
from katilim_analiz.ingestion.policy import validate_url_syntax
from katilim_analiz.ingestion.registry import BankRegistry
from katilim_analiz.runtime.composition import RuntimeConfigurationError
from katilim_analiz.runtime.paths import resolve_runtime_path
from katilim_analiz.runtime.registry import (
    SOURCE_JOB_KIND,
    CampaignSourceStatus,
    MonitoredCampaignSource,
    MonitoredCampaignSourceRegistry,
    load_monitored_campaign_registry,
    load_runtime_registry,
    sync_source_registry,
)
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.health import PostgresDatabaseHealth
from katilim_analiz.storage.repositories import (
    ArtifactRepository,
    JobRepository,
    MonitoredCampaignTargetRepository,
    MonitoredSourceStateRepository,
    monitored_campaign_key,
)

_SCAN_RUN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")


class CampaignSourceScanStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    SUCCESS = "success"
    NOT_MODIFIED = "not_modified"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CampaignTarget:
    bank_id: str
    campaign_key: str
    url: str


@dataclass(frozen=True, slots=True)
class DetailScanJob:
    scan_run_id: str
    bank_id: str
    bank_name: str
    campaign_key: str
    url: str


@dataclass(frozen=True, slots=True)
class SourceIndexObservation:
    previous_content_sha256: str | None
    current_content_sha256: str | None
    source_index_changed: bool


@dataclass(frozen=True, slots=True)
class CampaignSourceScanResult:
    bank_id: str
    status: CampaignSourceScanStatus
    known_targets: int
    discovered_targets: int
    enqueued_jobs: int
    deduplicated_jobs: int
    source_index_changed: bool = False
    review_required: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignScanResult:
    scan_run_id: str
    sources: tuple[CampaignSourceScanResult, ...]

    @property
    def enqueued_jobs(self) -> int:
        return sum(source.enqueued_jobs for source in self.sources)

    @property
    def deduplicated_jobs(self) -> int:
        return sum(source.deduplicated_jobs for source in self.sources)

    @property
    def review_required(self) -> bool:
        return any(source.review_required for source in self.sources)

    @property
    def has_blocked_sources(self) -> bool:
        """Report sources that refuse automated access, such as a CAPTCHA wall.

        This is a steady state the collection policy deliberately accepts rather
        than circumvents, so it is reported without failing the run.
        """

        return any(source.status is CampaignSourceScanStatus.BLOCKED for source in self.sources)

    @property
    def has_failures(self) -> bool:
        """Report failures a retry could plausibly clear."""

        return any(source.status is CampaignSourceScanStatus.FAILED for source in self.sources)


class CampaignIndexCollector(Protocol):
    async def fetch(self, bank_id: str, url: str) -> FetchResult: ...


class CampaignScanStore(Protocol):
    """Narrow persistence seam implemented by the PostgreSQL runtime adapter below."""

    async def list_active(self, bank_id: str) -> tuple[CampaignTarget, ...]: ...

    async def observe_index(
        self,
        artifact: FetchArtifact,
        *,
        registry_version: str,
    ) -> SourceIndexObservation: ...

    async def observe_index_error(
        self,
        *,
        bank_id: str,
        index_url: str,
        registry_version: str,
        observed_at: datetime,
        error_code: str,
    ) -> None: ...

    async def upsert_seen(
        self,
        *,
        bank_id: str,
        campaign_key: str,
        canonical_url: str,
        discovered_from: str,
        registry_version: str,
        observed_at: datetime,
    ) -> tuple[CampaignTarget, bool]: ...

    async def enqueue_detail(self, job: DetailScanJob, *, dedupe_key: str) -> bool: ...


def validate_scan_run_id(value: str) -> str:
    """Validate the Kubernetes label value used as the durable scan identity."""

    if not isinstance(value, str) or not _SCAN_RUN_ID.fullmatch(value):
        raise ValueError(
            "scan_run_id must be a 1..63 character Kubernetes label value "
            "using letters, digits, '.', '_', or '-'"
        )
    return value


def campaign_key_for_url(bank_id: str, canonical_url: str) -> str:
    return monitored_campaign_key(bank_id, canonical_url)


def scan_job_dedupe_key(scan_run_id: str, campaign_key: str, canonical_url: str) -> str:
    run_id = validate_scan_run_id(scan_run_id)
    material = f"{run_id}\0{campaign_key}\0{canonical_url}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CampaignScanner:
    """Fetch each verified index once, retain known targets, and enqueue details."""

    def __init__(
        self,
        *,
        bank_registry: BankRegistry,
        campaign_registry: MonitoredCampaignSourceRegistry,
        collector: CampaignIndexCollector,
        store: CampaignScanStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if campaign_registry.bank_registry_version != bank_registry.registry_version:
            raise ValueError("campaign and BDDK registries have different versions")
        self._bank_registry = bank_registry
        self._campaign_registry = campaign_registry
        self._collector = collector
        self._store = store
        self._clock = clock

    async def run(self, scan_run_id: str) -> CampaignScanResult:
        run_id = validate_scan_run_id(scan_run_id)
        results: list[CampaignSourceScanResult] = []
        for source in self._campaign_registry.sources:
            results.append(await self._scan_source(run_id, source))
        return CampaignScanResult(run_id, tuple(results))

    async def _scan_source(
        self,
        scan_run_id: str,
        source: MonitoredCampaignSource,
    ) -> CampaignSourceScanResult:
        bank = self._bank_registry.bank(source.bank_id)
        known = await self._store.list_active(bank.id)
        targets = {target.url: target for target in known}

        if source.status is CampaignSourceStatus.UNAVAILABLE:
            enqueued, deduplicated = await self._enqueue_targets(scan_run_id, targets)
            return CampaignSourceScanResult(
                bank_id=bank.id,
                status=CampaignSourceScanStatus.UNAVAILABLE,
                known_targets=len(known),
                discovered_targets=0,
                enqueued_jobs=enqueued,
                deduplicated_jobs=deduplicated,
            )

        if source.index_url is None or source.discovery_mode is None:
            raise ValueError(f"verified campaign source lacks discovery configuration: {bank.id}")
        try:
            fetched = await self._collector.fetch(bank.id, source.index_url)
        except Exception as exc:
            return await self._failed_index_result(
                scan_run_id=scan_run_id,
                source=source,
                targets=targets,
                known_count=len(known),
                error_code=_exception_code("index_fetch", exc),
            )

        try:
            artifact = fetched.artifact
            if artifact.bank_id != bank.id or str(artifact.requested_url) != source.index_url:
                raise ValueError("index collector returned an artifact for a different source")
            observation = await self._store.observe_index(
                artifact,
                registry_version=self._campaign_registry.registry_version,
            )
            discovered_count = 0
            source_index_changed = observation.source_index_changed
            review_required = False
            status = self._scan_status(artifact.status)
            error_code = artifact.error_code

            if artifact.status is FetchStatus.SUCCESS:
                raw_html = fetched.raw_content
                if raw_html is None or artifact.raw_sha256 is None:
                    raise ValueError("successful index fetch lacks content or its hash")
                if hashlib.sha256(raw_html).hexdigest() != artifact.raw_sha256:
                    raise ValueError("index content differs from the persisted fetch hash")
                final_url = str(artifact.final_url or artifact.requested_url)
                discovery = discover_campaign_index(
                    raw_html,
                    bank=bank,
                    index_url=final_url,
                    detail_path_prefixes=source.detail_path_prefixes,
                    max_links=source.max_links,
                    known_targets=targets,
                    previous_index_sha256=observation.previous_content_sha256,
                    mode=source.discovery_mode,
                )
                if discovery.index_sha256 != artifact.raw_sha256:
                    raise ValueError("discovery and fetch content hashes differ")
                if discovery.source_index_changed != observation.source_index_changed:
                    raise ValueError("discovery and durable index-change state disagree")
                review_required = discovery.review_required
                for canonical_url in discovery.all_detail_targets:
                    campaign_key = campaign_key_for_url(bank.id, canonical_url)
                    target, created = await self._store.upsert_seen(
                        bank_id=bank.id,
                        campaign_key=campaign_key,
                        canonical_url=canonical_url,
                        discovered_from=source.index_url,
                        registry_version=self._campaign_registry.registry_version,
                        observed_at=artifact.fetched_at,
                    )
                    targets[target.url] = target
                    discovered_count += int(created)
        except Exception as exc:
            return await self._failed_index_result(
                scan_run_id=scan_run_id,
                source=source,
                targets=targets,
                known_count=len(known),
                error_code=_exception_code("index_process", exc),
            )

        enqueued, deduplicated = await self._enqueue_targets(scan_run_id, targets)
        return CampaignSourceScanResult(
            bank_id=bank.id,
            status=status,
            known_targets=len(known),
            discovered_targets=discovered_count,
            enqueued_jobs=enqueued,
            deduplicated_jobs=deduplicated,
            source_index_changed=source_index_changed,
            review_required=review_required,
            error_code=error_code,
        )

    async def _failed_index_result(
        self,
        *,
        scan_run_id: str,
        source: MonitoredCampaignSource,
        targets: dict[str, CampaignTarget],
        known_count: int,
        error_code: str,
    ) -> CampaignSourceScanResult:
        if source.index_url is None:
            raise ValueError("verified campaign source lacks an index URL")
        await self._store.observe_index_error(
            bank_id=source.bank_id,
            index_url=source.index_url,
            registry_version=self._campaign_registry.registry_version,
            observed_at=self._now(),
            error_code=error_code,
        )
        enqueued, deduplicated = await self._enqueue_targets(scan_run_id, targets)
        return CampaignSourceScanResult(
            bank_id=source.bank_id,
            status=CampaignSourceScanStatus.FAILED,
            known_targets=known_count,
            discovered_targets=0,
            enqueued_jobs=enqueued,
            deduplicated_jobs=deduplicated,
            error_code=error_code,
        )

    async def _enqueue_targets(
        self,
        scan_run_id: str,
        targets: dict[str, CampaignTarget],
    ) -> tuple[int, int]:
        enqueued = 0
        deduplicated = 0
        for target in sorted(targets.values(), key=lambda item: (item.url, item.campaign_key)):
            bank = self._bank_registry.bank(target.bank_id)
            canonical_url, _ = validate_url_syntax(target.url, bank)
            if canonical_url != target.url:
                raise ValueError("persisted campaign target is not canonical")
            job = DetailScanJob(
                scan_run_id=scan_run_id,
                bank_id=bank.id,
                bank_name=bank.legal_name,
                campaign_key=target.campaign_key,
                url=canonical_url,
            )
            created = await self._store.enqueue_detail(
                job,
                dedupe_key=scan_job_dedupe_key(
                    scan_run_id,
                    target.campaign_key,
                    canonical_url,
                ),
            )
            enqueued += int(created)
            deduplicated += int(not created)
        return enqueued, deduplicated

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scan clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _scan_status(status: FetchStatus) -> CampaignSourceScanStatus:
        if status is FetchStatus.SUCCESS:
            return CampaignSourceScanStatus.SUCCESS
        if status is FetchStatus.NOT_MODIFIED:
            return CampaignSourceScanStatus.NOT_MODIFIED
        if status is FetchStatus.BLOCKED:
            return CampaignSourceScanStatus.BLOCKED
        return CampaignSourceScanStatus.FAILED


def _exception_code(phase: str, exc: Exception) -> str:
    """Describe an unexpected failure without leaking exception messages or credentials."""

    return f"{phase}:{type(exc).__name__}"[:191]


class PostgresCampaignScanStore:
    """Short-transaction PostgreSQL adapter for one direct campaign scan process."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_active(self, bank_id: str) -> tuple[CampaignTarget, ...]:
        async with self._database.session() as session:
            rows = await MonitoredCampaignTargetRepository(session).list_active(bank_id)
        return tuple(
            CampaignTarget(row.bank_id, row.campaign_key, row.canonical_url) for row in rows
        )

    async def observe_index(
        self,
        artifact: FetchArtifact,
        *,
        registry_version: str,
    ) -> SourceIndexObservation:
        status = _source_state_status(artifact.status)
        content_sha256 = artifact.raw_sha256 if artifact.status is FetchStatus.SUCCESS else None
        async with self._database.transaction() as session:
            await ArtifactRepository(session).add_fetch(artifact)
            result = await MonitoredSourceStateRepository(session).record_observation(
                bank_id=artifact.bank_id,
                index_url=str(artifact.requested_url),
                registry_version=registry_version,
                status=status,
                observed_at=artifact.fetched_at,
                content_sha256=content_sha256,
            )
        return SourceIndexObservation(
            result.previous_content_sha256,
            result.current_content_sha256,
            result.source_index_changed,
        )

    async def observe_index_error(
        self,
        *,
        bank_id: str,
        index_url: str,
        registry_version: str,
        observed_at: datetime,
        error_code: str,
    ) -> None:
        if not error_code.strip():
            raise ValueError("error_code must be non-empty")
        async with self._database.transaction() as session:
            await MonitoredSourceStateRepository(session).record_observation(
                bank_id=bank_id,
                index_url=index_url,
                registry_version=registry_version,
                status=CampaignSourceScanStatus.FAILED.value,
                observed_at=observed_at,
                content_sha256=None,
            )

    async def upsert_seen(
        self,
        *,
        bank_id: str,
        campaign_key: str,
        canonical_url: str,
        discovered_from: str,
        registry_version: str,
        observed_at: datetime,
    ) -> tuple[CampaignTarget, bool]:
        async with self._database.transaction() as session:
            row, created = await MonitoredCampaignTargetRepository(session).upsert_seen(
                bank_id=bank_id,
                campaign_key=campaign_key,
                canonical_url=canonical_url,
                discovered_from=discovered_from,
                registry_version=registry_version,
                observed_at=observed_at,
            )
        return CampaignTarget(row.bank_id, row.campaign_key, row.canonical_url), created

    async def enqueue_detail(self, job: DetailScanJob, *, dedupe_key: str) -> bool:
        expected_dedupe_key = scan_job_dedupe_key(
            job.scan_run_id,
            job.campaign_key,
            job.url,
        )
        if dedupe_key != expected_dedupe_key:
            raise ValueError("detail job dedupe key differs from its scan identity")
        source = SourceRequest(
            bank_id=job.bank_id,
            bank_name=job.bank_name,
            source_url=HttpUrl(job.url),
            campaign_key=job.campaign_key,
            scan_run_id=job.scan_run_id,
            observation_key=campaign_observation_key(
                job.scan_run_id,
                job.campaign_key,
                job.url,
            ),
        )
        async with self._database.transaction() as session:
            _, created = await JobRepository(session).enqueue(
                SOURCE_JOB_KIND,
                source.model_dump(mode="json", exclude_none=True),
                dedupe_key=dedupe_key,
            )
        return created


def _source_state_status(status: FetchStatus) -> str:
    if status is FetchStatus.SUCCESS:
        return CampaignSourceScanStatus.SUCCESS.value
    if status is FetchStatus.NOT_MODIFIED:
        return CampaignSourceScanStatus.NOT_MODIFIED.value
    if status is FetchStatus.BLOCKED:
        return CampaignSourceScanStatus.BLOCKED.value
    return CampaignSourceScanStatus.FAILED.value


async def run_operational_campaign_scan(
    settings: Settings,
    *,
    scan_run_id: str,
    registry_path: str | Path | None = None,
    campaign_registry_path: str | Path | None = None,
    database: Database | None = None,
) -> CampaignScanResult:
    """Compose policy-enforced HTTP discovery and durable PostgreSQL enqueueing."""

    run_id = validate_scan_run_id(scan_run_id)
    if not settings.ingest_network_enabled:
        raise RuntimeConfigurationError("campaign scan requires INGEST_NETWORK_ENABLED=true")
    bank_registry = load_runtime_registry(registry_path)
    campaign_registry = load_monitored_campaign_registry(
        campaign_registry_path,
        bank_registry=bank_registry,
    )
    configured_database = database or Database.from_settings(settings)
    owns_database = database is None
    ingestor: HttpIngestor | None = None
    try:
        health = PostgresDatabaseHealth(
            configured_database.engine,
            alembic_config=resolve_runtime_path("backend/alembic.ini"),
        )
        if not await health.ping():
            raise RuntimeConfigurationError("campaign scan cannot reach PostgreSQL")
        revisions = await health.migration_revisions()
        if revisions.current != revisions.head:
            raise RuntimeConfigurationError("campaign scan requires PostgreSQL at the Alembic head")
        await sync_source_registry(configured_database, bank_registry)
        policy = HostPolicy(
            min_interval_seconds=settings.ingest_per_host_delay_seconds,
            max_response_bytes=settings.ingest_max_bytes,
            user_agent=settings.ingest_user_agent,
        )
        ingestor = HttpIngestor(
            registry=bank_registry,
            artifact_store=PrivateFileArtifactStore(settings.private_raw_dir),
            response_cache=InMemoryResponseCache(),
            policy_provider=StaticHostPolicyProvider(default=policy),
        )
        return await run_campaign_scan(
            scan_run_id=run_id,
            bank_registry=bank_registry,
            campaign_registry=campaign_registry,
            collector=ingestor,
            store=PostgresCampaignScanStore(configured_database),
        )
    finally:
        if ingestor is not None:
            await ingestor.aclose()
        if owns_database:
            await configured_database.dispose()


async def run_campaign_scan(
    *,
    scan_run_id: str,
    bank_registry: BankRegistry,
    campaign_registry: MonitoredCampaignSourceRegistry,
    collector: CampaignIndexCollector,
    store: CampaignScanStore,
) -> CampaignScanResult:
    """Functional entrypoint used by tests and runtime composition."""

    return await CampaignScanner(
        bank_registry=bank_registry,
        campaign_registry=campaign_registry,
        collector=collector,
        store=store,
    ).run(scan_run_id)
