from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from katilim_analiz.contracts import FetchStatus
from katilim_analiz.ingestion.artifacts import MemoryArtifactStore
from katilim_analiz.ingestion.cache import InMemoryResponseCache
from katilim_analiz.ingestion.fetcher import HttpIngestor
from katilim_analiz.ingestion.policy import (
    HostPolicy,
    StaticAddressResolver,
    StaticHostPolicyProvider,
)
from katilim_analiz.ingestion.registry import load_registry
from katilim_analiz.ingestion.robots import InMemoryRobotsCache

REGISTRY_PATH = (
    Path(__file__).resolve().parents[4]
    / "data"
    / "registry"
    / "bddk-participation-banks-2026-07-18.json"
)
NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
PUBLIC_DNS = StaticAddressResolver(
    {
        "www.kuveytturk.com.tr": ("93.184.216.34",),
        "kuveytturk.com.tr": ("93.184.216.35",),
    }
)


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def acquire(self, host: str, minimum_interval_seconds: float) -> None:
        self.calls.append((host, minimum_interval_seconds))


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class BrokenAsyncStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise httpx.ReadError("stream failed")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        return None


def _ingestor(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    policy: HostPolicy | None = None,
    store: MemoryArtifactStore | None = None,
    cache: InMemoryResponseCache | None = None,
    robots_cache: InMemoryRobotsCache | None = None,
    rate_limiter: RecordingRateLimiter | None = None,
    sleeper: RecordingSleeper | None = None,
) -> HttpIngestor:
    return HttpIngestor(
        registry=load_registry(REGISTRY_PATH),
        artifact_store=store or MemoryArtifactStore(),
        resolver=PUBLIC_DNS,
        transport=httpx.MockTransport(handler),
        policy_provider=StaticHostPolicyProvider(policy or HostPolicy(min_interval_seconds=0)),
        response_cache=cache or InMemoryResponseCache(),
        robots_cache=robots_cache or InMemoryRobotsCache(),
        rate_limiter=rate_limiter or RecordingRateLimiter(),
        sleeper=sleeper or RecordingSleeper(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_successful_fetch_checks_robots_and_creates_immutable_artifact() -> None:
    requests: list[httpx.Request] = []
    store = MemoryArtifactStore()
    limiter = RecordingRateLimiter()
    raw = b"<html><main><h1>Kampanya</h1></main></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            content=raw,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "ETag": '"campaign-v1"',
            },
        )

    ingestor = _ingestor(handler, store=store, rate_limiter=limiter)
    result = await ingestor.fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/kampanyalar/ornek")

    digest = hashlib.sha256(raw).hexdigest()
    assert [request.url.path for request in requests] == ["/robots.txt", "/kampanyalar/ornek"]
    assert {request.url.host for request in requests} == {"93.184.216.34"}
    assert {request.headers["Host"] for request in requests} == {"www.kuveytturk.com.tr"}
    assert {request.extensions["sni_hostname"] for request in requests} == {"www.kuveytturk.com.tr"}
    assert result.artifact.status is FetchStatus.SUCCESS
    assert result.artifact.raw_sha256 == digest
    assert result.artifact.raw_size_bytes == len(raw)
    assert result.artifact.robots_allowed
    assert result.raw_content == raw
    assert store.read(digest) == raw
    assert len(limiter.calls) == 2


@pytest.mark.asyncio
async def test_robots_disallow_prevents_target_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    result = await _ingestor(handler).fetch(
        "kuveyt-turk", "https://www.kuveytturk.com.tr/private/campaign"
    )

    assert paths == ["/robots.txt"]
    assert result.artifact.status is FetchStatus.BLOCKED
    assert result.artifact.error_code == "robots_disallowed"
    assert not result.artifact.robots_allowed


@pytest.mark.asyncio
async def test_robots_redirect_to_an_unsafe_target_fails_closed() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/robots.txt"})

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert paths == ["/robots.txt"]
    assert result.artifact.status is FetchStatus.BLOCKED
    assert result.artifact.error_code == "robots_redirect_policy_violation"


