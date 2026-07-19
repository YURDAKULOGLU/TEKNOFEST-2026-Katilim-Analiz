from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update

from katilim_analiz.storage import repositories as repository_module
from katilim_analiz.storage.models import AuthSession, DurableJob, OutboxEvent
from katilim_analiz.storage.repositories import (
    AuthRepository,
    JobRepository,
    LeaseLostError,
    OutboxRepository,
)


class _AheadApplicationDateTime:
    @staticmethod
    def now(timezone: tzinfo | None = None) -> datetime:
        return datetime.now(timezone) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_generic_prepared_claim_plan_can_use_partial_index(database) -> None:  # type: ignore[no-untyped-def]
    async with database.transaction() as session:
        await session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        await session.execute(
            text(
                "PREPARE claim_job(text, text) AS "
                "SELECT * FROM durable_jobs "
                "WHERE status IN ('queued','running') "
                "AND available_at <= now() "
                "AND attempts < max_attempts "
                "AND (status = $1 OR (status = $2 AND lease_expires_at <= now())) "
                "ORDER BY priority DESC, available_at, created_at "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        )
        plan = str(
            (
                await session.execute(
                    text("EXPLAIN (COSTS OFF, FORMAT JSON) EXECUTE claim_job('queued','running')")
                )
            ).scalar_one()
        )
        await session.execute(text("DEALLOCATE claim_job"))

    assert "ix_durable_jobs_claim" in plan


@pytest.mark.asyncio
async def test_concurrent_workers_skip_locked_jobs_and_fence_stale_completion(
    database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(repository_module, "datetime", _AheadApplicationDateTime)
    async with database.transaction() as session:
        jobs = JobRepository(session)
        first_id, _ = await jobs.enqueue("extract", {"document": "a"}, dedupe_key="extract:a")
        second_id, _ = await jobs.enqueue("extract", {"document": "b"}, dedupe_key="extract:b")

    async with (
        database.session_factory() as first_session,
        database.session_factory() as second_session,
    ):
        await first_session.begin()
        await second_session.begin()
        first_lease = await JobRepository(first_session).claim_next("worker-a")
        second_lease = await JobRepository(second_session).claim_next("worker-b")
        assert first_lease is not None
        assert second_lease is not None
        assert {first_lease.id, second_lease.id} == {first_id, second_id}
        await first_session.commit()
        await second_session.commit()

    async with database.transaction() as session:
        with pytest.raises(LeaseLostError):
            await JobRepository(session).complete(replace(first_lease, token=uuid4()))

    async with database.transaction() as session:
        await JobRepository(session).complete(first_lease, {"record": "ready"})
        await JobRepository(session).complete(second_lease)

    async with database.session() as session:
        statuses = dict((await session.execute(select(DurableJob.id, DurableJob.status))).all())
    assert statuses[first_id] == "succeeded"
    assert statuses[second_id] == "succeeded"


@pytest.mark.asyncio
async def test_outbox_is_deduplicated_leased_and_published(database, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(repository_module, "datetime", _AheadApplicationDateTime)
    async with database.transaction() as session:
        outbox = OutboxRepository(session)
        event_id, created = await outbox.add(
            topic="campaigns",
            event_type="campaign.changed.v1",
            aggregate_type="campaign",
            aggregate_id="bank-a:kampanya",
            payload={"record_id": "record:1"},
            dedupe_key="record:1:changed",
        )
        duplicate_id, duplicate_created = await outbox.add(
            topic="campaigns",
            event_type="campaign.changed.v1",
            aggregate_type="campaign",
            aggregate_id="bank-a:kampanya",
            payload={"record_id": "record:1"},
            dedupe_key="record:1:changed",
        )
        assert created
        assert not duplicate_created
        assert duplicate_id == event_id

    # Preserve the clock-skew assertion: default readiness is based on the
    # database clock, not the monkeypatched application clock. Then make this
    # test's row deterministically first without assuming a globally empty
    # session-scoped outbox left by unrelated integration tests.
    async with database.session() as session:
        available_at, database_now = (
            await session.execute(
                select(OutboxEvent.available_at, func.now()).where(OutboxEvent.id == event_id)
            )
        ).one()
    assert available_at <= database_now
    async with database.transaction() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(available_at=datetime.min.replace(tzinfo=UTC))
        )

    async with database.transaction() as session:
        lease = await OutboxRepository(session).claim_next("publisher-a")
        assert lease is not None
        assert lease.id == event_id

    async with database.transaction() as session:
        await OutboxRepository(session).mark_published(lease)

    async with database.session() as session:
        event = await session.get(OutboxEvent, event_id)
    assert event is not None
    assert event.published_at is not None
    assert event.lease_token is None


@pytest.mark.asyncio
async def test_expired_final_job_attempt_is_moved_to_dead(database) -> None:  # type: ignore[no-untyped-def]
    dedupe_key = f"final-attempt:{uuid4()}"
    async with database.transaction() as session:
        jobs = JobRepository(session)
        job_id, _ = await jobs.enqueue(
            "extract", {"document": "crash"}, dedupe_key=dedupe_key, max_attempts=1
        )

    async with database.transaction() as session:
        lease = await JobRepository(session).claim_next("crashing-worker")
        assert lease is not None
        await session.execute(
            update(DurableJob)
            .where(DurableJob.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )

    async with database.transaction() as session:
        assert await JobRepository(session).claim_next("replacement-worker") is None

    async with database.session() as session:
        job = await session.get(DurableJob, job_id)
    assert job is not None
    assert job.status == "dead"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_auth_seam_persists_only_hashed_session_material(database) -> None:  # type: ignore[no-untyped-def]
    token_hash = "a" * 64
    csrf_hash = "b" * 64
    async with database.transaction() as session:
        auth = AuthRepository(session)
        user_id = await auth.create_user("admin", "$argon2id$v=19$m=65536,t=3,p=4$hash-material")
        session_id = await auth.create_session(
            user_id=user_id,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async with database.session() as session:
        stored = await session.get(AuthSession, session_id)
    assert stored is not None
    assert stored.token_hash == token_hash
    assert stored.csrf_hash == csrf_hash
    assert "raw" not in stored.client_context

    async with database.transaction() as session:
        assert await AuthRepository(session).revoke_session(session_id)
