from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from katilim_analiz.ingestion.policy import (
    HostPolicy,
    InMemoryHostRateLimiter,
    PolicyViolation,
    StaticAddressResolver,
    StaticHostPolicyProvider,
    exponential_backoff,
    parse_retry_after,
    validate_target,
)
from katilim_analiz.ingestion.registry import load_registry

REGISTRY_PATH = (
    Path(__file__).resolve().parents[4]
    / "data"
    / "registry"
    / "bddk-participation-banks-2026-07-18.json"
)


@pytest.fixture
def bank():  # type: ignore[no-untyped-def]
    return load_registry(REGISTRY_PATH).bank("kuveyt-turk")


@pytest.mark.asyncio
async def test_target_validation_canonicalizes_an_exact_allowlisted_host(bank) -> None:  # type: ignore[no-untyped-def]
    resolver = StaticAddressResolver({"www.kuveytturk.com.tr": ("93.184.216.34",)})

    target = await validate_target(
        "https://WWW.KUVEYTTURK.COM.TR./kampanyalar?q=katilim#ignored",
        bank,
        resolver,
    )

    assert target.url == "https://www.kuveytturk.com.tr/kampanyalar?q=katilim"
    assert target.host == "www.kuveytturk.com.tr"
    assert target.resolved_addresses == ("93.184.216.34",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://www.kuveytturk.com.tr/kampanya", "scheme_not_allowed"),
        ("https://user:secret@www.kuveytturk.com.tr/", "credentials_forbidden"),
        ("https://www.kuveytturk.com.tr:444/", "port_not_allowed"),
        ("https://campaigns.kuveytturk.com.tr/", "host_not_allowlisted"),
        ("https://kuveytturk.com.tr.attacker.example/", "host_not_allowlisted"),
        ("https://127.0.0.1/", "ip_literal_forbidden"),
        ("https://www.kuveytturk.com.tr\\@attacker.example/", "ambiguous_url"),
    ],
)
async def test_target_validation_fails_closed_for_unsafe_urls(bank, url: str, code: str) -> None:  # type: ignore[no-untyped-def]
    resolver = StaticAddressResolver({"www.kuveytturk.com.tr": ("93.184.216.34",)})

    with pytest.raises(PolicyViolation) as raised:
        await validate_target(url, bank, resolver)

    assert raised.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("10.0.0.1",), ("93.184.216.34", "::1")])
async def test_target_validation_rejects_any_non_public_dns_answer(bank, addresses) -> None:  # type: ignore[no-untyped-def]
    resolver = StaticAddressResolver({"www.kuveytturk.com.tr": addresses})

    with pytest.raises(PolicyViolation) as raised:
        await validate_target("https://www.kuveytturk.com.tr/", bank, resolver)

    assert raised.value.code == "non_public_address"


@pytest.mark.asyncio
async def test_target_validation_rejects_empty_dns_answer(bank) -> None:  # type: ignore[no-untyped-def]
    resolver = StaticAddressResolver({"www.kuveytturk.com.tr": ()})

    with pytest.raises(PolicyViolation) as raised:
        await validate_target("https://www.kuveytturk.com.tr/", bank, resolver)

    assert raised.value.code == "dns_resolution_failed"


@pytest.mark.asyncio
async def test_target_validation_converts_resolver_timeout_to_a_closed_decision(bank) -> None:  # type: ignore[no-untyped-def]
    class TimedOutResolver:
        async def resolve(self, host: str) -> tuple[str, ...]:
            raise TimeoutError

    with pytest.raises(PolicyViolation) as raised:
        await validate_target("https://www.kuveytturk.com.tr/", bank, TimedOutResolver())

    assert raised.value.code == "dns_resolution_failed"


def test_backoff_and_retry_after_are_bounded() -> None:
    policy = HostPolicy(backoff_base_seconds=2, backoff_cap_seconds=5, max_retry_after_seconds=30)
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)

    assert [exponential_backoff(index, policy) for index in range(4)] == [2, 4, 5, 5]
    assert parse_retry_after("12", now, policy) == 12
    assert parse_retry_after("120", now, policy) == 30
    assert parse_retry_after("Sat, 18 Jul 2026 12:00:08 GMT", now, policy) == 8
    assert parse_retry_after("not-a-delay", now, policy) is None
    assert parse_retry_after((now - timedelta(seconds=1)).isoformat(), now, policy) is None


def test_host_policy_requires_an_rfc9309_product_token() -> None:
    with pytest.raises(ValueError, match="product token"):
        HostPolicy(user_agent="Bot123/1.0")


def test_per_host_policy_provider_uses_exact_overrides() -> None:
    default = HostPolicy(min_interval_seconds=2)
    override = HostPolicy(min_interval_seconds=7)
    provider = StaticHostPolicyProvider(default, {"www.kuveytturk.com.tr": override})

    assert provider.for_host("WWW.KUVEYTTURK.COM.TR.") is override
    assert provider.for_host("kuveytturk.com.tr") is default


@pytest.mark.asyncio
async def test_in_memory_rate_limiter_spaces_each_host_independently() -> None:
    current_time = 0.0
    delays: list[float] = []

    def monotonic() -> float:
        return current_time

    async def sleep(delay: float) -> None:
        nonlocal current_time
        delays.append(delay)
        current_time += delay

    limiter = InMemoryHostRateLimiter(monotonic=monotonic, sleeper=sleep)
    await limiter.acquire("bank.example", 2)
    await limiter.acquire("other.example", 2)
    await limiter.acquire("bank.example", 2)
    await limiter.acquire("bank.example", 2)

    assert delays == [2, 2]
