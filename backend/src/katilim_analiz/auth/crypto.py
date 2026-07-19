"""Pinned Argon2id and opaque-token primitives for the local auth boundary."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Protocol, TypeVar

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from katilim_analiz.auth.models import PasswordVerification, SessionSecrets
from katilim_analiz.auth.passwords import normalize_password, validate_password_shape

ARGON2_TIME_COST = 2
ARGON2_MEMORY_COST_KIB = 19_456
ARGON2_PARALLELISM = 1
ARGON2_SALT_LENGTH = 16
ARGON2_HASH_LENGTH = 32
ARGON2_VERSION = 19
TOKEN_ENTROPY_BYTES = 32

# A non-secret, parameter-matched PHC used only when no eligible user hash may
# be selected. Authentication always masks its result for dummy paths.
_DUMMY_PHC_PARTS = (
    "$argon2id$v=19$m=19456,t=2,p=1$1g1JW/meDnamVHgAGBM1GQ$",
    "9OIYoFwF2g28bJlWkUWPrWUQzUiAxHiMW8Yh5Bs3y5k",
)
DUMMY_PASSWORD_PHC = "".join(_DUMMY_PHC_PARTS)

_SUPPORTED_PHC = re.compile(
    r"^\$argon2id\$v=19\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$"
    r"[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$"
)

_T = TypeVar("_T")


class PasswordBackend(Protocol):
    """Synchronous seam kept injectable for structural timing-path tests."""

    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def check_needs_rehash(self, password_hash: str) -> bool: ...


class Argon2PasswordBackend:
    """High-level argon2-cffi wrapper with ADR-010's explicit OWASP floor."""

    def __init__(self) -> None:
        self.hasher = PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST_KIB,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LENGTH,
            salt_len=ARGON2_SALT_LENGTH,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def check_needs_rehash(self, password_hash: str) -> bool:
        try:
            return self.hasher.check_needs_rehash(password_hash)
        except (VerificationError, InvalidHashError):
            return True


class PasswordCrypto:
    """Serialize expensive password work onto one bounded worker thread.

    The semaphore prevents more than one call from entering the executor queue,
    while callers waiting for capacity remain regular event-loop tasks.
    """

    def __init__(self, backend: PasswordBackend | None = None) -> None:
        self._backend = backend or Argon2PasswordBackend()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="argon2-password")
        self._capacity = asyncio.Semaphore(1)
        self._closed = False

    async def hash_password(self, password: str) -> str:
        """NFC-normalize, shape-check, and hash the complete password value."""

        normalized = validate_password_shape(password)
        return await self._run(self._backend.hash, normalized)

    async def verify_password(
        self,
        password: str,
        password_hash: str | None,
        *,
        eligible: bool,
    ) -> PasswordVerification:
        """Run one generic Argon2 path, substituting the dummy PHC when needed."""

        normalized = normalize_password(password)
        use_real_hash = (
            eligible
            and password_hash is not None
            and _SUPPORTED_PHC.fullmatch(password_hash) is not None
        )
        selected_hash = (
            password_hash if use_real_hash and password_hash is not None else DUMMY_PASSWORD_PHC
        )
        verified, needs_rehash = await self._run(
            self._verify_and_check,
            selected_hash,
            normalized,
        )
        authenticated = bool(use_real_hash and verified)
        return PasswordVerification(
            authenticated=authenticated,
            needs_rehash=authenticated and needs_rehash,
        )

    def _verify_and_check(self, password_hash: str, password: str) -> tuple[bool, bool]:
        # check_needs_rehash is intentionally called on both successful and
        # unsuccessful/dummy paths; spies assert this control-flow symmetry.
        verified = self._backend.verify(password_hash, password)
        needs_rehash = self._backend.check_needs_rehash(password_hash)
        return verified, needs_rehash

    async def _run(self, function: Callable[..., _T], *args: str) -> _T:
        if self._closed:
            raise RuntimeError("password crypto worker is closed")
        async with self._capacity:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(function, *args))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)


def sha256_text(value: str) -> str:
    """Hash a non-empty UTF-8 value without claiming anonymity."""

    if not isinstance(value, str) or not value:
        raise ValueError("value must be a non-empty string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_session_secrets() -> SessionSecrets:
    """Create independent 32-byte session/CSRF tokens and their storage digests."""

    token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    csrf_token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    if token == csrf_token:  # Defensive against a broken/injected entropy source.
        raise RuntimeError("session and CSRF entropy source returned the same token")
    return SessionSecrets(
        token=token,
        csrf_token=csrf_token,
        token_hash=sha256_text(token),
        csrf_hash=sha256_text(csrf_token),
    )


def token_matches_hash(raw_token: str, expected_hash: str) -> bool:
    """Compare a presented opaque token to a persisted digest in constant time."""

    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        return False
    try:
        presented_hash = sha256_text(raw_token)
    except ValueError:
        return False
    return secrets.compare_digest(presented_hash, expected_hash)
