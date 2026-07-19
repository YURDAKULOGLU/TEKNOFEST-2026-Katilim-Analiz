from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from katilim_analiz.application.models import NotificationCursor
from katilim_analiz.notifications import (
    CAMPAIGN_CHANGE_EVENT_TYPE,
    CAMPAIGN_CHANGE_TOPIC,
)
from katilim_analiz.storage.models import OutboxEvent
from katilim_analiz.storage.read_adapter import PostgresCampaignReadAdapter
from katilim_analiz.storage.repositories import OutboxRepository


def _payload(identifier: str, *, version: int = 1) -> dict[str, object]:
    return {
        "campaign_key": f"sequence-bank:{identifier}",
        "record_id": f"record:{identifier}:v{version}",
        "record_version": version,
        "change_kind": "created" if version == 1 else "updated",
        "record_status": "validated",
        "previous_record_id": None if version == 1 else f"record:{identifier}:v{version - 1}",
        "observed_at": datetime.now(UTC).isoformat(),
    }


async def _add_campaign_event(
    session,  # type: ignore[no-untyped-def]
    identifier: str,
    *,
    version: int = 1,
    event_id: UUID | None = None,
    available_at: datetime | None = None,
) -> tuple[UUID, bool]:
    return await OutboxRepository(session).add(
        topic=CAMPAIGN_CHANGE_TOPIC,
        event_type=CAMPAIGN_CHANGE_EVENT_TYPE,
        aggregate_type="campaign",
        aggregate_id=f"sequence-bank:{identifier}",
        payload=_payload(identifier, version=version),
        dedupe_key=f"sequence:{identifier}:v{version}",
        event_id=event_id,
        available_at=available_at,
    )


async def _feed_head(database) -> int:  # type: ignore[no-untyped-def]
    async with database.session() as session:
        return int(
            (
                await session.execute(select(func.coalesce(func.max(OutboxEvent.feed_sequence), 0)))
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_older_started_transaction_cannot_late_commit_behind_advanced_cursor(
    database,
) -> None:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex
    newer_id = uuid4()
    older_id = uuid4()
    adapter = PostgresCampaignReadAdapter(database.session_factory)

    async with (
        database.session_factory() as older,
        database.session_factory() as newer,
    ):
        await older.begin()
        older_started_at = (await older.execute(select(func.now()))).scalar_one()
        baseline = await _feed_head(database)
        await asyncio.sleep(0.01)

        await newer.begin()
        await _add_campaign_event(newer, f"{prefix}:newer", event_id=newer_id)
        await newer.commit()

        first_page = await adapter.list_notifications(
            after=NotificationCursor(feed_sequence=baseline),
            limit=100,
        )
        newer_item = next(item for item in first_page.items if item.event.id == newer_id)

        await _add_campaign_event(older, f"{prefix}:older", event_id=older_id)
        await older.commit()

    second_page = await adapter.list_notifications(
        after=NotificationCursor(feed_sequence=newer_item.feed_sequence),
        limit=100,
    )
    older_item = next(item for item in second_page.items if item.event.id == older_id)

    async with database.session() as session:
        rows = {
            row.id: row
            for row in (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.id.in_([newer_id, older_id]))
                )
            ).scalars()
        }

    assert rows[older_id].created_at == older_started_at
    assert rows[older_id].created_at < rows[newer_id].created_at
    assert older_item.feed_sequence > newer_item.feed_sequence


@pytest.mark.asyncio
async def test_feed_lock_blocks_a_second_allocator_until_first_transaction_finishes(
    database,
) -> None:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex
    async with (
        database.session_factory() as first,
        database.session_factory() as second,
    ):
        await first.begin()
        await second.begin()
        await _add_campaign_event(first, f"{prefix}:first")
        blocked_add = asyncio.create_task(_add_campaign_event(second, f"{prefix}:second"))
        try:
            done, _ = await asyncio.wait({blocked_add}, timeout=0.2)
            assert not done
            await first.commit()
            await asyncio.wait_for(blocked_add, timeout=2)
            await second.commit()
        finally:
            if first.in_transaction():
                await first.rollback()
            if second.in_transaction():
                await second.rollback()
            if not blocked_add.done():
                blocked_add.cancel()


