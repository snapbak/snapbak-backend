"""A polish request as model turns, folded around a correction, and the model's JSON back as a judged answer."""

import json
import re

from pydantic import BaseModel

from snapbak_backend.ai import routing
from snapbak_backend.ai.guardrail.judge import (
    PARAGRAPHS,
    SPLIT_JOINERS,
    Polished,
    Reason,
    Reject,
    joiner_put_back,
    judge,
    next_item,
    past_prior,
)
from snapbak_backend.ai.guardrail.shape import (
    announced_list,
    asked_marked,
    bullets_numbered,
    paragraphed,
    rejoined,
    unasked_marked,
)
from snapbak_backend.ai.prompts.examples import EXAMPLES
from snapbak_backend.ai.prompts.polish import SYSTEM, correction, message
from snapbak_backend.ai.providers.base import Turn
from snapbak_backend.schemas.polish import PolishRequest

# A full stop after a digit numbers a list item ("1. ") or ends a number, and starts no new sentence.
SENTENCE_END = re.compile(r"(?<!\d)[.?!।]\s+|\n")


MALFORMED = Reason(Reject.MALFORMED)


def last_turn(request: BaseModel) -> Turn:
    fields = request.model_dump(exclude={"deadline_ms"})
    return ("user", message(fields))


def correction_turn(request: PolishRequest, reason: Reason) -> Turn:
    """The request asked again with why its answer was refused in a "checker" field of its own, so the note is
    never read as dictation to polish (round b6, D1)."""
    fields = request.model_dump(exclude={"deadline_ms"})
    fields["checker"] = correction(reason)
    return ("user", message(fields))


def polish_turns(request: PolishRequest) -> list[Turn]:
    turns: list[Turn] = [("system", SYSTEM)]
    for said, cleaned in EXAMPLES:
        turns += [("user", said), ("assistant", cleaned)]
    turns.append(last_turn(request))
    return turns


def read(request: PolishRequest, asked: PolishRequest, output: str) -> Polished:
    """The answer to `asked`, judged as an answer to `request`."""
    try:
        answer = json.loads(output)
        text = answer["text"]
        revision = answer.get("revision")
    except ValueError, KeyError, TypeError, AttributeError:
        return Polished(request.text, None, Reject.MALFORMED, MALFORMED)
    if not isinstance(text, str) or not isinstance(revision, str | None):
        return Polished(request.text, None, Reject.MALFORMED, MALFORMED)
    kept, chunk = unfold(request, asked, revision, text)
    # A piece that takes nothing back leaves the typed prior as it is: a revision that holds it and goes on is the
    # piece's answer (round b5).
    tail = past_prior(request.prior, kept) if kept and not routing.has_cue(request.text) else None
    if tail is not None:
        kept, chunk = None, f"{tail} {chunk}".strip()
    judged = judge(request.prior, request.text, request.terms, kept, chunk)
    repaired = repair(request, chunk, judged)
    return repaired or judged


def repair(request: PolishRequest, text: str, judged: Polished) -> Polished | None:
    """A refused answer put right without a model, when the refusal names something a rule can mend (round b2): a
    piece going on with a sentence that lost its joining word, a paragraph said but run on, and a run-on split by
    dropping its joining word (round b5); and, once every word passed, a direct question left with a full stop, a
    "?" on a sentence that asks nothing, a counted list bulleted, or announced statements written as prose (round
    b6). None when no mended answer is accepted."""
    if judged.revision is not None or judged.reason is None:
        return None
    for mended in mendings(request, text, judged.reason):
        again = judge(request.prior, request.text, request.terms, None, mended)
        if again.rejected is None:
            return again
    return None


def mendings(request: PolishRequest, text: str, reason: Reason) -> list[str]:
    """The answers a rule makes of a refused one, to judge again in turn."""
    rule = reason.rule
    if rule == Reject.DROPPED_JOIN:
        mended = [rejoined(request.text, text)]
    elif rule == Reject.FORMAT and reason.words in PARAGRAPHS:
        mended = [paragraphed(request.text, text)]
    elif rule == Reject.DROPPED_WORD and set(reason.words) <= SPLIT_JOINERS:
        mended = [joiner_put_back(request.text, text)]
    elif rule == Reject.QUESTION:
        mended = asked_marked(text)
    elif rule == Reject.NOT_A_QUESTION:
        mended = unasked_marked(text)
    elif rule == Reject.NUMBERED:
        mended = [bullets_numbered(text, next_item(request.prior))]
    elif rule == Reject.UNLISTED and reason.words:
        mended = [announced_list(text, reason.words[0])]
    else:
        mended = []
    found = [answer for answer in mended if answer is not None]
    return found


def fold(request: PolishRequest) -> PolishRequest:
    """With a correction cue in the chunk, the prior's last sentence joins the chunk, so the model fixes a correction
    that reaches back as one piece of text; the rest of the prior stays as context."""
    if not request.prior or not routing.has_cue(request.text):
        return request
    prior = request.prior.rstrip()
    cut = max((found.end() for found in SENTENCE_END.finditer(prior)), default=0)
    folded = request.model_copy(update={"prior": prior[:cut].rstrip(), "text": f"{prior[cut:]} {request.text}"})
    return folded


def unfold(request: PolishRequest, asked: PolishRequest, revision: str | None, text: str) -> tuple[str | None, str]:
    """The (revision, text) answer to the folded `asked` as one to `request`: the chunk alone when the prior's last
    sentence came back as it was, else the answer with the rest of the prior as the revision (the app puts a list
    item on its own line)."""
    if asked is request:
        return revision, text
    last = asked.text.removesuffix(request.text).rstrip()
    rest = text.removeprefix(last)
    if revision is None and rest != text and rest[:1] in {"", " ", "\n"}:
        return None, rest.strip()
    kept = asked.prior if revision is None else revision
    return kept, text
