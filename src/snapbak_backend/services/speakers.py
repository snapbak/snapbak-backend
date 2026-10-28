"""The speaker identification loop (MEETINGS.md D42, PRD MOM-08): after the first draft, the model reads the
transcript and says who the people are, which speakers are one voice split in two, and which lines went to the wrong
speaker; pyannote diarizes again with the count it found. A name stands on two cues from what was said; a merge or a
move stands only when the model and the rerun agree (an LLM alone makes diarization worse: DiarizationLM, 2024).
Anything the user named or edited is never touched. These are the pure rules; the worker runs them."""

import json
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import Change, Cue, MinutesOut
from snapbak_backend.services import people
from snapbak_backend.services.folding import quoted, skeleton
from snapbak_backend.transcription.merge import Turn, assign

# A name stands on this many cues, each on a line of its own.
NAME_CUES = 2
# A voice whose confidence for a speaker's voiceprint reaches this is that speaker's (pyannote's 0-100; its own
# example scores a match 86 and another speaker 16).
SAME_VOICE = 75
# A line moves only with this many words: a short one ("Yeah.") is too little audio for a voice to be sure of, the
# diarization's most common miss (eval_speakers.py: every wrong move was a misheard fragment).
MOVE_WORDS = 3
# A second round runs after a change this big: this share of the lines moved, or this many merges.
BIG_SHARE = 0.2
BIG_MERGES = 2


@dataclass(frozen=True)
class Person:
    """Someone the model found: their `p` when they speak, the name it heard, its cues, and the other speakers it
    says are the same voice."""

    p: int | None
    name: str | None
    cues: list[Cue] = field(default_factory=list)
    same: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Proposal:
    """What the model proposed, every cue checked: the people, the voice count, and lines to move (`i` to `p`)."""

    count: int = 0
    people: list[Person] = field(default_factory=list)
    moves: dict[int, tuple[int, Cue]] = field(default_factory=dict)


def change_id() -> str:
    return f"chg_{uuid.uuid4().hex[:8]}"


def number(value: Any) -> int | None:
    """A JSON number that is a whole number, never a true or false (Python counts a bool as an int)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def cue_ok(raw: Any, by_index: dict[int, TranscriptLine], name: str | None) -> Cue | None:
    """A cue that is a real line's words, verbatim, and holds the name (or its first word) when there is one; else
    None."""
    if not isinstance(raw, dict) or number(raw.get("i")) is None or not isinstance(raw.get("quote"), str):
        return None
    # The name, or its first word: people say "Thanks, Brian", not his full name.
    said = f" {skeleton(raw['quote'])} "
    if name is not None and not any(f" {skeleton(part)} " in said for part in (name, name.split()[0])):
        return None
    # The model's line, else the one beside it whose words these are: a model often numbers a handover ("over to
    # Brian") by the line of the one handed to. The words, checked verbatim, say where they were said.
    for i in (raw["i"], raw["i"] - 1, raw["i"] + 1):
        line = by_index.get(i)
        if line is not None and quoted(raw["quote"], line.text):
            made = Cue(i=i, quote=raw["quote"])
            return made
    return None


def proposal_of(answer: str, by_index: dict[int, TranscriptLine], speakers: dict[UUID, Speaker]) -> Proposal:
    """The model's answer with every cue checked against the transcript and every `p` a speaker's; nothing usable
    from an answer that isn't the JSON asked for."""
    try:
        said = json.loads(answer)
    except ValueError:
        return Proposal()
    if not isinstance(said, dict):
        return Proposal()
    numbered = people.by_number(speakers.values())
    found: list[Person] = []
    for raw in listed(said.get("people")):
        if not isinstance(raw, dict):
            continue
        p = number(raw.get("p"))
        p = p if p in numbered else None
        given = raw.get("name")
        name = (given.strip() or None) if isinstance(given, str) else None
        cues = [cue for each in listed(raw.get("cues")) if (cue := cue_ok(each, by_index, name))]
        same = [other for each in listed(raw.get("same")) if (other := number(each)) in numbered and other != p]
        found.append(Person(p=p, name=name, cues=cues, same=same))
    moves: dict[int, tuple[int, Cue]] = {}
    for raw in listed(said.get("moves")):
        if not isinstance(raw, dict):
            continue
        cue = cue_ok(raw.get("cue"), by_index, None)
        i, to = number(raw.get("i")), number(raw.get("to"))
        if cue and i in by_index and to in numbered:
            moves[i] = (to, cue)
    made = Proposal(count=number(said.get("count")) or 0, people=found, moves=moves)
    return made


