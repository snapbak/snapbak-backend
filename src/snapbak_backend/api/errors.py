"""Errors as the contract's JSON body, each logged (docs/api.md, docs/cloud.md). The dictation routes answer
`{"error": code}`; the cloud routes answer `{"error": {"code", "message"}}`, and the app shows its own copy per code."""

from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from snapbak_backend.config.settings import SHARES_PER_DEVICE
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.metrics import log

# The routes that keep the dictation contract's flat error body (HLD open question 4).
DICTATION = ("/v1/polish", "/v1/command")
MESSAGES = {
    "unauthorized": "Register this device again.",
    "not_found": "Nothing with this ID belongs to this device.",
    "job_exists": "This recording already has a transcript or one in progress.",
    "upload_state": "This upload was aborted; start a new one.",
    "too_long": "Recordings over 2 hours can't be transcribed.",
    "upload_too_large": "The file is larger than this kind of upload allows.",
    "unsupported_language": "This language isn't supported yet.",
    "invalid": "The request body is not valid.",
    "upload_size_mismatch": "The uploaded file's size differs from the size declared.",
    "daily_limit": "This device has used today's transcription minutes.",
    "rate_limited": "Too many devices registered from this address; try again in an hour.",
    "share_limit": f"This device has {SHARES_PER_DEVICE} share links; delete one to share again.",
    "provider": "The language model didn't answer in time.",
    "recording_not_found": "No recording with this ID belongs to this device.",
    "track_not_found": "No caption track with this ID belongs to this device.",
    "line_not_found": "This caption track has no line with this index.",
    "transcript_not_ready": "This recording's transcript isn't ready yet.",
    "too_many_lines": "Send at most 200 lines at a time.",
    "language_unsupported": "Captions can't be translated into this language.",
    "translation_limit": "This device has translated today's caption lines.",
    "internal": "Something went wrong on the server.",
}


class Failure(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def cloud_body(code: str, **extra: Any) -> dict[str, Any]:
    body = {"error": {"code": code, "message": MESSAGES[code]}, **extra}
    return body


async def failed(_: Request, error: Failure) -> JSONResponse:
    return JSONResponse({"error": error.code}, status_code=error.status)


def route_of(request: Request) -> str:
    """The matched route's template, such as `/v1/shares/{slug}`: a path holds IDs and slugs, which logs never do."""
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    return template


async def cloud_failed(request: Request, error: CloudFailure) -> JSONResponse:
    log(route_of(request), "-", "-", 0, 0, error.code)
    body = cloud_body(error.code, **error.extra)
    return JSONResponse(body, status_code=error.status)


def answer(request: Request, code: str, status: int) -> JSONResponse:
    log(route_of(request), "-", "-", 0, 0, code)
    body = {"error": code} if request.url.path in DICTATION else cloud_body(code)
    return JSONResponse(body, status_code=status)


async def invalid(request: Request, _: RequestValidationError) -> JSONResponse:
    return answer(request, "invalid", 422)


async def internal(request: Request, _: Exception) -> JSONResponse:
    return answer(request, "internal", 500)


async def share_host_or_json(request: Request, error: HTTPException) -> Response:
    """An unknown path or a wrong method on the share host is the share page's own "isn't available", with the
    status; on any other host, FastAPI's JSON."""
    cloud = request.app.state.cloud
    if cloud is not None and request.url.netloc == urlsplit(cloud.config.share_url).netloc:
        # Imported here: the page loads SQLAlchemy, which the dictation-only server never does.
        from snapbak_backend.api.share_page import unavailable

        page = unavailable(request, "gone", error.status_code)
        # A 405 names the methods that work (RFC 9110).
        page.headers.update(error.headers or {})
        return page
    answer = await http_exception_handler(request, error)
    return answer
