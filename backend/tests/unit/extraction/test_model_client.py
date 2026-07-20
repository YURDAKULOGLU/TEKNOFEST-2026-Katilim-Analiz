from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from _factories import make_document

from katilim_analiz.contracts import CleanDocument
from katilim_analiz.llm import (
    CircuitBreaker,
    ModelFactField,
    ModelFailureCode,
    ModelInferenceError,
    ModelInferenceSkipped,
    OllamaStructuredClient,
)
from katilim_analiz.llm.contracts import MAX_QUOTE_CHARS


def _envelope(
    content: str,
    *,
    thinking: str | None = None,
    done_reason: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking is not None:
        message["thinking"] = thinking
    envelope = {
        "model": "qwen3.5:4b",
        "created_at": "2026-07-18T12:00:00Z",
        "message": message,
        "done": True,
    }
    if done_reason is not None:
        envelope["done_reason"] = done_reason
    return envelope


def _content(*, facts: list[dict[str, Any]] | None = None) -> str:
    supplied_facts = facts or []
    return json.dumps({"facts": supplied_facts})


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    circuit_breaker: CircuitBreaker | None = None,
    timeout_seconds: float = 120,
    concurrency: int = 1,
) -> tuple[OllamaStructuredClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=transport)
    client = OllamaStructuredClient(
        base_url="http://ollama:11434",
        model="qwen3.5:4b",
        model_digest="2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
        timeout_seconds=timeout_seconds,
        max_context=4096,
        keep_alive="-1",
        concurrency=concurrency,
        http_client=http_client,
        circuit_breaker=circuit_breaker,
    )
    return client, http_client


@pytest.mark.asyncio
async def test_request_reaches_ollama_with_schema_think_disabled_and_no_tools(
    campaign_document: CleanDocument,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_envelope(_content()))

    client, http_client = _client(httpx.MockTransport(handler))
    try:
        response = await client.extract(
            campaign_document,
            frozenset({ModelFactField.RATE}),
        )
    finally:
        await http_client.aclose()

    assert response.document_id == campaign_document.id
    assert response.schema_version == "model-extraction/1.1"
    assert captured["think"] is False
    assert captured["stream"] is False
    assert captured["keep_alive"] == -1
    assert "tools" not in captured
    assert captured["options"]["num_ctx"] == 4096
    assert captured["format"]["additionalProperties"] is False
    assert set(captured["format"]["properties"]) == {"facts"}
    assert set(captured["format"]["properties"]["facts"]["items"]["required"]) == {
        "field",
        "quote",
    }
    assert set(captured["format"]["properties"]["facts"]["items"]["properties"]) == {
        "field",
        "quote",
    }
    assert captured["format"]["properties"]["facts"]["maxItems"] == 1
    assert (
        captured["format"]["properties"]["facts"]["items"]["properties"]["quote"]["maxLength"]
        == MAX_QUOTE_CHARS
    )
    assert captured["options"]["num_predict"] == 192
    assert "abstentions" not in captured["format"]["properties"]
    assert "outcome" not in captured["format"]["properties"]
    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload["requested_fields"] == ["rate"]
    assert "field_guides" not in user_payload
    assert "oran türünü ve aylık/yıllık dönemini" in captured["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ('{"facts":', ModelFailureCode.JSON_INVALID),
        (
            '{"facts":[],"facts":[]}',
            ModelFailureCode.JSON_INVALID,
        ),
        (
            json.dumps(
                {
                    "facts": [],
                    "extra": "schema abuse",
                }
            ),
            ModelFailureCode.SCHEMA_INVALID,
        ),
    ],
)
async def test_malformed_duplicate_or_extra_json_fails_closed(
    campaign_document: CleanDocument,
    content: str,
    expected_code: ModelFailureCode,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_envelope(content)))
    client, http_client = _client(transport)
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()
    assert error.value.code is expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts",
    [
        [{"field": "rate", "quote": "x" * (MAX_QUOTE_CHARS + 1)}],
        [
            {"field": "rate", "quote": "aylık oran %1,99"},
            {"field": "rate", "quote": "yıllık oran %20"},
        ],
        [{"field": "rate", "quote": "aylık oran %1,99", "block_id": "forbidden"}],
    ],
)
async def test_live_body_enforces_quote_fact_and_extra_property_limits(
    campaign_document: CleanDocument,
    facts: list[dict[str, Any]],
) -> None:
    client, http_client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_envelope(_content(facts=facts)))
        )
    )
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.SCHEMA_INVALID


@pytest.mark.asyncio
async def test_nonempty_thinking_is_rejected(campaign_document: CleanDocument) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_envelope(_content(), thinking="hidden reasoning"),
        )
    )
    client, http_client = _client(transport)
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()
    assert error.value.code is ModelFailureCode.THINKING_PRESENT


