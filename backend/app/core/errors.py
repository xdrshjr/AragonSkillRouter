"""Uniform error envelope.

Every failure the browser or an external agent sees has the same shape:

    {"error": {"code", "message", "details", "request_id", "actions": [...]}}

`request_id` is the same value logged server-side and stored on invocations, so
a user-reported number leads straight to the trace. `actions` is the actionable
next step list rendered by the UI — an error message alone is not an answer.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import get_request_id


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Any = None,
        actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.actions = actions or []


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    actions: list[str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": get_request_id(),
                "actions": actions or [],
            }
        },
    )


def not_found(what: str) -> ApiError:
    return ApiError("not_found", f"{what} not found.", status_code=status.HTTP_404_NOT_FOUND)


def conflict(code: str, message: str, *, details: Any = None, actions: list[str] | None = None) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_409_CONFLICT, details=details, actions=actions)


def unauthorized(message: str = "Sign in to continue.") -> ApiError:
    return ApiError(
        "unauthenticated",
        message,
        status_code=status.HTTP_401_UNAUTHORIZED,
        actions=["Sign in again."],
    )


def forbidden(code: str, message: str, *, actions: list[str] | None = None) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_403_FORBIDDEN, actions=actions)


def unprocessable(code: str, message: str, *, details: Any = None, actions: list[str] | None = None) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, details=details, actions=actions)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            actions=exc.actions,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for err in exc.errors():
            location = ".".join(str(part) for part in err.get("loc", ()) if part not in ("body", "query"))
            details.append({"field": location or "request", "message": err.get("msg", "invalid value")})
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_failed",
            message="The request has invalid or missing fields.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
            429: "rate_limited",
        }
        return error_response(
            status_code=exc.status_code,
            code=codes.get(exc.status_code, "http_error"),
            message=str(exc.detail) or "Request failed.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - safety net
        import logging

        logging.getLogger("skillrouter").exception("unhandled error", exc_info=exc)
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="The server hit an unexpected error. Use the request id to find it in the logs.",
            actions=["Retry the action.", "If it repeats, check server logs for this request id."],
        )
