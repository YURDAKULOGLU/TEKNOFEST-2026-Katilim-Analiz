"""RFC 9457 error rendering without request-body or exception leakage."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from katilim_analiz.application.cursor import InvalidCursorError
from katilim_analiz.application.preview import PreviewBankNotAllowedError
from katilim_analiz.application.services import CampaignNotFoundError
from katilim_analiz.contracts import ProblemDetail
from katilim_analiz.logging import current_correlation_id

_LOGGER = logging.getLogger(__name__)


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    code: str,
    detail: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    problem = ProblemDetail(
        type=f"urn:katilim-analiz:problem:{code}",
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        correlation_id=current_correlation_id(),
        errors=[] if errors is None else errors,
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
    )


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    return [
        {
            "location": [str(part) for part in error.get("loc", ())],
            "message": str(error.get("msg", "Geçersiz değer."))[:300],
            "type": str(error.get("type", "validation_error"))[:100],
        }
        for error in exc.errors()[:50]
    ]


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(InvalidCursorError)
    async def invalid_cursor(request: Request, exc: InvalidCursorError) -> JSONResponse:
        return problem_response(
            request,
            status=400,
            title="Geçersiz sayfalama imleci",
            code="invalid_cursor",
            detail="Sayfalama imleci geçerli veya güncel değil.",
        )

    @app.exception_handler(CampaignNotFoundError)
    async def campaign_not_found(request: Request, exc: CampaignNotFoundError) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            title="Kampanya bulunamadı",
            code=exc.code,
            detail="İstenen kampanya seçilen tarih için bulunamadı.",
        )

    @app.exception_handler(PreviewBankNotAllowedError)
    async def invalid_preview_bank(
        request: Request,
        exc: PreviewBankNotAllowedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=422,
            title="Banka seçimi doğrulanamadı",
            code=exc.code,
            detail="Banka kimliği aktif BDDK katılım bankası kaydında bulunmuyor.",
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            status=422,
            title="İstek doğrulanamadı",
            code="request_validation_failed",
            detail="Bir veya daha fazla alan API sözleşmesine uymuyor.",
            errors=_safe_validation_errors(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 400 and exc.detail == "There was an error parsing the body":
            # FastAPI raises this bare 400 when the request body cannot be
            # decoded (for example a cp1254/Windows-1254 encoded payload).
            # Name the actual requirement instead of an empty detail.
            return problem_response(
                request,
                status=400,
                title="İstek gövdesi çözümlenemedi",
                code="invalid_request_body",
                detail="İstek gövdesi okunamadı; JSON gövde UTF-8 kodlanmış olmalıdır.",
            )
        code = "route_not_found" if exc.status_code == 404 else "http_error"
        title = "Yol bulunamadı" if exc.status_code == 404 else "HTTP isteği tamamlanamadı"
        return problem_response(
            request,
            status=exc.status_code,
            title=title,
            code=code,
            detail="İstenen API kaynağı bulunamadı." if exc.status_code == 404 else None,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        _LOGGER.exception("unhandled API error", extra={"event": "api.unhandled_error"})
        return problem_response(
            request,
            status=500,
            title="Beklenmeyen sunucu hatası",
            code="internal_error",
            detail="İstek güvenli biçimde tamamlanamadı.",
        )
