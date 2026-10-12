"""POST /v1/command behind the bearer token (docs/api.md)."""

import time

from fastapi import APIRouter, Depends, Request

from snapbak_backend.ai.chain import ProviderFailed, elapsed_ms, retries
from snapbak_backend.api import deps
from snapbak_backend.api.errors import Failure
from snapbak_backend.schemas.command import CommandRequest, CommandResponse
from snapbak_backend.services.command import command
from snapbak_backend.services.metrics import log, verdicts

router = APIRouter()


@router.post("/v1/command", dependencies=[Depends(deps.authorize)])
async def command_route(body: CommandRequest, request: Request) -> CommandResponse:
    started = time.monotonic()
    try:
        response, calls = await command(body, deps.tiers(request))
    except ProviderFailed as error:
        log("/v1/command", error.engine, "big", elapsed_ms(started), 0, error.verdict, error.retries)
        raise Failure(502, "provider") from error
    log("/v1/command", response.engine, "big", response.ms, response.tokens, verdicts(calls), retries(calls))
    return response
