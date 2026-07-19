from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from katilim_analiz.storage.repositories import (
    MonitoredCampaignTargetRepository,
    MonitoredSourceStateRepository,
    SourceRepository,
    monitored_campaign_key,
)

NOW = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)


async def _seed_bank(session) -> str:  # type: ignore[no-untyped-def]
    suffix = uuid4().hex[:12]
    bank_id = f"monitor-{suffix}"
    await SourceRepository(session).upsert(
        source_id=bank_id,
        registry_version=f"monitor-{suffix}",
        listing_order=1,
        legal_name=f"Monitor Bank {suffix}",
        homepage_url=f"https://{suffix}.example.test",
        allowed_hosts=[f"{suffix}.example.test"],
        digital_bank=False,
    )
    return bank_id


@pytest.mark.asyncio
async def test_targets_upsert_seen_and_list_active_are_stable(database) -> None:  # type: ignore[no-untyped-def]
    async with database.transaction() as session:
        bank_id = await _seed_bank(session)
        repository = MonitoredCampaignTargetRepository(session)
        index_url = "https://bank.example.test/kampanyalar"
        later_url = "https://bank.example.test/kampanyalar/z"
        earlier_url = "https://bank.example.test/kampanyalar/a"
        later_key = monitored_campaign_key(bank_id, later_url)
        earlier_key = monitored_campaign_key(bank_id, earlier_url)

        first, first_created = await repository.upsert_seen(
            bank_id=bank_id,
            campaign_key=later_key,
            canonical_url=later_url,
            discovered_from=index_url,
            registry_version="2026-07-19.1",
            observed_at=NOW,
        )
        repeated, repeated_created = await repository.upsert_seen(
            bank_id=bank_id,
            campaign_key=later_key,
            canonical_url=later_url,
            discovered_from=index_url,
            registry_version="2026-07-19.2",
            observed_at=NOW + timedelta(minutes=1),
        )
        await repository.upsert_seen(
            bank_id=bank_id,
            campaign_key=earlier_key,
            canonical_url=earlier_url,
            discovered_from=index_url,
            registry_version="2026-07-19.2",
            observed_at=NOW + timedelta(minutes=1),
        )
        active = await repository.list_active(bank_id)

    assert first_created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert repeated.first_seen_at == NOW
    assert repeated.last_seen_at == NOW + timedelta(minutes=1)
    assert [(row.canonical_url, row.campaign_key) for row in active] == [
        (earlier_url, earlier_key),
        (later_url, later_key),
    ]


@pytest.mark.asyncio
async def test_target_rejects_non_deterministic_campaign_key(database) -> None:  # type: ignore[no-untyped-def]
    async with database.transaction() as session:
        bank_id = await _seed_bank(session)
        repository = MonitoredCampaignTargetRepository(session)
        with pytest.raises(ValueError, match="must derive"):
            await repository.upsert_seen(
                bank_id=bank_id,
                campaign_key="caller-selected-key",
                canonical_url="https://bank.example.test/kampanyalar/a",
                discovered_from="https://bank.example.test/kampanyalar",
                registry_version="2026-07-19.1",
                observed_at=NOW,
            )


@pytest.mark.asyncio
async def test_source_state_preserves_hash_on_failures_and_detects_real_change(database) -> None:  # type: ignore[no-untyped-def]
    async with database.transaction() as session:
        bank_id = await _seed_bank(session)
        repository = MonitoredSourceStateRepository(session)
        index_url = "https://bank.example.test/kampanyalar"
        first = await repository.record_observation(
            bank_id=bank_id,
            index_url=index_url,
            registry_version="2026-07-19.1",
            status="success",
            observed_at=NOW,
            content_sha256="a" * 64,
        )
        failed = await repository.record_observation(
            bank_id=bank_id,
            index_url=index_url,
            registry_version="2026-07-19.1",
            status="failed",
            observed_at=NOW + timedelta(minutes=1),
            content_sha256=None,
        )
        unchanged = await repository.record_observation(
            bank_id=bank_id,
            index_url=index_url,
            registry_version="2026-07-19.1",
            status="not_modified",
            observed_at=NOW + timedelta(minutes=2),
            content_sha256=None,
        )
        changed = await repository.record_observation(
            bank_id=bank_id,
            index_url=index_url,
            registry_version="2026-07-19.2",
            status="success",
            observed_at=NOW + timedelta(minutes=3),
            content_sha256="b" * 64,
        )

    assert first.previous_content_sha256 is None
    assert first.current_content_sha256 == "a" * 64
    assert first.source_index_changed is False
    assert failed.current_content_sha256 == "a" * 64
    assert failed.source_index_changed is False
    assert unchanged.current_content_sha256 == "a" * 64
    assert unchanged.source_index_changed is False
    assert changed.previous_content_sha256 == "a" * 64
    assert changed.current_content_sha256 == "b" * 64
    assert changed.source_index_changed is True
