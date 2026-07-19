"""Read-only V1.1 campaign-change notification contracts and cursor support."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from katilim_analiz.notifications.models import (
    CAMPAIGN_CHANGE_EVENT_TYPE,
    CAMPAIGN_CHANGE_TOPIC,
    MalformedCampaignChangeEventError,
    campaign_change_event_from_outbox,
)

if TYPE_CHECKING:
    from katilim_analiz.notifications.cursor import NotificationCursorCodec

__all__ = [
    "CAMPAIGN_CHANGE_EVENT_TYPE",
    "CAMPAIGN_CHANGE_TOPIC",
    "MalformedCampaignChangeEventError",
    "NotificationCursorCodec",
    "campaign_change_event_from_outbox",
]


def __getattr__(name: str) -> object:
    if name != "NotificationCursorCodec":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    cursor_codec = import_module("katilim_analiz.notifications.cursor").NotificationCursorCodec
    globals()[name] = cursor_codec
    return cursor_codec
