from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from katilim_analiz.application.processing import SourceRequest
from katilim_analiz.contracts import FetchStatus
from katilim_analiz.extraction import ExtractionPipeline
from katilim_analiz.ingestion import clean_html
from katilim_analiz.ingestion.artifacts import create_fetch_artifact
from katilim_analiz.runtime import adapters
from katilim_analiz.runtime.adapters import (
    OllamaModelHealth,
    PipelineExtractionAdapter,
    PipelinePreviewAdapter,
    RecordIdentity,
)

NOW = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)
MODEL_DIGEST = "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
RAW = """
<html lang="tr"><head><title>Konut Finansmanı Kampanyası</title></head>
<body><main><h1>Konut Finansmanı Kampanyası</h1>
<p>Aylık kâr payı oranı %1,99 ve finansman tutarı 100.000 TL.</p></main></body></html>
""".encode()


class FixedVersions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, campaign_key: str, record_sha256: str) -> RecordIdentity:
        self.calls.append((campaign_key, record_sha256))
        return RecordIdentity(id=f"record:{record_sha256}", version=1)


def _document():  # type: ignore[no-untyped-def]
    raw_sha = hashlib.sha256(RAW).hexdigest()
    artifact = create_fetch_artifact(
        bank_id="bank-a",
        requested_url="https://bank.example/kampanya",
        final_url="https://bank.example/kampanya",
        status=FetchStatus.SUCCESS,
        http_status=200,
        fetched_at=NOW,
        robots_allowed=True,
        content_type="text/html",
        raw_sha256=raw_sha,
        raw_size_bytes=len(RAW),
        private_raw_path=f"sha256/{raw_sha}.html",
    )
    return clean_html(artifact, RAW, cleaned_at=NOW)


@pytest.mark.asyncio
async def test_extraction_adapter_is_retry_deterministic_and_builds_record() -> None:
    versions = FixedVersions()
    adapter = PipelineExtractionAdapter(ExtractionPipeline(model_enabled=False), versions)
    source = SourceRequest(
        bank_id="bank-a",
        bank_name="Banka A",
        source_url="https://bank.example/kampanya",
        campaign_key="bank-a:campaign",
    )
    document = _document()

    first = await adapter.extract_and_validate(source, document)
    second = await adapter.extract_and_validate(source, document)

    assert first.candidate is not None
    assert first.record is not None
    assert first.candidate.metadata.started_at == NOW
    assert first.candidate.metadata.completed_at == NOW
    assert second.candidate == first.candidate
    assert second.record == first.record
    assert versions.calls[0] == versions.calls[1]


@pytest.mark.asyncio
async def test_preview_adapter_returns_candidate_without_record_or_versioning() -> None:
    adapter = PipelinePreviewAdapter(ExtractionPipeline(model_enabled=False, clock=lambda: NOW))

    result = await adapter.extract(_document())

    assert result.candidate is not None
    assert result.candidate.data.title == "Konut Finansmanı Kampanyası"
    assert result.model_attempted is False
    assert result.accepted_model_facts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "models": [
                    {
                        "name": "qwen3.5:4b",
                        "model": "qwen3.5:4b",
                        "digest": MODEL_DIGEST,
                    }
                ]
            },
            True,
        ),
        (
            {
                "models": [
                    {
                        "name": "other:latest",
                        "model": "other:latest",
                        "digest": MODEL_DIGEST,
                    }
                ]
            },
            False,
        ),
        (
            {
                "models": [
                    {
                        "name": "qwen3.5:4b",
                        "model": "qwen3.5:4b",
                        "digest": "0" * 64,
                    }
                ]
            },
            False,
        ),
        ({"unexpected": []}, False),
    ],
)
async def test_model_health_requires_the_configured_local_model(
    payload: dict[str, object], expected: bool
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = OllamaModelHealth(
            base_url="http://ollama:11434",
            model="qwen3.5:4b",
            expected_digest=MODEL_DIGEST,
            client=client,
        )
        assert await health.ping() is expected


@pytest.mark.asyncio
async def test_model_health_fails_closed_on_transport_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = OllamaModelHealth(
            base_url="http://ollama:11434",
            model="qwen3.5:4b",
            expected_digest=MODEL_DIGEST,
            client=client,
        )
        assert not await health.ping()


@pytest.mark.asyncio
async def test_owned_model_health_client_ignores_environment_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = httpx.AsyncClient()
    constructor_kwargs: dict[str, object] = {}

    def build_client(**kwargs: object) -> httpx.AsyncClient:
        constructor_kwargs.update(kwargs)
        return owned

    monkeypatch.setattr(adapters.httpx, "AsyncClient", build_client)
    health = OllamaModelHealth(
        base_url="http://ollama:11434",
        model="qwen3.5:4b",
        expected_digest=MODEL_DIGEST,
    )

    await health.aclose()

    assert constructor_kwargs == {"follow_redirects": False, "trust_env": False}
    assert owned.is_closed
