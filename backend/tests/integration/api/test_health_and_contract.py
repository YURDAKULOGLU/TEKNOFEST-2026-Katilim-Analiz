from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from conftest import FakeDatabaseHealth


@pytest.mark.asyncio
async def test_liveness_and_readiness_are_separate(client: httpx.AsyncClient) -> None:
    live = await client.get("/health/live")
    ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"
    assert ready.json()["checks"]["migration"] == "ok"


@pytest.mark.asyncio
async def test_readiness_degrades_for_database_or_migration_drift(
    client_factory: Callable[..., httpx.AsyncClient],
) -> None:
    async with client_factory(database_health=FakeDatabaseHealth(healthy=False)) as db_client:
        unavailable = await db_client.get("/health/ready")
    async with client_factory(
        database_health=FakeDatabaseHealth(current="rev-old", head="rev-head")
    ) as drift_client:
        drift = await drift_client.get("/health/ready")

    assert unavailable.status_code == 503
    assert unavailable.json()["checks"]["database"] == "failed"
    assert drift.status_code == 503
    assert drift.json()["checks"]["migration"] == "out_of_date"


@pytest.mark.asyncio
async def test_openapi_contract_is_31_and_exposes_only_versioned_v1_routes(
    client: httpx.AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()

    assert document["openapi"].startswith("3.1")
    assert {
        "/health/live",
        "/health/ready",
        "/api/v1/campaigns",
        "/api/v1/campaigns/{campaign_id}",
        "/api/v1/notifications",
        "/api/v1/coverage",
        "/api/v1/comparisons",
        "/api/v1/chat",
        "/api/v1/previews/extractions",
    }.issubset(document["paths"])
    assert "ProblemDetail" in document["components"]["schemas"]


@pytest.mark.asyncio
async def test_correlation_id_is_validated_and_returned(client: httpx.AsyncClient) -> None:
    accepted = await client.get("/health/live", headers={"x-correlation-id": "demo-123"})
    rejected = await client.get("/health/live", headers={"x-correlation-id": "bad value\r\nsecret"})

    assert accepted.headers["x-correlation-id"] == "demo-123"
    assert rejected.headers["x-correlation-id"] != "bad value\r\nsecret"
    assert len(rejected.headers["x-correlation-id"]) == 32


@pytest.mark.asyncio
async def test_configured_cors_origin_is_narrowly_allowed(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/v1/campaigns",
        headers={
            "origin": "http://localhost:5173",
            "access-control-request-method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers.get("access-control-allow-credentials") is None


@pytest.mark.asyncio
async def test_untrusted_host_is_rejected_before_cors(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/v1/campaigns",
        headers={
            "host": "attacker.example",
            "origin": "http://localhost:5173",
            "access-control-request-method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
