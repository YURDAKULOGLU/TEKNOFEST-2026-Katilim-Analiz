from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from conftest import NOW, FakeReads

from katilim_analiz.contracts import CampaignChangeEvent, RecordStatus


def change_event(identifier: int, *, record_version: int) -> CampaignChangeEvent:
    return CampaignChangeEvent(
        id=UUID(f"00000000-0000-0000-0000-{identifier:012d}"),
        campaign_key="bank-a:campaign-a",
        record_id=f"campaign-a-v{record_version}",
        record_version=record_version,
        change_kind="created" if record_version == 1 else "updated",
        record_status=(
            RecordStatus.VALIDATED if record_version == 1 else RecordStatus.NEEDS_REVIEW
        ),
        previous_record_id=(None if record_version == 1 else f"campaign-a-v{record_version - 1}"),
        observed_at=NOW,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_notification_feed_is_strict_bounded_and_duplicate_safe(
    client: httpx.AsyncClient,
    reads: FakeReads,
) -> None:
    reads.notifications = [
        change_event(1, record_version=1),
        change_event(2, record_version=2),
    ]

    first = await client.get("/api/v1/notifications", params={"limit": 1})
    assert first.status_code == 200
    first_payload = first.json()
    assert len(first_payload["items"]) == 1
    assert set(first_payload["items"][0]) == {
        "id",
        "campaign_key",
        "record_id",
        "record_version",
        "change_kind",
        "record_status",
        "previous_record_id",
        "observed_at",
        "created_at",
    }
    assert first_payload["items"][0]["record_status"] == "validated"

    assert first_payload["next_cursor"] is not None

    second = await client.get(
        "/api/v1/notifications",
        params={"limit": 1, "cursor": first_payload["next_cursor"]},
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert [item["id"] for item in second_payload["items"]] == [
        "00000000-0000-0000-0000-000000000002"
    ]
    assert second_payload["items"][0]["record_status"] == "needs_review"
    # The feed is exhausted after this page, so pagination must terminate
    # instead of echoing a cursor that yields the same empty page forever.
    assert second_payload["next_cursor"] is None

    exhausted = await client.get(
        "/api/v1/notifications",
        params={"limit": 100, "cursor": first_payload["next_cursor"]},
    )
    assert exhausted.status_code == 200
    exhausted_payload = exhausted.json()
    assert [item["id"] for item in exhausted_payload["items"]] == [
        "00000000-0000-0000-0000-000000000002"
    ]
    assert exhausted_payload["next_cursor"] is None


@pytest.mark.asyncio
async def test_notification_feed_rejects_bad_cursor_and_unbounded_limit(
    client: httpx.AsyncClient,
) -> None:
    bad_cursor = await client.get("/api/v1/notifications", params={"cursor": "not-a-cursor"})
    unbounded = await client.get("/api/v1/notifications", params={"limit": 101})

    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "invalid_cursor"
    assert unbounded.status_code == 422
    assert unbounded.json()["code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_notification_feed_has_no_ack_mutation(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/notifications/00000000-0000-0000-0000-000000000001/ack")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_malformed_internal_notification_fails_closed_without_leaking(
    client_factory,  # type: ignore[no-untyped-def]
    reads: FakeReads,
) -> None:
    reads.notification_error = True
    async with client_factory(raise_app_exceptions=False) as api_client:
        response = await api_client.get("/api/v1/notifications")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "raw-html" not in response.text
    assert "prompt" not in response.text
    assert "database-password" not in response.text
