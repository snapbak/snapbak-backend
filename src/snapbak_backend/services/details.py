"""The no-dropped-details guard (minutes v2, 26 Oct 2026): the transcript's salient details, found by rules, and
whether the minutes kept them. A unit is one of:

- a number or amount ("5,000", "11:30", "2"), digits as said;
- a time or date word: a weekday, a month, today, tomorrow, tonight, and the Hinglish kal, parso, aaj, subah,
  shaam, raat;
- a name: a capitalized word that does not start a sentence (Latin script only: Devanagari has no capitals), and
  every speaker's chosen name;
- a commitment: "I'll", "I will", "we'll", "we will", "let's", "need to", "have to", or the Hinglish karunga,
  karungi, karenge, dunga, dungi, denge, lunga, karna hai; one per line that holds one, and none when the phrase
  is negated ("don't need to"), conditional ("would have to") or rhetorical ("let's see", "let's say").

A number, time or name is kept when its folded text (`folding.skeleton`) is whole words somewhere in the title,
summary, minutes or any item; a commitment is kept when an action item or a follow-up cites its line, or the
completion judged it none (`notCommitments`). Filler
words make no unit. Only counts and ratios of this ever reach a log."""

import re
from dataclasses import dataclass
from uuid import UUID

from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import MinutesOut
from snapbak_backend.services.folding import skeleton
from snapbak_backend.services.recordings import speaker_of

NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
TIMES = frozenset(
    (
        "monday tuesday wednesday thursday friday saturday sunday january february march april may june july "
        "august september october november december today tomorrow tonight yesterday weekend kal parso aaj subah "
        "shaam raat"
    ).split()
)
COMMITMENT = re.compile(
    r"\b(i'll|i will|we'll|we will|let's|need to|have to|karunga|karungi|karenge|dunga|dungi|denge|lunga|karna hai)\b",
    re.IGNORECASE,
)
# Capitalized words that are no name wherever they stand.
NOT_NAMES = frozenset("i i'll i'm i've i'd ok okay yes no so and but the a an".split())
SENTENCE_END = re.compile(r"[.!?]$")


# What makes a commitment phrase none (a sample of AMI's uncovered units, 26 Oct 2026, was 14 of 15 these): a
# negation before it ("don't need to"), a conditional ("would have to", "we'd have to"), or a rhetorical "let's see"
# or "let's say". The rest of the sample (narration of the meeting itself, requirements, questions) is left to the
# completion's judgment: rules cannot tell "I'll explain the components" from "I'll send the components".
NOT_PROMISED = re.compile(r"(?:\b(?:don't|doesn't|didn't|not|never|would)\s+|'d\s+)$", re.IGNORECASE)
RHETORICAL = re.compile(r"\s*(?:see|say|think)\b", re.IGNORECASE)


def commitment_in(text: str) -> str | None:
    """The line's first commitment phrase that is one, or None."""
    for found in COMMITMENT.finditer(text):
        negated = NOT_PROMISED.search(text[: found.start()])
        rhetorical = found.group(1).lower() == "let's" and RHETORICAL.match(text, found.end())
        if not negated and not rhetorical:
            return found.group(1)
    return None


@dataclass(frozen=True)
class Unit:
    kind: str  # number, time, name or commitment
    text: str  # folded as `folding.skeleton` folds it
    idx: int  # the line it was said in


def names_in(text: str) -> list[str]:
    """Capitalized words that don't start a sentence."""
    found = []
    starts = True
    for word in text.split():
        bare = word.strip("\"'()[],;:!?.")
        if bare and bare[0].isupper() and bare.isascii() and not starts and bare.lower() not in NOT_NAMES:
            found.append(bare)
        starts = bool(SENTENCE_END.search(word))
    return found


def inventory(lines: list[TranscriptLine], speakers: dict[UUID, Speaker]) -> list[Unit]:
    """Every salient detail of the transcript, once per text, at the first line it was said in."""
    units: dict[tuple[str, str], Unit] = {}

    def add(kind: str, said: str, idx: int) -> None:
        folded = skeleton(said)
        if folded and (kind, folded) not in units:
            units[(kind, folded)] = Unit(kind, folded, idx)

    for line in lines:
        for said in NUMBER.findall(line.text):
            add("number", said, line.idx)
        for word in skeleton(line.text).split():
            if word in TIMES:
                add("time", word, line.idx)
        for said in names_in(line.text):
            add("name", said, line.idx)
        promise = commitment_in(line.text)
        if promise:
            # One unit per line that promises: every "I'll" counts, not only a meeting's first.
            units[("commitment", f"{line.idx}")] = Unit("commitment", skeleton(promise), line.idx)
        name = speaker_of(speakers.get(line.speaker_id))
        if name and not name.startswith("Speaker "):
            add("name", name, line.idx)
    return list(units.values())


def written(doc: MinutesOut) -> str:
    """Everything the minutes say, folded, with a space at each end for whole-word matching."""
    parts = [doc.title or "", doc.summary, doc.mom]
    for item in [*doc.actionItems, *doc.followUps]:
        parts += [item.text, item.owner or "", item.due or ""]
    for claim in [*doc.openQuestions, *doc.contradictions]:
        parts.append(claim.text)
    for decision in doc.decisions:
        parts += [decision.text, decision.rationale or "", *decision.alternatives]
    for risk in doc.risks:
        parts += [risk.text, risk.owner or ""]
    parts += [topic.topic for topic in [*doc.agenda, *doc.nextAgenda]]
    return f" {skeleton(' '.join(parts))} "


def missing(doc: MinutesOut, units: list[Unit], lines: list[TranscriptLine]) -> list[Unit]:
    """The units the minutes left out."""
    text = written(doc)
    index_of = {line.id: line.idx for line in lines}
    cited = {index_of.get(entry.lineId) for item in [*doc.actionItems, *doc.followUps] for entry in item.derivedFrom}
    cited |= {index_of.get(judged.lineId) for judged in doc.notCommitments}
    left = []
    for unit in units:
        kept = unit.idx in cited if unit.kind == "commitment" else f" {unit.text} " in text
        if not kept:
            left.append(unit)
    return left


def recall(doc: MinutesOut, units: list[Unit], lines: list[TranscriptLine]) -> float:
    """The share of the transcript's details the minutes kept; 1 when it has none."""
    if not units:
        return 1.0
    return 1 - len(missing(doc, units, lines)) / len(units)


def topics_missing(doc: MinutesOut) -> list[str]:
    """The agenda's topics with no `### ` heading of their own in the minutes (matched folded, either way round)."""
    headings = [skeleton(line[4:]) for line in doc.mom.splitlines() if line.startswith("### ")]
    left = []
    for topic in doc.agenda:
        folded = skeleton(topic.topic)
        if not any(folded in heading or heading in folded for heading in headings if heading):
            left.append(topic.topic)
    return left
