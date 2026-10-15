"""Command: the selection rewritten by the big model (docs/api.md)."""

import json
import time

from snapbak_backend.ai.chain import Call, ProviderFailed, Tiers, ask, deadline_of, elapsed_ms, total
from snapbak_backend.schemas.command import CommandRequest, CommandResponse
from snapbak_backend.transformers.command import command_turns


async def command(request: CommandRequest, tiers: Tiers) -> tuple[CommandResponse, list[Call]]:
    started = time.monotonic()
    deadline = deadline_of(started, request.deadline_ms)
    max_tokens = 2 * len(request.selection) + 200
    calls: list[Call] = []
    output = await ask(tiers.chain(tiers.command), command_turns(request), max_tokens, deadline, calls)
    try:
        text = json.loads(output)["text"]
    except (ValueError, KeyError, TypeError) as error:
        raise ProviderFailed(calls[-1].engine, "malformed") from error
    if not isinstance(text, str):
        raise ProviderFailed(calls[-1].engine, "malformed")
    response = CommandResponse(text=text.strip(), engine=calls[-1].engine, ms=elapsed_ms(started), tokens=total(calls))
    return response, calls
