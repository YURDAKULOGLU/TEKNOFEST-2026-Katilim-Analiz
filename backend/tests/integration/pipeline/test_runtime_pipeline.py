from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from katilim_analiz.application.processing import ProcessOutcome, ProcessSourceUseCase
from katilim_analiz.config import AppEnvironment, ModelProfile, Settings
from katilim_analiz.extraction import ExtractionPipeline
from katilim_analiz.ingestion import (
    HostPolicy,
    HttpIngestor,
    InMemoryResponseCache,
    MemoryArtifactStore,
    StaticAddressResolver,
    StaticHostPolicyProvider,
)
from katilim_analiz.runtime.adapters import (
    HttpCollectionAdapter,
    PipelineExtractionAdapter,
    PostgresCampaignVersionStore,
    StructuredJobOutcomeLogger,
)
from katilim_analiz.runtime.registry import (
    build_source_request,
    enqueue_source_job,
    load_runtime_registry,
    sync_source_registry,
)
from katilim_analiz.runtime.worker import (
    PostgresJobStore,
    WorkerRunner,
    build_worker_runtime,
)
from katilim_analiz.storage.models import (
    CampaignObservationRow,
    CampaignRecordRow,
    CleanDocumentRow,
    CoverageEntryRow,
    DurableJob,
    ExtractionCandidateRow,
    FetchArtifactRow,
    FetchDocumentLink,
    OutboxEvent,
    Source,
)
from katilim_analiz.storage.write_adapter import PostgresCampaignWriteAdapter

NOW = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)
SOURCE_URL = "https://dunyakatilim.com.tr/kampanyalar/network"
RAW_HTML = """
<html lang="tr"><head><title>Konut Finansmanı İndirimi</title></head>
<body><main><h1>Konut Finansmanı İndirimi</h1>
<p>Yalnızca yeni müşterilere ve bireysel müşterilere özel aylık %10 indirim oranı,
mobil şubeden başvuranlara sunulur.</p>
<p>100.000 TL finansman tutarı için 12 ay vade sunulur.</p>
<p>Kampanya 1 Temmuz 2026 - 31 Ağustos 2026 tarihleri arasında geçerlidir.</p>
<p>Finansman tahsis ücreti 1.000 TL'dir.</p>
</main></body></html>
""".encode()


