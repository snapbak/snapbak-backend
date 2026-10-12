"""Errors as the contract's JSON body, each logged (docs/api.md)."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from snapbak_backend.services.metrics import log


class Failure(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


async def failed(_: Request, error: Failure) -> JSONResponse:
    return JSONResponse({"error": error.code}, status_code=error.status)


async def invalid(request: Request, _: RequestValidationError) -> JSONResponse:
    log(request.url.path, "-", "-", 0, 0, "invalid")
    return JSONResponse({"error": "invalid"}, status_code=422)


async def internal(request: Request, _: Exception) -> JSONResponse:
    log(request.url.path, "-", "-", 0, 0, "internal")
    return JSONResponse({"error": "internal"}, status_code=500)
