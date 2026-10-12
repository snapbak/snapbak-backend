"""A provider that answers without a model, for tests (docs/providers.md)."""

import asyncio
import json
import re
from typing import Any

from snapbak_backend.ai.providers.base import ProviderError, Turn, Usage

FAKE_MODES = {"echo", "edit", "fail", "slow", "reply"}


class Fake:
    """Answers without a model, for the app's E2E tests: echo, edit (a final "!", or for "<sentence> no wait <word>",
    the form the server sends a correction in, the sentence with its last word swapped, and for "<sentence> nahi nahi
    <word>" or "नहीं नहीं", its first day word), fail, slow (5 s, then echo) or reply ("Sure, " first, which the
    guardrail rejects)."""

    def __init__(self, mode: str) -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"Unknown fake mode '{mode}'; use one of {sorted(FAKE_MODES)}.")
        self.mode = mode
        self.engine = f"fake:{mode}"

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
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


CORRECTION = re.compile(r"(?P<head>.*\S)\s+no,?\s+wait,?\s+(?P<word>[^\s.?!।]+)[.?!।]?", re.IGNORECASE)
DOUBLED_NAHI = re.compile(
    r"(?P<head>.*\S)\s+(?:nahi nahi|nahin nahin|नहीं नहीं),?\s+(?P<word>[^\s.?!।]+)[.?!।]?", re.IGNORECASE
)
DAY_WORD = re.compile(r"(?<!\S)(?:kal|aaj|कल|आज)(?!\S)", re.IGNORECASE)


def revise(text: str) -> dict[str, Any]:
    """For "<sentence> no wait <word>", the sentence with its last word swapped for that word; for "<sentence> nahi
    nahi <word>", with its first day word swapped; otherwise the text with a bang."""
    doubled = DOUBLED_NAHI.fullmatch(text)
    day = doubled and DAY_WORD.search(doubled["head"])
    if doubled and day:
        word = doubled["word"]
        said = word[0].upper() + word[1:] if day.group()[0].isupper() else word
        moved = doubled["head"][: day.start()] + said + doubled["head"][day.end() :]
        return {"revision": None, "text": moved}
    found = CORRECTION.fullmatch(text)
    if found is None:
        return {"revision": None, "text": bang(text)}
    head = found["head"]
    last = head.split()[-1]
    stem = last.rstrip(".?!।")
    swapped = head.removesuffix(last) + found["word"] + last.removeprefix(stem)
    return {"revision": None, "text": swapped}


def bang(text: str) -> str:
    stem = text.removesuffix(text[-1:]) if text.endswith((".", "?", "!")) else text
    return stem + "!" if text else text
