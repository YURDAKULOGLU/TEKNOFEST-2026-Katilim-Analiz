"""Strict deterministic base64url keyset cursors."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime

from pydantic import ValidationError

from katilim_analiz.application.models import CampaignCursor


class InvalidCursorError(ValueError):
    """An opaque cursor was malformed, non-canonical, or outside bounds."""


class CursorCodec:
    _MAX_ENCODED_LENGTH = 500

    def encode(self, position: CampaignCursor) -> str:
        payload = json.dumps(
            [
                position.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                position.campaign_id,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    def decode(self, value: str) -> CampaignCursor:
        if not value or len(value) > self._MAX_ENCODED_LENGTH or not value.isascii():
            raise InvalidCursorError("cursor is malformed")
        try:
            padding = "=" * (-len(value) % 4)
            raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
            decoded = json.loads(raw)
            if (
                not isinstance(decoded, list)
                or len(decoded) != 2
                or not all(isinstance(item, str) for item in decoded)
            ):
                raise InvalidCursorError("cursor has an invalid shape")
            observed_at = datetime.fromisoformat(decoded[0])
            position = CampaignCursor(observed_at=observed_at, campaign_id=decoded[1])
        except (
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise InvalidCursorError("cursor is malformed") from exc
        if self.encode(position) != value:
            raise InvalidCursorError("cursor is not canonical")
        return position
