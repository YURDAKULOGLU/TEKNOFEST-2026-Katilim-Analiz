from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from factories import (
    campaign_data,
    candidate,
    clean_document,
    evidence,
    fetch_artifact,
    record,
)
from sqlalchemy import func, select

from katilim_analiz.application.processing import (
    CoverageObservation,
    PersistenceBundle,
    SourceRequest,
)
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CleanDocument,
    CoverageStatus,
    EvidenceRef,
    ExtractionCandidate,
    FetchArtifact,
    RecordStatus,
)
from katilim_analiz.storage.models import (
    CampaignObservationRow,
    CampaignRecordRow,
    CleanDocumentRow,
    CoverageEntryRow,
    ExtractionCandidateRow,
    FetchArtifactRow,
    OutboxEvent,
)
from katilim_analiz.storage.repositories import (
    EvidenceIntegrityError,
    ImmutableConflictError,
    SourceRepository,
)
from katilim_analiz.storage.serialization import canonical_sha256
from katilim_analiz.storage.write_adapter import (
    PostgresCampaignWriteAdapter,
    RegistrySourceMismatchError,
    RegistrySourceMissingError,
)


def _bundle(prefix: str | None = None, *, include_record: bool = True) -> PersistenceBundle:
    unique = prefix or uuid4().hex[:12]
    bank_id = f"write-{unique}"
    bank_name = f"Yazma Test Katılım {unique}"
    host = f"{unique}.write.example.test"
    source_url = f"https://{host}/kampanya"
    document_hash = hashlib.sha256(f"document:{unique}".encode()).hexdigest()

    fetched = FetchArtifact.model_validate(
        {
            **fetch_artifact().model_dump(),
            "id": f"fetch:write:{unique}",
            "bank_id": bank_id,
            "requested_url": source_url,
            "final_url": source_url,
        }
    )
    source_block = clean_document().blocks[0].model_copy(update={"id": f"block:write:{unique}"})
    document = CleanDocument.model_validate(
        {
            **clean_document().model_dump(),
            "id": f"clean:{document_hash}",
            "fetch_artifact_id": fetched.id,
            "bank_id": bank_id,
            "canonical_url": source_url,
            "clean_sha256": document_hash,
            "blocks": [source_block],
        }
    )
    evidence_ref = EvidenceRef.model_validate(
        {
            **evidence().model_dump(),
            "id": f"evidence:write:{unique}",
            "source_document_id": document.id,
            "block_id": source_block.id,
        }
    )
    data = CampaignData.model_validate({**campaign_data().model_dump(), "bank_id": bank_id})
    extraction = candidate().metadata
    extracted = ExtractionCandidate.model_validate(
        {
            **candidate().model_dump(),
            "id": f"candidate:write:{unique}",
            "source_document_id": document.id,
            "data": data,
            "evidence": [evidence_ref],
        }
    )
    campaign = CampaignRecord.model_validate(
        {
            **record().model_dump(),
            "id": f"record:write:{unique}",
            "source_document_id": document.id,
            "data": data,
            "evidence": [evidence_ref],
            "extraction": extraction,
            "record_sha256": canonical_sha256(
                {
                    "source_document_id": document.id,
                    "data": data,
                    "extraction": extraction,
                }
            ),
        }
    )
    return PersistenceBundle(
        source=SourceRequest(
            bank_id=bank_id,
            bank_name=bank_name,
            source_url=source_url,
            campaign_key=f"{bank_id}:campaign",
            job_id=f"job:{unique}",
        ),
        fetch_artifact=fetched,
        clean_document=document,
        candidate=extracted,
        record=campaign if include_record else None,
        coverage_observation=CoverageObservation(
            bank_id=bank_id,
            bank_name=bank_name,
            observed_at=fetched.fetched_at,
            status=CoverageStatus.SUCCESS,
        ),
    )


