from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
from asgi_lifespan import LifespanManager

from katilim_analiz.config import AppEnvironment, ModelProfile, Settings
from katilim_analiz.runtime import composition
from katilim_analiz.runtime.composition import build_api_runtime, create_production_app
from katilim_analiz.storage.database import Database


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        app_allowed_hosts=["testserver"],
        database_url="postgresql+asyncpg://test:test@127.0.0.1:1/test",
        model_profile=ModelProfile.RULES_ONLY,
        log_format="console",
    )


async def test_frontend_fallback_preserves_api_health_and_static_semantics(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "index.html").write_text("<html>spa-shell</html>", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('asset')", encoding="utf-8")
    database = Database.from_settings(_settings())
    original_dispose = database.dispose
    database.dispose = AsyncMock(wraps=original_dispose)  # type: ignore[method-assign]
    app = create_production_app(
        _settings(),
        database=database,
        frontend_dir=tmp_path,
    )

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            assert live.status_code == 200
            assert live.json() == {"status": "ok"}

            for path in ("/api", "/api/v1/not-a-route", "/health", "/health/not-a-route"):
                reserved = await client.get(path, headers={"Accept": "text/html"})
                assert reserved.status_code == 404
                assert "json" in reserved.headers["content-type"]
                assert "spa-shell" not in reserved.text

            navigation = await client.get(
                "/campaigns/example",
                headers={"Accept": "text/html"},
            )
            assert navigation.status_code == 200
            assert "spa-shell" in navigation.text

            non_html = await client.get(
                "/campaigns/example",
                headers={"Accept": "application/json"},
            )
            assert non_html.status_code == 404

            asset = await client.get("/assets/app.js")
            assert asset.status_code == 200
            assert "console.log" in asset.text
            missing_asset = await client.get("/assets/missing.js")
            assert missing_asset.status_code == 404

    database.dispose.assert_awaited_once()  # type: ignore[attr-defined]


async def test_model_health_and_preview_pipeline_share_one_lifespan_client(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings().model_copy(update={"model_profile": ModelProfile.LAPTOP})
    shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    )
    observed: dict[str, object] = {}
    real_pipeline_from_settings = composition.pipeline_from_settings

    def capture_pipeline(settings, *, http_client=None, clock=None):  # type: ignore[no-untyped-def]
        observed["pipeline_client"] = http_client
        return real_pipeline_from_settings(settings, http_client=http_client, clock=clock)

    def build_client(**kwargs):  # type: ignore[no-untyped-def]
        observed["constructor_kwargs"] = kwargs
        return shared_client

    monkeypatch.setattr(composition.httpx, "AsyncClient", build_client)
    monkeypatch.setattr(composition, "pipeline_from_settings", capture_pipeline)
    database = Database.from_settings(settings)
    runtime = build_api_runtime(settings, database=database)

    assert observed["pipeline_client"] is shared_client
    assert runtime.model_health is not None
    assert runtime.model_health._client is shared_client
    assert observed["constructor_kwargs"] == {
        "follow_redirects": False,
        "trust_env": False,
    }
    assert not shared_client.is_closed

    await runtime.aclose()

    assert shared_client.is_closed
