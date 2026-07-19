"""FastAPI application factory with explicit, testable composition."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, Path, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Lifespan

from katilim_analiz.api.errors import install_exception_handlers
from katilim_analiz.api.middleware import CorrelationIdMiddleware, RequestBodyLimitMiddleware
from katilim_analiz.api.presentation import localize_campaign_list
from katilim_analiz.application.container import ApplicationContainer
from katilim_analiz.application.models import (
    CampaignDetail,
    CampaignListFilters,
    CampaignListResponse,
    HealthResponse,
    NotificationListResponse,
    ValidityFilter,
)
from katilim_analiz.config import Settings, get_settings
from katilim_analiz.contracts import (
    ChatAnswer,
    ChatRequest,
    ComparisonRequest,
    ComparisonResponse,
    CoverageEntry,
    ExtractionPreviewRequest,
    ExtractionPreviewResponse,
    ProblemDetail,
    ProductFamily,
    SalesChannel,
)

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ProblemDetail, "description": "Geçersiz istek"},
    404: {"model": ProblemDetail, "description": "Kaynak bulunamadı"},
    422: {"model": ProblemDetail, "description": "Sözleşme doğrulama hatası"},
    500: {"model": ProblemDetail, "description": "Sunucu hatası"},
}


class LiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ok"] = "ok"


def _container(request: Request) -> ApplicationContainer:
    value: ApplicationContainer = request.app.state.container
    return value


def create_app(
    *,
    container: ApplicationContainer,
    settings: Settings | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    effective_settings = settings or get_settings()
    app = FastAPI(
        title=effective_settings.app_name,
        version="0.1.0",
        description="Kanıt temelli katılım bankacılığı kampanya analiz API'si.",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    install_exception_handlers(app)

    if effective_settings.app_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=effective_settings.app_cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Accept", "Content-Type", "X-Correlation-ID"],
            expose_headers=["X-Correlation-ID"],
            max_age=600,
        )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=65_536)
    app.add_middleware(CorrelationIdMiddleware)
    # Added last so Host validation remains outside CORS, including preflight requests.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=effective_settings.app_allowed_hosts,
    )

    @app.get(
        "/health/live",
        response_model=LiveResponse,
        tags=["health"],
        summary="İşlem liveness kontrolü",
    )
    async def live() -> LiveResponse:
        return LiveResponse()

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse, "description": "Servis hazır değil"}},
        tags=["health"],
        summary="Bağımlılık readiness kontrolü",
    )
    async def ready(request: Request) -> HealthResponse | JSONResponse:
        response = await _container(request).readiness.check()
        if response.status == "degraded":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=response.model_dump()
            )
        return response

    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/campaigns",
        response_model=CampaignListResponse,
        responses=_ERROR_RESPONSES,
        tags=["campaigns"],
    )
    async def list_campaigns(
        request: Request,
        bank_id: Annotated[
            str | None,
            Query(max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
        ] = None,
        product_family: Annotated[ProductFamily | None, Query()] = None,
        currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
        customer_segment: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        sales_channel: Annotated[SalesChannel | None, Query()] = None,
        validity: Annotated[ValidityFilter | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        as_of: Annotated[AwareDatetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> CampaignListResponse:
        filters = CampaignListFilters(
            bank_id=bank_id,
            product_family=product_family,
            currency=currency,
            customer_segment=customer_segment,
            sales_channel=sales_channel,
            validity=validity,
        )
        return localize_campaign_list(
            await _container(request).campaigns.list_campaigns(
                filters,
                cursor=cursor,
                limit=limit,
                as_of=as_of,
            )
        )

    @router.get(
        "/campaigns/{campaign_id}",
        response_model=CampaignDetail,
        responses=_ERROR_RESPONSES,
        tags=["campaigns"],
    )
    async def get_campaign(
        request: Request,
        campaign_id: Annotated[
            str,
            Path(min_length=1, max_length=191, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,190}$"),
        ],
        as_of: Annotated[AwareDatetime | None, Query()] = None,
    ) -> CampaignDetail:
        return await _container(request).campaigns.get_campaign(campaign_id, as_of=as_of)

    @router.get(
        "/notifications",
        response_model=NotificationListResponse,
        responses=_ERROR_RESPONSES,
        tags=["notifications"],
        summary="Kampanya değişiklik bildirimlerini salt okunur listele",
    )
    async def list_notifications(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> NotificationListResponse:
        return await _container(request).campaigns.list_notifications(
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/coverage",
        response_model=list[CoverageEntry],
        responses=_ERROR_RESPONSES,
        tags=["coverage"],
    )
    async def coverage(
        request: Request,
        as_of: Annotated[AwareDatetime | None, Query()] = None,
    ) -> list[CoverageEntry]:
        return await _container(request).campaigns.coverage(as_of=as_of)

    @router.post(
        "/comparisons",
        response_model=ComparisonResponse,
        responses=_ERROR_RESPONSES,
        tags=["comparisons"],
    )
    async def comparisons(request: Request, body: ComparisonRequest) -> ComparisonResponse:
        return await _container(request).campaigns.compare(body)

    @router.post(
        "/chat",
        response_model=ChatAnswer,
        responses=_ERROR_RESPONSES,
        tags=["chat"],
    )
    async def chat(request: Request, body: ChatRequest) -> ChatAnswer:
        return await _container(request).chat.answer(body)

    @router.post(
        "/previews/extractions",
        response_model=ExtractionPreviewResponse,
        responses=_ERROR_RESPONSES,
        tags=["previews"],
        summary="Yapıştırılan metinden doğrulanmamış çıkarım önizlemesi üret",
    )
    async def preview_extraction(
        request: Request,
        body: ExtractionPreviewRequest,
    ) -> ExtractionPreviewResponse:
        return await _container(request).previews.preview(body)

    app.include_router(router)
    return app