async def _seed_source(
    session,  # type: ignore[no-untyped-def]
    bundle: PersistenceBundle,
    *,
    legal_name: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> None:
    host = bundle.source.source_url.host
    assert host is not None
    await SourceRepository(session).upsert(
        source_id=bundle.source.bank_id,
        registry_version=f"write-{bundle.source.bank_id}",
        listing_order=1,
        legal_name=legal_name or bundle.source.bank_name,
        homepage_url=f"https://{host}",
        allowed_hosts=allowed_hosts or [host],
        digital_bank=False,
    )


@pytest.mark.asyncio
async def test_bundle_is_atomic_idempotent_and_coverage_uses_database_facts(database) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle()
    async with database.transaction() as session:
        await _seed_source(session, bundle)

    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    first = await adapter.persist(bundle)
    second = await adapter.persist(bundle)

    assert first.record_id is not None
    assert first.record_id.startswith("record:")
    assert first.record_id != (bundle.record.id if bundle.record is not None else None)
    assert first.record_created is True
    assert second.record_id == first.record_id
    assert second.record_created is False
    assert first.coverage.source_count == 1
    assert first.coverage.campaign_count == 1
    assert second.coverage == first.coverage

    async with database.session() as session:
        fetch_count = (
            await session.execute(
                select(func.count())
                .select_from(FetchArtifactRow)
                .where(FetchArtifactRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
        campaign_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.bank_id == bundle.source.bank_id)
            )
        ).scalar_one()
        coverage_count = (
            await session.execute(
                select(func.count())
                .select_from(CoverageEntryRow)
                .where(CoverageEntryRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
    assert (fetch_count, campaign_count, coverage_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_bundle_without_record_reports_zero_campaigns(database) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle(include_record=False)
    async with database.transaction() as session:
        await _seed_source(session, bundle)

    result = await PostgresCampaignWriteAdapter(database.session_factory).persist(bundle)

    assert result.record_id is None
    assert result.record_created is None
    assert result.coverage.source_count == 1
    assert result.coverage.campaign_count == 0


@pytest.mark.asyncio
async def test_coverage_counts_only_latest_validated_logical_campaigns(database) -> None:  # type: ignore[no-untyped-def]
    original = _bundle()
    assert original.record is not None
    review_record = original.record.model_copy(
        update={
            "status": RecordStatus.NEEDS_REVIEW,
            "record_sha256": hashlib.sha256(
                f"{original.source.campaign_key}:review".encode()
            ).hexdigest(),
        }
    )
    review_bundle = original.model_copy(update={"record": review_record})
    async with database.transaction() as session:
        await _seed_source(session, original)

    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    review_result = await adapter.persist(review_bundle)
    assert review_result.coverage.campaign_count == 0

    validated_record = review_record.model_copy(
        update={
            "id": f"{review_record.id}:validated",
            "version": 2,
            "observed_at": review_record.observed_at + timedelta(minutes=1),
            "status": RecordStatus.VALIDATED,
            "record_sha256": hashlib.sha256(
                f"{original.source.campaign_key}:validated".encode()
            ).hexdigest(),
        }
    )
    validated_observation = original.coverage_observation.model_copy(
        update={"observed_at": original.coverage_observation.observed_at + timedelta(minutes=1)}
    )
    validated_result = await adapter.persist(
        original.model_copy(
            update={
                "source": original.source.model_copy(
                    update={"job_id": f"{original.source.job_id}:validated"}
                ),
                "record": validated_record,
                "coverage_observation": validated_observation,
            }
        )
    )
    assert validated_result.coverage.campaign_count == 1

    rejected_record = validated_record.model_copy(
        update={
            "id": f"{review_record.id}:rejected",
            "version": 3,
            "observed_at": validated_record.observed_at + timedelta(minutes=1),
            "status": RecordStatus.REJECTED,
            "record_sha256": hashlib.sha256(
                f"{original.source.campaign_key}:rejected".encode()
            ).hexdigest(),
        }
    )
    rejected_observation = original.coverage_observation.model_copy(
        update={"observed_at": original.coverage_observation.observed_at + timedelta(minutes=2)}
    )
    rejected_result = await adapter.persist(
        original.model_copy(
            update={
                "source": original.source.model_copy(
                    update={"job_id": f"{original.source.job_id}:rejected"}
                ),
                "record": rejected_record,
                "coverage_observation": rejected_observation,
            }
        )
    )
    assert rejected_result.coverage.campaign_count == 0


@pytest.mark.asyncio
async def test_registry_is_required_and_identity_and_hosts_fail_closed(database) -> None:  # type: ignore[no-untyped-def]
    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    missing = _bundle()
    with pytest.raises(RegistrySourceMissingError):
        await adapter.persist(missing)

    wrong_name = _bundle()
    async with database.transaction() as session:
        await _seed_source(session, wrong_name, legal_name="Başka Katılım")
    with pytest.raises(RegistrySourceMismatchError, match="legal name"):
        await adapter.persist(wrong_name)

    wrong_host = _bundle()
    async with database.transaction() as session:
        await _seed_source(session, wrong_host, allowed_hosts=["allowed.example.test"])
    with pytest.raises(RegistrySourceMismatchError, match="allowlist"):
        await adapter.persist(wrong_host)

    async with database.session() as session:
        persisted = (
            await session.execute(
                select(func.count())
                .select_from(FetchArtifactRow)
                .where(
                    FetchArtifactRow.source_id.in_(
                        [
                            missing.source.bank_id,
                            wrong_name.source.bank_id,
                            wrong_host.source.bank_id,
                        ]
                    )
                )
            )
        ).scalar_one()
    assert persisted == 0


@pytest.mark.asyncio
async def test_record_write_without_scan_or_durable_job_identity_fails_closed(database) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle()
    missing_identity = bundle.model_copy(
        update={
            "source": bundle.source.model_copy(
                update={"job_id": None, "scan_run_id": None, "observation_key": None}
            )
        }
    )
    async with database.transaction() as session:
        await _seed_source(session, bundle)

    with pytest.raises(ValueError, match="scan_run_id or durable job_id"):
        await PostgresCampaignWriteAdapter(database.session_factory).persist(missing_identity)

    async with database.session() as session:
        fetch_count = (
            await session.execute(
                select(func.count())
                .select_from(FetchArtifactRow)
                .where(FetchArtifactRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
    assert fetch_count == 0


@pytest.mark.asyncio
async def test_downstream_integrity_failure_rolls_back_every_bundle_write(database) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle()
    assert bundle.candidate is not None
    bad_evidence = bundle.candidate.evidence[0].model_copy(update={"evidence_sha256": "f" * 64})
    bad_candidate = bundle.candidate.model_copy(update={"evidence": [bad_evidence]})
    broken = bundle.model_copy(update={"candidate": bad_candidate, "record": None})
    async with database.transaction() as session:
        await _seed_source(session, bundle)

    with pytest.raises(EvidenceIntegrityError, match="evidence_sha256"):
        await PostgresCampaignWriteAdapter(database.session_factory).persist(broken)

    async with database.session() as session:
        fetch_count = (
            await session.execute(
                select(func.count())
                .select_from(FetchArtifactRow)
                .where(FetchArtifactRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
        document_count = (
            await session.execute(
                select(func.count())
                .select_from(CleanDocumentRow)
                .where(CleanDocumentRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
        coverage_count = (
            await session.execute(
                select(func.count())
                .select_from(CoverageEntryRow)
                .where(CoverageEntryRow.source_id == bundle.source.bank_id)
            )
        ).scalar_one()
    assert (fetch_count, document_count, coverage_count) == (0, 0, 0)


@pytest.mark.asyncio
async def test_candidate_reuse_ignores_times_but_semantic_collision_fails(database) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle(include_record=False)
    assert bundle.candidate is not None
    async with database.transaction() as session:
        await _seed_source(session, bundle)
    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    await adapter.persist(bundle)

    shifted_metadata = bundle.candidate.metadata.model_copy(
        update={
            "started_at": bundle.candidate.metadata.started_at + timedelta(hours=2),
            "completed_at": bundle.candidate.metadata.completed_at + timedelta(hours=2),
        }
    )
    reused = bundle.model_copy(
        update={"candidate": bundle.candidate.model_copy(update={"metadata": shifted_metadata})}
    )
    await adapter.persist(reused)

    changed_data = bundle.candidate.data.model_copy(
        update={"title": f"{bundle.candidate.data.title} changed"}
    )
    collision = bundle.model_copy(
        update={"candidate": bundle.candidate.model_copy(update={"data": changed_data})}
    )
    with pytest.raises(ImmutableConflictError, match="different content"):
        await adapter.persist(collision)

    async with database.session() as session:
        candidates = (
            (
                await session.execute(
                    select(ExtractionCandidateRow).where(
                        ExtractionCandidateRow.id == bundle.candidate.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(candidates) == 1
    assert candidates[0].started_at == bundle.candidate.metadata.started_at
    assert candidates[0].data["title"] == bundle.candidate.data.title


def _observed_bundle(
    bundle: PersistenceBundle,
    *,
    scan_run_id: str,
    record_sha256: str,
    minute: int,
    status: RecordStatus = RecordStatus.VALIDATED,
    shift_candidate_times: bool = False,
) -> PersistenceBundle:
    assert bundle.record is not None
    assert bundle.candidate is not None
    observed_at = bundle.record.observed_at + timedelta(minutes=minute)
    candidate_value = bundle.candidate
    if shift_candidate_times:
        metadata = candidate_value.metadata.model_copy(
            update={
                "started_at": candidate_value.metadata.started_at + timedelta(hours=1),
                "completed_at": candidate_value.metadata.completed_at + timedelta(hours=1),
            }
        )
        candidate_value = candidate_value.model_copy(update={"metadata": metadata})
    issues = [] if status is RecordStatus.VALIDATED else ["semantic_change"]
    record_value = bundle.record.model_copy(
        update={
            "id": f"provisional:{scan_run_id}",
            "version": 99,
            "observed_at": observed_at,
            "extraction": candidate_value.metadata,
            "status": status,
            "validation_issues": issues,
            "record_sha256": record_sha256,
        }
    )
    return bundle.model_copy(
        update={
            "source": bundle.source.model_copy(
                update={
                    "job_id": None,
                    "scan_run_id": scan_run_id,
                    "observation_key": None,
                }
            ),
            "candidate": candidate_value,
            "record": record_value,
            "coverage_observation": bundle.coverage_observation.model_copy(
                update={"observed_at": observed_at}
            ),
        }
    )


@pytest.mark.asyncio
async def test_observations_preserve_a_to_b_to_a_and_old_retry_mapping(database) -> None:  # type: ignore[no-untyped-def]
    base = _bundle()
    assert base.record is not None
    assert base.candidate is not None
    record_a = "a" * 64
    record_b = "b" * 64
    first_a = _observed_bundle(
        base,
        scan_run_id="scan-a-1",
        record_sha256=record_a,
        minute=0,
    )
    second_a = _observed_bundle(
        base,
        scan_run_id="scan-a-2",
        record_sha256=record_a,
        minute=1,
        shift_candidate_times=True,
    )
    changed_b = _observed_bundle(
        base,
        scan_run_id="scan-b",
        record_sha256=record_b,
        minute=2,
        status=RecordStatus.NEEDS_REVIEW,
        shift_candidate_times=True,
    )
    third_a = _observed_bundle(
        base,
        scan_run_id="scan-a-3",
        record_sha256=record_a,
        minute=3,
        shift_candidate_times=True,
    )
    async with database.transaction() as session:
        await _seed_source(session, base)

    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    created_a = await adapter.persist(first_a)
    unchanged_a = await adapter.persist(second_a)
    created_b = await adapter.persist(changed_b)
    reverted_a = await adapter.persist(third_a)
    old_retry = await adapter.persist(first_a)

    assert created_a.record_created is True
    assert unchanged_a.record_created is False
    assert unchanged_a.record_id == created_a.record_id
    assert created_b.record_created is True
    assert reverted_a.record_created is True
    assert old_retry.record_created is False
    assert old_retry.record_id == created_a.record_id
    assert len({created_a.record_id, created_b.record_id, reverted_a.record_id}) == 3

    async with database.session() as session:
        records = (
            (
                await session.execute(
                    select(CampaignRecordRow)
                    .where(CampaignRecordRow.campaign_key == base.source.campaign_key)
                    .order_by(CampaignRecordRow.version)
                )
            )
            .scalars()
            .all()
        )
        observations = (
            await session.execute(
                select(func.count())
                .select_from(CampaignObservationRow)
                .where(CampaignObservationRow.campaign_key == base.source.campaign_key)
            )
        ).scalar_one()
        outbox = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.aggregate_id == base.source.campaign_key)
                    .order_by(OutboxEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        )
        candidates = (
            (
                await session.execute(
                    select(ExtractionCandidateRow).where(
                        ExtractionCandidateRow.id == base.candidate.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert [item.version for item in records] == [1, 2, 3]
    assert [item.record_sha256 for item in records] == [record_a, record_b, record_a]
    assert observations == 4
    assert len(candidates) == 1
    assert candidates[0].started_at == base.candidate.metadata.started_at
    assert [event.payload["change_kind"] for event in outbox] == [
        "created",
        "updated",
        "updated",
    ]
    assert [event.payload["record_status"] for event in outbox] == [
        RecordStatus.VALIDATED.value,
        RecordStatus.NEEDS_REVIEW.value,
        RecordStatus.VALIDATED.value,
    ]
    assert [event.payload["record_version"] for event in outbox] == [1, 2, 3]
    assert [event.payload["previous_record_id"] for event in outbox] == [
        None,
        created_a.record_id,
        created_b.record_id,
    ]
    assert all(
        set(event.payload)
        == {
            "campaign_key",
            "record_id",
            "record_version",
            "change_kind",
            "record_status",
            "previous_record_id",
            "observed_at",
        }
        for event in outbox
    )
    assert all(event.topic == "notifications.campaigns.v1" for event in outbox)
    assert all(event.event_type == "campaign_record.changed.v1" for event in outbox)
    assert all(event.aggregate_type == "campaign" for event in outbox)
    assert all(
        event.dedupe_key == f"campaign-change:{event.payload['record_id']}" for event in outbox
    )


@pytest.mark.asyncio
async def test_concurrent_observations_of_one_change_share_one_version_and_outbox(database) -> None:  # type: ignore[no-untyped-def]
    base = _bundle()
    first = _observed_bundle(
        base,
        scan_run_id="concurrent-a",
        record_sha256="c" * 64,
        minute=0,
    )
    left = _observed_bundle(
        base,
        scan_run_id="concurrent-b-left",
        record_sha256="d" * 64,
        minute=1,
        status=RecordStatus.NEEDS_REVIEW,
    )
    right = _observed_bundle(
        base,
        scan_run_id="concurrent-b-right",
        record_sha256="d" * 64,
        minute=1,
        status=RecordStatus.NEEDS_REVIEW,
    )
    async with database.transaction() as session:
        await _seed_source(session, base)
    adapter = PostgresCampaignWriteAdapter(database.session_factory)
    await adapter.persist(first)

    left_result, right_result = await asyncio.gather(
        adapter.persist(left),
        adapter.persist(right),
    )

    assert left_result.record_id == right_result.record_id
    assert sorted([left_result.record_created, right_result.record_created]) == [False, True]
    async with database.session() as session:
        record_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.campaign_key == base.source.campaign_key)
            )
        ).scalar_one()
        observation_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignObservationRow)
                .where(CampaignObservationRow.campaign_key == base.source.campaign_key)
            )
        ).scalar_one()
        outbox_count = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == base.source.campaign_key)
            )
        ).scalar_one()
    assert (record_count, observation_count, outbox_count) == (2, 3, 2)


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_record_observation_and_outbox(
    database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle()
    async with database.transaction() as session:
        await _seed_source(session, bundle)

    async def fail_outbox(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced_outbox_failure")

    monkeypatch.setattr("katilim_analiz.storage.write_adapter.OutboxRepository.add", fail_outbox)
    with pytest.raises(RuntimeError, match="forced_outbox_failure"):
        await PostgresCampaignWriteAdapter(database.session_factory).persist(bundle)

    async with database.session() as session:
        record_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignRecordRow)
                .where(CampaignRecordRow.bank_id == bundle.source.bank_id)
            )
        ).scalar_one()
        observation_count = (
            await session.execute(
                select(func.count())
                .select_from(CampaignObservationRow)
                .where(CampaignObservationRow.campaign_key == bundle.source.campaign_key)
            )
        ).scalar_one()
        outbox_count = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == bundle.source.campaign_key)
            )
        ).scalar_one()
        candidate_count = (
            await session.execute(
                select(func.count())
                .select_from(ExtractionCandidateRow)
                .where(ExtractionCandidateRow.bank_id == bundle.source.bank_id)
            )
        ).scalar_one()
    assert (record_count, observation_count, outbox_count, candidate_count) == (0, 0, 0, 0)
