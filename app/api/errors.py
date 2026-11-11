"""Structured API errors. Responses never include stack traces or internal paths."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.api")

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    503: "unavailable",
}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_payload(code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_payload(code, message, request_id), status_code=status_code, headers=headers
    )


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            request_id=_request_id(request),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; submitted values are not echoed back.
        problems = [
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.errors()
        ]
        return error_response(
            422,
            "validation_error",
            "Invalid request. " + "; ".join(problems[:10]),
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
        return error_response(
            exc.status_code,
            code,
            message,
            request_id=_request_id(request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error request_id=%s path=%s", _request_id(request), request.url.path
        )
        return error_response(
            500,
            "internal_error",
            "Unexpected server error. Nothing further was executed by this request.",
            request_id=_request_id(request),
        )