def user_named(speaker: Speaker) -> bool:
    return speaker.name_source == "user"


def named_by_now(speaker: Speaker) -> bool:
    """A speaker whose name the loop leaves be: the user's, or one it guessed before (a guess never flips to
    another; the user can always rename)."""
    return speaker.name_source in ("user", "guessed")


def same_name(one: str | None, other: str | None) -> bool:
    return one is not None and other is not None and one.casefold() == other.casefold()


# Words that introduce the speaker ("I'm", "my name is", "this is", "call me"), a filler or two allowed before the name.
INTRO = r"(?:\bi\s*'?\s*a?m|\bmy\s+name(?:'s|\s+is)|\bthis\s+is|\bcall\s+me)(?:\W+(?:uh|um|er))*\W+"


def introduces(text: str, name: str) -> bool:
    """Whether the words introduce their speaker as `name` ("I'm uh Tim", "my name is Danny Wolfs")."""
    found = re.search(INTRO + re.escape(name.split()[0]), text, re.IGNORECASE)
    return found is not None


def addressed(text: str, name: str) -> bool:
    """Whether the words say `name` to someone, not about them: the name, or its first word, then a comma, a question
    or full stop, or the end ("Thanks, Maya.", "Maya, how…"), never "Maya said"."""
    spoken = {name, name.split()[0]}
    found = any(re.search(rf"\b{re.escape(said)}\b\s*(?:[,.?!]|$)", text, re.IGNORECASE) for said in spoken)
    return found


# Words that hand the floor to someone by name: "over to Brian", "back to you, Maya", "comes from the line of Jane".
HANDOVER = (
    r"\b(?:over\s+to|back\s+to|comes?\s+from|coming\s+from|pass(?:ing)?\s+(?:it\s+)?to|hand(?:ing)?\s+(?:it\s+)?to)\b"
)


def hands_over(text: str, name: str) -> bool:
    """Whether the words hand the floor to `name`: a handover, then the name later in the same sentence ("I'll turn
    the call over to our CFO, Brian D'Ambrosia")."""
    first = re.escape(name.split()[0])
    found = re.search(rf"{HANDOVER}[^.?!]{{0,120}}?\b{first}\b", text, re.IGNORECASE)
    return found is not None


def calls_on(text: str, name: str) -> bool:
    """Whether a sentence of the words opens on `name` to call on them ("Arjun, you're up", "Omar, what are you
    hearing?"): the one called on speaks next."""
    first = re.escape(name.split()[0])
    found = re.search(rf"(?:^|[.?!]\s+){first}\b\s*,", text, re.IGNORECASE)
    return found is not None


def opens_with(text: str, name: str) -> bool:
    """Whether the words open by saying `name` to someone ("Thank you, Brett, and good morning"): an answer's thanks
    go to whoever spoke just before."""
    opening = " ".join(text.split()[:5])
    return addressed(opening, name)


def supports(cue: Cue, speaker: Speaker, by_index: dict[int, TranscriptLine], name: str) -> bool:
    """Whether a cue names `speaker`, by who said it (REPERE's lesson: a right name on the wrong speaker is the common
    error): said by them, only as an introduction; said by another, as a handover or a call on them ("Arjun, you're
    up") on the line just before theirs, as thanks opening the line just after theirs, or else said to someone only
    when they are the one other speaker on the lines around it (crosstalk with two others names neither,
    eval_speakers.py)."""
    line = by_index[cue.i]
    if line.speaker_id == speaker.id:
        return introduces(line.text, name)
    before, after = by_index.get(cue.i - 1), by_index.get(cue.i + 1)
    if (
        after is not None
        and after.speaker_id == speaker.id
        and (hands_over(line.text, name) or calls_on(line.text, name))
    ):
        return True
    if before is not None and before.speaker_id == speaker.id and opens_with(line.text, name):
        return True
    beside = [before, after]
    others = {each.speaker_id for each in beside if each is not None and each.speaker_id != line.speaker_id}
    return others == {speaker.id} and addressed(line.text, name)


