from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from katilim_analiz.application.processing import (
    JobDisposition,
    ProcessOutcome,
    ProcessSourceResult,
    ProcessStage,
    SourceRequest,
)
from katilim_analiz.contracts import CoverageEntry, CoverageStatus
from katilim_analiz.runtime import worker
from katilim_analiz.runtime.registry import SOURCE_JOB_KIND
from katilim_analiz.runtime.worker import WorkerRunner
from katilim_analiz.storage.repositories import JobLease

NOW = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)


def _lease(*, kind: str = SOURCE_JOB_KIND) -> JobLease:
    return JobLease(
        id=uuid4(),
        kind=kind,
        payload={
            "bank_id": "bank-a",
            "bank_name": "Banka A",
            "source_url": "https://bank.example/kampanya",
            "campaign_key": "bank-a:campaign",
        },
        attempt=1,
        max_attempts=3,
        worker_id="worker-a",
        token=uuid4(),
        expires_at=NOW + timedelta(minutes=2),
    )


def _result(disposition: JobDisposition) -> ProcessSourceResult:
    return ProcessSourceResult(
        job_id=None,
        bank_id="bank-a",
        terminal_stage=ProcessStage.PERSIST,
        outcome=ProcessOutcome.RECORD_CREATED,
        job_disposition=disposition,
        coverage=CoverageEntry(
            bank_id="bank-a",
            bank_name="Banka A",
            observed_at=NOW,
            status=CoverageStatus.SUCCESS,
            source_count=1,
            campaign_count=1,
        ),
        record_id="record:1",
        issues=["retry_reason"] if disposition is JobDisposition.RETRY else [],
    )


class FakeJobs:
    def __init__(self, lease: JobLease | None) -> None:
        self.lease = lease
        self.completed: list[ProcessSourceResult] = []
        self.retries: list[str] = []
        self.renewals = 0

    async def claim(self, worker_id: str, lease_for: timedelta) -> JobLease | None:
        assert worker_id == "worker-a"
        return self.lease

    async def renew(self, lease: JobLease, lease_for: timedelta) -> None:
        self.renewals += 1

    async def complete(self, lease: JobLease, result: ProcessSourceResult) -> None:
        self.completed.append(result)

    async def retry(self, lease: JobLease, error: str, retry_after: timedelta) -> bool:
        self.retries.append(error)
        return True


class ResultProcessor:
    def __init__(self, result: ProcessSourceResult, *, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay

    async def execute(self, source: SourceRequest) -> ProcessSourceResult:
        assert source.job_id is not None
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result.model_copy(update={"job_id": source.job_id})


@pytest.mark.asyncio
async def test_worker_completes_terminal_outcome_and_renews_lease() -> None:
    jobs = FakeJobs(_lease())
    runner = WorkerRunner(
        jobs=jobs,
        processor=ResultProcessor(_result(JobDisposition.SUCCEEDED), delay=0.03),
        worker_id="worker-a",
        lease_for=timedelta(seconds=1),
        heartbeat_seconds=0.005,
    )
    assert await runner.run_once(asyncio.Event())
    assert len(jobs.completed) == 1
    assert jobs.renewals >= 1
    assert jobs.retries == []


@pytest.mark.asyncio
async def test_worker_requeues_retry_disposition_and_poisoned_kind() -> None:
    jobs = FakeJobs(_lease())
    runner = WorkerRunner(
        jobs=jobs,
        processor=ResultProcessor(_result(JobDisposition.RETRY)),
        worker_id="worker-a",
    )
    assert await runner.run_once(asyncio.Event())
    assert jobs.retries == ["retry_reason"]
    assert jobs.completed == []

    poisoned = FakeJobs(_lease(kind="unknown"))
    poisoned_runner = WorkerRunner(
        jobs=poisoned,
        processor=ResultProcessor(_result(JobDisposition.SUCCEEDED)),
        worker_id="worker-a",
    )
    assert await poisoned_runner.run_once(asyncio.Event())
    assert poisoned.retries[0].startswith("invalid_job:unsupported job kind")


@pytest.mark.asyncio
async def test_shutdown_cancels_processing_and_releases_the_lease_for_retry() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProcessor:
        async def execute(self, source: SourceRequest) -> ProcessSourceResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return _result(JobDisposition.SUCCEEDED)

    jobs = FakeJobs(_lease())
    runner = WorkerRunner(
        jobs=jobs,
        processor=BlockingProcessor(),
        worker_id="worker-a",
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_once(stop))
    await started.wait()
    stop.set()
    assert await task
    assert cancelled.is_set()
    assert jobs.retries == ["worker_shutdown"]


@pytest.mark.asyncio
async def test_worker_private_model_client_ignores_environment_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = httpx.AsyncClient()
    constructor_kwargs: dict[str, object] = {}

    def build_client(**kwargs: object) -> httpx.AsyncClient:
        constructor_kwargs.update(kwargs)
        return owned

    monkeypatch.setattr(worker.httpx, "AsyncClient", build_client)

    client = worker._new_private_model_http_client()
    await client.aclose()

    assert constructor_kwargs == {"follow_redirects": False, "trust_env": False}
    assert owned.is_closed
