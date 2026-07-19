from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher, extract_parameters
from argon2.low_level import Type

from katilim_analiz.auth.crypto import (
    ARGON2_HASH_LENGTH,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LENGTH,
    ARGON2_TIME_COST,
    ARGON2_VERSION,
    DUMMY_PASSWORD_PHC,
    TOKEN_ENTROPY_BYTES,
    PasswordCrypto,
    generate_session_secrets,
    sha256_text,
    token_matches_hash,
)


@pytest.fixture
def crypto() -> Iterator[PasswordCrypto]:
    value = PasswordCrypto()
    yield value
    value.close()


@pytest.mark.asyncio
async def test_argon2id_hash_uses_the_explicit_owasp_floor_not_a_default_profile(
    crypto: PasswordCrypto,
) -> None:
    password_hash = await crypto.hash_password("e\u0301vidence-safe-password")
    parameters = extract_parameters(password_hash)

    assert parameters.type is Type.ID
    assert parameters.version == ARGON2_VERSION == 19
    assert parameters.memory_cost == ARGON2_MEMORY_COST_KIB == 19_456
    assert parameters.time_cost == ARGON2_TIME_COST == 2
    assert parameters.parallelism == ARGON2_PARALLELISM == 1
    assert parameters.salt_len == ARGON2_SALT_LENGTH == 16
    assert parameters.hash_len == ARGON2_HASH_LENGTH == 32

    verified = await crypto.verify_password("évidence-safe-password", password_hash, eligible=True)
    assert verified.authenticated
    assert not verified.needs_rehash


@pytest.mark.asyncio
async def test_successful_legacy_parameter_verification_requests_rehash(
    crypto: PasswordCrypto,
) -> None:
    old_hasher = PasswordHasher(
        time_cost=3,
        memory_cost=65_536,
        parallelism=4,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    password_hash = old_hasher.hash("legacy-parameter-password")

    result = await crypto.verify_password("legacy-parameter-password", password_hash, eligible=True)
    assert result.authenticated
    assert result.needs_rehash


class SpyBackend:
    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str]] = []
        self.rehash_calls: list[str] = []

    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        self.verify_calls.append((password_hash, password))
        return True

    def check_needs_rehash(self, password_hash: str) -> bool:
        self.rehash_calls.append(password_hash)
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("password_hash", "eligible"),
    [(None, False), ("malformed", True), (DUMMY_PASSWORD_PHC, False)],
)
async def test_unknown_inactive_or_malformed_paths_run_the_same_dummy_backend_calls(
    password_hash: str | None, eligible: bool
) -> None:
    backend = SpyBackend()
    crypto = PasswordCrypto(backend)
    try:
        result = await crypto.verify_password(
            "candidate-password", password_hash, eligible=eligible
        )
    finally:
        crypto.close()

    assert not result.authenticated
    assert not result.needs_rehash
    assert backend.verify_calls == [(DUMMY_PASSWORD_PHC, "candidate-password")]
    assert backend.rehash_calls == [DUMMY_PASSWORD_PHC]


class BlockingSpyBackend(SpyBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum_active = 0
        self.worker_threads: set[int] = set()

    def verify(self, password_hash: str, password: str) -> bool:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.worker_threads.add(threading.get_ident())
        self.started.set()
        self.release.wait(timeout=5)
        self.active -= 1
        return super().verify(password_hash, password)


@pytest.mark.asyncio
async def test_argon2_work_is_off_loop_and_executor_submission_is_single_capacity() -> None:
    backend = BlockingSpyBackend()
    crypto = PasswordCrypto(backend)
    event_loop_thread = threading.get_ident()
    first = asyncio.create_task(crypto.verify_password("candidate-password", None, eligible=False))
    try:
        started = await asyncio.to_thread(backend.started.wait, 5)
        assert started
        second = asyncio.create_task(
            crypto.verify_password("candidate-password", None, eligible=False)
        )
        await asyncio.sleep(0)
        assert len(backend.verify_calls) == 0
        backend.release.set()
        await asyncio.gather(first, second)
    finally:
        backend.release.set()
        if not first.done():
            await first
        crypto.close()

    assert backend.maximum_active == 1
    assert backend.worker_threads and event_loop_thread not in backend.worker_threads
    assert len(backend.verify_calls) == 2


def test_session_and_csrf_tokens_use_independent_32_byte_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter(("session-token", "csrf-token"))
    requested: list[int] = []

    def fake_token_urlsafe(byte_count: int) -> str:
        requested.append(byte_count)
        return next(generated)

    monkeypatch.setattr("katilim_analiz.auth.crypto.secrets.token_urlsafe", fake_token_urlsafe)
    values = generate_session_secrets()

    assert requested == [TOKEN_ENTROPY_BYTES, TOKEN_ENTROPY_BYTES] == [32, 32]
    assert values.token_hash == sha256_text("session-token")
    assert values.csrf_hash == sha256_text("csrf-token")
    assert token_matches_hash(values.token, values.token_hash)
    assert not token_matches_hash("wrong-token", values.token_hash)
    assert values.token not in values.token_hash
    assert values.csrf_token not in values.csrf_hash
