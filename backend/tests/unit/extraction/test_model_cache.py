from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from _factories import make_document

from katilim_analiz.contracts import CleanDocument
from katilim_analiz.llm import (
    CircuitBreaker,
    FileModelResponseCache,
    InMemoryModelResponseCache,
    LayeredModelResponseCache,
    ModelFactField,
    OllamaStructuredClient,
)
from katilim_analiz.llm.cache import (
    CACHE_SCHEMA_VERSION,
    CachedModelAnswer,
    ModelResponseCache,
    model_response_cache_key,
)

MODEL_DIGEST = "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"


def _envelope(content: str) -> dict[str, Any]:
    return {
        "model": "qwen3.5:4b",
        "created_at": "2026-07-18T12:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }


def _counting_transport(
    facts: list[dict[str, Any]],
) -> tuple[httpx.MockTransport, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_envelope(json.dumps({"facts": facts})))

    return httpx.MockTransport(handler), calls


def _client(
    transport: httpx.AsyncBaseTransport,
    cache: ModelResponseCache | None,
    *,
    circuit_breaker: CircuitBreaker | None = None,
) -> tuple[OllamaStructuredClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=transport)
    client = OllamaStructuredClient(
        base_url="http://ollama:11434",
        model="qwen3.5:4b",
        model_digest=MODEL_DIGEST,
        timeout_seconds=120,
        max_context=4096,
        keep_alive="-1",
        http_client=http_client,
        circuit_breaker=circuit_breaker,
        response_cache=cache,
    )
    return client, http_client


@pytest.mark.asyncio
async def test_identical_question_is_answered_once_and_replayed_from_cache(
    campaign_document: CleanDocument,
) -> None:
    quote = "finansman kâr payı oranı aylık %1,89"
    transport, calls = _counting_transport([{"field": "rate", "quote": quote}])
    client, http_client = _client(transport, InMemoryModelResponseCache())
    try:
        first = await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        second = await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert len(calls) == 1, "the identical narrow question must not hit the model twice"
    assert first == second
    assert [fact.proposed_quote for fact in second.facts] == [quote]


@pytest.mark.asyncio
async def test_empty_validated_answer_is_also_cached(
    campaign_document: CleanDocument,
) -> None:
    transport, calls = _counting_transport([])
    client, http_client = _client(transport, InMemoryModelResponseCache())
    try:
        for _ in range(3):
            response = await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
            assert response.facts == []
    finally:
        await http_client.aclose()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_changed_document_content_misses_the_cache(
    campaign_document: CleanDocument,
) -> None:
    changed = make_document(
        ("heading", "Avantajlı Konut Finansmanı"),
        ("paragraph", "Finansman kâr payı oranı aylık %2,05 olarak güncellenmiştir."),
    )
    transport, calls = _counting_transport([])
    client, http_client = _client(transport, InMemoryModelResponseCache())
    try:
        await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        await client.extract(changed, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert len(calls) == 2, "different source content is a different question"


@pytest.mark.asyncio
async def test_cache_replay_does_not_touch_the_circuit_breaker(
    campaign_document: CleanDocument,
) -> None:
    transport, calls = _counting_transport([])
    breaker = CircuitBreaker(failure_threshold=1)
    client, http_client = _client(
        transport,
        InMemoryModelResponseCache(),
        circuit_breaker=breaker,
    )
    try:
        await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        breaker.content_failures = 11  # one step from tripping
        await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert len(calls) == 1
    assert breaker.content_failures == 11, "a replay is not an inference outcome"


@pytest.mark.asyncio
async def test_poisoned_persisted_entry_never_attaches_and_falls_back_to_live_model(
    campaign_document: CleanDocument,
    tmp_path: Path,
) -> None:
    """A cached quote that is not in the current bounded request must not replay."""

    file_cache = FileModelResponseCache(tmp_path)
    quote = "finansman kâr payı oranı aylık %1,89"
    transport, calls = _counting_transport([{"field": "rate", "quote": quote}])
    client, http_client = _client(transport, file_cache)

    # Poison the exact key this question will use with an ungrounded quote.
    from katilim_analiz.llm.prompt import PROMPT_VERSION, build_prompt_package

    package = build_prompt_package(campaign_document, frozenset({ModelFactField.RATE}))
    key = model_response_cache_key(
        model_digest=MODEL_DIGEST,
        prompt_version=PROMPT_VERSION,
        requested_fields=frozenset({ModelFactField.RATE}),
        user_content=package.user_content,
    )
    file_cache.put(
        key,
        CachedModelAnswer(facts=((ModelFactField.RATE, "bu alıntı kaynakta hiç yok"),)),
    )
    try:
        response = await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert len(calls) == 1, "the ungrounded cached entry must degrade to a live call"
    assert [fact.proposed_quote for fact in response.facts] == [quote]


@pytest.mark.asyncio
async def test_file_cache_round_trips_across_client_instances(
    campaign_document: CleanDocument,
    tmp_path: Path,
) -> None:
    quote = "finansman kâr payı oranı aylık %1,89"
    transport, calls = _counting_transport([{"field": "rate", "quote": quote}])
    first_client, first_http = _client(transport, FileModelResponseCache(tmp_path))
    try:
        await first_client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await first_http.aclose()

    second_transport, second_calls = _counting_transport([])
    second_client, second_http = _client(
        second_transport,
        LayeredModelResponseCache([InMemoryModelResponseCache(), FileModelResponseCache(tmp_path)]),
    )
    try:
        replayed = await second_client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await second_http.aclose()

    assert len(calls) == 1
    assert second_calls == [], "a fresh process must replay the persisted answer"
    assert [fact.proposed_quote for fact in replayed.facts] == [quote]


def test_file_cache_rejects_malformed_or_out_of_contract_entries(tmp_path: Path) -> None:
    cache = FileModelResponseCache(tmp_path)
    key = "a" * 64
    path = tmp_path / f"{key}.json"

    path.write_text("{not json", encoding="utf-8")
    assert cache.get(key) is None

    path.write_text(
        json.dumps({"schema_version": "model-response-cache/999", "facts": []}),
        encoding="utf-8",
    )
    assert cache.get(key) is None

    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "facts": [{"field": "not_a_field", "quote": "x"}],
            }
        ),
        encoding="utf-8",
    )
    assert cache.get(key) is None

    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "facts": [{"field": "rate", "quote": "x", "sql": "DROP TABLE"}],
            }
        ),
        encoding="utf-8",
    )
    assert cache.get(key) is None


def test_in_memory_cache_evicts_least_recently_used_entries() -> None:
    cache = InMemoryModelResponseCache(max_entries=2)
    answers = {name: CachedModelAnswer(facts=((ModelFactField.RATE, name),)) for name in "abc"}
    cache.put("a" * 64, answers["a"])
    cache.put("b" * 64, answers["b"])
    assert cache.get("a" * 64) == answers["a"]  # refresh a
    cache.put("c" * 64, answers["c"])  # evicts b

    assert cache.get("b" * 64) is None
    assert cache.get("a" * 64) == answers["a"]
    assert cache.get("c" * 64) == answers["c"]