@pytest.mark.asyncio
async def test_robots_crawl_delay_increases_the_target_host_interval() -> None:
    limiter = RecordingRateLimiter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 7\n")
        return httpx.Response(200, content=b"<p>ok</p>", headers={"Content-Type": "text/html"})

    result = await _ingestor(handler, rate_limiter=limiter).fetch(
        "kuveyt-turk", "https://www.kuveytturk.com.tr/campaign"
    )

    assert result.artifact.status is FetchStatus.SUCCESS
    assert limiter.calls == [
        ("www.kuveytturk.com.tr", 0),
        ("www.kuveytturk.com.tr", 7),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/admin",
        "https://attacker.example/collect",
        "http://www.kuveytturk.com.tr/insecure",
    ],
)
async def test_redirects_are_revalidated_before_following(location: str) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": location})

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert len(requested_urls) == 2
    assert result.artifact.status is FetchStatus.FAILED
    assert result.artifact.error_code == "redirect_policy_violation"


@pytest.mark.asyncio
async def test_allowlisted_redirect_is_followed_with_a_fresh_dns_check() -> None:
    paths_and_hosts: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        logical_host = request.headers["Host"]
        paths_and_hosts.append((logical_host, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if logical_host == "www.kuveytturk.com.tr":
            return httpx.Response(301, headers={"Location": "https://kuveytturk.com.tr/final"})
        return httpx.Response(200, content=b"<p>ok</p>", headers={"Content-Type": "text/html"})

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert paths_and_hosts == [
        ("www.kuveytturk.com.tr", "/robots.txt"),
        ("www.kuveytturk.com.tr", "/campaign"),
        ("kuveytturk.com.tr", "/robots.txt"),
        ("kuveytturk.com.tr", "/final"),
    ]
    assert str(result.artifact.final_url) == "https://kuveytturk.com.tr/final"


@pytest.mark.asyncio
async def test_transient_failures_use_bounded_exponential_backoff() -> None:
    target_attempts = 0
    sleeper = RecordingSleeper()
    policy = HostPolicy(
        min_interval_seconds=0,
        max_attempts=3,
        backoff_base_seconds=1,
        backoff_cap_seconds=10,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal target_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        target_attempts += 1
        if target_attempts < 3:
            return httpx.Response(503, headers={"Retry-After": "invalid"})
        return httpx.Response(200, content=b"<p>ok</p>", headers={"Content-Type": "text/html"})

    result = await _ingestor(handler, policy=policy, sleeper=sleeper).fetch(
        "kuveyt-turk", "https://www.kuveytturk.com.tr/campaign"
    )

    assert result.artifact.status is FetchStatus.SUCCESS
    assert target_attempts == 3
    assert sleeper.delays == [1, 2]


@pytest.mark.asyncio
async def test_auth_and_captcha_responses_are_blocked_without_retry_or_storage() -> None:
    for response in (
        httpx.Response(401, text="login"),
        httpx.Response(
            200,
            text="<html><title>CAPTCHA</title><p>verify you are human</p></html>",
            headers={"Content-Type": "text/html"},
        ),
    ):
        attempts = 0
        store = MemoryArtifactStore()

        def handler(
            request: httpx.Request,
            selected_response: httpx.Response = response,
        ) -> httpx.Response:
            nonlocal attempts
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            attempts += 1
            return selected_response

        result = await _ingestor(handler, store=store).fetch(
            "kuveyt-turk", "https://www.kuveytturk.com.tr/campaign"
        )

        assert result.artifact.status is FetchStatus.BLOCKED
        assert result.artifact.error_code in {"authentication_required", "access_challenge"}
        assert attempts == 1
        assert store.items == {}


@pytest.mark.asyncio
async def test_conditional_cache_turns_304_into_a_hash_linked_artifact() -> None:
    target_attempts = 0
    cache = InMemoryResponseCache()
    robots_cache = InMemoryRobotsCache()
    raw = b"<p>cached</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal target_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        target_attempts += 1
        if target_attempts == 1:
            return httpx.Response(
                200,
                content=raw,
                headers={"Content-Type": "text/html", "ETag": '"v1"'},
            )
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    ingestor = _ingestor(handler, cache=cache, robots_cache=robots_cache)
    first = await ingestor.fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")
    second = await ingestor.fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert first.artifact.status is FetchStatus.SUCCESS
    assert second.artifact.status is FetchStatus.NOT_MODIFIED
    assert second.artifact.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert second.artifact.raw_size_bytes == len(raw)
    assert second.raw_content is None
    assert target_attempts == 2


@pytest.mark.asyncio
async def test_redirect_change_never_carries_old_resource_validators() -> None:
    cache = InMemoryResponseCache()
    robots_cache = InMemoryRobotsCache()
    start_attempts = 0
    observed_headers: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal start_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        observed_headers.append((request.url.path, request.headers.get("If-None-Match")))
        if request.url.path == "/start":
            start_attempts += 1
            destination = "/old" if start_attempts == 1 else "/new"
            return httpx.Response(302, headers={"Location": destination})
        if request.url.path == "/old":
            return httpx.Response(
                200,
                content=b"<p>old resource</p>",
                headers={"Content-Type": "text/html", "ETag": '"shared"'},
            )
        if request.headers.get("If-None-Match") == '"shared"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=b"<p>new resource</p>",
            headers={"Content-Type": "text/html", "ETag": '"shared"'},
        )

    ingestor = _ingestor(handler, cache=cache, robots_cache=robots_cache)
    first = await ingestor.fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/start")
    second = await ingestor.fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/start")

    assert first.artifact.status is FetchStatus.SUCCESS
    assert str(first.artifact.final_url) == "https://www.kuveytturk.com.tr/old"
    assert second.artifact.status is FetchStatus.SUCCESS
    assert str(second.artifact.final_url) == "https://www.kuveytturk.com.tr/new"
    assert second.raw_content == b"<p>new resource</p>"
    assert observed_headers == [
        ("/start", None),
        ("/old", None),
        ("/start", None),
        ("/new", None),
    ]


@pytest.mark.asyncio
async def test_content_type_and_stream_size_limits_fail_closed() -> None:
    responses = (
        httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"}),
        httpx.Response(
            200,
            content=b"x" * 11,
            headers={"Content-Type": "text/html", "Content-Length": "11"},
        ),
    )
    expected_codes = ("unsupported_content_type", "response_too_large")

    for response, expected_code in zip(responses, expected_codes, strict=True):

        def handler(
            request: httpx.Request,
            selected_response: httpx.Response = response,
        ) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return selected_response

        result = await _ingestor(
            handler,
            policy=HostPolicy(min_interval_seconds=0, max_response_bytes=10),
        ).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

        assert result.artifact.status is FetchStatus.FAILED
        assert result.artifact.error_code == expected_code


@pytest.mark.asyncio
async def test_partial_content_is_never_accepted_as_a_complete_source_artifact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            206,
            content=b"<p>partial</p>",
            headers={"Content-Type": "text/html"},
        )

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert result.artifact.status is FetchStatus.FAILED
    assert result.artifact.error_code == "unexpected_success_status"


@pytest.mark.asyncio
async def test_stream_failure_returns_an_explicit_failed_artifact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            stream=BrokenAsyncStream(),
        )

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert result.artifact.status is FetchStatus.FAILED
    assert result.artifact.error_code == "body_read_error"


@pytest.mark.asyncio
async def test_robots_stream_failure_is_complete_disallow() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=BrokenAsyncStream())

    result = await _ingestor(handler).fetch("kuveyt-turk", "https://www.kuveytturk.com.tr/campaign")

    assert calls == 1
    assert result.artifact.status is FetchStatus.BLOCKED
    assert result.artifact.error_code == "robots_unreachable"
