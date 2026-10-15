"""A provider that answers without a model, for tests (docs/providers.md)."""

import asyncio
import json
import re
from typing import Any

from snapbak_backend.ai import routing
from snapbak_backend.ai.providers.base import ProviderError, Turn, Usage

FAKE_MODES = {"echo", "edit", "fail", "slow", "reply"}


class Fake:
    """Answers without a model, for the app's E2E tests: echo, edit (a final "!", or for "<sentence> <cue> <word>",
    such as "no wait" or "scratch that", the form the server sends a correction in, the sentence with its last word
    swapped), fail, slow (5 s, then echo) or reply ("Sure, " first, which the guardrail rejects)."""

    def __init__(self, mode: str) -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"Unknown fake mode '{mode}'; use one of {sorted(FAKE_MODES)}.")
        self.mode = mode
        self.engine = f"fake:{mode}"

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        if self.mode == "fail":
            raise ProviderError("fake:fail")
        if self.mode == "slow":
            await asyncio.sleep(5)
        # The request is the last JSON turn; a correction follows it with the refused answer and the reasons.
        request = json.loads(
            next(content for role, content in reversed(turns) if role == "user" and content[:1] == "{")
        )
        edit = self.mode == "edit"
        if "selection" in request:
            selection = request["selection"]
            answer: dict[str, Any] = {"text": selection.upper() if edit else selection}
        elif edit:
            answer = revise(request["text"])
        else:
            text = request["text"]
            answer = {"revision": None, "text": f"Sure, {text}" if self.mode == "reply" else text}
        return json.dumps(answer, ensure_ascii=False), Usage(0, 0)

    async def aclose(self) -> None:
        pass


# Any correction cue, a comma, full stop or "!" allowed after each of its words, with a "no" before or after it ("No,
# don't do this", "Wait, no").
SAID_CUE = "|".join(re.escape(cue.rstrip(",")).replace(r"\ ", r"[,.!]?\s+") for cue in routing.CUES)
CORRECTION = re.compile(
    rf"(?P<head>.*?[^\s,]),?\s+(?:no[,.!]?\s+)?(?:{SAID_CUE})[,.!]?\s+(?:no[,.!]?\s+)?(?P<word>[^\s.?!,]+)(?P<mark>[.?!]?)",
    re.IGNORECASE,
)


def revise(text: str) -> dict[str, Any]:
    """For "<sentence> <cue> <word>", the sentence with its last word swapped for that word; otherwise the text with
    a bang. Words that end with a cue or a "no" are no sentence to put right ("no wait make it friday")."""
    found = CORRECTION.fullmatch(text)
    head = found["head"].lower() if found else ""
    ends_with_cue = head.split()[-1:] == ["no"] or any(cue.end() == len(head) for cue in routing.CUE.finditer(head))
    if found is None or ends_with_cue:
        return {"revision": None, "text": bang(text)}
    last = found["head"].split()[-1]
    stem = last.rstrip(".?!")
    mark = last.removeprefix(stem) or found["mark"]
    swapped = found["head"].removesuffix(last) + found["word"] + mark
    return {"revision": None, "text": swapped}


def bang(text: str) -> str:
    stem = text.removesuffix(text[-1:]) if text.endswith((".", "?", "!")) else text
    return stem + "!" if text else text
