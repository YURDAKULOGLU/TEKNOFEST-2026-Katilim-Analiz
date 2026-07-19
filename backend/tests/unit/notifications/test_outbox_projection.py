from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from katilim_analiz.notifications.models import (
    MalformedCampaignChangeEventError,
    campaign_change_event_from_outbox,
)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
EVENT_ID = UUID("00000000-0000-0000-0000-000000000001")


def payload() -> dict[str, object]:
    return {
        "campaign_key": "bank-a:campaign-a",
        "record_id": "record:a:v1",
        "record_version": 1,
        "change_kind": "created",
        "record_status": "validated",
        "previous_record_id": None,
        "observed_at": "2026-07-19T12:00:00Z",
    }


def project(payload_value: object, *, aggregate_id: str = "bank-a:campaign-a"):
    return campaign_change_event_from_outbox(
        event_id=EVENT_ID,
        aggregate_type="campaign",
        aggregate_id=aggregate_id,
        payload=payload_value,
        created_at=NOW,
    )


def test_exact_wp111_payload_projects_without_publisher_metadata() -> None:
    event = project(payload())

    assert event.id == EVENT_ID
    assert event.previous_record_id is None
    assert set(event.model_dump()) == {
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


@pytest.mark.parametrize(
    "payload_value,aggregate_id",
    [
        ({**payload(), "raw_html": "<script>secret</script>"}, "bank-a:campaign-a"),
        (
            {key: value for key, value in payload().items() if key != "record_status"},
            "bank-a:campaign-a",
        ),
        ({**payload(), "record_status": "active"}, "bank-a:campaign-a"),
        (payload(), "bank-a:different"),
        (["not", "an", "object"], "bank-a:campaign-a"),
    ],
)
def test_malformed_internal_payload_fails_closed(
    payload_value: object,
    aggregate_id: str,
) -> None:
    with pytest.raises(MalformedCampaignChangeEventError):
        project(payload_value, aggregate_id=aggregate_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("campaign_key", 7),
        ("record_id", True),
        ("record_version", True),
        ("record_version", "1"),
        ("record_version", 1.0),
        ("change_kind", 1),
        ("record_status", True),
        ("previous_record_id", False),
        ("observed_at", 1_753_012_800),
        ("observed_at", NOW),
    ],
)
def test_outbox_projection_rejects_payload_type_coercion(
    field: str,
    value: object,
) -> None:
    with pytest.raises(MalformedCampaignChangeEventError):
        project({**payload(), field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"change_kind": "created", "record_version": 2, "previous_record_id": None},
        {
            "change_kind": "created",
            "record_version": 1,
            "previous_record_id": "record:a:v0",
        },
        {
            "change_kind": "updated",
            "record_version": 1,
            "previous_record_id": "record:a:v0",
        },
        {"change_kind": "updated", "record_version": 2, "previous_record_id": None},
    ],
)
def test_outbox_projection_rejects_contradictory_change_metadata(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(MalformedCampaignChangeEventError):
        project({**payload(), **overrides})