@pytest.mark.asyncio
async def test_timeout_opens_circuit_and_prevents_repeated_slow_calls(
    campaign_document: CleanDocument,
) -> None:
    calls = 0

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow local model", request=request)

    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=300)
    client, http_client = _client(httpx.MockTransport(timeout_handler), circuit_breaker=breaker)
    try:
        for _ in range(2):
            with pytest.raises(ModelInferenceError) as error:
                await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
            assert error.value.code is ModelFailureCode.TIMEOUT
        with pytest.raises(ModelInferenceError) as open_error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert open_error.value.code is ModelFailureCode.CIRCUIT_OPEN
    assert calls == 2


class _DripStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        while True:
            await asyncio.sleep(0.01)
            yield b" "


@pytest.mark.asyncio
async def test_timeout_is_a_total_wall_clock_deadline(
    campaign_document: CleanDocument,
) -> None:
    async def drip_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DripStream())

    client, http_client = _client(
        httpx.MockTransport(drip_handler),
        timeout_seconds=0.05,
    )
    try:
        with pytest.raises(ModelInferenceError) as error:
            await asyncio.wait_for(
                client.extract(campaign_document, frozenset({ModelFactField.RATE})),
                timeout=1,
            )
    finally:
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.TIMEOUT


@pytest.mark.asyncio
async def test_each_concurrent_call_has_one_queue_and_inference_deadline(
    campaign_document: CleanDocument,
) -> None:
    first_request_started = asyncio.Event()
    calls = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_request_started.set()
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=_envelope(_content()))

    client, http_client = _client(
        httpx.MockTransport(slow_handler),
        timeout_seconds=0.15,
        concurrency=1,
    )
    first = asyncio.create_task(client.extract(campaign_document, frozenset({ModelFactField.RATE})))
    try:
        await asyncio.wait_for(first_request_started.wait(), timeout=1)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        elapsed = asyncio.get_running_loop().time() - started
        assert (await first).document_id == campaign_document.id
    finally:
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.TIMEOUT
    assert elapsed < 0.22


@pytest.mark.asyncio
async def test_timeout_waiting_for_model_slot_does_not_trip_circuit(
    campaign_document: CleanDocument,
) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=300)
    client, http_client = _client(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_envelope(_content()))),
        circuit_breaker=breaker,
        timeout_seconds=0.05,
    )
    await client._semaphore.acquire()
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        client._semaphore.release()
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.TIMEOUT
    assert breaker.failures == 0
    assert breaker.state.value == "closed"


def _ungrounded_facts() -> list[dict[str, Any]]:
    return [{"field": "rate", "quote": "bu alıntı gönderilen bloklarda yok"}]


@pytest.mark.asyncio
async def test_rejected_quote_keeps_serving_later_documents(
    campaign_document: CleanDocument,
) -> None:
    """A responsive model must not be taken out of service by grounding rejects."""

    calls = 0

    async def ungrounded_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_envelope(_content(facts=_ungrounded_facts())))

    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=300)
    client, http_client = _client(
        httpx.MockTransport(ungrounded_handler),
        circuit_breaker=breaker,
    )
    try:
        for _ in range(5):
            with pytest.raises(ModelInferenceError) as error:
                await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
            assert error.value.code is ModelFailureCode.BLOCK_NOT_SENT
    finally:
        await http_client.aclose()

    assert calls == 5, "the model must still be consulted for every later document"
    assert breaker.state.value == "closed"
    assert breaker.failures == 0
    assert breaker.content_failures == 5


@pytest.mark.asyncio
async def test_persistently_ungrounded_model_is_eventually_taken_out_of_service(
    campaign_document: CleanDocument,
) -> None:
    calls = 0

    async def ungrounded_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_envelope(_content(facts=_ungrounded_facts())))

    breaker = CircuitBreaker(content_failure_threshold=3, recovery_seconds=300)
    client, http_client = _client(
        httpx.MockTransport(ungrounded_handler),
        circuit_breaker=breaker,
    )
    try:
        for _ in range(3):
            with pytest.raises(ModelInferenceError):
                await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        with pytest.raises(ModelInferenceError) as open_error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert open_error.value.code is ModelFailureCode.CIRCUIT_OPEN
    assert calls == 3, "inference slots must stop being burned once the model is useless"


@pytest.mark.asyncio
async def test_recovery_probe_answered_with_bad_quote_reopens_the_service() -> None:
    """The probe proves the dependency answers; only its content was unusable."""

    clock = 1_000.0
    breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_seconds=120,
        monotonic=lambda: clock,
    )
    breaker.acquire()
    breaker.failure()
    breaker.acquire()
    breaker.failure()
    assert breaker.state.value == "open"

    clock += 121
    breaker.acquire()
    assert breaker.state.value == "half_open"
    breaker.failure(dependency=False)

    assert breaker.state.value == "closed"
    assert breaker.failures == 0
    breaker.acquire()  # must not raise: the circuit is serving traffic again