@pytest.mark.asyncio
async def test_rollback_gap_and_non_campaign_gap_do_not_break_pagination(database) -> None:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex
    rolled_back_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    baseline = await _feed_head(database)

    async with database.session_factory() as rolled_back:
        await rolled_back.begin()
        await _add_campaign_event(
            rolled_back,
            f"{prefix}:rollback",
            event_id=rolled_back_id,
        )
        await rolled_back.rollback()

    async with database.transaction() as session:
        await _add_campaign_event(session, prefix, event_id=first_id)
        await OutboxRepository(session).add(
            topic="unrelated.audit.v1",
            event_type="unrelated.v1",
            aggregate_type="test",
            aggregate_id=prefix,
            payload={"safe": True},
            dedupe_key=f"sequence:{prefix}:unrelated",
        )
        await _add_campaign_event(session, prefix, version=2, event_id=second_id)

    async with database.transaction() as session:
        await session.execute(
            OutboxEvent.__table__.update()
            .where(OutboxEvent.id == first_id)
            .values(created_at=func.now() + timedelta(days=1))
        )
        await session.execute(
            OutboxEvent.__table__.update()
            .where(OutboxEvent.id == second_id)
            .values(created_at=func.now() - timedelta(days=1))
        )

    adapter = PostgresCampaignReadAdapter(database.session_factory)
    first_page = await adapter.list_notifications(
        after=NotificationCursor(feed_sequence=baseline),
        limit=1,
    )
    assert [item.event.id for item in first_page.items] == [first_id]

    second_page = await adapter.list_notifications(
        after=NotificationCursor(feed_sequence=first_page.items[0].feed_sequence),
        limit=1,
    )
    assert [item.event.id for item in second_page.items] == [second_id]
    assert second_page.items[0].feed_sequence > first_page.items[0].feed_sequence + 1

    async with database.session() as session:
        assert await session.get(OutboxEvent, rolled_back_id) is None


@pytest.mark.asyncio
async def test_publisher_updates_preserve_feed_position_and_api_visibility(database) -> None:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex
    event_id = uuid4()
    baseline = await _feed_head(database)
    async with database.transaction() as session:
        await _add_campaign_event(
            session,
            prefix,
            event_id=event_id,
            available_at=datetime(2000, 1, 1, tzinfo=UTC),
        )

    async with database.session() as session:
        before = await session.get(OutboxEvent, event_id)
        assert before is not None
        feed_sequence = before.feed_sequence

    async with database.transaction() as session:
        lease = await OutboxRepository(session).claim_next(f"publisher:{prefix}")
        assert lease is not None
        assert lease.id == event_id
    async with database.transaction() as session:
        await OutboxRepository(session).mark_published(lease)

    adapter = PostgresCampaignReadAdapter(database.session_factory)
    page = await adapter.list_notifications(
        after=NotificationCursor(feed_sequence=baseline),
        limit=100,
    )
    projected = next(item for item in page.items if item.event.id == event_id)
    async with database.session() as session:
        after = await session.get(OutboxEvent, event_id)

    assert after is not None
    assert after.published_at is not None
    assert after.feed_sequence == feed_sequence == projected.feed_sequence


@pytest.mark.asyncio
async def test_feed_sequence_is_database_enforced_immutable(database) -> None:  # type: ignore[no-untyped-def]
    prefix = uuid4().hex
    event_id = uuid4()
    async with database.transaction() as session:
        await _add_campaign_event(session, prefix, event_id=event_id)

    with pytest.raises(IntegrityError, match="feed_sequence is immutable"):
        async with database.transaction() as session:
            await session.execute(
                OutboxEvent.__table__.update()
                .where(OutboxEvent.id == event_id)
                .values(feed_sequence=OutboxEvent.feed_sequence + 1)
            )