def named(proposal: Proposal, speakers: dict[UUID, Speaker], lines: list[TranscriptLine]) -> list[Change]:
    """The names: applied (as guessed) on two cues from two lines that name that very speaker (`supports`) and no
    rival claim, suggested on fewer or a rival; never on a speaker the user named, nor a name the speaker has
    already."""
    numbered = people.by_number(speakers.values())
    by_index = {line.idx: line for line in lines}
    claims = Counter(person.name.casefold() for person in proposal.people if person.p is not None and person.name)
    changes: list[Change] = []
    for person in proposal.people:
        speaker = numbered.get(person.p) if person.p is not None else None
        if speaker is None or person.name is None or named_by_now(speaker) or same_name(speaker.name, person.name):
            continue
        cues = [cue for cue in person.cues if supports(cue, speaker, by_index, person.name)]
        said_on = {cue.i for cue in cues}
        taken = any(same_name(other.name, person.name) for other in speakers.values() if other is not speaker)
        rival = claims[person.name.casefold()] > 1 or taken
        if not said_on:
            continue
        state = "applied" if len(said_on) >= NAME_CUES and not rival else "suggested"
        made = Change(
            id=change_id(),
            kind="name",
            state=state,
            speakerId=speaker.id,
            name=person.name,
            before=speaker.name,
            cues=cues,
        )
        changes.append(made)
    return changes


def spoken_ms(line: TranscriptLine) -> int:
    """How long a line was said for: lines end at a change of speaker, so a line's time is one speaker's."""
    return line.end_ms - line.start_ms


def speech(lines: list[TranscriptLine]) -> Counter[UUID]:
    """Each speaker's time speaking, in ms."""
    spoken: Counter[UUID] = Counter()
    for line in lines:
        spoken[line.speaker_id] += spoken_ms(line)
    return spoken


def voice_of(lines: list[TranscriptLine], voices: dict[int, str]) -> dict[UUID, str]:
    """Each speaker's voice in the rerun: the one holding most of their time."""
    held: dict[UUID, Counter[str]] = {}
    for line in lines:
        voice = voices.get(line.idx)
        if voice is not None:
            held.setdefault(line.speaker_id, Counter())[voice] += spoken_ms(line)
    made = {speaker: counted.most_common(1)[0][0] for speaker, counted in held.items()}
    return made


def voice_groups(heard: Any, labels: dict[str, UUID]) -> list[frozenset[UUID]]:
    """The speakers pyannote's identify hears as one voice (MOM-08): a voice whose confidence for two or more speakers'
    voiceprints (`labels`, label to speaker) reaches SAME_VOICE. Anything not the documented shape is ignored."""
    groups: list[frozenset[UUID]] = []
    for voice in heard if isinstance(heard, list) else []:
        scores = voice.get("confidence") if isinstance(voice, dict) else None
        if not isinstance(scores, dict):
            continue
        matched = frozenset(
            labels[label]
            for label, score in scores.items()
            if label in labels and isinstance(score, int | float) and score >= SAME_VOICE
        )
        if len(matched) > 1 and matched not in groups:
            groups.append(matched)
    return groups


