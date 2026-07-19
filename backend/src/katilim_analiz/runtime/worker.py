"""Durable PostgreSQL worker loop with fenced leases and graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import httpx
from pydantic import ValidationError

from katilim_analiz.application.processing import (
    JobDisposition,
    ProcessSourceResult,
    ProcessSourceUseCase,
    SourceRequest,
)
from katilim_analiz.config import ModelProfile, Settings, get_settings
from katilim_analiz.extraction import pipeline_from_settings
from katilim_analiz.ingestion import (
    HostPolicy,
    HttpIngestor,
    InMemoryResponseCache,
    PrivateFileArtifactStore,
    StaticHostPolicyProvider,
)
from katilim_analiz.ingestion.registry import BankRegistry
from katilim_analiz.runtime.adapters import (
    HttpCollectionAdapter,
    PipelineExtractionAdapter,
    PostgresCampaignVersionStore,
    StructuredJobOutcomeLogger,
)
from katilim_analiz.runtime.composition import RuntimeConfigurationError
from katilim_analiz.runtime.paths import resolve_runtime_path
from katilim_analiz.runtime.registry import (
    SOURCE_JOB_KIND,
    load_runtime_registry,
    sync_source_registry,
)
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.health import PostgresDatabaseHealth
from katilim_analiz.storage.repositories import (
    JobLease,
    JobRepository,
    LeaseLostError,
)
from katilim_analiz.storage.write_adapter import PostgresCampaignWriteAdapter

_LOGGER = logging.getLogger("katilim_analiz.worker")
_DEFAULT_LEASE = timedelta(minutes=2)
_DEFAULT_HEARTBEAT_SECONDS = 30.0
_DEFAULT_RETRY = timedelta(seconds=30)


def _new_private_model_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, trust_env=False)


class WorkerStopping(RuntimeError):
    """The process was asked to release its active lease and stop."""


class SourceProcessor(Protocol):
    async def execute(self, source: SourceRequest) -> ProcessSourceResult: ...


class JobStore(Protocol):
    async def claim(self, worker_id: str, lease_for: timedelta) -> JobLease | None: ...

    async def renew(self, lease: JobLease, lease_for: timedelta) -> None: ...

    async def complete(self, lease: JobLease, result: ProcessSourceResult) -> None: ...

    async def retry(self, lease: JobLease, error: str, retry_after: timedelta) -> bool: ...


class PostgresJobStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, worker_id: str, lease_for: timedelta) -> JobLease | None:
        async with self._database.transaction() as session:
            return await JobRepository(session).claim_next(worker_id, lease_for=lease_for)

    async def renew(self, lease: JobLease, lease_for: timedelta) -> None:
        async with self._database.transaction() as session:
            await JobRepository(session).renew(lease, lease_for=lease_for)

    async def complete(self, lease: JobLease, result: ProcessSourceResult) -> None:
        async with self._database.transaction() as session:
            await JobRepository(session).complete(lease, result.model_dump(mode="json"))

    async def retry(self, lease: JobLease, error: str, retry_after: timedelta) -> bool:
        async with self._database.transaction() as session:
            return await JobRepository(session).fail(
                lease,
                error[:10_000],
                retry_after=retry_after,
            )


class WorkerRunner:
    def __init__(
        self,
        *,
        jobs: JobStore,
        processor: SourceProcessor,
        worker_id: str,
        lease_for: timedelta = _DEFAULT_LEASE,
        heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        retry_after: timedelta = _DEFAULT_RETRY,
        poll_seconds: float = 2.0,
    ) -> None:
        if not worker_id.strip() or len(worker_id) > 191:
            raise ValueError("worker_id must contain 1..191 characters")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_for.total_seconds():
            raise ValueError("heartbeat must be positive and shorter than the lease")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._jobs = jobs
        self._processor = processor
        self._worker_id = worker_id
        self._lease_for = lease_for
        self._heartbeat_seconds = heartbeat_seconds
        self._retry_after = retry_after
        self._poll_seconds = poll_seconds

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        while not stop.is_set():
            processed = await self.run_once(stop)
            if once:
                return
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def run_once(self, stop: asyncio.Event) -> bool:
        if stop.is_set():
            return False
        lease = await self._jobs.claim(self._worker_id, self._lease_for)
        if lease is None:
            return False
        try:
            source = self._source_from_lease(lease)
            result = await self._execute_with_heartbeat(lease, source, stop)
        except WorkerStopping:
            await self._jobs.retry(lease, "worker_shutdown", timedelta(seconds=1))
            return True
        except LeaseLostError:
            _LOGGER.error(
                "worker lost its fenced lease",
                extra={"event": "job_lease_lost", "job_id": str(lease.id)},
                exc_info=True,
            )
            raise
        except (ValidationError, ValueError) as exc:
            await self._jobs.retry(lease, f"invalid_job:{exc}", self._retry_after)
            return True
        except Exception as exc:
            _LOGGER.error(
                "source job execution failed",
                extra={"event": "job_execution_failed", "job_id": str(lease.id)},
                exc_info=True,
            )
            await self._jobs.retry(
                lease,
                f"{type(exc).__name__}:{str(exc) or 'job execution failed'}",
                self._retry_after,
            )
            return True

        if result.job_disposition is JobDisposition.RETRY:
            reason = result.issues[0] if result.issues else result.outcome.value
            await self._jobs.retry(lease, reason, self._retry_after)
        else:
            # FAILED is a terminal, persisted business outcome (for example a
            # non-retryable clean failure); operational execution still completed.
            await self._jobs.complete(lease, result)
        return True

    @staticmethod
    def _source_from_lease(lease: JobLease) -> SourceRequest:
        if lease.kind != SOURCE_JOB_KIND:
            raise ValueError(f"unsupported job kind: {lease.kind}")
        payload = dict(lease.payload)
        payload["job_id"] = str(lease.id)
        return SourceRequest.model_validate(payload)

    async def _heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self._jobs.renew(lease, self._lease_for)

    async def _execute_with_heartbeat(
        self,
        lease: JobLease,
        source: SourceRequest,
        stop: asyncio.Event,
    ) -> ProcessSourceResult:
        processing = asyncio.create_task(self._processor.execute(source))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        shutdown = asyncio.create_task(stop.wait())
        tasks = (processing, heartbeat, shutdown)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                heartbeat.result()
                raise RuntimeError("lease heartbeat stopped unexpectedly")
            if processing in done:
                return processing.result()
            raise WorkerStopping("shutdown requested while a job was active")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(slots=True)
class WorkerRuntime:
    database: Database
    registry: BankRegistry
    runner: WorkerRunner | None
    ingestor: HttpIngestor | None
    model_client: httpx.AsyncClient | None

    async def aclose(self) -> None:
        try:
            if self.ingestor is not None:
                await self.ingestor.aclose()
        finally:
            try:
                if self.model_client is not None:
                    await self.model_client.aclose()
            finally:
                await self.database.dispose()


async def build_worker_runtime(
    settings: Settings,
    *,
    worker_id: str,
    registry_path: str | None = None,
    database: Database | None = None,
    poll_seconds: float = 2.0,
) -> WorkerRuntime:
    registry = load_runtime_registry(registry_path)
    alembic_config = resolve_runtime_path("backend/alembic.ini")
    configured_database = database or Database.from_settings(settings)
    ingestor: HttpIngestor | None = None
    model_client: httpx.AsyncClient | None = None
    health = PostgresDatabaseHealth(
        configured_database.engine,
        alembic_config=alembic_config,
    )
    try:
        if not await health.ping():
            raise RuntimeConfigurationError("worker cannot reach PostgreSQL")
        revisions = await health.migration_revisions()
        if revisions.current != revisions.head:
            raise RuntimeConfigurationError("worker requires PostgreSQL at the Alembic head")
        await sync_source_registry(configured_database, registry)
        if not settings.ingest_network_enabled:
            return WorkerRuntime(configured_database, registry, None, None, None)

        policy = HostPolicy(
            min_interval_seconds=settings.ingest_per_host_delay_seconds,
            max_response_bytes=settings.ingest_max_bytes,
            user_agent=settings.ingest_user_agent,
        )
        ingestor = HttpIngestor(
            registry=registry,
            artifact_store=PrivateFileArtifactStore(settings.private_raw_dir),
            response_cache=InMemoryResponseCache(),
            policy_provider=StaticHostPolicyProvider(default=policy),
        )
        model_client = (
            None
            if settings.model_profile is ModelProfile.RULES_ONLY
            else _new_private_model_http_client()
        )
        pipeline = pipeline_from_settings(settings, http_client=model_client)
        processor = ProcessSourceUseCase(
            collection=HttpCollectionAdapter(ingestor),
            extraction=PipelineExtractionAdapter(
                pipeline,
                PostgresCampaignVersionStore(configured_database),
            ),
            writes=PostgresCampaignWriteAdapter(configured_database.session_factory),
            job_outcomes=StructuredJobOutcomeLogger(),
        )
        runner = WorkerRunner(
            jobs=PostgresJobStore(configured_database),
            processor=processor,
            worker_id=worker_id,
            poll_seconds=poll_seconds,
        )
        return WorkerRuntime(
            configured_database,
            registry,
            runner,
            ingestor,
            model_client,
        )
    except BaseException:
        await WorkerRuntime(
            configured_database,
            registry,
            None,
            ingestor,
            model_client,
        ).aclose()
        raise


async def run_worker(
    settings: Settings | None = None,
    *,
    worker_id: str = "worker-1",
    registry_path: str | None = None,
    poll_seconds: float = 2.0,
    once: bool = False,
    stop: asyncio.Event | None = None,
) -> None:
    effective = settings or get_settings()
    stop_event = stop or asyncio.Event()
    runtime = await build_worker_runtime(
        effective,
        worker_id=worker_id,
        registry_path=registry_path,
        poll_seconds=poll_seconds,
    )
    try:
        if runtime.runner is None:
            _LOGGER.info(
                "network ingestion is disabled; registry is synced and worker is idle",
                extra={"event": "worker_network_disabled"},
            )
            if not once:
                await stop_event.wait()
            return
        await runtime.runner.run(stop_event, once=once)
    finally:
        await runtime.aclose()
