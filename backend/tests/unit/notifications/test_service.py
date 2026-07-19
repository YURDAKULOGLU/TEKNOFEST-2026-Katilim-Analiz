from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from katilim_analiz.application.models import NotificationReadItem, NotificationReadSlice
from katilim_analiz.application.services import CampaignService
from katilim_analiz.contracts import CampaignChangeEvent, RecordStatus
from katilim_analiz.notifications.cursor import NotificationCursorCodec

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


class DuplicateNotificationReads:
    def __init__(self, event: CampaignChangeEvent) -> None:
        self.event = event

    async def list_notifications(self, *, after, limit):  # type: ignore[no-untyped-def]
        item = NotificationReadItem(feed_sequence=7, event=self.event)
        return NotificationReadSlice(items=[item, item][:limit])


class EmptyNotificationReads:
    async def list_notifications(self, *, after, limit):  # type: ignore[no-untyped-def]
        return NotificationReadSlice(items=[])


@pytest.mark.asyncio
async def test_repeated_cursor_cannot_reemit_the_same_event() -> None:
    event = CampaignChangeEvent(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        campaign_key="bank-a:campaign-a",
        record_id="record:a:v1",
        record_version=1,
        change_kind="created",
        record_status=RecordStatus.VALIDATED,
        previous_record_id=None,
        observed_at=NOW,
        created_at=NOW,
    )
    service = CampaignService(DuplicateNotificationReads(event))  # type: ignore[arg-type]

    first = await service.list_notifications(cursor=None, limit=20)
    repeated = await service.list_notifications(cursor=first.next_cursor, limit=20)

    assert [item.id for item in first.items] == [event.id]
    assert repeated.items == []
    assert repeated.next_cursor == first.next_cursor


@pytest.mark.asyncio
async def test_legacy_cursor_on_empty_feed_is_replaced_with_v2_zero_cursor() -> None:
    raw = json.dumps(
        ["2026-07-19T12:00:00Z", "00000000-0000-0000-0000-000000000001"],
        separators=(",", ":"),
    ).encode()
    legacy = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    service = CampaignService(EmptyNotificationReads())  # type: ignore[arg-type]

    empty = await service.list_notifications(cursor=legacy, limit=20)

    assert empty.items == []
    assert empty.next_cursor != legacy
    assert NotificationCursorCodec().decode(empty.next_cursor or "").feed_sequence == 0
