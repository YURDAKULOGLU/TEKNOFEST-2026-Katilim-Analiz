from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignListFilters,
)
from katilim_analiz.storage.read_adapter import (
    InvalidReadCursorError,
    PostgresCampaignReadAdapter,
)

NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)


def _adapter_without_database() -> PostgresCampaignReadAdapter:
    return PostgresCampaignReadAdapter(cast(async_sessionmaker[AsyncSession], object()))


@pytest.mark.asyncio
async def test_list_rejects_unbounded_limit_before_opening_a_session() -> None:
    adapter = _adapter_without_database()

    with pytest.raises(ValueError, match="between 1 and 100"):
        await adapter.list_latest(filters=CampaignListFilters(), after=None, limit=101, as_of=NOW)


@pytest.mark.asyncio
async def test_list_rejects_naive_as_of_before_opening_a_session() -> None:
    adapter = _adapter_without_database()

    with pytest.raises(ValueError, match="timezone"):
        await adapter.list_latest(
            filters=CampaignListFilters(),
            after=None,
            limit=10,
            as_of=NOW.replace(tzinfo=None),
        )


@pytest.mark.asyncio
async def test_list_rejects_cursor_after_snapshot_cutoff() -> None:
    adapter = _adapter_without_database()
    cursor = CampaignCursor(observed_at=NOW + timedelta(seconds=1), campaign_id="record:1")

    with pytest.raises(InvalidReadCursorError, match="later"):
        await adapter.list_latest(filters=CampaignListFilters(), after=cursor, limit=10, as_of=NOW)
