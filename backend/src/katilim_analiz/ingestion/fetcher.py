"""Policy-enforcing asynchronous HTTP ingestion without auth or challenge bypasses."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from katilim_analiz.contracts import FetchStatus
from katilim_analiz.ingestion.artifacts import (
    FetchResult,
    RawArtifactStore,
    create_fetch_artifact,
    has_access_challenge,
    is_html_content_type,
)
from katilim_analiz.ingestion.cache import ResponseCache, ResponseCacheEntry
from katilim_analiz.ingestion.policy import (
    AddressResolver,
    HostPolicy,
    HostPolicyProvider,
    HostRateLimiter,
    InMemoryHostRateLimiter,
    PolicyViolation,
    StaticHostPolicyProvider,
    SystemAddressResolver,
    ValidatedTarget,
    exponential_backoff,
    parse_retry_after,
    validate_target,
)
from katilim_analiz.ingestion.registry import BankRegistry, BankSource
from katilim_analiz.ingestion.robots import (
    MAX_ROBOTS_BYTES,
    InMemoryRobotsCache,
    RobotsCache,
    RobotsDecision,
    RobotsSnapshot,
    robots_response_decision,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403, 407})


@dataclass(frozen=True, slots=True)
class _AttemptResult:
    response: httpx.Response | None
    target: ValidatedTarget | None
    error_code: str | None = None
    error_detail: str | None = None


class HttpIngestor:
    """Fetch approved HTML while enforcing policy independently at every hop."""

    def __init__(
        self,
        *,
        registry: BankRegistry,
        artifact_store: RawArtifactStore,
        response_cache: ResponseCache,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: AddressResolver | None = None,
        policy_provider: HostPolicyProvider | None = None,
        robots_cache: RobotsCache | None = None,
        rate_limiter: HostRateLimiter | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        processing_identity: str | None = None,
    ) -> None:
        self._registry = registry
        self._artifact_store = artifact_store
        self._response_cache = response_cache
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0)
        self._owns_transport = transport is None
        self._resolver = resolver or SystemAddressResolver()
        self._policy_provider = policy_provider or StaticHostPolicyProvider()
        self._robots_cache = robots_cache or InMemoryRobotsCache()
        self._rate_limiter = rate_limiter or InMemoryHostRateLimiter()
        self._sleeper = sleeper
        self._clock = clock
        # Identity of the downstream processing pipeline (extractor + prompt
        # version).  A 304 short-circuit skips cleaning and extraction, so it
        # is only sound while the pipeline that saw the cached bytes is the
        # pipeline that would run today.
        self._processing_identity = processing_identity

    async def __aenter__(self) -> HttpIngestor:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("ingestion clock must return a timezone-aware datetime")
        return now

    async def _send_with_retries(
        self,
        *,
        bank: BankSource,
        url: str,
        headers: dict[str, str],
        extra_interval_seconds: float = 0.0,
    ) -> _AttemptResult:
        try:
            target = await validate_target(url, bank, self._resolver)
        except PolicyViolation as exc:
            return _AttemptResult(None, None, exc.code, exc.detail)
        policy = self._policy_provider.for_host(target.host)
        for attempt in range(policy.max_attempts):
            try:
                # Resolve on every attempt. This fails closed when any answer is non-public.
                target = await validate_target(url, bank, self._resolver)
            except PolicyViolation as exc:
                return _AttemptResult(None, None, exc.code, exc.detail)
            policy = self._policy_provider.for_host(target.host)
            await self._rate_limiter.acquire(
                target.host,
                max(policy.min_interval_seconds, extra_interval_seconds),
            )
            request_headers = dict(headers)
            request_headers["Host"] = target.host
            # A fresh connection prevents an IP-keyed pool from crossing logical hosts.
            request_headers["Connection"] = "close"
            request = httpx.Request(
                "GET",
                _pinned_url(target, attempt),
                headers=request_headers,
                extensions={"sni_hostname": target.host},
            )
            try:
                async with asyncio.timeout(policy.request_timeout_seconds):
                    response = await self._transport.handle_async_request(request)
            except (TimeoutError, httpx.TransportError, OSError) as exc:
                if attempt + 1 >= policy.max_attempts:
                    detail = str(exc).strip() or type(exc).__name__
                    return _AttemptResult(None, target, "transport_error", detail[:500])
                await self._sleeper(exponential_backoff(attempt, policy))
                continue
            if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < policy.max_attempts:
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After"), self._now(), policy
                )
                await response.aclose()
                await self._sleeper(
                    retry_after if retry_after is not None else exponential_backoff(attempt, policy)
                )
                continue
            return _AttemptResult(response, target)
        return _AttemptResult(None, target, "retry_exhausted", "request retry budget exhausted")

    async def _read_limited(
        self,
        response: httpx.Response,
        *,
        limit: int,
        timeout_seconds: float,
    ) -> tuple[bytes, bool]:
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None:
            try:
                if int(declared_size) > limit:
                    return b"", True
            except ValueError:
                pass
        chunks: list[bytes] = []
        size = 0
        async with asyncio.timeout(timeout_seconds):
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    chunks.append(chunk[: max(0, limit + 1 - (size - len(chunk)))])
                    return b"".join(chunks), True
                chunks.append(chunk)
        return b"".join(chunks), False

    async def _fetch_robots_snapshot(
        self,
        bank: BankSource,
        host: str,
        policy: HostPolicy,
    ) -> RobotsSnapshot | RobotsDecision:
        current_url = f"https://{host}/robots.txt"
        redirect_count = 0
        while True:
            outcome = await self._send_with_retries(
                bank=bank,
                url=current_url,
                headers={
                    "User-Agent": policy.user_agent,
                    "Accept": "text/plain,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                },
            )
            if outcome.response is None:
                return RobotsDecision(False, "robots_unreachable")
            response = outcome.response
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        return RobotsDecision(False, "robots_redirect_without_location")
                    if redirect_count >= policy.max_redirects:
                        return RobotsDecision(False, "robots_redirect_limit")
                    candidate = urljoin(current_url, location)
                    try:
                        validated = await validate_target(candidate, bank, self._resolver)
                    except PolicyViolation:
                        return RobotsDecision(False, "robots_redirect_policy_violation")
                    current_url = validated.url
                    redirect_count += 1
                    continue
                content = b""
                if 200 <= response.status_code < 300:
                    try:
                        content, too_large = await self._read_limited(
                            response,
                            limit=MAX_ROBOTS_BYTES,
                            timeout_seconds=policy.request_timeout_seconds,
                        )
                    except (TimeoutError, httpx.TransportError, OSError):
                        return RobotsDecision(False, "robots_unreachable")
                    if too_large:
                        content = b" " * (MAX_ROBOTS_BYTES + 1)
                now = self._now()
                return RobotsSnapshot(
                    source_url=current_url,
                    status_code=response.status_code,
                    content=content,
                    fetched_at=now,
                    expires_at=now + timedelta(seconds=policy.robots_ttl_seconds),
                )
            finally:
                await response.aclose()

    async def _robots_decision(
        self,
        bank: BankSource,
        target_url: str,
        host: str,
    ) -> RobotsDecision:
        policy = self._policy_provider.for_host(host)
        cache_key = f"https://{host}"
        now = self._now()
        snapshot = await self._robots_cache.get(cache_key, now)
        if snapshot is None:
            fetched = await self._fetch_robots_snapshot(bank, host, policy)
            if isinstance(fetched, RobotsDecision):
                return fetched
            snapshot = fetched
            await self._robots_cache.put(cache_key, snapshot)
        return robots_response_decision(
            snapshot.status_code,
            snapshot.content,
            target_url,
            policy.robots_product_token,
        )

    def _failure_result(
        self,
        *,
        bank_id: str,
        requested_url: str,
        final_url: str | None,
        status: FetchStatus,
        robots: RobotsDecision,
        error_code: str,
        error_detail: str,
        http_status: int | None = None,
    ) -> FetchResult:
        artifact = create_fetch_artifact(
            bank_id=bank_id,
            requested_url=requested_url,
            final_url=final_url,
            status=status,
            http_status=http_status,
            fetched_at=self._now(),
            robots_allowed=robots.allowed,
            error_code=error_code[:500],
            error_detail=(error_detail.strip() or error_code)[:500],
        )
        return FetchResult(artifact, None, robots)

    async def fetch(self, bank_id: str, url: str) -> FetchResult:
        """Fetch one allowlisted URL and return a frozen, provenance-bearing result."""

        bank = self._registry.bank(bank_id)
        # Initial violations are programmer/configuration errors; no network request is made.
        initial_target = await validate_target(url, bank, self._resolver)
        requested_url = initial_target.url
        cache_entry = await self._response_cache.get(requested_url)
        current_url = requested_url
        redirect_count = 0
        robots = await self._robots_decision(bank, current_url, initial_target.host)
        if not robots.allowed:
            return self._failure_result(
                bank_id=bank.id,
                requested_url=requested_url,
                final_url=None,
                status=FetchStatus.BLOCKED,
                robots=robots,
                error_code=robots.reason,
                error_detail="robots.txt policy denied collection",
            )

        while True:
            current_target = await validate_target(current_url, bank, self._resolver)
            policy = self._policy_provider.for_host(current_target.host)
            headers = {
                "User-Agent": policy.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9",
                "Accept-Encoding": "identity",
            }
            # Entity validators belong to the exact selected resource that
            # produced them.  Re-discover a redirect chain unconditionally,
            # then attach validators only if it still reaches the cached final
            # URL.  Carrying B's ETag from A→B onto a changed A→C chain could
            # otherwise make C's 304 resurrect B's bytes.  Validators are also
            # bound to the processing identity that consumed the cached bytes:
            # after an extractor or prompt upgrade a 304 would silently skip
            # reprocessing unchanged content, so a stale-identity entry must
            # degrade to a full unconditional fetch.
            if (
                cache_entry is not None
                and cache_entry.final_url == current_url
                and cache_entry.processing_identity == self._processing_identity
            ):
                if cache_entry.etag:
                    headers["If-None-Match"] = cache_entry.etag
                if cache_entry.last_modified:
                    headers["If-Modified-Since"] = cache_entry.last_modified
            outcome = await self._send_with_retries(
                bank=bank,
                url=current_url,
                headers=headers,
                extra_interval_seconds=robots.crawl_delay_seconds or 0.0,
            )
            if outcome.response is None:
                return self._failure_result(
                    bank_id=bank.id,
                    requested_url=requested_url,
                    final_url=current_url,
                    status=FetchStatus.FAILED,
                    robots=robots,
                    error_code=outcome.error_code or "request_failed",
                    error_detail=outcome.error_detail or "HTTP request failed",
                )
            response = outcome.response
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        return self._failure_result(
                            bank_id=bank.id,
                            requested_url=requested_url,
                            final_url=current_url,
                            status=FetchStatus.FAILED,
                            robots=robots,
                            error_code="redirect_without_location",
                            error_detail="redirect response omitted Location",
                            http_status=response.status_code,
                        )
                    if redirect_count >= policy.max_redirects:
                        return self._failure_result(
                            bank_id=bank.id,
                            requested_url=requested_url,
                            final_url=current_url,
                            status=FetchStatus.FAILED,
                            robots=robots,
                            error_code="redirect_limit",
                            error_detail="redirect limit exceeded",
                            http_status=response.status_code,
                        )
                    candidate = urljoin(current_url, location)
                    try:
                        redirected = await validate_target(candidate, bank, self._resolver)
                    except PolicyViolation as exc:
                        return self._failure_result(
                            bank_id=bank.id,
                            requested_url=requested_url,
                            final_url=current_url,
                            status=FetchStatus.FAILED,
                            robots=robots,
                            error_code="redirect_policy_violation",
                            error_detail=exc.detail,
                            http_status=response.status_code,
                        )
                    redirected_robots = await self._robots_decision(
                        bank, redirected.url, redirected.host
                    )
                    if not redirected_robots.allowed:
                        return self._failure_result(
                            bank_id=bank.id,
                            requested_url=requested_url,
                            final_url=redirected.url,
                            status=FetchStatus.BLOCKED,
                            robots=redirected_robots,
                            error_code=redirected_robots.reason,
                            error_detail="redirect target is denied by robots.txt",
                            http_status=response.status_code,
                        )
                    robots = redirected_robots
                    current_url = redirected.url
                    redirect_count += 1
                    continue

                if response.status_code in _AUTH_STATUSES:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.BLOCKED,
                        robots=robots,
                        error_code="authentication_required",
                        error_detail="server requires authentication or blocks automated access",
                        http_status=response.status_code,
                    )
                if response.status_code == 304:
                    if (
                        cache_entry is None
                        or cache_entry.final_url != current_url
                        or cache_entry.processing_identity != self._processing_identity
                    ):
                        return self._failure_result(
                            bank_id=bank.id,
                            requested_url=requested_url,
                            final_url=current_url,
                            status=FetchStatus.FAILED,
                            robots=robots,
                            error_code="unexpected_not_modified",
                            error_detail=(
                                "server returned 304 without a cache entry for the exact resource"
                            ),
                            http_status=304,
                        )
                    artifact = create_fetch_artifact(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.NOT_MODIFIED,
                        http_status=304,
                        fetched_at=self._now(),
                        robots_allowed=True,
                        content_type=cache_entry.content_type,
                        etag=cache_entry.etag,
                        last_modified=cache_entry.last_modified,
                        raw_sha256=cache_entry.raw_sha256,
                        raw_size_bytes=cache_entry.raw_size_bytes,
                        private_raw_path=cache_entry.private_raw_path,
                    )
                    return FetchResult(artifact, None, robots)
                if not 200 <= response.status_code < 300:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code=f"http_{response.status_code}",
                        error_detail=f"server returned HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                if response.status_code != 200:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code="unexpected_success_status",
                        error_detail=(
                            f"full source retrieval requires HTTP 200, got {response.status_code}"
                        ),
                        http_status=response.status_code,
                    )

                content_type = response.headers.get("Content-Type")
                if not is_html_content_type(content_type):
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code="unsupported_content_type",
                        error_detail="only HTML response content is accepted",
                        http_status=response.status_code,
                    )
                try:
                    raw_content, too_large = await self._read_limited(
                        response,
                        limit=policy.max_response_bytes,
                        timeout_seconds=policy.request_timeout_seconds,
                    )
                except (TimeoutError, httpx.TransportError, OSError) as exc:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code="body_read_error",
                        error_detail=str(exc).strip() or type(exc).__name__,
                        http_status=response.status_code,
                    )
                if too_large:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code="response_too_large",
                        error_detail="response exceeded the configured byte limit",
                        http_status=response.status_code,
                    )
                if not raw_content:
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.FAILED,
                        robots=robots,
                        error_code="empty_response",
                        error_detail="HTML response body is empty",
                        http_status=response.status_code,
                    )
                if has_access_challenge(raw_content):
                    return self._failure_result(
                        bank_id=bank.id,
                        requested_url=requested_url,
                        final_url=current_url,
                        status=FetchStatus.BLOCKED,
                        robots=robots,
                        error_code="access_challenge",
                        error_detail="CAPTCHA or interactive authentication challenge detected",
                        http_status=response.status_code,
                    )

                raw_sha256 = hashlib.sha256(raw_content).hexdigest()
                private_raw_path = self._artifact_store.put(raw_sha256, raw_content, suffix=".html")
                etag = _header_value(response, "ETag")
                last_modified = _header_value(response, "Last-Modified")
                normalized_content_type = _header_value(response, "Content-Type")
                cache_entry = ResponseCacheEntry(
                    requested_url=requested_url,
                    final_url=current_url,
                    etag=etag,
                    last_modified=last_modified,
                    content_type=normalized_content_type,
                    raw_sha256=raw_sha256,
                    raw_size_bytes=len(raw_content),
                    private_raw_path=private_raw_path,
                    stored_at=self._now(),
                    processing_identity=self._processing_identity,
                )
                await self._response_cache.put(cache_entry)
                artifact = create_fetch_artifact(
                    bank_id=bank.id,
                    requested_url=requested_url,
                    final_url=current_url,
                    status=FetchStatus.SUCCESS,
                    http_status=response.status_code,
                    fetched_at=self._now(),
                    robots_allowed=True,
                    content_type=normalized_content_type,
                    etag=etag,
                    last_modified=last_modified,
                    raw_sha256=raw_sha256,
                    raw_size_bytes=len(raw_content),
                    private_raw_path=private_raw_path,
                )
                return FetchResult(artifact, raw_content, robots)
            finally:
                await response.aclose()


def _header_value(response: httpx.Response, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped[:500] or None


def _pinned_url(target: ValidatedTarget, attempt: int) -> str:
    """Connect to one validated IP while preserving Host and TLS SNI separately."""

    address = ipaddress.ip_address(
        target.resolved_addresses[attempt % len(target.resolved_addresses)]
    )
    network_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    logical = urlsplit(target.url)
    return urlunsplit(("https", network_host, logical.path, logical.query, ""))
