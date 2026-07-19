from datetime import UTC, datetime

import pytest

from katilim_analiz.contracts import (
    ComparisonDimension,
    ComparisonRequest,
    ComparisonResponse,
)
from katilim_analiz.storage.repositories import (
    ComparisonRepository,
    comparison_request_sha256,
)
from katilim_analiz.storage.serialization import canonical_sha256


@pytest.mark.asyncio
async def test_comparisons_are_content_addressed_with_ruleset_version(database) -> None:  # type: ignore[no-untyped-def]
    request = ComparisonRequest(
        campaign_ids=["campaign:a", "campaign:b"],
        dimensions=[ComparisonDimension.RATE],
    )
    generated_at = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    response = ComparisonResponse(
        ruleset_version="comparison/1",
        generated_at=generated_at,
        items=[],
        canonical_sha256=canonical_sha256(
            {"ruleset_version": "comparison/1", "generated_at": generated_at}
        ),
    )

    async with database.transaction() as session:
        repository = ComparisonRepository(session)
        comparison_id, created = await repository.put(request, response, record_ids=[])
        duplicate_id, duplicate_created = await repository.put(request, response, record_ids=[])
        distinct_request = ComparisonRequest(
            campaign_ids=["campaign:c", "campaign:d"],
            dimensions=[ComparisonDimension.RATE],
        )
        distinct_id, distinct_created = await repository.put(
            distinct_request, response, record_ids=[]
        )

    request_hash = comparison_request_sha256(
        request,
        ruleset_version=response.ruleset_version,
        generated_at=response.generated_at,
    )
    async with database.session() as session:
        stored = await ComparisonRepository(session).by_request_hash(request_hash)

    assert created
    assert not duplicate_created
    assert comparison_id == duplicate_id
    assert distinct_created
    assert distinct_id != comparison_id
    assert stored is not None
    assert stored.response["canonical_sha256"] == response.canonical_sha256
