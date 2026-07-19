"""Fail-closed URL, DNS, per-host pacing, and retry policy primitives."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from katilim_analiz.ingestion.registry import BankSource, RegistryValidationError, normalize_host


class PolicyViolation(ValueError):
    """A requested network target violates the deterministic collection policy."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    url: str
    host: str
    resolved_addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HostPolicy:
    min_interval_seconds: float = 2.0
    request_timeout_seconds: float = 20.0
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 30.0
    max_retry_after_seconds: float = 60.0
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    robots_ttl_seconds: float = 86_400.0
    user_agent: str = "KatilimAnalizBot/0.1"

    def __post_init__(self) -> None:
        positive = {
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_attempts": self.max_attempts,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_cap_seconds": self.backoff_cap_seconds,
            "max_response_bytes": self.max_response_bytes,
            "robots_ttl_seconds": self.robots_ttl_seconds,
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("host policy limits must be positive")
        if self.min_interval_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ValueError("host policy delays cannot be negative")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if not self.user_agent.strip() or any(ord(character) < 32 for character in self.user_agent):
            raise ValueError("user_agent must be a visible non-empty value")
        product_token = self.robots_product_token
        if not product_token or any(
            not (character.isascii() and (character.isalpha() or character in "_-"))
            for character in product_token
        ):
            raise ValueError("robots product token must contain only ASCII letters, '_' or '-'")

    @property
    def robots_product_token(self) -> str:
        return self.user_agent.split("/", 1)[0].split(maxsplit=1)[0]


class AddressResolver(Protocol):
    async def resolve(self, host: str) -> Sequence[str]: ...


class SystemAddressResolver:
    """Resolve all address records using the event loop's configured resolver."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("DNS timeout must be positive")
        self._timeout_seconds = timeout_seconds

    async def resolve(self, host: str) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(self._timeout_seconds):
            records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(record[4][0].split("%", 1)[0] for record in records))


class StaticAddressResolver:
    """Deterministic resolver for offline runs and tests."""

    def __init__(self, addresses: Mapping[str, Sequence[str]]) -> None:
        self._addresses = {
            normalize_host(host): tuple(values) for host, values in addresses.items()
        }

    async def resolve(self, host: str) -> tuple[str, ...]:
        return self._addresses.get(normalize_host(host), ())


class HostPolicyProvider(Protocol):
    def for_host(self, host: str) -> HostPolicy: ...


class StaticHostPolicyProvider:
    def __init__(
        self,
        default: HostPolicy | None = None,
        overrides: Mapping[str, HostPolicy] | None = None,
    ) -> None:
        self._default = default or HostPolicy()
        self._overrides = {
            normalize_host(host): policy for host, policy in (overrides or {}).items()
        }

    def for_host(self, host: str) -> HostPolicy:
        return self._overrides.get(normalize_host(host), self._default)


class HostRateLimiter(Protocol):
    async def acquire(self, host: str, minimum_interval_seconds: float) -> None: ...


class InMemoryHostRateLimiter:
    """Serialize requests per host and reserve the next policy-compliant start time."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}

    async def acquire(self, host: str, minimum_interval_seconds: float) -> None:
        normalized_host = normalize_host(host)
        lock = self._locks.setdefault(normalized_host, asyncio.Lock())
        async with lock:
            delay = self._next_allowed.get(normalized_host, 0.0) - self._monotonic()
            if delay > 0:
                await self._sleeper(delay)
            self._next_allowed[normalized_host] = self._monotonic() + minimum_interval_seconds


def validate_url_syntax(url: str, bank: BankSource) -> tuple[str, str]:
    """Validate an HTTPS URL against one bank's exact host allowlist, without DNS."""

    if not isinstance(url, str) or not url or any(ord(character) <= 31 for character in url):
        raise PolicyViolation("ambiguous_url", "URL contains empty or control-character input")
    if "\\" in url:
        raise PolicyViolation("ambiguous_url", "backslashes are forbidden in network URLs")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise PolicyViolation("ambiguous_url", "URL cannot be parsed unambiguously") from exc
    if parts.scheme.casefold() != "https":
        raise PolicyViolation("scheme_not_allowed", "connected ingestion requires HTTPS")
    if parts.username is not None or parts.password is not None:
        raise PolicyViolation("credentials_forbidden", "URL credentials are forbidden")
    if not parts.hostname:
        raise PolicyViolation("missing_host", "URL requires a hostname")
    try:
        host = normalize_host(parts.hostname)
    except RegistryValidationError as exc:
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            raise PolicyViolation("invalid_host", str(exc)) from exc
        raise PolicyViolation("ip_literal_forbidden", "IP-literal targets are forbidden") from exc
    try:
        port = parts.port
    except ValueError as exc:
        raise PolicyViolation("invalid_port", "URL port is invalid") from exc
    if port not in {None, 443}:
        raise PolicyViolation("port_not_allowed", "only the default HTTPS port is allowed")
    if not bank.permits_host(host):
        raise PolicyViolation(
            "host_not_allowlisted", f"host is not approved for bank {bank.id}: {host}"
        )
    path = parts.path or "/"
    canonical = urlunsplit(("https", host, path, parts.query, ""))
    return canonical, host


async def validate_target(
    url: str,
    bank: BankSource,
    resolver: AddressResolver,
) -> ValidatedTarget:
    """Validate URL syntax and every current DNS answer before each request hop."""

    canonical, host = validate_url_syntax(url, bank)
    try:
        raw_addresses = await resolver.resolve(host)
    except (OSError, TimeoutError, socket.gaierror, UnicodeError) as exc:
        raise PolicyViolation("dns_resolution_failed", f"DNS resolution failed for {host}") from exc
    if not raw_addresses:
        raise PolicyViolation("dns_resolution_failed", f"DNS returned no addresses for {host}")
    addresses: list[str] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except ValueError as exc:
            raise PolicyViolation(
                "dns_resolution_failed", "DNS returned an invalid IP address"
            ) from exc
        if not address.is_global:
            raise PolicyViolation(
                "non_public_address",
                f"DNS for {host} includes a non-public address; request denied",
            )
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    return ValidatedTarget(canonical, host, tuple(addresses))


def exponential_backoff(retry_index: int, policy: HostPolicy) -> float:
    if retry_index < 0:
        raise ValueError("retry_index cannot be negative")
    return min(policy.backoff_cap_seconds, policy.backoff_base_seconds * (2.0**retry_index))


def parse_retry_after(value: str | None, now: datetime, policy: HostPolicy) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdecimal():
        return min(float(stripped), policy.max_retry_after_seconds)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or now.tzinfo is None:
        return None
    delay = (retry_at - now).total_seconds()
    if delay <= 0:
        return None
    return min(delay, policy.max_retry_after_seconds)
