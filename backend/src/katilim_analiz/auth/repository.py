"""PostgreSQL repositories for local users, sessions, login windows, and audit."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from katilim_analiz.auth.models import (
    AuditOutcome,
    AuthAuditRecord,
    AuthSessionRecord,
    AuthUserRecord,
    LoginRateDecision,
    require_aware,
)
from katilim_analiz.storage.models import (
    AuthAuditEvent,
    AuthRateReservation,
    AuthSession,
    AuthUser,
)

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)
USERNAME_RATE_WINDOW = timedelta(minutes=15)
USERNAME_RATE_LIMIT = 10
CLIENT_RATE_WINDOW = timedelta(seconds=60)
CLIENT_RATE_LIMIT = 20

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_USERNAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SYMBOLIC = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CORRELATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}$")
_ARGON2ID_V19 = "$argon2id$v=19$"
_BOOTSTRAP_LOCK_ID = 6_247_830_121

_AUDIT_EVENT_TYPES = frozenset(
    {
        "admin.bootstrap_created",
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.login_rate_limited",
        "auth.password_rehashed",
        "auth.session_rotated",
        "auth.logout",
        "auth.session_revoked",
        "auth.request_denied",
    }
)
_AUDIT_REASON_CODES = frozenset(
    {
        "authentication_required",
        "authorization_denied",
        "bootstrap_complete",
        "csrf_invalid",
        "fetch_metadata_invalid",
        "inactive_account",
        "invalid_credentials",
        "locked_account",
        "logout",
        "origin_invalid",
        "rate_limited_client",
        "rate_limited_username",
        "replaced_by_login",
        "session_expired",
        "session_revoked",
    }
)


class AuthStorageError(RuntimeError):
    """Base failure for security persistence invariants."""


class BootstrapConflictError(AuthStorageError):
    """Create-once bootstrap found an existing local identity."""


def canonical_username(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("username must be a string")
    canonical = value.casefold()
    if not _USERNAME.fullmatch(canonical):
        raise ValueError("username must use 1-64 lowercase ASCII identifier characters")
    return canonical


def _require_sha256(value: str, field: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_password_hash(value: str) -> str:
    if not value.startswith(_ARGON2ID_V19) or len(value) > 500:
        raise ValueError("password_hash must be an Argon2id v19 PHC")
    return value


def _require_positive_duration(value: timedelta, field: str) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{field} must be positive")


def _user_record(row: AuthUser) -> AuthUserRecord:
    return AuthUserRecord(
        id=row.id,
        username=row.username,
        password_hash=row.password_hash,
        roles=tuple(str(role) for role in row.roles),
        active=row.active,
        failed_attempts=row.failed_attempts,
        locked_until=row.locked_until,
        password_changed_at=row.password_changed_at,
        last_authenticated_at=row.last_authenticated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_record(row: AuthSession) -> AuthSessionRecord:
    return AuthSessionRecord(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        csrf_hash=row.csrf_hash,
        absolute_expires_at=row.absolute_expires_at,
        idle_expires_at=row.idle_expires_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


def _audit_record(row: AuthAuditEvent) -> AuthAuditRecord:
    return AuthAuditRecord(
        id=row.id,
        occurred_at=row.occurred_at,
        event_type=row.event_type,
        outcome=AuditOutcome(row.outcome),
        actor_user_id=row.actor_user_id,
        session_id=row.session_id,
        username_hash=row.username_hash,
        client_hash=row.client_hash,
        reason_code=row.reason_code,
        correlation_id=row.correlation_id,
    )


class AuthRepository:
    """Caller-transaction-owned user and server-side session operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        roles: list[str] | tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> UUID:
        created_at = now or datetime.now(UTC)
        require_aware(created_at, "now")
        normalized_roles = sorted(set(("admin",) if roles is None else roles))
        if not normalized_roles or any(not _SYMBOLIC.fullmatch(role) for role in normalized_roles):
            raise ValueError("roles must be non-empty symbolic identifiers")
        identifier = uuid4()
        self.session.add(
            AuthUser(
                id=identifier,
                username=canonical_username(username),
                password_hash=_require_password_hash(password_hash),
                roles=normalized_roles,
                active=True,
                failed_attempts=0,
                password_changed_at=created_at,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await self.session.flush()
        return identifier

    async def create_initial_user(
        self,
        username: str,
        password_hash: str,
        *,
        roles: list[str] | tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> UUID:
        """Create the sole bootstrap user under a transaction advisory lock."""

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _BOOTSTRAP_LOCK_ID}
        )
        existing = (
            await self.session.execute(select(func.count()).select_from(AuthUser))
        ).scalar_one()
        if existing:
            raise BootstrapConflictError("local administrator bootstrap has already completed")
        return await self.create_user(username, password_hash, roles=roles, now=now)

    async def get_user(self, user_id: UUID, *, for_update: bool = False) -> AuthUserRecord | None:
        statement = select(AuthUser).where(AuthUser.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else _user_record(row)

    async def get_user_by_username(
        self, username: str, *, for_update: bool = False
    ) -> AuthUserRecord | None:
        statement = select(AuthUser).where(AuthUser.username == canonical_username(username))
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else _user_record(row)

    async def record_login_failure(self, user_id: UUID, *, now: datetime) -> AuthUserRecord:
        """Increment failure state and apply ADR-010's bounded exponential wait."""

        require_aware(now, "now")
        row = (
            await self.session.execute(
                select(AuthUser).where(AuthUser.id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuthStorageError("auth user no longer exists")
        row.failed_attempts += 1
        if row.failed_attempts >= 5:
            delay_seconds = (
                15 * 60 if row.failed_attempts >= 10 else 30 * (2 ** (row.failed_attempts - 5))
            )
            row.locked_until = now + timedelta(seconds=delay_seconds)
        row.updated_at = now
        await self.session.flush()
        return _user_record(row)

    async def record_login_success(
        self,
        user_id: UUID,
        *,
        now: datetime,
        rehashed_password_hash: str | None = None,
    ) -> AuthUserRecord:
        require_aware(now, "now")
        row = (
            await self.session.execute(
                select(AuthUser).where(AuthUser.id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuthStorageError("auth user no longer exists")
        row.failed_attempts = 0
        row.locked_until = None
        row.last_authenticated_at = now
        row.updated_at = now
        if rehashed_password_hash is not None:
            row.password_hash = _require_password_hash(rehashed_password_hash)
            row.password_changed_at = now
        await self.session.flush()
        return _user_record(row)

    async def rotate_session(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        csrf_hash: str,
        now: datetime,
        idle_for: timedelta = SESSION_IDLE_TIMEOUT,
        absolute_for: timedelta = SESSION_ABSOLUTE_TIMEOUT,
    ) -> AuthSessionRecord:
        require_aware(now, "now")
        _require_positive_duration(idle_for, "idle_for")
        _require_positive_duration(absolute_for, "absolute_for")
        if idle_for > absolute_for:
            raise ValueError("idle timeout cannot exceed absolute timeout")
        _require_sha256(token_hash, "token_hash")
        _require_sha256(csrf_hash, "csrf_hash")
        if token_hash == csrf_hash:
            raise ValueError("session and CSRF hashes must be distinct")

        await self.session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.idle_expires_at > now,
                AuthSession.absolute_expires_at > now,
            )
            .values(revoked_at=now)
        )
        row = AuthSession(
            id=uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            absolute_expires_at=now + absolute_for,
            idle_expires_at=now + idle_for,
            last_seen_at=now,
            created_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return _session_record(row)

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        csrf_hash: str,
        expires_at: datetime,
        client_context: dict[str, object] | None = None,
    ) -> UUID:
        """Compatibility seam; new callers should use ``rotate_session``."""

        if client_context:
            raise ValueError("raw client context is not persisted")
        require_aware(expires_at, "expires_at")
        now = datetime.now(UTC)
        absolute_for = expires_at - now
        _require_positive_duration(absolute_for, "expires_at")
        record = await self.rotate_session(
            user_id=user_id,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            now=now,
            idle_for=min(SESSION_IDLE_TIMEOUT, absolute_for),
            absolute_for=absolute_for,
        )
        return record.id

    async def get_active_session(
        self, token_hash: str, *, now: datetime
    ) -> tuple[AuthSessionRecord, AuthUserRecord] | None:
        _require_sha256(token_hash, "token_hash")
        require_aware(now, "now")
        result = await self.session.execute(
            select(AuthSession, AuthUser)
            .join(AuthUser, AuthUser.id == AuthSession.user_id)
            .where(
                AuthSession.token_hash == token_hash,
                AuthSession.revoked_at.is_(None),
                AuthSession.idle_expires_at > now,
                AuthSession.absolute_expires_at > now,
                AuthUser.active.is_(True),
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _session_record(row[0]), _user_record(row[1])

    async def touch_active_session(
        self,
        token_hash: str,
        *,
        now: datetime,
        idle_for: timedelta = SESSION_IDLE_TIMEOUT,
        write_interval: timedelta = SESSION_TOUCH_INTERVAL,
    ) -> AuthSessionRecord | None:
        _require_sha256(token_hash, "token_hash")
        require_aware(now, "now")
        _require_positive_duration(idle_for, "idle_for")
        _require_positive_duration(write_interval, "write_interval")
        row = (
            await self.session.execute(
                select(AuthSession)
                .where(
                    AuthSession.token_hash == token_hash,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.idle_expires_at > now,
                    AuthSession.absolute_expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        user_active = (
            await self.session.execute(select(AuthUser.active).where(AuthUser.id == row.user_id))
        ).scalar_one_or_none()
        if user_active is not True:
            return None
        if row.last_seen_at <= now - write_interval:
            row.last_seen_at = now
            row.idle_expires_at = min(now + idle_for, row.absolute_expires_at)
            await self.session.flush()
        return _session_record(row)

    async def revoke_session(self, session_id: UUID, *, now: datetime | None = None) -> bool:
        revoked_at = now or datetime.now(UTC)
        require_aware(revoked_at, "now")
        statement = (
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
            .returning(AuthSession.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def revoke_session_by_token_hash(self, token_hash: str, *, now: datetime) -> bool:
        _require_sha256(token_hash, "token_hash")
        require_aware(now, "now")
        statement = (
            update(AuthSession)
            .where(AuthSession.token_hash == token_hash, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(AuthSession.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def revoke_user_sessions(self, user_id: UUID, *, now: datetime) -> int:
        require_aware(now, "now")
        result = await self.session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(AuthSession.id)
        )
        return len(result.scalars().all())


class LoginRateRepository:
    """Reserve both ADR-010 login windows atomically under ordered xact locks."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve_login_attempt(
        self,
        *,
        username_hash: str,
        client_hash: str,
        now: datetime,
        username_limit: int = USERNAME_RATE_LIMIT,
        username_window: timedelta = USERNAME_RATE_WINDOW,
        client_limit: int = CLIENT_RATE_LIMIT,
        client_window: timedelta = CLIENT_RATE_WINDOW,
    ) -> LoginRateDecision:
        username_hash = _require_sha256(username_hash, "username_hash")
        client_hash = _require_sha256(client_hash, "client_hash")
        require_aware(now, "now")
        _require_positive_duration(username_window, "username_window")
        _require_positive_duration(client_window, "client_window")
        if username_limit <= 0 or client_limit <= 0:
            raise ValueError("rate limits must be positive")

        locks = sorted(
            {
                _advisory_lock_key("username", username_hash),
                _advisory_lock_key("client", client_hash),
            }
        )
        for lock_id in locks:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id}
            )

        username_count, username_oldest = await self._window_state(
            "username", username_hash, now - username_window
        )
        client_count, client_oldest = await self._window_state(
            "client", client_hash, now - client_window
        )
        allowed = username_count < username_limit and client_count < client_limit
        retry_candidates: list[datetime] = []
        if username_count >= username_limit and username_oldest is not None:
            retry_candidates.append(username_oldest + username_window)
        if client_count >= client_limit and client_oldest is not None:
            retry_candidates.append(client_oldest + client_window)

        if allowed:
            self.session.add_all(
                [
                    AuthRateReservation(
                        dimension="username", subject_hash=username_hash, reserved_at=now
                    ),
                    AuthRateReservation(
                        dimension="client", subject_hash=client_hash, reserved_at=now
                    ),
                ]
            )
            await self.session.flush()
            username_count += 1
            client_count += 1

        return LoginRateDecision(
            allowed=allowed,
            username_count=username_count,
            username_limit=username_limit,
            client_count=client_count,
            client_limit=client_limit,
            retry_at=max(retry_candidates) if retry_candidates else None,
        )

    async def _window_state(
        self, dimension: str, subject_hash: str, starts_at: datetime
    ) -> tuple[int, datetime | None]:
        count, oldest = (
            await self.session.execute(
                select(func.count(), func.min(AuthRateReservation.reserved_at)).where(
                    AuthRateReservation.dimension == dimension,
                    AuthRateReservation.subject_hash == subject_hash,
                    AuthRateReservation.reserved_at > starts_at,
                )
            )
        ).one()
        return int(count), oldest

    async def prune_before(self, before: datetime) -> int:
        require_aware(before, "before")
        result = await self.session.execute(
            delete(AuthRateReservation)
            .where(AuthRateReservation.reserved_at < before)
            .returning(AuthRateReservation.id)
        )
        return len(result.scalars().all())


class AuthAuditRepository:
    """Append only a fixed, redacted event shape; no free-form detail exists."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        occurred_at: datetime,
        event_type: str,
        outcome: AuditOutcome,
        actor_user_id: UUID | None = None,
        session_id: UUID | None = None,
        username_hash: str | None = None,
        client_hash: str | None = None,
        reason_code: str | None = None,
        correlation_id: str | None = None,
        event_id: UUID | None = None,
    ) -> AuthAuditRecord:
        require_aware(occurred_at, "occurred_at")
        if event_type not in _AUDIT_EVENT_TYPES:
            raise ValueError("event_type is not an approved redacted auth event")
        if reason_code is not None and reason_code not in _AUDIT_REASON_CODES:
            raise ValueError("reason_code is not an approved redacted auth reason")
        if username_hash is not None:
            _require_sha256(username_hash, "username_hash")
        if client_hash is not None:
            _require_sha256(client_hash, "client_hash")
        if correlation_id is not None and not _CORRELATION.fullmatch(correlation_id):
            raise ValueError("correlation_id has an invalid format")
        row = AuthAuditEvent(
            id=event_id or uuid4(),
            occurred_at=occurred_at,
            event_type=event_type,
            outcome=outcome.value,
            actor_user_id=actor_user_id,
            session_id=session_id,
            username_hash=username_hash,
            client_hash=client_hash,
            reason_code=reason_code,
            correlation_id=correlation_id,
        )
        self.session.add(row)
        await self.session.flush()
        return _audit_record(row)

    async def list_recent(self, *, limit: int = 100) -> list[AuthAuditRecord]:
        if not 1 <= limit <= 1_000:
            raise ValueError("audit limit must be between 1 and 1000")
        rows = (
            await self.session.execute(
                select(AuthAuditEvent)
                .order_by(AuthAuditEvent.occurred_at.desc(), AuthAuditEvent.id.desc())
                .limit(limit)
            )
        ).scalars()
        return [_audit_record(row) for row in rows]


def _advisory_lock_key(dimension: str, subject_hash: str) -> int:
    digest = hashlib.sha256(f"auth-rate:{dimension}:{subject_hash}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