@pytest.mark.asyncio
async def test_total_deadline_includes_response_parsing(
    campaign_document: CleanDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_parse = OllamaStructuredClient._parse_envelope

    def slow_parse(raw: bytes, document_id: str):  # type: ignore[no-untyped-def]
        time.sleep(0.2)
        return original_parse(raw, document_id)

    monkeypatch.setattr(
        OllamaStructuredClient,
        "_parse_envelope",
        staticmethod(slow_parse),
    )
    client, http_client = _client(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_envelope(_content()))),
        timeout_seconds=0.05,
    )
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.TIMEOUT
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_owned_private_client_ignores_environment_proxy_settings(
    campaign_document: CleanDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_envelope(_content()))
        )
    )
    constructor_kwargs: dict[str, object] = {}

    def build_client(**kwargs: object) -> httpx.AsyncClient:
        constructor_kwargs.update(kwargs)
        return owned

    monkeypatch.setattr("katilim_analiz.llm.client.httpx.AsyncClient", build_client)
    client = OllamaStructuredClient(
        base_url="http://ollama:11434",
        model="qwen3.5:4b",
        model_digest="2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
        timeout_seconds=120,
        max_context=4096,
        keep_alive="-1",
    )

    response = await client.extract(campaign_document, frozenset({ModelFactField.RATE}))

    assert response.document_id == campaign_document.id
    assert constructor_kwargs["follow_redirects"] is False
    assert constructor_kwargs["trust_env"] is False
    assert owned.is_closed


@pytest.mark.asyncio
async def test_unrequested_field_and_unsent_block_are_rejected(
    campaign_document: CleanDocument,
) -> None:
    quote = campaign_document.blocks[0].text
    facts = [
        {
            "field": "product_family",
            "quote": quote,
        }
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_envelope(_content(facts=facts)))
    )
    client, http_client = _client(transport)
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()
    assert error.value.code is ModelFailureCode.FIELD_NOT_REQUESTED


@pytest.mark.asyncio
async def test_length_stopped_output_is_reported_as_truncated_before_json_parse(
    campaign_document: CleanDocument,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_envelope('{"facts":', done_reason="length"),
        )
    )
    client, http_client = _client(transport)
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.OUTPUT_TRUNCATED


@pytest.mark.asyncio
async def test_client_makes_one_compact_request_for_highest_signal_family(
    campaign_document: CleanDocument,
) -> None:
    captured_requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(200, json=_envelope(_content()))

    client, http_client = _client(httpx.MockTransport(handler))
    try:
        await client.extract(campaign_document, frozenset(ModelFactField))
    finally:
        await http_client.aclose()

    assert len(captured_requests) == 1
    user_payload = json.loads(captured_requests[0]["messages"][1]["content"])
    selected = set(user_payload["requested_fields"])
    assert 0 < len(selected) <= 3
    assert any(
        selected <= family
        for family in (
            {"title", "summary", "product_family", "campaign_type"},
            {"rate", "financing_amount", "term", "fee", "reward"},
            {
                "validity",
                "customer_segment",
                "eligibility_condition",
                "sales_channel",
                "new_customer_only",
            },
        )
    )


@pytest.mark.asyncio
async def test_no_signal_field_is_typed_skip_without_calling_model() -> None:
    document = make_document(
        ("heading", "Eğitim Kampanyası"),
        ("paragraph", "Ödemelerde vade farksız 3 taksit fırsatı sunulur."),
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client, http_client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelInferenceSkipped) as skipped:
            await client.extract(document, frozenset({ModelFactField.TERM}))
    finally:
        await http_client.aclose()

    assert skipped.value.code == "no_relevant_source_signal"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forbidden_property", "forbidden_value"),
    [
        ("schema_version", "model-extraction/1.0"),
        ("document_id", "clean-test-document"),
        ("outcome", "not_stated"),
    ],
)
async def test_live_client_rejects_compatibility_and_identity_properties(
    campaign_document: CleanDocument,
    forbidden_property: str,
    forbidden_value: str,
) -> None:
    live_content: dict[str, object] = {"facts": []}
    live_content[forbidden_property] = forbidden_value
    client, http_client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_envelope(json.dumps(live_content)))
        )
    )
    try:
        with pytest.raises(ModelInferenceError) as error:
            await client.extract(campaign_document, frozenset({ModelFactField.RATE}))
    finally:
        await http_client.aclose()

    assert error.value.code is ModelFailureCode.SCHEMA_INVALID
