"""`just eval-transcript`: how well our code turns the vendors' answers into speakers, lines, fillers, pauses and
captions, scored against the AMI Meeting Corpus (13 LLD, Evals).

`uv run scripts/eval_transcript.py [--save-baseline]` is the replay tier. It is free and calls no vendor. The corpus
(`bench/transcription/ami.json`) is downloaded once into `~/.snapbak-bench/ami/` and checked against its SHA-256.
Each excerpt's true words become an AssemblyAI answer and a pyannote answer with seeded, realistic errors (see
`vendor_inputs`). The real `merge` runs on them, and each metric is scored against the truth. Recorded real vendor
answers in `bench/transcription/recorded/<excerpt>/` are scored too, as their own rows. The first run records
`bench/transcription/baseline.json`; a run worse than it on any metric fails, and `--save-baseline` moves it forward
when no metric got worse.

`... --live --budget-usd USD [--ledger PATH]` is the live tier: each excerpt's audio through the real
`AssemblyAiPyannoteStrategy`, adding WER, cpWER, pyannote's DER, latency and cost. It refuses to start without both
vendor keys and a budget, writes its spend to the ledger that `bench.py` uses, and saves the vendors' answers for the
replay tier.

The pause rule mirrors 14-transcript-editing A6 and the caption rule 15-captions C1; the app's own code for both lives
in the app repo.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import sys
import urllib.request
import wave
import zipfile
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise, permutations
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from vendor_run import CAP_S, WORD, heard, percentile, save_answers, word_error_rate

from bench import append_ledger, ledger_total, require_budget
from snapbak_backend.config.settings import load
from snapbak_backend.services.storage import Storage, s3_client
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy
from snapbak_backend.transcription.merge import PUNCTUATION, SENTENCE_END, Word, lines, merge

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "bench" / "transcription" / "ami.json"
BASELINE = ROOT / "bench" / "transcription" / "baseline.json"
RECORDED = ROOT / "bench" / "transcription" / "recorded"
CACHE = Path.home() / ".snapbak-bench" / "ami"
OUT = Path.home() / ".snapbak-bench" / "eval-transcript.json"
LEDGER = Path.home() / ".snapbak-bench" / "spend.jsonl"
SEED = 13
# AMI's spellings of a hesitation. "Mm-hmm" and "uh-huh" mean yes, so they are words, not fillers.
AMI_FILLERS = frozenset({"um", "uh", "hmm", "mm", "ah"})
NITE_ID = "{http://nite.sourceforge.net/}id"

# The errors the replay tier adds to the truth, each from what the vendors are known to do.
# AssemblyAI hears one stream: its words keep their true order and never overlap, so a word said over someone else
# starts when the word before it ends. A word's start and end are off by a normal error of this standard deviation,
WORD_JITTER_MS = 40
# it misses this share of words (quiet backchannels, cross-talk),
MISSED_WORDS = 0.02
# and hears this share twice (a repeated word).
EXTRA_WORDS = 0.01
# pyannote: each turn's edges are off by a normal error of this standard deviation,
TURN_SHIFT_MS = 150
# this share of turns goes to another speaker,
SWAPPED_TURNS = 0.03
# and turns shorter than this are missed.
SHORTEST_TURN_MS = 250
# One speaker's words closer than this are one turn.
TURN_GAP_MS = 500
# DER is counted on a grid of this step.
FRAME_MS = 10

# 14-transcript-editing A6: a gap of a second or more is a pause, and its cut leaves 150 ms each side.
PAUSE_MS = 1000
PAUSE_KEEP_MS = 150
# 15-captions C1: two rows of 42 characters and 7 words, a break at a 700 ms gap, 7 s at most.
ROW_CHARS = 42
ROW_WORDS = 7
CAPTION_GAP_MS = 700
LONGEST_MS = 7000
CAPTION_END = (*SENTENCE_END, "؟")

# HLD "Cost per hour of audio": AssemblyAI $0.25 with diarization and identification, pyannoteAI about $0.12.
PRICE_PER_HOUR = 0.37
HOUR_MS = 3_600_000
TEN_MINUTES_MS = 600_000

# Metric -> higher is better.
HIGHER = {
    "speaker words %": True,
    "merge DER %": False,
    "word time median ms": False,
    "word time p95 ms": False,
    "line breaks F1 %": True,
    "mixed-speaker lines %": False,
    "filler precision %": True,
    "filler recall %": True,
    "pause F1 %": True,
    "pause cuts into speech %": False,
    "caption violations %": False,
    "WER %": False,
    "cpWER %": False,
    "DER %": False,
    "s per 10 min": False,
    "$ per audio hour": False,
}

type Span = tuple[str, int, int]
type Answers = tuple[dict[str, Any], dict[str, Any]]
type Rows = dict[str, dict[str, float]]


@dataclass(frozen=True)
class Excerpt:
    name: str
    start_ms: int
    ms: int
    truth: list[Word]  # in ms from the excerpt's start, by start
    audio: Path  # the whole meeting's audio in the cache; only the live tier fetches it


def share(part: float, whole: float, empty: float = 0.0) -> float:
    """`part` as a percentage of `whole`, or `empty` when there is nothing to count."""
    if not whole:
        return empty
    return 100 * part / whole


def f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def bare(said: str) -> str:
    stripped = said.lower().strip(PUNCTUATION)
    return stripped


def ami_speaker(root: ElementTree.Element, speaker: str) -> tuple[list[Word], list[tuple[str, int | None]]]:
    """One speaker's timed words from AMI's `words/<meeting>.<speaker>.words.xml` (a mark joins the word before it,
    untimed words and sounds are skipped), and each element's nite:id in file order with the index of the word it
    is part of, or None: the dialogue acts and summaries point at those ids."""
    spoken: list[Word] = []
    owners: list[tuple[str, int | None]] = []
    timed = False
    for item in root:
        nite_id = item.get(NITE_ID, "")
        if item.tag != "w":
            owners.append((nite_id, None))
            continue
        said = item.text or ""
        if item.get("punc") == "true":
            joined = timed and bool(spoken)
            if joined:
                spoken[-1] = replace(spoken[-1], w=spoken[-1].w + said)
            owners.append((nite_id, len(spoken) - 1 if joined else None))
            continue
        start, end = float(item.get("starttime", "nan")), float(item.get("endtime", "nan"))
        timed = math.isfinite(start) and math.isfinite(end)
        if timed:
            filler = bare(said) in AMI_FILLERS
            spoken.append(Word(said, round(start * 1000), round(end * 1000), filler, 1.0, speaker))
        owners.append((nite_id, len(spoken) - 1 if timed else None))
    return spoken, owners


def ami_words(archive: Path, meeting: str, start_s: int, end_s: int) -> list[Word]:
    """The meeting's timed words between the two times (`ami_speaker`), with times counted from `start_s`."""
    words: list[Word] = []
    with zipfile.ZipFile(archive) as opened:
        names = sorted(name for name in opened.namelist() if name.startswith(f"words/{meeting}."))
        for name in names:
            spoken, _ = ami_speaker(ElementTree.fromstring(opened.read(name)), name.split(".")[1])
            words += spoken
    offset, last = start_s * 1000, end_s * 1000
    inside = [
        replace(item, s=item.s - offset, e=item.e - offset) for item in words if offset <= item.s and item.e <= last
    ]
    inside.sort(key=lambda item: (item.s, item.e))
    return inside


