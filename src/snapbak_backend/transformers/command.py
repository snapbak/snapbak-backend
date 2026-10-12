"""A command request as model turns."""

from snapbak_backend.ai.prompts.command import COMMAND_SYSTEM
from snapbak_backend.ai.providers.base import Turn
from snapbak_backend.schemas.command import CommandRequest
from snapbak_backend.transformers.polish import last_turn


def command_turns(request: CommandRequest) -> list[Turn]:
    return [("system", COMMAND_SYSTEM), last_turn(request)]
