"""A provider that answers without a model, for tests (docs/providers.md)."""

import asyncio
import json
import re
from typing import Any

from snapbak_backend.ai import routing
from snapbak_backend.ai.providers.base import EFFORTS, ProviderError, Turn, Usage

# The most completion tokens OpenAI's models take in one answer.
MOST_TOKENS = 128_000
FAKE_MODES = {"echo", "edit", "fail", "slow", "reply", "translate-drop", "meetings", "junk"}


class Fake:
    """Answers without a model, for the app's E2E tests: echo, edit (a final "!", or for "<sentence> <cue> <word>",
    such as "no wait" or "scratch that", the form the server sends a correction in, the sentence with its last word
    swapped), fail, slow (5 s, then echo) or reply ("Sure, " first, which the guardrail rejects). A transliteration
    comes back with each word marked "r:", and a caption translation with each line as "[<target>] <text>"; in
    translate-drop mode (otherwise echo) the last line is left out, for the retry path. The meetings mode answers
    the minutes, re-rank, ask and correction shapes with a body built from the request, valid by construction (a
    fabrication probe: the minutes answer quotes the first line with one word swapped, which the validator must
    drop and the correction turn must fix). Junk mode answers prose, never JSON."""

    def __init__(self, mode: str) -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"Unknown fake mode '{mode}'; use one of {sorted(FAKE_MODES)}.")
        self.mode = mode
        self.engine = f"fake:{mode}"

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        # What OpenAI refuses with a 400, refused here too, so a test catches a call the real model never answers:
        # `reason` is sent as the reasoning effort; JSON mode (every call) needs "json" in a message; and the
        # completion budget has its range.
        if reason and reason not in EFFORTS:
            raise ValueError(f"'{reason}' is not a reasoning effort")
        if not any("json" in content.lower() for _, content in turns):
            raise ValueError("JSON mode needs the word json in a message")
        if not 0 < max_tokens <= MOST_TOKENS:
            raise ValueError(f"max_completion_tokens {max_tokens} is out of range")
        if self.mode == "fail":
            raise ProviderError("fake:fail")
        if self.mode == "junk":
            # An answer that is no JSON at all, every time: the minutes' parse and repair must end it, not wedge.
            return "Sure! Here are the minutes you asked for.", Usage(0, 0)
        if self.mode == "slow":
            await asyncio.sleep(5)
        # The request is the last JSON turn; a correction follows it with the refused answer and the reasons.
        request = json.loads(
            next(content for role, content in reversed(turns) if role == "user" and content[:1] == "{")
        )
        edit = self.mode == "edit"
        if self.mode == "meetings":
            made = answer_meetings(request, corrected=len(turns) >= 3)
            return json.dumps(made, ensure_ascii=False), Usage(0, 0)
        if "translate" in request:
            # A caption translation (16-caption-translation LLD): each line marked with its target.
            mark = f"[{request['target']}]"
            lines = [{"idx": line["idx"], "text": f"{mark} {line['text']}"} for line in request["translate"]]
            kept = lines[:-1] if self.mode == "translate-drop" else lines
            return json.dumps({"lines": kept}, ensure_ascii=False), Usage(0, 0)
        if "lines" in request:
            # A transliteration (docs/cloud.md): each word marked, so a test sees the model's answer was used.
            marked = [{"i": line["i"], "words": [f"r:{word}" for word in line["words"]]} for line in request["lines"]]
            return json.dumps({"lines": marked}, ensure_ascii=False), Usage(0, 0)
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


def answer_meetings(request: dict[str, Any], corrected: bool) -> dict[str, Any]:
    """A minutes, re-rank or ask answer built from the request. The first minutes call fabricates one derivation (a
    word nothing said); the correction call answers clean, so the validator's drop-and-retry is exercised."""
    if "kept" in request or "candidates" in request:
        candidates = request["candidates"]
        kept = [item["n"] for item in candidates]
        return {"kept": kept[: request["keep"]]}
    if "question" in request and "lines" in request:
        first = request["lines"][0]
        return {
            "answer": f"[{first['sname']}] {first['text']}",
            "citations": [{"i": first["i"], "quote": first["text"]}],
        }
    if request.get("section") == "details":
        # The detail guard's call: every missing detail worked into the minutes, as said.
        said = " ".join(item["text"] for item in request["missing"] if item["kind"] != "commitment")
        by_index = {line["i"]: line for line in request["lines"]}
        promised = [by_index[item["i"]] for item in request["missing"] if item["kind"] == "commitment"]
        actions = [
            {
                "id": f"act_d{line['i']}",
                "text": "Do what was promised",
                "owner": line["sname"],
                "due": None,
                "derivedFrom": [{"i": line["i"], "speaker": line["sname"], "atMs": line["s"], "quote": line["text"]}],
            }
            for line in promised
        ]
        return {"summary": request["summary"], "mom": f"{request['mom']}\n\n{said}", "actionItems": actions}
    return answer_section(request, corrected)


def answer_section(request: dict[str, Any], corrected: bool) -> dict[str, Any]:
    """One section's answer, grounded on the first line (a contradiction on the first two). The first action-items
    call fabricates a word nothing said; its repair answers clean, so the validator's drop-and-repair runs."""
    lines = request["lines"]
    first = lines[0]
    words = first["text"].split()
    if not corrected and request["section"] == "actionItems":
        words[-1] = "definitelyunspoken"
    said = {"i": first["i"], "speaker": first["sname"], "atMs": first["s"], "quote": " ".join(words)}
    other = lines[1] if len(lines) > 1 else None
    pair = (
        []
        if other is None
        else [said, {"i": other["i"], "speaker": other["sname"], "atMs": other["s"], "quote": other["text"]}]
    )
    sections: dict[str, Any] = {
        "summary": {"title": f"About {request['title']}", "summary": f"Summary of {request['title']}."},
        "mom": {"mom": "### Topic\n\nThe meeting's minutes."},
        "actionItems": [
            {"id": "act_1", "text": "Do the thing", "owner": first["sname"], "due": None, "derivedFrom": [said]}
        ],
        "decisions": [
            {"id": "dec_1", "text": "We decided", "rationale": None, "alternatives": [], "derivedFrom": [said]}
        ],
        "openQuestions": [{"id": "q_1", "text": "What next?", "derivedFrom": [said]}],
        "risks": [{"id": "risk_1", "kind": "risk", "text": "A risk", "owner": None, "derivedFrom": [said]}],
        "followUps": [{"id": "fu_1", "text": "Follow up", "owner": None, "due": None, "derivedFrom": [said]}],
        "nextAgenda": [{"id": "next_1", "topic": "Next steps", "derivedFrom": [said]}],
        "agenda": [{"id": "ag_1", "topic": "Topic", "status": "covered", "derivedFrom": [said]}],
        "contradictions": [{"id": "con_1", "text": "Said both", "derivedFrom": pair}] if pair else [],
    }
    made = sections[request["section"]]
    return made if request["section"] in ("summary", "mom") else {"items": made}


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
