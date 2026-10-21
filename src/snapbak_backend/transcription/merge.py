"""AssemblyAI's words and pyannoteAI's turns as one transcript (D10, D11, Q114; docs/cloud.md, "Merge"). Pure
functions, no I/O, no recursion. A word's times stay exactly as AssemblyAI gave them."""

from dataclasses import dataclass
from typing import Any, TypeIs

from snapbak_backend.transcription.languages import base

# Latin, Devanagari danda, CJK full stop, and the full-width "?" and "!".
SENTENCE_END = (".", "?", "!", "।", "。", "\uff1f", "\uff01")
MOST_WORDS = 30
SOFT_BREAK_FROM = 20
# Latin, Arabic and CJK commas, and the full-width comma.
SOFT_BREAK = (",", ";", "،", "、", "\uff0c")
# A word in a gap takes the nearest turn this close; farther, the previous word's speaker.
NEAREST_MS = 1000
FILLERS = frozenset({"um", "uh", "umm", "uhh", "hmm", "mm", "er", "ah"})
NO_SPACE = frozenset({"ja", "zh"})
# The one speaker when pyannote found no speech at all.
ONLY_SPEAKER = "SPEAKER_00"
PUNCTUATION = ".,?!;:\"'()-\u2013\u2014\u2026"


@dataclass(frozen=True)
class Turn:
    speaker: str
    start: int
    end: int


@dataclass(frozen=True)
class Word:
    w: str
    s: int
    e: int
    filler: bool
    conf: float
    speaker: str  # the pyannote speaker key


@dataclass(frozen=True)
class SpeakerOut:
    key: str  # vendor_label
    name: str | None


@dataclass(frozen=True)
class Merged:
    language: str
    audio_ms: int
    speakers: list[SpeakerOut]  # in order of first word
    lines: list[list[Word]]


def turns_of(diarization: list[dict[str, Any]]) -> list[Turn]:
    """pyannote's `exclusiveDiarization`, in seconds, as turns in ms sorted by start."""
    made = [Turn(item["speaker"], round(item["start"] * 1000), round(item["end"] * 1000)) for item in diarization]
    made.sort(key=lambda turn: turn.start)
    return made


def nearest(turns: list[Turn], start: int, end: int, first: int) -> str | None:
    """Of the turn that ended before the word and the one that starts after it, the closer, within 1 s."""
    best, best_gap = None, NEAREST_MS + 1
    if first > 0:
        before = turns[first - 1]
        best, best_gap = before.speaker, start - before.end
    if first < len(turns) and turns[first].start - end < best_gap:
        best, best_gap = turns[first].speaker, turns[first].start - end
    return best if best_gap <= NEAREST_MS else None


def assign(words: list[dict[str, Any]], turns: list[Turn]) -> list[str]:
    """Step 1: a speaker for every word, by largest overlap; ties go to the earlier turn. One sweep."""
    speakers: list[str] = []
    first = 0  # the first turn that may still overlap the current word
    for word in words:
        start, end = word["start"], word["end"]
        while first < len(turns) and turns[first].end <= start:
            first += 1
        best, best_overlap = None, 0
        i = first
        while i < len(turns) and turns[i].start < end:
            overlap = min(end, turns[i].end) - max(start, turns[i].start)
            if overlap > best_overlap:
                best, best_overlap = turns[i].speaker, overlap
            i += 1
        if best is None:
            best = nearest(turns, start, end, first)
        if best is None:
            first_speaker = turns[0].speaker if turns else ONLY_SPEAKER
            best = speakers[-1] if speakers else first_speaker
        speakers.append(best)
    return speakers


def is_name(speaker: str | None) -> TypeIs[str]:
    """AssemblyAI's speaker is a name once identification found one, else a generic letter."""
    if not speaker:
        return False
    letter = len(speaker) == 1 and speaker.isupper()
    return not letter


def name_speakers(words: list[dict[str, Any]], assigned: list[str]) -> dict[str, str]:
    """Step 2: each AssemblyAI name goes to the pyannote speaker holding most of its word time, if that is at least
    half of it; two names on one speaker leave the larger."""
    time_by_name: dict[str, dict[str, int]] = {}
    for word, speaker in zip(words, assigned, strict=True):
        name = word.get("speaker")
        if not is_name(name):
            continue
        per_speaker = time_by_name.setdefault(name, {})
        per_speaker[speaker] = per_speaker.get(speaker, 0) + word["end"] - word["start"]
    claims: dict[str, tuple[int, str]] = {}
    for name, per_speaker in time_by_name.items():
        speaker, held = max(per_speaker.items(), key=lambda item: item[1])
        if 2 * held < sum(per_speaker.values()):
            continue
        if speaker not in claims or held > claims[speaker][0]:
            claims[speaker] = (held, name)
    names = {speaker: name for speaker, (_, name) in claims.items()}
    return names


def lines(words: list[Word]) -> list[list[Word]]:
    """Step 3: a line is one sentence, split again on a speaker change, at a comma after 20 words, and at 30."""
    out: list[list[Word]] = []
    line: list[Word] = []
    for word in words:
        if line and word.speaker != line[-1].speaker:
            out.append(line)
            line = []
        line.append(word)
        ends = word.w.endswith(SENTENCE_END)
        soft = len(line) >= SOFT_BREAK_FROM and word.w.endswith(SOFT_BREAK)
        if ends or soft or len(line) >= MOST_WORDS:
            out.append(line)
            line = []
    if line:
        out.append(line)
    return out


def text(words: list[Word], language: str) -> str:
    """A line's text: words joined by one space, or none for Japanese and Chinese."""
    joiner = "" if base(language) in NO_SPACE else " "
    return joiner.join(word.w for word in words)


def is_filler(said: str) -> bool:
    bare = said.lower().strip(PUNCTUATION)
    return bare in FILLERS


def merge(transcript: dict[str, Any], diarization: list[dict[str, Any]], requested: str) -> Merged:
    """AssemblyAI's `GET /v2/transcript` body and pyannote's `exclusiveDiarization` as speakers and lines. Fillers
    are tagged in English only, never for Hinglish (D13)."""
    words = transcript.get("words") or []
    language = transcript.get("language_code") or ("hi" if requested == "hinglish" else requested)
    fillers = base(language) == "en" and requested != "hinglish"
    assigned = assign(words, turns_of(diarization))
    names = name_speakers(words, assigned)
    merged_words = [
        Word(
            w=word["text"],
            s=word["start"],
            e=word["end"],
            filler=fillers and is_filler(word["text"]),
            conf=word["confidence"],
            speaker=speaker,
        )
        for word, speaker in zip(words, assigned, strict=True)
    ]
    order = list(dict.fromkeys(assigned))
    speakers = [SpeakerOut(key, names.get(key)) for key in order]
    audio_ms = round((transcript.get("audio_duration") or 0) * 1000)
    split = lines(merged_words)
    merged = Merged(language=language, audio_ms=audio_ms, speakers=speakers, lines=split)
    return merged