def joined(
    group: list[Speaker], lines: list[TranscriptLine], names: dict[UUID, str | None], gone: set[UUID], stays: set[UUID]
) -> list[Change]:
    """One group's merges into the speaker kept: one an earlier group kept (`stays`, so merges never chain: A into B,
    then B into C), then the one the user named, then a named one (`names`: as they are after this round's guesses,
    so a name guessed now is never merged away), then the one who spoke most. A speaker the user named is never
    merged away, and two different names never merge (REPERE, Poignant and Bredin); `gone` keeps a speaker from
    merging twice."""
    spoken = speech(lines)
    group = [each for each in group if each.id not in gone]
    if len(group) < 2:
        return []
    kept = max(
        group, key=lambda each: (each.id in stays, user_named(each), names.get(each.id) is not None, spoken[each.id])
    )
    stays.add(kept.id)
    changes = []
    for loser in group:
        if loser is kept or loser.id in stays or user_named(loser):
            continue
        lost = names.get(loser.id)
        if lost and not same_name(lost, names.get(kept.id)):
            continue
        moved = [line.id for line in lines if line.speaker_id == loser.id]
        changes.append(
            Change(
                id=change_id(),
                kind="merge",
                state="applied",
                speakerId=loser.id,
                # Who the merged speaker was, for "Speaker 4 and Noah are the same person": its row goes.
                before=lost or loser.label,
                intoId=kept.id,
                lineIds=moved,
            )
        )
        gone.add(loser.id)
    return changes


def merged(
    proposal: Proposal,
    speakers: dict[UUID, Speaker],
    lines: list[TranscriptLine],
    voices: dict[int, str] | None,
    same_voice: list[frozenset[UUID]],
    names: dict[UUID, str | None],
) -> list[Change]:
    """The merges the evidence agrees on: speakers whose voiceprints match one voice (`same_voice`), and speakers
    the model says are one person with the same voice in the rerun (`voices`)."""
    numbered = people.by_number(speakers.values())
    groups = [[speakers[each] for each in group if each in speakers] for group in same_voice]
    voice = voice_of(lines, voices or {})
    for person in proposal.people:
        if voices is None or person.p is None or not person.same or person.p not in numbered:
            continue
        lead = numbered[person.p]
        said = [lead, *(numbered[p] for p in person.same)]
        groups.append(
            [each for each in said if voice.get(lead.id) is not None and voice.get(each.id) == voice.get(lead.id)]
        )
    gone: set[UUID] = set()
    stays: set[UUID] = set()
    changes = [change for group in groups for change in joined(group, lines, names, gone, stays)]
    return changes


def moved(
    proposal: Proposal,
    speakers: dict[UUID, Speaker],
    lines: list[TranscriptLine],
    voices: dict[int, str],
    merges: list[Change],
) -> list[Change]:
    """The lines the model and the rerun agree went to the wrong speaker (after the merges): the model moves a line
    to a speaker, and the rerun hears that line in the voice that speaker holds most of. A line under MOVE_WORDS, or
    one the user edited or reassigned (`rev` past 1), never moves."""
    numbered = people.by_number(speakers.values())
    into = {change.speakerId: change.intoId for change in merges if change.intoId is not None}
    owner: dict[str, Counter[UUID]] = {}
    for line in lines:
        voice = voices.get(line.idx)
        if voice is not None:
            who = into.get(line.speaker_id, line.speaker_id)
            owner.setdefault(voice, Counter())[who] += spoken_ms(line)
    by_index = {line.idx: line for line in lines}
    changes: list[Change] = []
    for idx, (p, cue) in proposal.moves.items():
        line = by_index[idx]
        now = into.get(line.speaker_id, line.speaker_id)
        target = into.get(numbered[p].id, numbered[p].id)
        heard = owner.get(voices.get(idx, ""), Counter()).most_common(1)
        if len(line.text.split()) < MOVE_WORDS or line.rev > 1:
            continue
        if target == now or not heard or heard[0][0] != target:
            continue
        changes.append(
            Change(
                id=change_id(),
                kind="move",
                state="applied",
                speakerId=now,
                intoId=target,
                lineIds=[line.id],
                cues=[cue],
            )
        )
    return changes


