from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
import pytest
from conftest import FakeReads


@pytest.mark.asyncio
async def test_campaign_list_has_typed_facets_as_of_and_cursor(
    client: httpx.AsyncClient, reads: FakeReads
) -> None:
    response = await client.get(
        "/api/v1/campaigns",
        params={"bank_id": "bank-a", "product_family": "financing", "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == ["campaign-a"]
    assert payload["items"][0]["campaign_key"] == "bank-a:campaign-a"
    assert payload["facets"]["banks"][0] == {
        "value": "bank-a",
        "label": "Banka A",
        "count": 1,
    }
    assert payload["facets"]["product_families"][0] == {
        "value": "financing",
        "label": "Finansman",
        "count": 2,
    }
    assert payload["facets"]["sales_channels"][0] == {
        "value": "all",
        "label": "Tüm Kanallar",
        "count": 2,
    }
    assert payload["as_of"] == "2026-07-18T18:30:00Z"
    assert reads.last_query is not None
    assert reads.last_query[0].bank_id == "bank-a"


@pytest.mark.asyncio
async def test_campaign_cursor_request_reuses_the_first_page_snapshot(
    client: httpx.AsyncClient,
    reads: FakeReads,
) -> None:
    first = await client.get("/api/v1/campaigns", params={"limit": 1})
    first_payload = first.json()

    second = await client.get(
        "/api/v1/campaigns",
        params={
            "limit": 1,
            "cursor": first_payload["next_cursor"],
            "as_of": first_payload["as_of"],
        },
    )

    assert second.status_code == 200
    assert second.json()["as_of"] == first_payload["as_of"]
    assert reads.last_query is not None
    assert reads.last_query[3] == datetime.fromisoformat(first_payload["as_of"])


@pytest.mark.asyncio
async def test_cursor_and_filter_validation_return_problem_details(
    client: httpx.AsyncClient,
) -> None:
    bad_cursor = await client.get("/api/v1/campaigns", params={"cursor": "not-a-cursor"})
    bad_filter = await client.get("/api/v1/campaigns", params={"sales_channel": "sql;drop table"})

    assert bad_cursor.status_code == 400
    assert bad_cursor.headers["content-type"].startswith("application/problem+json")
    assert bad_cursor.json()["code"] == "invalid_cursor"
    assert bad_filter.status_code == 422
    assert bad_filter.json()["code"] == "request_validation_failed"
    assert "input" not in str(bad_filter.json()["errors"])


@pytest.mark.asyncio
async def test_naive_as_of_is_rejected_at_http_boundary(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/campaigns",
        params={"as_of": "2026-07-18T18:30:00"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_campaign_detail_projects_source_and_field_evidence(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/campaigns/campaign-a")

    assert response.status_code == 200
    payload = response.json()
    assert payload["campaign"]["bank_name"] == "Banka A"
    assert payload["source_url"] == "https://example.test/campaign-a"
    assert payload["evidence"][0]["field_pointer"] == "/data/rates/0/value_percent"
    assert payload["extraction"]["method"] == "hybrid"


@pytest.mark.asyncio
async def test_unknown_campaign_returns_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/campaigns/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "campaign_not_found"


@pytest.mark.asyncio
async def test_coverage_is_deterministic(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/coverage")

    assert response.status_code == 200
    assert response.json()[0]["bank_id"] == "bank-a"


@pytest.mark.asyncio
async def test_unhandled_error_is_opaque_rfc9457_problem(
    client_factory: Callable[..., httpx.AsyncClient], reads: FakeReads
) -> None:
    reads.raise_error = True
    async with client_factory(raise_app_exceptions=False) as api_client:
        response = await api_client.get("/api/v1/campaigns")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "internal_error"
    assert "database-password" not in response.text
