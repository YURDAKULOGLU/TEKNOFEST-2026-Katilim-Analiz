"""Fail-closed translation of internal campaign-change outbox rows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from katilim_analiz.contracts import CampaignChangeEvent

CAMPAIGN_CHANGE_TOPIC = "notifications.campaigns.v1"
CAMPAIGN_CHANGE_EVENT_TYPE = "campaign_record.changed.v1"
CAMPAIGN_AGGREGATE_TYPE = "campaign"

_PAYLOAD_FIELDS = frozenset(
    {
        "campaign_key",
        "record_id",
        "record_version",
        "change_kind",
        "record_status",
        "previous_record_id",
        "observed_at",
    }
)


class MalformedCampaignChangeEventError(RuntimeError):
    """An internal outbox row cannot safely cross the public API boundary."""


def _payload_has_exact_types(payload: Mapping[str, Any]) -> bool:
    previous_record_id = payload["previous_record_id"]
    return (
        type(payload["campaign_key"]) is str
        and type(payload["record_id"]) is str
        and type(payload["record_version"]) is int
        and type(payload["change_kind"]) is str
        and type(payload["record_status"]) is str
        and (previous_record_id is None or type(previous_record_id) is str)
        and type(payload["observed_at"]) is str
    )


def campaign_change_event_from_outbox(
    *,
    event_id: UUID,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any] | object,
    created_at: datetime,
) -> CampaignChangeEvent:
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        raise MalformedCampaignChangeEventError(
            "campaign-change payload does not match the V1 contract"
        )
    if not _payload_has_exact_types(payload):
        raise MalformedCampaignChangeEventError(
            "campaign-change payload contains non-canonical value types"
        )
    if aggregate_type != CAMPAIGN_AGGREGATE_TYPE:
        raise MalformedCampaignChangeEventError("campaign-change aggregate type is invalid")
    try:
        event = CampaignChangeEvent.model_validate(
            {
                "id": event_id,
                **payload,
                "created_at": created_at,
            }
        )
    except (TypeError, ValidationError) as exc:
        raise MalformedCampaignChangeEventError(
            "campaign-change payload failed contract validation"
        ) from exc
    if event.campaign_key != aggregate_id:
        raise MalformedCampaignChangeEventError(
            "campaign-change aggregate ID disagrees with its payload"
        )
    return event
