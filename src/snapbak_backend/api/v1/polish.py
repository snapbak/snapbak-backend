"""POST /v1/polish behind the bearer token (docs/api.md)."""

import time

from fastapi import APIRouter, Depends, Request

from snapbak_backend.ai.chain import ProviderFailed, elapsed_ms, retries
from snapbak_backend.api import deps
from snapbak_backend.api.errors import Failure
from snapbak_backend.schemas.polish import PolishRequest, PolishResponse
from snapbak_backend.services.metrics import log, verdicts
from snapbak_backend.services.polish import polish

router = APIRouter()


@router.post("/v1/polish", dependencies=[Depends(deps.authorize)])
async def polish_route(body: PolishRequest, request: Request) -> PolishResponse:
    started = time.monotonic()
    try:
        response, calls = await polish(body, deps.tiers(request))
    except ProviderFailed as error:
        log("/v1/polish", error.engine, "-", elapsed_ms(started), 0, error.verdict, error.retries)
        raise Failure(502, "provider") from error
    log("/v1/polish", response.engine, response.tier, response.ms, response.tokens, verdicts(calls), retries(calls))
    return response
