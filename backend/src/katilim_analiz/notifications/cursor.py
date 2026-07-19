"""Opaque canonical keyset cursors for the append-only notification feed."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from katilim_analiz.application.cursor import InvalidCursorError
from katilim_analiz.application.models import NotificationCursor


class NotificationCursorCodec:
    _MAX_ENCODED_LENGTH = 500
    _VERSION = "v2"

    def encode(self, position: NotificationCursor) -> str:
        return self._encode_payload([self._VERSION, str(position.feed_sequence)])

    def decode(self, value: str) -> NotificationCursor:
        if not value or len(value) > self._MAX_ENCODED_LENGTH or not value.isascii():
            raise InvalidCursorError("notification cursor is malformed")
        try:
            padding = "=" * (-len(value) % 4)
            raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
            decoded = json.loads(raw)
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise InvalidCursorError("notification cursor has an invalid shape")
            if decoded[0] == self._VERSION:
                position = self._decode_v2(decoded, value)
            else:
                position = self._decode_legacy(decoded, value)
        except (
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise InvalidCursorError("notification cursor is malformed") from exc
        return position

    def _decode_v2(self, decoded: list[object], encoded: str) -> NotificationCursor:
        sequence_text = decoded[1]
        if (
            not isinstance(sequence_text, str)
            or not sequence_text.isascii()
            or not sequence_text.isdecimal()
        ):
            raise InvalidCursorError("notification cursor has an invalid shape")
        position = NotificationCursor(feed_sequence=int(sequence_text))
        if str(position.feed_sequence) != sequence_text or self.encode(position) != encoded:
            raise InvalidCursorError("notification cursor is not canonical")
        return position

    def _decode_legacy(self, decoded: list[object], encoded: str) -> NotificationCursor:
        created_at_text, event_id_text = decoded
        if not isinstance(created_at_text, str) or not isinstance(event_id_text, str):
            raise InvalidCursorError("notification cursor has an invalid shape")
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise InvalidCursorError("notification cursor is malformed")
        event_id = UUID(event_id_text)
        legacy = self._encode_payload(
            [
                created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                str(event_id),
            ]
        )
        if legacy != encoded:
            raise InvalidCursorError("notification cursor is not canonical")
        # Translating a legacy row would retain the exact skip this migration
        # repairs. A valid v1 cursor therefore deliberately replays the feed.
        return NotificationCursor(feed_sequence=0)

    @staticmethod
    def _encode_payload(payload: list[str]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
