"""Production FastAPI composition root and lifecycle-owned resources."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException

from katilim_analiz.api.app import create_app
from katilim_analiz.application.container import ApplicationContainer
from katilim_analiz.application.health import ReadinessService
from katilim_analiz.application.preview import ExtractionPreviewService
from katilim_analiz.application.services import CampaignService, ChatService
from katilim_analiz.config import AppEnvironment, ModelProfile, Settings, get_settings
from katilim_analiz.extraction import pipeline_from_settings
from katilim_analiz.llm.composer import OllamaAnswerComposer
from katilim_analiz.logging import configure_logging
from katilim_analiz.runtime.adapters import OllamaModelHealth, PipelinePreviewAdapter
from katilim_analiz.runtime.paths import RuntimePathError, resolve_runtime_path
from katilim_analiz.runtime.registry import load_runtime_registry
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.health import PostgresDatabaseHealth
from katilim_analiz.storage.read_adapter import PostgresCampaignReadAdapter
from katilim_analiz.storage.write_adapter import PostgresCampaignWriteAdapter

_FRONTEND_PATH = Path("web/dist")
_ALEMBIC_PATH = Path("backend/alembic.ini")
_FRONTEND_REQUIRED = frozenset(
    {
        AppEnvironment.LOCAL_KUBERNETES,
        AppEnvironment.PRODUCTION,
        AppEnvironment.INSTITUTION,
    }
)


class RuntimeConfigurationError(RuntimeError):
    """Production resources cannot be composed from the provided configuration."""


@dataclass(slots=True)
class ApiRuntime:
    settings: Settings
    database: Database
    container: ApplicationContainer
    writes: PostgresCampaignWriteAdapter
    model_health: OllamaModelHealth | None
    model_http_client: httpx.AsyncClient | None

    async def aclose(self) -> None:
        try:
            if self.model_health is not None:
                await self.model_health.aclose()
        finally:
            try:
                if self.model_http_client is not None:
                    await self.model_http_client.aclose()
            finally:
                await self.database.dispose()


def build_api_runtime(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
) -> ApiRuntime:
    effective = settings or get_settings()
    try:
        alembic_config = resolve_runtime_path(_ALEMBIC_PATH)
    except RuntimePathError as exc:
        raise RuntimeConfigurationError(str(exc)) from exc
    configured_database = database or Database.from_settings(effective)
    reads = PostgresCampaignReadAdapter(configured_database.session_factory)
    writes = PostgresCampaignWriteAdapter(configured_database.session_factory)
    database_health = PostgresDatabaseHealth(
        configured_database.engine,
        alembic_config=alembic_config,
    )
    model_required = effective.model_profile is not ModelProfile.RULES_ONLY
    model_http_client = (
        httpx.AsyncClient(follow_redirects=False, trust_env=False) if model_required else None
    )
    model_health = (
        OllamaModelHealth(
            base_url=str(effective.ollama_base_url),
            model=effective.ollama_model,
            expected_digest=effective.ollama_model_digest,
            timeout_seconds=min(5.0, float(effective.model_timeout_seconds)),
            client=model_http_client,
        )
        if model_required
        else None
    )
    preview_pipeline = pipeline_from_settings(effective, http_client=model_http_client)
    # Issue #16: composition is optional by design — without a model the chat
    # serves the deterministic template built from the same retrieved facts.
    answer_composer = (
        OllamaAnswerComposer(
            base_url=str(effective.ollama_base_url),
            model=effective.ollama_model,
            timeout_seconds=float(effective.model_timeout_seconds),
            max_context=effective.model_max_context,
            keep_alive=effective.model_keep_alive,
            http_client=model_http_client,
        )
        if model_required
        else None
    )
    container = ApplicationContainer(
        campaigns=CampaignService(reads),
        chat=ChatService(reads, composer=answer_composer),
        readiness=ReadinessService(
            database=database_health,
            model=model_health,
            model_required=model_required,
        ),
        previews=ExtractionPreviewService(
            registry=load_runtime_registry(),
            extractor=PipelinePreviewAdapter(preview_pipeline),
        ),
    )
    return ApiRuntime(
        settings=effective,
        database=configured_database,
        container=container,
        writes=writes,
        model_health=model_health,
        model_http_client=model_http_client,
    )


def _install_reserved_route_guards(app: FastAPI) -> None:
    """Keep unknown API/health URLs out of the HTML navigation fallback."""

    @app.api_route(
        "/api",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unmatched_api_root() -> None:
        raise HTTPException(status_code=404, detail="API route not found")

    @app.api_route(
        "/api/{unmatched_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unmatched_api(unmatched_path: str) -> None:
        del unmatched_path
        raise HTTPException(status_code=404, detail="API route not found")

    @app.api_route(
        "/health",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unmatched_health_root() -> None:
        raise HTTPException(status_code=404, detail="Health route not found")

    @app.api_route(
        "/health/{unmatched_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unmatched_health(unmatched_path: str) -> None:
        del unmatched_path
        raise HTTPException(status_code=404, detail="Health route not found")


def _frontend_directory(
    settings: Settings,
    explicit: str | Path | None,
) -> Path | None:
    try:
        directory = resolve_runtime_path(explicit or _FRONTEND_PATH)
    except RuntimePathError as exc:
        if settings.app_env in _FRONTEND_REQUIRED:
            raise RuntimeConfigurationError(str(exc)) from exc
        return None
    if not directory.is_dir() or not (directory / "index.html").is_file():
        if settings.app_env in _FRONTEND_REQUIRED:
            raise RuntimeConfigurationError(f"frontend build has no index.html: {directory}")
        return None
    return directory


def create_production_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    frontend_dir: str | Path | None = None,
) -> FastAPI:
    effective = settings or get_settings()
    configure_logging(effective)
    frontend = _frontend_directory(effective, frontend_dir)
    runtime = build_api_runtime(effective, database=database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            await runtime.aclose()

    app = create_app(
        container=runtime.container,
        settings=effective,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    _install_reserved_route_guards(app)
    if frontend is not None:
        app.frontend("/", directory=frontend, fallback="index.html")
    return app
