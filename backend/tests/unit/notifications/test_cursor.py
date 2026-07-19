from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from katilim_analiz.application.cursor import InvalidCursorError
from katilim_analiz.application.models import NotificationCursor
from katilim_analiz.notifications.cursor import NotificationCursorCodec


def test_notification_cursor_round_trip_is_canonical() -> None:
    cursor = NotificationCursor(feed_sequence=42)
    codec = NotificationCursorCodec()

    encoded = codec.encode(cursor)

    assert codec.decode(encoded) == cursor
    assert "42" not in encoded


def test_valid_legacy_cursor_replays_from_zero_and_upgrades_to_v2() -> None:
    payload = json.dumps(
        [
            datetime(2026, 7, 19, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            str(UUID("00000000-0000-0000-0000-000000000001")),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    codec = NotificationCursorCodec()

    replay = codec.decode(legacy)

    assert replay == NotificationCursor(feed_sequence=0)
    assert codec.encode(replay) != legacy
    assert codec.decode(codec.encode(replay)) == replay


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-base64",
        "e30",
        "W1wiMjAyNi0wNy0xOVQxMjowMDowMFwiLFwibm90LXV1aWRcIl0",
        base64.urlsafe_b64encode(b'["v2","01"]').rstrip(b"=").decode("ascii"),
        base64.urlsafe_b64encode(b'["v2","9223372036854775808"]').rstrip(b"=").decode("ascii"),
    ],
)
def test_notification_cursor_rejects_malformed_values(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        NotificationCursorCodec().decode(value)
