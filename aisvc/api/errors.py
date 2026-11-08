"""RFC 9457 problem JSON (SPEC.md section 10).

"Errors use RFC 9457 problem JSON; breaking changes require a new
version prefix." Every error the API returns goes through here, so the
plugin and the CLI get one shape to parse.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aisvc.hf import HfError
from aisvc.profiles import ParameterRejected
from aisvc.registry import ManifestNotFound, RegistryError
from aisvc.spec import SpecError

__all__ = ["ApiError", "problem", "register_error_handlers"]

PROBLEM_MEDIA_TYPE = "application/problem+json"

#: Spelled out rather than taken from `status`, whose name for it has
#: changed across Starlette versions.
HTTP_422 = 422
PROBLEM_BASE = "https://cloudstack.apache.org/aisvc/errors"


class ApiError(Exception):
    """An error with a status code and a stable type slug."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.extra = extra or {}


def problem(status_code: int, detail: str, *, code: str, **extra: Any) -> JSONResponse:
    """One RFC 9457 problem document."""
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}/{code}",
        "title": code.replace("_", " "),
        "status": status_code,
        "detail": detail,
    }
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_MEDIA_TYPE)


def register_error_handlers(app: FastAPI) -> None:
    """Map every failure this engine can raise onto problem JSON."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return problem(exc.status_code, exc.detail, code=exc.code, **exc.extra)

    @app.exception_handler(SpecError)
    async def _spec_error(_: Request, exc: SpecError) -> JSONResponse:
        return problem(
            HTTP_422,
            "the platform spec is not valid",
            code="invalid_spec",
            problems=[str(p) for p in exc.problems],
        )

    @app.exception_handler(ParameterRejected)
    async def _parameter_rejected(_: Request, exc: ParameterRejected) -> JSONResponse:
        return problem(
            status.HTTP_400_BAD_REQUEST,
            str(exc),
            code="parameter_rejected",
            parameter=exc.parameter,
        )

    @app.exception_handler(ManifestNotFound)
    async def _not_mirrored(_: Request, exc: ManifestNotFound) -> JSONResponse:
        return problem(status.HTTP_404_NOT_FOUND, str(exc), code="not_mirrored")

    @app.exception_handler(RegistryError)
    async def _registry_error(_: Request, exc: RegistryError) -> JSONResponse:
        # Not a 404: the registry being unreachable is our problem, and
        # must not read to the caller as "the model does not exist".
        return problem(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), code="registry_unavailable")

    @app.exception_handler(HfError)
    async def _hf_error(_: Request, exc: HfError) -> JSONResponse:
        return problem(status.HTTP_400_BAD_REQUEST, str(exc), code="hub_error")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            exc.status_code,
            str(exc.detail),
            code=_slug_for(exc.status_code),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(
            HTTP_422,
            "the request body or query is not valid",
            code="invalid_request",
            errors=[
                {"loc": [str(part) for part in err["loc"]], "msg": err["msg"]}
                for err in exc.errors()
            ],
        )


def _slug_for(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        503: "unavailable",
    }.get(status_code, "error")
