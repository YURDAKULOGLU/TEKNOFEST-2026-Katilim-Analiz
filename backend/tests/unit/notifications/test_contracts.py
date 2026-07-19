from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from katilim_analiz.contracts import CampaignChangeEvent, RecordStatus

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
EVENT_ID = UUID("00000000-0000-0000-0000-000000000001")


def event_data() -> dict[str, object]:
    return {
        "id": EVENT_ID,
        "campaign_key": "bank-a:campaign-a",
        "record_id": "record:a:v2",
        "record_version": 2,
        "change_kind": "updated",
        "record_status": RecordStatus.NEEDS_REVIEW,
        "previous_record_id": "record:a:v1",
        "observed_at": NOW,
        "created_at": NOW,
    }


def test_campaign_change_event_is_strict_and_timezone_aware() -> None:
    event = CampaignChangeEvent.model_validate(event_data())

    assert event.record_status is RecordStatus.NEEDS_REVIEW
    assert event.record_version == 2

    for field, value in (
        ("record_version", 0),
        ("record_status", "active"),
        ("observed_at", NOW.replace(tzinfo=None)),
        ("created_at", NOW.replace(tzinfo=None)),
    ):
        invalid = {**event_data(), field: value}
        with pytest.raises(ValidationError):
            CampaignChangeEvent.model_validate(invalid)


def test_campaign_change_event_rejects_internal_or_untrusted_extra_fields() -> None:
    for extra in ("raw_html", "evidence", "prompt", "secret"):
        with pytest.raises(ValidationError):
            CampaignChangeEvent.model_validate({**event_data(), extra: "must-not-leak"})


def test_created_event_has_an_explicit_nullable_previous_record() -> None:
    created = CampaignChangeEvent.model_validate(
        {
            **event_data(),
            "change_kind": "created",
            "record_version": 1,
            "previous_record_id": None,
        }
    )

    assert created.previous_record_id is None


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
def test_campaign_change_event_rejects_contradictory_change_metadata(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CampaignChangeEvent.model_validate({**event_data(), **overrides})


@pytest.mark.parametrize("record_version", [True, False, "2", 2.0])
def test_campaign_change_event_does_not_coerce_record_version(
    record_version: object,
) -> None:
    with pytest.raises(ValidationError):
        CampaignChangeEvent.model_validate({**event_data(), "record_version": record_version})