@pytest.mark.asyncio
async def test_worker_composition_checks_migrations_and_builds_network_role(
    database,  # type: ignore[no-untyped-def]
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        database_url=str(database.engine.url),
        model_profile=ModelProfile.RULES_ONLY,
        ingest_network_enabled=True,
        private_raw_dir=tmp_path / "private-raw",
    )
    runtime = await build_worker_runtime(
        settings,
        worker_id="composition-e2e",
        database=database,
        poll_seconds=0.01,
    )
    try:
        assert runtime.runner is not None
        assert runtime.ingestor is not None
        assert runtime.model_client is None
        async with database.session() as session:
            assert (
                await session.execute(select(func.count()).select_from(Source))
            ).scalar_one() == 10
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_registry_job_worker_pipeline_persists_to_real_postgres(database) -> None:  # type: ignore[no-untyped-def]
    registry = load_runtime_registry()
    await sync_source_registry(database, registry)
    source = build_source_request(
        registry,
        bank_id="dunya-katilim",
        source_url=SOURCE_URL,
    )
    job_id, created = await enqueue_source_job(database, registry, source)
    assert created

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Host"] == "dunyakatilim.com.tr"
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"User-agent: *\nAllow: /\n",
                request=request,
            )
        assert request.url.path == "/kampanyalar/network"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=RAW_HTML,
            request=request,
        )

    transport = httpx.MockTransport(handler)
    ingestor = HttpIngestor(
        registry=registry,
        artifact_store=MemoryArtifactStore(),
        response_cache=InMemoryResponseCache(),
        transport=transport,
        resolver=StaticAddressResolver({"dunyakatilim.com.tr": ["1.1.1.1"]}),
        policy_provider=StaticHostPolicyProvider(
            default=HostPolicy(
                min_interval_seconds=0,
                request_timeout_seconds=2,
                max_attempts=1,
                max_response_bytes=1_000_000,
                user_agent="KatilimAnalizBot/0.1",
            )
        ),
        clock=lambda: NOW,
    )
    processor = ProcessSourceUseCase(
        collection=HttpCollectionAdapter(ingestor),
        extraction=PipelineExtractionAdapter(
            ExtractionPipeline(model_enabled=False),
            PostgresCampaignVersionStore(database),
        ),
        writes=PostgresCampaignWriteAdapter(database.session_factory),
        job_outcomes=StructuredJobOutcomeLogger(),
    )
    runner = WorkerRunner(
        jobs=PostgresJobStore(database),
        processor=processor,
        worker_id="pipeline-e2e",
        poll_seconds=0.01,
    )

    async with database.session() as session:
        queued = await session.get(DurableJob, job_id)
        database_now = (await session.execute(select(func.now()))).scalar_one()
    assert queued is not None
    assert queued.status == "queued"
    assert queued.available_at <= database_now, (queued.available_at, database_now)

    try:
        assert await runner.run_once(asyncio.Event())
    finally:
        await ingestor.aclose()
        await transport.aclose()

    async with database.session() as session:
        job = await session.get(DurableJob, job_id)
        source_count = (
            await session.execute(select(func.count()).select_from(Source))
        ).scalar_one()
        fetch_count = (
            await session.execute(
                select(func.count())
                .select_from(FetchArtifactRow)
                .where(FetchArtifactRow.source_id == source.bank_id)
            )
        ).scalar_one()
        document_count = (
            await session.execute(
                select(func.count())
                .select_from(CleanDocumentRow)
                .where(CleanDocumentRow.source_id == source.bank_id)
            )
        ).scalar_one()
        campaign_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.bank_id == source.bank_id)
            )
        ).scalar_one()
        coverage_count = (
            await session.execute(
                select(func.count())
                .select_from(CoverageEntryRow)
                .where(CoverageEntryRow.source_id == source.bank_id)
            )
        ).scalar_one()

    assert job is not None
    assert job.status == "succeeded"
    assert job.attempts == 1
    assert job.result is not None
    assert job.result["bank_id"] == source.bank_id
    assert job.result["record_id"]
    assert source_count == 10
    assert (fetch_count, document_count, campaign_count, coverage_count) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_fresh_ingestors_treat_same_html_200_as_unchanged_content(database) -> None:  # type: ignore[no-untyped-def]
    registry = load_runtime_registry()
    await sync_source_registry(database, registry)
    replay_source_url = "https://dunyakatilim.com.tr/kampanyalar/replay-idempotency"
    source = build_source_request(
        registry,
        bank_id="dunya-katilim",
        source_url=replay_source_url,
    )
    campaign_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal campaign_requests
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"User-agent: *\nAllow: /\n",
                request=request,
            )
        assert request.url.path == "/kampanyalar/replay-idempotency"
        assert "If-None-Match" not in request.headers
        assert "If-Modified-Since" not in request.headers
        campaign_requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=RAW_HTML,
            request=request,
        )

    async def process_at(observed_at: datetime):
        transport = httpx.MockTransport(handler)
        ingestor = HttpIngestor(
            registry=registry,
            artifact_store=MemoryArtifactStore(),
            response_cache=InMemoryResponseCache(),
            transport=transport,
            resolver=StaticAddressResolver({"dunyakatilim.com.tr": ["1.1.1.1"]}),
            policy_provider=StaticHostPolicyProvider(
                default=HostPolicy(
                    min_interval_seconds=0,
                    request_timeout_seconds=2,
                    max_attempts=1,
                    max_response_bytes=1_000_000,
                    user_agent="KatilimAnalizBot/0.1",
                )
            ),
            clock=lambda: observed_at,
        )
        processor = ProcessSourceUseCase(
            collection=HttpCollectionAdapter(ingestor),
            extraction=PipelineExtractionAdapter(
                ExtractionPipeline(model_enabled=False),
                PostgresCampaignVersionStore(database),
            ),
            writes=PostgresCampaignWriteAdapter(database.session_factory),
            job_outcomes=StructuredJobOutcomeLogger(),
        )
        try:
            return await processor.execute(
                source.model_copy(update={"scan_run_id": f"replay-{observed_at.isoformat()}"})
            )
        finally:
            await ingestor.aclose()
            await transport.aclose()

    first = await process_at(NOW)
    second = await process_at(NOW.replace(minute=1))

    assert first.outcome in {ProcessOutcome.RECORD_CREATED, ProcessOutcome.REVIEW_REQUIRED}
    assert second.outcome is ProcessOutcome.RECORD_UNCHANGED
    assert second.record_id == first.record_id
    assert campaign_requests == 2

    async with database.session() as session:
        fetches = (
            (
                await session.execute(
                    select(FetchArtifactRow)
                    .where(
                        FetchArtifactRow.source_id == source.bank_id,
                        FetchArtifactRow.requested_url == replay_source_url,
                    )
                    .order_by(FetchArtifactRow.fetched_at)
                )
            )
            .scalars()
            .all()
        )
        fetch_link_count = (
            await session.execute(
                select(func.count())
                .select_from(FetchDocumentLink)
                .join(
                    FetchArtifactRow,
                    FetchDocumentLink.fetch_artifact_id == FetchArtifactRow.id,
                )
                .where(FetchArtifactRow.requested_url == replay_source_url)
            )
        ).scalar_one()
        document_count = (
            await session.execute(
                select(func.count())
                .select_from(CleanDocumentRow)
                .where(CleanDocumentRow.canonical_url == replay_source_url)
            )
        ).scalar_one()
        candidate_count = (
            await session.execute(
                select(func.count())
                .select_from(ExtractionCandidateRow)
                .join(
                    CleanDocumentRow,
                    ExtractionCandidateRow.source_document_id == CleanDocumentRow.id,
                )
                .where(CleanDocumentRow.canonical_url == replay_source_url)
            )
        ).scalar_one()
        record_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.campaign_key == source.campaign_key)
            )
        ).scalar_one()
        observation_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignObservationRow)
                .where(CampaignObservationRow.campaign_key == source.campaign_key)
            )
        ).scalar_one()
        outbox_count = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == source.campaign_key)
            )
        ).scalar_one()

    assert [(fetch.http_status, fetch.fetched_at) for fetch in fetches] == [
        (200, NOW),
        (200, NOW.replace(minute=1)),
    ]
    assert (fetch_link_count, document_count, candidate_count, record_count) == (2, 1, 1, 1)
    assert (observation_count, outbox_count) == (2, 1)