def speaker_turns(words: list[Word]) -> list[Span]:
    """Each speaker's words joined into turns across gaps under half a second, by start; turns may overlap."""
    turns: list[list[Any]] = []
    open_turn: dict[str, int] = {}
    for item in words:
        at = open_turn.get(item.speaker)
        if at is not None and item.s - turns[at][2] < TURN_GAP_MS:
            turns[at][2] = max(turns[at][2], item.e)
            continue
        open_turn[item.speaker] = len(turns)
        turns.append([item.speaker, item.s, item.e])
    spans = [(speaker, start, end) for speaker, start, end in turns]
    return spans


def exclusive_turns(words: list[Word]) -> list[Span]:
    """The turns with no overlap, as pyannote's `exclusiveDiarization` gives them: where two speak at once, the one
    who started last holds the floor."""
    holder: dict[int, str] = {}
    for speaker, start, end in speaker_turns(words):
        for frame in range(start // FRAME_MS, end // FRAME_MS):
            holder[frame] = speaker
    runs: list[list[Any]] = []
    for frame in sorted(holder):
        if runs and runs[-1][0] == holder[frame] and runs[-1][2] == frame * FRAME_MS:
            runs[-1][2] += FRAME_MS
            continue
        runs.append([holder[frame], frame * FRAME_MS, (frame + 1) * FRAME_MS])
    spans = [(speaker, start, end) for speaker, start, end in runs]
    return spans


def vendor_inputs(truth: list[Word], seed: str | None) -> Answers:
    """An AssemblyAI `GET /v2/transcript` body and a pyannote output made from the truth, with the errors above drawn
    from `seed`; with no seed, only the one-stream rule applies. AssemblyAI's speakers are AMI's letters, which the
    merge reads as no name."""
    rng = random.Random(seed)
    scale = 0.0 if seed is None else 1.0
    words = []
    heard_until = 0  # AssemblyAI hears one stream: each word starts after the one before it ends, in true order
    for item in truth:
        if rng.random() < MISSED_WORDS * scale:
            continue
        start = max(heard_until, round(item.s + rng.gauss(0, WORD_JITTER_MS * scale)))
        if rng.random() < EXTRA_WORDS * scale:
            again = item.w.rstrip(PUNCTUATION) or item.w
            middle = start + max(1, (item.e - item.s) // 2)
            words.append({"text": again, "start": start, "end": middle, "confidence": 1.0, "speaker": item.speaker})
            start = middle
        end = max(start + 1, round(item.e + rng.gauss(0, WORD_JITTER_MS * scale)))
        words.append({"text": item.w, "start": start, "end": end, "confidence": 1.0, "speaker": item.speaker})
        heard_until = end
    last_end = max((item.e for item in truth), default=0)
    transcript = {"language_code": "en", "audio_duration": last_end / 1000, "words": words}
    turns = exclusive_turns(truth)
    labels = {speaker: f"SPEAKER_{n:02d}" for n, speaker in enumerate(dict.fromkeys(turn[0] for turn in turns))}
    diarization = []
    for speaker, start, end in turns:
        if end - start < SHORTEST_TURN_MS * scale:
            continue
        others = [other for other in labels if other != speaker]
        heard_as = rng.choice(others) if others and rng.random() < SWAPPED_TURNS * scale else speaker
        shifted_start = max(0, round(start + rng.gauss(0, TURN_SHIFT_MS * scale)))
        shifted_end = max(shifted_start + FRAME_MS, round(end + rng.gauss(0, TURN_SHIFT_MS * scale)))
        diarization.append({"speaker": labels[heard_as], "start": shifted_start / 1000, "end": shifted_end / 1000})
    output = {"exclusiveDiarization": diarization}
    return transcript, output


def best_mapping(gain: Mapping[tuple[str, str], float]) -> dict[str, str]:
    """Hypothesis speaker -> true speaker, one to one, with the largest total gain. Every pairing is tried, which is
    cheap for a meeting's handful of speakers."""
    hyps = sorted({hyp for hyp, _ in gain})
    refs = sorted({ref for _, ref in gain})
    if len(hyps) >= len(refs):
        pairings = [list(zip(chosen, refs, strict=True)) for chosen in permutations(hyps, len(refs))]
    else:
        pairings = [list(zip(hyps, chosen, strict=True)) for chosen in permutations(refs, len(hyps))]
    best = max(pairings, key=lambda pairing: sum(gain.get(pair, 0.0) for pair in pairing))
    mapping = dict(best)
    return mapping


def frames(spans: list[Span]) -> dict[int, set[str]]:
    found: dict[int, set[str]] = {}
    for speaker, start, end in spans:
        for frame in range(start // FRAME_MS, end // FRAME_MS):
            found.setdefault(frame, set()).add(speaker)
    return found


def der(ref: list[Span], hyp: list[Span]) -> float:
    """Diarization error rate: the time with speech missed, speech added, or the wrong speaker, over the true speech
    time, with speakers paired for the least error. Overlapping speech counts once per speaker."""
    truth, heard_frames = frames(ref), frames(hyp)
    gain = Counter((h, r) for frame, said in heard_frames.items() for h in said for r in truth.get(frame, ()))
    mapped = best_mapping(gain)
    errors = 0
    for frame in truth.keys() | heard_frames.keys():
        expected = truth.get(frame, set())
        got = heard_frames.get(frame, set())
        right = len({mapped.get(h) for h in got} & expected)
        errors += max(len(expected), len(got)) - right
    speech = sum(len(said) for said in truth.values())
    rate = share(errors, speech)
    return rate


def align(truth: list[Word], hyp: list[Word]) -> list[tuple[int, int]]:
    """(truth index, hypothesis index) of the words both have, matched on their letters."""
    matcher = SequenceMatcher(None, [bare(item.w) for item in truth], [bare(item.w) for item in hyp], autojunk=False)
    blocks = matcher.get_matching_blocks()
    pairs = [(block.a + k, block.b + k) for block in blocks for k in range(block.size)]
    return pairs


def aligned(
    groups: list[list[Word]], truth: list[Word], hyp: list[Word], pairs: list[tuple[int, int]]
) -> list[list[Word]]:
    """Each group of hypothesis words as the true words they matched."""
    to_truth = {j: i for i, j in pairs}
    index = {id(item): j for j, item in enumerate(hyp)}
    matched = [[truth[to_truth[index[id(item)]]] for item in group if index[id(item)] in to_truth] for group in groups]
    return matched


def speaker_words(truth: list[Word], hyp: list[Word], pairs: list[tuple[int, int]]) -> float:
    """The share of true words heard with the right speaker, speakers paired by the words they share."""
    gain = Counter((hyp[j].speaker, truth[i].speaker) for i, j in pairs)
    mapped = best_mapping(gain)
    right = sum(mapped.get(hyp[j].speaker) == truth[i].speaker for i, j in pairs)
    accuracy = share(right, len(truth))
    return accuracy


def boundary_errors(truth: list[Word], hyp: list[Word], pairs: list[tuple[int, int]]) -> tuple[float, float]:
    """The median and 95th percentile, in ms, of how far matched words' starts and ends are from the truth."""
    starts = [float(abs(hyp[j].s - truth[i].s)) for i, j in pairs]
    errors = starts + [float(abs(hyp[j].e - truth[i].e)) for i, j in pairs]
    if not errors:
        return 0.0, 0.0
    median = float(statistics.median(errors))
    p95 = float(percentile(errors, 95))
    return median, p95


def line_breaks(truth: list[Word], hyp: list[Word], hyp_lines: list[list[Word]], pairs: list[tuple[int, int]]) -> float:
    """F1 of where our lines break against where the line rule breaks the true transcript."""
    truth_index = {id(item): i for i, item in enumerate(truth)}
    expected = {truth_index[id(line[-1])] for line in lines(truth)[:-1]}
    ends = aligned([[line[-1]] for line in hyp_lines[:-1]], truth, hyp, pairs)
    hits = sum(bool(end) and truth_index[id(end[0])] in expected for end in ends)
    score = f1(share(hits, len(ends), 100.0), share(hits, len(expected), 100.0))
    return score


def mixed_lines(truth: list[Word], hyp: list[Word], hyp_lines: list[list[Word]], pairs: list[tuple[int, int]]) -> float:
    """The share of our lines holding words of more than one true speaker."""
    heard_lines = aligned(hyp_lines, truth, hyp, pairs)
    mixed = sum(len({item.speaker for item in line}) > 1 for line in heard_lines)
    rate = share(mixed, len(hyp_lines))
    return rate


def filler_scores(truth: list[Word], hyp: list[Word], pairs: list[tuple[int, int]]) -> tuple[float, float]:
    """Precision (flagged words that are true fillers) and recall (true fillers flagged)."""
    flagged = sum(item.filler for item in hyp)
    fillers = sum(item.filler for item in truth)
    both = sum(hyp[j].filler and truth[i].filler for i, j in pairs)
    precision = share(both, flagged, 100.0)
    recall = share(both, fillers, 100.0)
    return precision, recall


def offered_pauses(words: list[Word]) -> list[tuple[int, int]]:
    """A6 as `cleanup.ts` runs it on English: each pair of words next to each other in reading order, neither a
    filler, with a gap of a second or more, as (end of the first, start of the second)."""
    found = [(a.e, b.s) for a, b in pairwise(words) if not (a.filler or b.filler) and b.s - a.e >= PAUSE_MS]
    return found


def silences(words: list[Word]) -> list[tuple[int, int]]:
    """The true pauses: a second or more in which nobody speaks, with no filler at either side."""
    found = []
    previous: Word | None = None
    until = 0
    for item in sorted(words, key=lambda said: said.s):
        quiet = previous is not None and not previous.filler and not item.filler
        if quiet and item.s - until >= PAUSE_MS:
            found.append((until, item.s))
        until = max(until, item.e)
        previous = item
    return found


def overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def pause_scores(truth: list[Word], hyp: list[Word]) -> tuple[float, float]:
    """F1 of the pauses we offer against the true ones, and the share of our pause cuts that would clip a true
    word."""
    expected, offered = silences(truth), offered_pauses(hyp)
    precision = share(sum(overlaps(*pause, expected) for pause in offered), len(offered), 100.0)
    recall = share(sum(overlaps(*pause, offered) for pause in expected), len(expected), 100.0)
    spoken = [(item.s, item.e) for item in truth]
    clipped = sum(overlaps(start + PAUSE_KEEP_MS, end - PAUSE_KEEP_MS, spoken) for start, end in offered)
    found = f1(precision, recall)
    clipped_rate = share(clipped, len(offered))
    return found, clipped_rate


def row_fits(items: list[Word]) -> bool:
    chars = sum(len(item.w) for item in items) + len(items) - 1
    return chars <= ROW_CHARS and len(items) <= ROW_WORDS


def fits(items: list[Word]) -> bool:
    """C1 step 4: one row, or two rows split somewhere."""
    split = any(row_fits(items[:k]) and row_fits(items[k:]) for k in range(1, len(items)))
    return row_fits(items) or split


def captions(words: list[Word]) -> list[list[Word]]:
    """C1 steps 2 and 3: fillers skipped, and a caption closed on a new speaker, a 700 ms gap, a sentence end, past
    7 s, or when the next word doesn't fit in two rows."""
    out: list[list[Word]] = []
    shown: list[Word] = []
    for item in words:
        if item.filler:
            continue
        if shown:
            last = shown[-1]
            new_speaker = item.speaker != last.speaker
            gap = item.s - last.e >= CAPTION_GAP_MS
            too_long = item.e - shown[0].s > LONGEST_MS
            if new_speaker or gap or last.w.endswith(CAPTION_END) or too_long or not fits([*shown, item]):
                out.append(shown)
                shown = []
        shown.append(item)
    if shown:
        out.append(shown)
    return out


def caption_violations(
    truth: list[Word], hyp: list[Word], groups: list[list[Word]], pairs: list[tuple[int, int]]
) -> float:
    """The share of captions that show two true speakers at once or run across a true 700 ms pause."""
    wrong = 0
    for heard_words in aligned(groups, truth, hyp, pairs):
        ordered = sorted(heard_words, key=lambda item: item.s)
        two_speakers = len({item.speaker for item in ordered}) > 1
        across_pause = any(b.s - a.e >= CAPTION_GAP_MS for a, b in pairwise(ordered))
        wrong += two_speakers or across_pause
    rate = share(wrong, len(groups))
    return rate


def merged_words(answers: Answers) -> tuple[list[Word], list[list[Word]]]:
    transcript, output = answers
    merged = merge(transcript, output.get("exclusiveDiarization") or [], "en")
    hyp = [item for line in merged.lines for item in line]
    return hyp, merged.lines


def score(truth: list[Word], answers: Answers) -> dict[str, float]:
    """Every replay metric for one excerpt, our real merge run on the vendors' answers."""
    hyp, hyp_lines = merged_words(answers)
    pairs = align(truth, hyp)
    median, p95 = boundary_errors(truth, hyp, pairs)
    precision, recall = filler_scores(truth, hyp, pairs)
    pause_f1, clipped = pause_scores(truth, hyp)
    ref_spans = [(item.speaker, item.s, item.e) for item in truth]
    hyp_spans = [(item.speaker, item.s, item.e) for item in hyp]
    scores = {
        "speaker words %": speaker_words(truth, hyp, pairs),
        "merge DER %": der(ref_spans, hyp_spans),
        "word time median ms": median,
        "word time p95 ms": p95,
        "line breaks F1 %": line_breaks(truth, hyp, hyp_lines, pairs),
        "mixed-speaker lines %": mixed_lines(truth, hyp, hyp_lines, pairs),
        "filler precision %": precision,
        "filler recall %": recall,
        "pause F1 %": pause_f1,
        "pause cuts into speech %": clipped,
        "caption violations %": caption_violations(truth, hyp, captions(hyp), pairs),
    }
    rounded = {name: round(value, 2) for name, value in scores.items()}
    return rounded


def by_speaker(words: list[Word]) -> dict[str, str]:
    said: dict[str, list[str]] = {}
    for item in words:
        said.setdefault(item.speaker, []).append(item.w)
    joined = {speaker: " ".join(spoken) for speaker, spoken in said.items()}
    return joined


def cp_wer(truth: list[Word], hyp: list[Word]) -> float:
    """Each speaker's words as one text, speakers paired for the fewest word errors, over the true word count; a
    speaker with no pair counts all their words as errors."""
    refs, hyps = by_speaker(truth), by_speaker(hyp)
    ref_counts = {speaker: len(WORD.findall(said.lower())) for speaker, said in refs.items()}
    hyp_counts = {speaker: len(WORD.findall(said.lower())) for speaker, said in hyps.items()}
    saved: dict[tuple[str, str], float] = {}
    for h, heard_text in hyps.items():
        for r, said in refs.items():
            errors = round(word_error_rate(said, heard_text) * ref_counts[r])
            saved[(h, r)] = ref_counts[r] + hyp_counts[h] - errors
    mapped = best_mapping(saved)
    total = sum(ref_counts.values())
    errors = total + sum(hyp_counts.values()) - sum(saved[pair] for pair in mapped.items())
    rate = share(errors, total)
    return rate


def live_score(excerpt: Excerpt, elapsed_s: float, answers: Answers) -> dict[str, float]:
    """The replay metrics plus what only real vendors show."""
    transcript, output = answers
    hyp, _ = merged_words(answers)
    reference = " ".join(item.w for item in excerpt.truth)
    said = " ".join(item["text"] for item in transcript.get("words") or [])
    turns = output.get("exclusiveDiarization") or []
    diarized = [(turn["speaker"], round(turn["start"] * 1000), round(turn["end"] * 1000)) for turn in turns]
    scores = {
        **score(excerpt.truth, answers),
        "WER %": round(100 * word_error_rate(reference, said), 2),
        "cpWER %": round(cp_wer(excerpt.truth, hyp), 2),
        "DER %": round(der(speaker_turns(excerpt.truth), diarized), 2),
        "s per 10 min": round(elapsed_s * TEN_MINUTES_MS / excerpt.ms, 2),
        # Vendors don't return the charge, so this is the list price; the vendors' dashboards are the check.
        "$ per audio hour": PRICE_PER_HOUR,
    }
    return scores


type Transcribe = Callable[[Excerpt], Awaitable[tuple[float, dict[str, Any], dict[str, Any]]]]


async def live(excerpts: list[Excerpt], transcribe: Transcribe, ledger: Path, limit: float, recorded: Path) -> Rows:
    """Each excerpt through the vendors while the ledger, this run and the excerpt's list price stay within `limit`
    US$. An excerpt is charged before it is sent, since the vendors bill a job even if this run dies after."""
    before = ledger_total(ledger)
    spent, sent = 0.0, 0
    rows: Rows = {}
    note = "crashed"
    try:
        for excerpt in excerpts:
            cost = excerpt.ms / HOUR_MS * PRICE_PER_HOUR
            if before + spent + cost > limit:
                note = "budget reached"
                break
            spent += cost
            sent += 1
            elapsed, transcript, output = await transcribe(excerpt)
            save_answers(recorded / excerpt.name, transcript, output)
            rows[f"{excerpt.name} live"] = live_score(excerpt, elapsed, (transcript, output))
        else:
            note = "ok"
    finally:
        append_ledger(ledger, spent, sent, "eval-transcript-live", note)
        print(f"$ this run: {spent:.4f}; ledger total: {before + spent:.4f}")
    return rows


def fetch(entry: dict[str, Any], cache: Path | None = None) -> Path:
    """The manifest entry's file in `cache` (the AMI cache unless given), downloaded if missing; a file whose SHA-256
    differs is refused."""
    cache = cache or CACHE
    path = cache / entry["file"]
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        print(f"downloading {entry['url']}")
        urllib.request.urlretrieve(entry["url"], partial)
        partial.rename(path)
    with path.open("rb") as opened:
        digest = hashlib.file_digest(opened, "sha256").hexdigest()
    if digest != entry["sha256"]:
        raise SystemExit(f"{path}: checksum {digest} is not the manifest's {entry['sha256']}; delete it and run again.")
    return path


def excerpts_of(manifest: dict[str, Any]) -> list[Excerpt]:
    archive = fetch(manifest["annotations"])
    made = []
    for entry in manifest["excerpts"]:
        start, end = entry["start_s"], entry["end_s"]
        truth = ami_words(archive, entry["meeting"], start, end)
        name = f"{entry['meeting']}-{start}"
        made.append(Excerpt(name, start * 1000, (end - start) * 1000, truth, CACHE / entry["file"]))
    return made


def table(rows: Rows) -> str:
    names = list(rows)
    metrics = list(dict.fromkeys(metric for row in rows.values() for metric in row))
    width = max(len(name) for name in names) + 2
    header = "metric".ljust(26) + "".join(name.rjust(width) for name in names)
    body = [
        metric.ljust(26)
        + "".join(
            f"{rows[name][metric]:.2f}".rjust(width) if metric in rows[name] else "-".rjust(width) for name in names
        )
        for metric in metrics
    ]
    printed = "\n".join([header, *body])
    return printed


def regressions(baseline: Rows, results: Rows, higher: Mapping[str, bool] = HIGHER) -> list[str]:
    """Every baseline metric this run made worse, and every baseline row it lacks; `higher` says which way is
    better."""
    found = []
    for row, metrics in baseline.items():
        now = results.get(row)
        if now is None:
            found.append(f"{row}: missing")
            continue
        for metric, before in metrics.items():
            after = now.get(metric)
            if after is None:
                found.append(f"{row}: {metric} missing")
                continue
            worse = after < before if higher[metric] else after > before
            if worse:
                found.append(f"{row}: {metric} {before:.2f} -> {after:.2f}")
    return found


def write(path: Path, rows: Rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def averaged(rows: Rows) -> dict[str, float]:
    """Each metric's mean over the rows, to two decimals: the replay's `all` column."""
    first = next(iter(rows.values()))
    return {metric: round(statistics.fmean(row[metric] for row in rows.values()), 2) for metric in first}


def replay(save_baseline: bool) -> int:
    excerpts = excerpts_of(json.loads(MANIFEST.read_text()))
    results: Rows = {}
    for excerpt in excerpts:
        results[excerpt.name] = score(excerpt.truth, vendor_inputs(excerpt.truth, f"{excerpt.name}:{SEED}"))
    results["all"] = averaged(results)
    for excerpt in excerpts:
        folder = RECORDED / excerpt.name
        if folder.is_dir():
            transcript = json.loads((folder / "assemblyai.json").read_text())
            output = json.loads((folder / "pyannote.json").read_text())
            results[f"{excerpt.name} recorded"] = score(excerpt.truth, (transcript, output))
    write(OUT, results)
    print(table(results))
    print(f"results: {OUT}")
    if not BASELINE.exists():
        write(BASELINE, results)
        print(f"baseline recorded: {BASELINE}")
        return 0
    worse = regressions(json.loads(BASELINE.read_text()), results)
    if save_baseline and worse:
        raise SystemExit("Not saved, worse than the baseline: " + "; ".join(worse))
    if save_baseline:
        write(BASELINE, results)
        print(f"baseline saved: {BASELINE}")
        return 0
    if worse:
        print("worse than the baseline: " + "; ".join(worse))
        return 1
    print("no metric worse than the baseline")
    return 0


def cut(excerpt: Excerpt) -> Path:
    """The excerpt's stretch of the meeting's WAV as its own file in the cache."""
    clip = CACHE / f"{excerpt.name}.wav"
    with wave.open(str(excerpt.audio), "rb") as source, wave.open(str(clip), "wb") as out:
        rate = source.getframerate()
        source.setpos(excerpt.start_ms * rate // 1000)
        out.setparams(source.getparams())
        out.writeframes(source.readframes(excerpt.ms * rate // 1000))
    return clip


def run_live(budget_usd: float | None, ledger: Path) -> int:
    budget_usd = require_budget(budget_usd, "The live tier")
    for key in ("ASSEMBLYAI_API_KEY", "PYANNOTE_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"Set {key}: the live tier needs both vendors (docs/cloud.md).")
    config = load(os.environ).cloud
    if config is None or not config.assemblyai_key or not config.pyannote_key:
        raise SystemExit("Set DATABASE_URL and S3_BUCKET: the vendors fetch the audio from the bucket.")
    manifest = json.loads(MANIFEST.read_text())
    excerpts = excerpts_of(manifest)
    for entry in manifest["excerpts"]:
        fetch(entry)

    async def both() -> Rows:
        storage = Storage(s3_client(config), config.bucket)
        strategy = AssemblyAiPyannoteStrategy(config.assemblyai_key, config.pyannote_key, storage)

        async def transcribe(excerpt: Excerpt) -> tuple[float, dict[str, Any], dict[str, Any]]:
            print(excerpt.name)
            answered = await heard(strategy, storage, cut(excerpt), "en")
            return answered

        try:
            rows = await live(excerpts, transcribe, ledger, budget_usd, RECORDED)
        finally:
            await strategy.aclose()
        return rows

    rows = asyncio.run(both())
    if rows:
        print(table(rows))
        write(OUT.with_name("eval-transcript-live.json"), rows)
    over_cap = [name for name, row in rows.items() if row["s per 10 min"] > CAP_S]
    if over_cap:
        print(f"over the {CAP_S} s cap: " + ", ".join(over_cap))
    finished = len(rows) == len(excerpts)
    if not finished:
        print(f"budget reached after {len(rows)} of {len(excerpts)} excerpts")
    return 0 if finished and not over_cap else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save-baseline", action="store_true", help="move the baseline to this run if none is worse")
    parser.add_argument("--live", action="store_true", help="the real vendors (costs money; needs --budget-usd)")
    parser.add_argument("--budget-usd", type=float, help="live: send nothing that would take the ledger past this")
    parser.add_argument("--ledger", type=Path, default=LEDGER, help="live: the JSONL spend ledger bench.py shares")
    options = parser.parse_args(argv)
    if options.live:
        return run_live(options.budget_usd, options.ledger)
    return replay(options.save_baseline)


if __name__ == "__main__":
    sys.exit(main())
