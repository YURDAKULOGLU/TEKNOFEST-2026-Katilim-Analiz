"""Pure authentication records shared by the persistence and service layers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    authenticated: bool
    needs_rehash: bool

    def __post_init__(self) -> None:
        if self.needs_rehash and not self.authenticated:
            raise ValueError("only an authenticated password can need rehashing")


@dataclass(frozen=True, slots=True)
class SessionSecrets:
    """Raw values are returned once; repositories accept only their digests."""

    token: str
    csrf_token: str
    token_hash: str
    csrf_hash: str

    def __post_init__(self) -> None:
        if not self.token or not self.csrf_token or self.token == self.csrf_token:
            raise ValueError("session and CSRF tokens must be distinct non-empty values")
        if not _SHA256.fullmatch(self.token_hash) or not _SHA256.fullmatch(self.csrf_hash):
            raise ValueError("session and CSRF token digests must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class AuthUserRecord:
    id: UUID
    username: str
    password_hash: str
    roles: tuple[str, ...]
    active: bool
    failed_attempts: int
    locked_until: datetime | None
    password_changed_at: datetime
    last_authenticated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for field, value in (
            ("password_changed_at", self.password_changed_at),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            require_aware(value, field)
        if self.locked_until is not None:
            require_aware(self.locked_until, "locked_until")
        if self.last_authenticated_at is not None:
            require_aware(self.last_authenticated_at, "last_authenticated_at")
        if self.failed_attempts < 0:
            raise ValueError("failed_attempts cannot be negative")

    def is_login_eligible(self, now: datetime) -> bool:
        require_aware(now, "now")
        return self.active and (self.locked_until is None or self.locked_until <= now)


@dataclass(frozen=True, slots=True)
class AuthSessionRecord:
    id: UUID
    user_id: UUID
    token_hash: str
    csrf_hash: str
    absolute_expires_at: datetime
    idle_expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.token_hash) or not _SHA256.fullmatch(self.csrf_hash):
            raise ValueError("stored session material must be lowercase SHA-256")
        for field, value in (
            ("absolute_expires_at", self.absolute_expires_at),
            ("idle_expires_at", self.idle_expires_at),
            ("last_seen_at", self.last_seen_at),
            ("created_at", self.created_at),
        ):
            require_aware(value, field)
        if self.revoked_at is not None:
            require_aware(self.revoked_at, "revoked_at")
        if self.absolute_expires_at <= self.created_at:
            raise ValueError("absolute session expiry must follow creation")
        if not self.created_at <= self.last_seen_at < self.idle_expires_at:
            raise ValueError("session idle timestamps are inconsistent")
        if self.idle_expires_at > self.absolute_expires_at:
            raise ValueError("idle expiry cannot exceed absolute expiry")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("session revocation cannot precede creation")

    def is_active(self, now: datetime) -> bool:
        require_aware(now, "now")
        return (
            self.revoked_at is None
            and now < self.idle_expires_at
            and now < self.absolute_expires_at
        )


@dataclass(frozen=True, slots=True)
class LoginRateDecision:
    allowed: bool
    username_count: int
    username_limit: int
    client_count: int
    client_limit: int
    retry_at: datetime | None

    def __post_init__(self) -> None:
        if (
            min(
                self.username_count,
                self.username_limit,
                self.client_count,
                self.client_limit,
            )
            < 0
        ):
            raise ValueError("rate-limit counts and limits cannot be negative")
        if self.retry_at is not None:
            require_aware(self.retry_at, "retry_at")


@dataclass(frozen=True, slots=True)
class AuthAuditRecord:
    id: UUID
    occurred_at: datetime
    event_type: str
    outcome: AuditOutcome
    actor_user_id: UUID | None
    session_id: UUID | None
    username_hash: str | None
    client_hash: str | None
    reason_code: str | None
    correlation_id: str | None

    def __post_init__(self) -> None:
        require_aware(self.occurred_at, "occurred_at")
        for field, value in (
            ("username_hash", self.username_hash),
            ("client_hash", self.client_hash),
        ):
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError(f"{field} must be lowercase SHA-256")