def decide(
    proposal: Proposal,
    speakers: dict[UUID, Speaker],
    lines: list[TranscriptLine],
    voices: dict[int, str] | None,
    same_voice: Sequence[frozenset[UUID]] = (),
    history: Sequence[Change] = (),
) -> list[Change]:
    """Every change the round makes or offers: names; merges on matching voiceprints, or on the model and a rerun
    agreeing; moves only when there was a rerun to agree with."""
    changes = named(proposal, speakers, lines)
    # Each speaker's name after this round's guesses, so a merge keeps the one just named.
    names: dict[UUID, str | None] = {speaker.id: speaker.name for speaker in speakers.values()}
    names |= {change.speakerId: change.name for change in changes if change.state == "applied"}
    # A merge the user undid stays undone, so no move is worked out through it either.
    proposed = merged(proposal, speakers, lines, voices, list(same_voice), names)
    merges = [change for change in proposed if not said_no(change, history)]
    moves = [] if voices is None else moved(proposal, speakers, lines, voices, merges)
    made = [*changes, *merges, *moves]
    return made


def said_no(change: Change, history: Sequence[Change]) -> bool:
    """Whether this very change (its kind, speakers, name and lines) was asked before: the user undid or turned it
    down, so the loop run again on the same evidence never brings it back, or it is a suggestion still waiting."""
    for each in history:
        waiting = each.state == "suggested" and change.state == "suggested"
        answered = each.state in ("undone", "rejected") or waiting
        same_speakers = (each.kind, each.speakerId, each.intoId) == (change.kind, change.speakerId, change.intoId)
        if not (answered and same_speakers):
            continue
        same_move = each.kind == "move" and each.lineIds == change.lineIds
        if each.kind == "merge" or same_name(each.name, change.name) or same_move:
            return True
    return False


def relabel(lines: list[TranscriptLine], turns: list[Turn]) -> dict[int, str]:
    """Each line's voice in the rerun: the one holding most of its words' time (the lines and their ids stay)."""
    words = [{"start": word["s"], "end": word["e"]} for line in lines for word in line.words]
    heard = iter(assign(words, turns))
    voices: dict[int, str] = {}
    for line in lines:
        held: Counter[str] = Counter()
        for word in line.words:
            held[next(heard)] += word["e"] - word["s"]
        if held:
            voices[line.idx] = held.most_common(1)[0][0]
    return voices


def big(changes: list[Change], line_count: int) -> bool:
    """Whether the round changed enough for one more: a share of the lines moved, or several merges."""
    applied = [change for change in changes if change.state == "applied"]
    lines = sum(len(change.lineIds) for change in applied if change.kind == "move")
    merges = sum(change.kind == "merge" for change in applied)
    return line_count > 0 and (lines >= BIG_SHARE * line_count or merges >= BIG_MERGES)


def touched(doc: MinutesOut, moved_lines: set[UUID]) -> list[str]:
    """The sections to write again after lines moved: the summary and the minutes (they say who said what), and each
    item section citing a moved line. Names and merges need none: tokens show the speaker as they are now."""
    if not moved_lines:
        return []
    cited = [
        section
        for section in people.SECTIONS
        if any(entry.lineId in moved_lines for item in getattr(doc, section) for entry in item.derivedFrom)
    ]
    return ["summary", "mom", *cited]


def undone_sections(doc: MinutesOut, change: Change, into: int | None) -> list[str]:
    """The sections to write again after a change is undone: for a move, those citing its line (with the summary and
    the minutes); for a merge, those naming the kept speaker `into`, since some of those named the merged one (a
    cite needs nothing: it names its line's speaker as they are now)."""
    again = set(touched(doc, set(change.lineIds))) if change.kind == "move" else set()
    if change.kind == "merge" and into is not None:
        token = f"{{{{p:{into}}}}}"
        again |= {name for name in ("summary", "mom") if token in getattr(doc, name)}
        again |= {
            section
            for section in people.SECTIONS
            if any(token in item.model_dump_json(exclude={"derivedFrom"}) for item in getattr(doc, section))
        }
    ordered = [name for name in ("summary", "mom", *people.SECTIONS) if name in again]
    return ordered
