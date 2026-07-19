"""Small request middleware for correlation and bounded request metadata."""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from katilim_analiz.contracts import ProblemDetail
from katilim_analiz.logging import (
    bind_correlation_id,
    current_correlation_id,
    reset_correlation_id,
)

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RequestBodyLimitMiddleware:
    """Buffer only a small bounded body before schema parsing, including chunked input."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = 65_536) -> None:
        if max_bytes <= 0:
            raise ValueError("request body limit must be positive")
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send, code="invalid_content_length", status=400)
                return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        code: str = "request_body_too_large",
        status: int = 413,
    ) -> None:
        detail = (
            "İstek gövdesi izin verilen sınırı aşıyor."
            if status == 413
            else "Content-Length başlığı geçerli değil."
        )
        problem = ProblemDetail(
            type=f"urn:katilim-analiz:problem:{code}",
            title="İstek gövdesi reddedildi",
            status=status,
            detail=detail,
            instance=str(scope.get("path", "")),
            code=code,
            correlation_id=current_correlation_id(),
        )
        response = JSONResponse(
            status_code=status,
            content=problem.model_dump(mode="json"),
            media_type="application/problem+json",
        )
        await response(scope, receive, send)


class CorrelationIdMiddleware:
    """Bind one request ID without BaseHTTPMiddleware's ContextVar boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-correlation-id", "")
        correlation_id = supplied if _CORRELATION_ID.fullmatch(supplied) else uuid4().hex
        token = bind_correlation_id(correlation_id)

        async def send_with_correlation_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-correlation-id"] = correlation_id
            await send(message)

        try:
            await self._app(scope, receive, send_with_correlation_id)
        finally:
            reset_correlation_id(token)
