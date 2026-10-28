"""`just eval-minutes`: how well the meeting minutes and Ask stay grounded, scored against the AMI Meeting Corpus and
QMSum (MEETINGS.md 7.2 and 10, PRD MOM-05: zero fabricated citations served; docs/bench.md).

`uv run scripts/eval_minutes.py [--save-baseline]` is the replay tier. It is free and calls no model. Each AMI test
meeting (`bench/minutes/manifest.json`, CC BY 4.0) becomes our transcript lines: its true words through the real
line rule (`merge.lines`), each speaker named by their role. The gold is AMI's abstractive summary, its action and
decision sentences, and the dialogue acts each one links to, as lines. Three rows come from it:

- `validator`: real citations of gold evidence lines, and fabrications made from them (the wrong line, an altered
  quote, the wrong speaker, a time outside the line, an unknown line), through the real `keep_derivations`; every
  good one must be kept and every fabrication refused.
- `fake minutes`: the fake provider's meetings mode through the real `minutes.run`, its fabricating first
  action-items answer and that section's one correction, so no fabrication is served.
- `minutes` and `qa`: the model answers the live tier recorded (`bench/minutes/recorded/`, each minutes answer
  with the section it answered), replayed through the real `minutes.run` and the real Ask (`qa.candidates`, then one
  `qa.answer`), so a change to the validator or the scorers is measured without a model call. Q&A runs on QMSum's
  Product test split (MIT; fetched once into `~/.snapbak-bench/qmsum/`).
- `probes`, `contradictions` and `hinglish`: hand-made meetings (`bench/minutes/slices.json`) replayed the same
  way. A probe has nothing in it (a mic check, fillers, small talk), so every item its minutes hold counts against
  it; a contradiction counts when an item cites both of its gold lines; the Hinglish meetings have gold actions
  and decisions, and their details count as any meeting's.

Each minutes row scores, per section, recall and precision against the gold (actions against action items and
follow-ups, decisions, AMI's problems against risks and open questions), the share of the transcript's details the
minutes kept (`services/details.py`), the agenda topics with a `### ` section, citation precision before the
validator and fabrications served after it (always 0), and the sections left incomplete.

The first run records `bench/minutes/baseline.json`; a run worse than it on any metric, or one that misses a gate in
`GATES`, fails, and `--save-baseline` moves the baseline forward when no metric got worse.

`... --live --budget-usd USD [--ledger PATH] [--limit N]` asks the server's polish model (`SNAPBAK_POLISH`, built as
the worker builds it, without the fallback so one engine is measured) for each meeting's minutes and each QMSum
question's answer, charges every call to the ledger `bench.py` uses, records the answers for the replay tier and
scores them the same way. It refuses to start without the OpenAI key and a budget, the most the ledger may reach.

`... --private DIR --budget-usd USD [--report PATH]` runs the live minutes on meetings exported to DIR, a folder
outside the repository (`~/.snapbak-bench/private/`), and writes the owner's report, which quotes their meeting,
to PATH (DIR/private-eval.md by default). Only metrics reach the terminal.

Nothing here prints a quote, a summary or a transcript line: only meeting IDs and metrics.
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from xml.etree import ElementTree

from eval_transcript import MANIFEST as AMI_MANIFEST
from eval_transcript import NITE_ID, ami_speaker, fetch, regressions, share, table, write

from bench import Budget, Metered, append_ledger, ledger_total, require_budget
from snapbak_backend.ai.chain import ProviderFailed, Tiers
from snapbak_backend.ai.providers.base import Provider, ProviderError, Turn, Usage
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.config.pricing import PRICES
from snapbak_backend.config.settings import EMBED, load
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import AskResponse, DerivationOut, MinutesOut
from snapbak_backend.services import details, meeting_index, minutes, people, qa, recordings
from snapbak_backend.services.embeddings import Embeddings, make_embeddings
from snapbak_backend.services.minutes import RawDerivation
from snapbak_backend.transcription.merge import Word, lines

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "bench" / "minutes" / "manifest.json"
BASELINE = ROOT / "bench" / "minutes" / "baseline.json"
RECORDED = ROOT / "bench" / "minutes" / "recorded"
# The hand-made slices: no-content probes, contradictions and Hinglish meetings, each with its gold.
SLICES = ROOT / "bench" / "minutes" / "slices.json"
QMSUM_CACHE = Path.home() / ".snapbak-bench" / "qmsum"
OUT = Path.home() / ".snapbak-bench" / "eval-minutes.json"
LEDGER = Path.home() / ".snapbak-bench" / "spend.jsonl"
SEED = 5
# Live calls in flight at once; the budget is checked before each item, so a run can overshoot by these.
CONCURRENCY = 4

# AMI's roles, as the meetings name them (QMSum spells them the same way).
ROLES = {"PM": "Project Manager", "ID": "Industrial Designer", "UI": "User Interface", "ME": "Marketing"}
GOLD_KINDS = ("abstract", "actions", "decisions", "problems")
HREF = re.compile(r"#id\(([^)]+)\)(?:\.\.id\(([^)]+)\))?")
MARKER = re.compile(r"\{\w+\}")
NITE_CHILD = "{http://nite.sourceforge.net/}child"

# The truth table: up to this many gold evidence lines per meeting, each with at least this many words.
CASES_PER_MEETING = 15
CASE_WORDS = 4
FABRICATIONS = ("wrong line", "altered quote", "wrong speaker", "time outside", "unknown line")
# Words swapped into a quote to make it one nobody said.
DECOYS = ("purple", "tuesday", "seventeen", "giraffe")

# A gold action or decision is covered when one of our items of the same kind shares this much of its content words
# (`overlap_f1`): deterministic and free, so the replay tier scores it too. Chosen before the first live run.
COVERED = 30.0
STOPWORDS = frozenset(
    "a an the and or but of to for in on at by with from about into as is are was were be been being will would "
    "should could can may might must shall do does did done have has had it its this that these those there here "
    "they them their we our us you your he she his her him i me my not no so then than also just very".split()
)

# Metric -> higher is better.
HIGHER = {
    "cases": True,
    "good kept %": True,
    **{f"{kind} refused %": True for kind in FABRICATIONS},
    "citation precision %": True,
    "served fabricated": False,
    "token validity %": True,
    "served bad tokens": False,
    "claims kept %": True,
    "gold items cited %": True,
    "action coverage %": True,
    "decision coverage %": True,
    "action precision %": True,
    "decision precision %": True,
    "problem coverage %": True,
    "problem precision %": True,
    "detail recall %": True,
    "commitments covered %": True,
    "topics covered %": True,
    "probe items": False,
    "invalid due dates": False,
    "contradiction recall %": True,
    "contradiction precision %": True,
    "sections incomplete %": False,
    "summary ROUGE-L %": True,
    "repaired %": False,
    "failed %": False,
    "citations in span %": True,
    "answer ROUGE-L %": True,
    "not in meeting %": False,
    "retrieval hit %": True,
}

# What each row must reach, whatever the baseline says (docs/bench.md, "Minutes and Q&A evals").
type Gate = tuple[str, float]
GATES: dict[str, dict[str, Gate]] = {
    "validator": {"good kept %": (">=", 100.0), **{f"{kind} refused %": (">=", 100.0) for kind in FABRICATIONS}},
    "fake minutes": {"served fabricated": ("<=", 0.0)},
    # Below the lower of two live runs on 25 Oct 2026, after the Q&A retrieval rewrite (gpt-6-luna), by a margin for
    # the model's run-to-run spread.
    # Minutes v2 (one call per section and the detail guard), set below the measured runs by a margin for the model's
    # run-to-run spread. Action and decision coverage: two hybrid runs on 26 Oct 2026 after the one-item-per-task
    # and one-item-per-decision prompts (83.33 and 86.67; 40.43 and 48.94; before them 73.33 to 86.67 and 20.21 to
    # 31.91). The detail and commitment gates are run 4's, the first with one commitment per line (73.69, 98.08).
    "minutes": {
        "served fabricated": ("<=", 0.0),
        # Every people token served names a speaker (D41); the model's own tokens, before the correction turn.
        "served bad tokens": ("<=", 0.0),
        "token validity %": (">=", 95.0),
        "citation precision %": (">=", 90.0),
        "gold items cited %": (">=", 85.0),
        "action coverage %": (">=", 75.0),
        "decision coverage %": (">=", 35.0),
        "problem coverage %": (">=", 12.0),
        # Floors under precision, so flooding a section with items fails a gate too (grading 4): below the lowest
        # of runs 3 to 5 and the two hybrid runs (action 17.54 to 24.21, decision 16.04 to 22.71, problem 4.33 to 5).
        "action precision %": (">=", 15.0),
        "decision precision %": (">=", 14.0),
        "problem precision %": (">=", 4.0),
        "summary ROUGE-L %": (">=", 14.0),
        "detail recall %": (">=", 65.0),
        "commitments covered %": (">=", 95.0),
        "topics covered %": (">=", 70.0),
        "sections incomplete %": ("<=", 2.0),
        "invalid due dates": ("<=", 0.0),
        # D39's at least 99 % unattended: a meeting the live run serves no minutes counts against it.
        "failed %": ("<=", 1.0),
    },
    # A meeting with nothing in it gives empty sections (the evidence rule): not one item.
    "probes": {"served fabricated": ("<=", 0.0), "probe items": ("<=", 0.0), "failed %": ("<=", 0.0)},
    "contradictions": {
        "served fabricated": ("<=", 0.0),
        "contradiction recall %": (">=", 100.0),
        "contradiction precision %": (">=", 100.0),
    },
    "hinglish": {
        "served fabricated": ("<=", 0.0),
        "invalid due dates": ("<=", 0.0),
        "action coverage %": (">=", 100.0),
        "decision coverage %": (">=", 100.0),
        "commitments covered %": (">=", 100.0),
    },
    # MOM-04 and MOM-06 (MEETINGS.md 8): ten sequential section calls of a ~30 min AMI meeting must land inside
    # what the 3 min stop-to-minutes budget leaves after the transcript (about 40 s at P95), the first one fast;
    # a question and a search answer inside 5 s. Live runs only.
    "speed": {
        "minutes p95 ms": ("<=", 140_000.0),
        "first section p95 ms": ("<=", 10_000.0),
        "qa p95 ms": ("<=", 5_000.0),
        "search p95 ms": ("<=", 5_000.0),
        # The 20 AMI meetings are five four-part series on one design task, so a query from one part's decisions
        # finds a sibling part as often as its own: recall counts the series, at the bar first set, 80 % (own-meeting
        # recall is reported beside it, ungated: 40 and 55 % on two live runs, 25 Oct 2026).
        "search series recall@5 %": (">=", 80.0),
        # The founder's 3 s recording (one speaker, two lines) ends ready or failed with a code, never stuck.
        "tiny meeting ended": (">=", 1.0),
    },
    # Questions about the meeting as a whole (the founder's Ask bug, 26 Oct 2026): an answerable one is refused
    # "Not in this meeting." at most 2 % of the time, and nothing fabricated is ever served.
    "meta": {"wrongly not in meeting %": ("<=", 2.0), "served fabricated": ("<=", 0.0)},
    "qa": {
        "served fabricated": ("<=", 0.0),
        "citations in span %": (">=", 50.0),
        "answer ROUGE-L %": (">=", 14.0),
        "not in meeting %": ("<=", 25.0),
        "retrieval hit %": (">=", 80.0),
        "failed %": ("<=", 5.0),
    },
}

type Counts = dict[str, float]
type Rows = dict[str, dict[str, float]]


@dataclass(frozen=True)
class Gold:
    kind: str  # one of GOLD_KINDS
    text: str
    lines: frozenset[int]  # the idx of the lines its linked dialogue acts cover


@dataclass(frozen=True)
class Meeting:
    name: str
    lines: list[TranscriptLine]
    speakers: dict[UUID, Speaker]
    gold: list[Gold]
    slice: str = "minutes"  # the row it is scored in: minutes (AMI), probes, contradictions, hinglish or private


@dataclass(frozen=True)
class Question:
    query: str
    answer: str
    spans: tuple[tuple[int, int], ...]  # QMSum's relevant turns, inclusive


@dataclass(frozen=True)
class QMeeting:
    name: str
    lines: list[TranscriptLine]
    speakers: dict[UUID, Speaker]
    questions: list[Question]


def made_speaker(meeting: str, label: str, name: str) -> Speaker:
    """A speaker as the backend stores one: labelled "Speaker N" as diarization numbers them (so the minutes can write
    their `{{p:N}}`), the corpus's own letter kept as the vendor's label, named `name`."""
    numbered = f"Speaker {ord(label) - ord('A') + 1}" if len(label) == 1 and label.isupper() else label
    made = Speaker(
        id=uuid5(NAMESPACE_URL, f"{meeting}/speaker/{label}"),
        recording_id=uuid5(NAMESPACE_URL, meeting),
        label=numbered,
        vendor_label=label,
        name=name,
    )
    return made


def made_line(meeting: str, idx: int, speaker: Speaker, start_ms: int, end_ms: int, text: str) -> TranscriptLine:
    made = TranscriptLine(
        id=uuid5(NAMESPACE_URL, f"{meeting}/line/{idx}"),
        recording_id=speaker.recording_id,
        speaker_id=speaker.id,
        idx=idx,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        words=[],
    )
    return made


def roles(opened: zipfile.ZipFile, meeting: str) -> dict[str, str]:
    """Each speaker letter's role name, from `corpusResources/meetings.xml`; a letter with no role keeps itself."""
    root = ElementTree.fromstring(opened.read("corpusResources/meetings.xml"))
    found = {}
    for entry in root.iter("meeting"):
        if entry.get("observation") != meeting:
            continue
        for speaker in entry.iter("speaker"):
            letter = speaker.get("nxt_agent", "")
            found[letter] = ROLES.get(speaker.get("role", ""), letter)
    return found


def word_spans(href: str) -> tuple[str, str, str]:
    """A NITE child pointer as (file, first id, last id)."""
    found = HREF.search(href)
    if found is None:
        raise ValueError(f"not a NITE pointer: {href}")
    first, last = found.group(1), found.group(2) or found.group(1)
    return href.split("#")[0], first, last


def empty_gold(text: str) -> bool:
    """AMI writes "NA." or "None." where a meeting had no action or decision."""
    return re.fullmatch(r"\W*(n/?a|none)\W*", text, re.IGNORECASE) is not None


def ami_meeting(archive: Path, name: str) -> Meeting:
    """One AMI meeting as our transcript lines with its gold summary sentences and their evidence lines."""
    with zipfile.ZipFile(archive) as opened:
        named = roles(opened, name)
        words: list[Word] = []
        ids: dict[str, list[tuple[str, Word | None]]] = {}  # per words file, each element's id and its word
        positions: dict[str, dict[str, int]] = {}
        for file in sorted(item for item in opened.namelist() if item.startswith(f"words/{name}.")):
            letter = file.split(".")[1]
            spoken, owners = ami_speaker(ElementTree.fromstring(opened.read(file)), letter)
            words += spoken
            key = file.removeprefix("words/")
            ids[key] = [(nite_id, None if at is None else spoken[at]) for nite_id, at in owners]
            positions[key] = {nite_id: k for k, (nite_id, _) in enumerate(owners)}
        words.sort(key=lambda item: (item.s, item.e))
        grouped = lines(words)
        line_of = {id(item): idx for idx, line in enumerate(grouped) for item in line}

        def covered(href: str) -> set[int]:
            file, first, last = word_spans(href)
            span = ids[file][positions[file][first] : positions[file][last] + 1]
            return {line_of[id(item)] for _, item in span if item is not None}

        acts: dict[str, set[int]] = {}
        for file in (item for item in opened.namelist() if item.startswith(f"dialogueActs/{name}.")):
            for act in ElementTree.fromstring(opened.read(file)).iter("dact"):
                acts[act.get(NITE_ID, "")] = {
                    idx for child in act.iter(NITE_CHILD) for idx in covered(child.get("href", ""))
                }
        evidence: dict[str, set[int]] = {}
        links = f"extractive/{name}.summlink.xml"
        for link in ElementTree.fromstring(opened.read(links)).iter("summlink") if links in opened.namelist() else []:
            pointers = {pointer.get("role"): word_spans(pointer.get("href", ""))[1] for pointer in link}
            evidence.setdefault(pointers["abstractive"], set()).update(acts.get(pointers["extractive"], set()))
        summary = ElementTree.fromstring(opened.read(f"abstractive/{name}.abssumm.xml"))
    gold = [
        Gold(kind, sentence.text or "", frozenset(evidence.get(sentence.get(NITE_ID, ""), set())))
        for kind in GOLD_KINDS
        for section in summary.iter(kind)
        for sentence in section.iter("sentence")
        if not empty_gold(sentence.text or "")
    ]
    speakers = {letter: made_speaker(name, letter, role) for letter, role in named.items()}
    for line in grouped:
        speakers.setdefault(line[0].speaker, made_speaker(name, line[0].speaker, line[0].speaker))
    made = [
        made_line(name, idx, speakers[line[0].speaker], line[0].s, line[-1].e, " ".join(item.w for item in line))
        for idx, line in enumerate(grouped)
    ]
    return Meeting(name, made, {speaker.id: speaker for speaker in speakers.values()}, gold)


def qmsum_meetings(path: Path) -> list[QMeeting]:
    """QMSum's meetings, each turn a line numbered as its `relevant_text_span` counts them, with markers such as
    `{vocalsound}` dropped. Times are made up (a second per turn): Ask checks only a citation's quote."""
    found = []
    for n, row in enumerate(path.read_text(encoding="utf-8").splitlines()):
        said = json.loads(row)
        name = f"qmsum-{n}"
        speakers: dict[str, Speaker] = {}
        made = []
        for idx, turn in enumerate(said["meeting_transcripts"]):
            who = turn["speaker"]
            speaker = speakers.setdefault(who, made_speaker(name, who, who))
            text = " ".join(MARKER.sub(" ", turn["content"]).split())
            made.append(made_line(name, idx, speaker, idx * 1000, idx * 1000 + 999, text))
        questions = [
            Question(item["query"], item["answer"], tuple((int(a), int(b)) for a, b in item["relevant_text_span"]))
            for item in said["specific_query_list"]
        ]
        found.append(QMeeting(name, made, {speaker.id: speaker for speaker in speakers.values()}, questions))
    return found


def tokens(text: str) -> list[str]:
    found = re.findall(r"\w+", text.lower())
    return found


def contains(text: str, quote: str) -> bool:
    """The quote's words appear, whole and in order, in the text: the eval's own check, independent of the
    validator's normalization (English only, which AMI and QMSum are)."""
    said = " ".join(tokens(quote))
    return bool(said) and f" {said} " in f" {' '.join(tokens(text))} "


def rouge_l(hyp: str, ref: str) -> float:
    """ROUGE-L F-measure, in %, of the longest common subsequence of the two texts' lowercase words."""
    a, b = tokens(hyp), tokens(ref)
    if not a or not b:
        return 0.0
    previous = [0] * (len(b) + 1)
    for word in a:
        current = [0]
        for j, other in enumerate(b):
            current.append(previous[j] + 1 if word == other else max(previous[j + 1], current[j]))
        previous = current
    common = previous[-1]
    if not common:
        return 0.0
    precision, recall = common / len(a), common / len(b)
    return 100 * 2 * precision * recall / (precision + recall)


def overlap_f1(ours: str, gold: str) -> float:
    """F1, in %, of the two texts' content words (lowercase, stopwords left out), as sets."""
    a = set(tokens(ours)) - STOPWORDS
    b = set(tokens(gold)) - STOPWORDS
    both = len(a & b)
    if not both:
        return 0.0
    return 100 * 2 * both / (len(a) + len(b))


def line_of(found: list[TranscriptLine], line_id: UUID) -> TranscriptLine | None:
    return next((line for line in found if line.id == line_id), None)


def grounded(meeting: Meeting, derivation: DerivationOut) -> bool:
    """A served derivation names a real line, that line's speaker, a time inside it and words it said."""
    line = line_of(meeting.lines, derivation.lineId)
    if line is None:
        return False
    speaker = recordings.speaker_of(meeting.speakers.get(line.speaker_id))
    inside = line.start_ms <= derivation.atMs <= line.end_ms
    return inside and derivation.speaker == speaker and contains(line.text, derivation.quote)


def cases_of(meeting: Meeting, line: TranscriptLine, names: list[str]) -> list[tuple[str, RawDerivation]]:
    """Good citations of one line, and one fabrication of each kind made from them."""
    who = recordings.speaker_of(meeting.speakers.get(line.speaker_id))
    said = line.text.split()[1:-1]
    quote = " ".join(said)
    middle = (line.start_ms + line.end_ms) // 2
    shouted = re.sub(r"[.,;:!?]", "", quote).upper()  # the case and the sentence marks changed, the words kept
    cases = [
        ("good", RawDerivation(i=line.idx, speaker=who, atMs=line.start_ms, quote=quote)),
        ("good", RawDerivation(i=line.idx, speaker=who, atMs=line.end_ms, quote=quote)),
        ("good", RawDerivation(i=line.idx, speaker=who, atMs=middle, quote=shouted)),
        ("time outside", RawDerivation(i=line.idx, speaker=who, atMs=line.end_ms + 1, quote=quote)),
        ("unknown line", RawDerivation(i=len(meeting.lines), speaker=who, atMs=middle, quote=quote)),
    ]
    if line.start_ms > 0:
        cases.append(("time outside", RawDerivation(i=line.idx, speaker=who, atMs=line.start_ms - 1, quote=quote)))
    others = [other for other in meeting.lines if other.idx != line.idx and not contains(other.text, quote)]
    if others:
        other = min(others, key=lambda item: (item.idx - line.idx) % len(meeting.lines))
        speaker = recordings.speaker_of(meeting.speakers.get(other.speaker_id))
        cases.append(("wrong line", RawDerivation(i=other.idx, speaker=speaker, atMs=other.start_ms, quote=quote)))
    altered = [" ".join([*said[:1], decoy, *said[2:]]) for decoy in DECOYS]
    fake = next((made for made in altered if not contains(line.text, made)), None)
    if fake is not None:
        cases.append(("altered quote", RawDerivation(i=line.idx, speaker=who, atMs=middle, quote=fake)))
    wrong = next((name for name in names if name != who), None)
    if wrong is not None:
        cases.append(("wrong speaker", RawDerivation(i=line.idx, speaker=wrong, atMs=middle, quote=quote)))
    return cases


def truth_cases(meeting: Meeting, rng: random.Random) -> list[tuple[str, RawDerivation]]:
    """The truth table's cases on a sample of the meeting's gold evidence lines."""
    evidence = sorted({idx for item in meeting.gold for idx in item.lines})
    by_index = {line.idx: line for line in meeting.lines}
    long_enough = [by_index[idx] for idx in evidence if len(by_index[idx].text.split()) >= CASE_WORDS]
    chosen = rng.sample(long_enough, min(CASES_PER_MEETING, len(long_enough)))
    names = sorted({recordings.speaker_of(speaker) for speaker in meeting.speakers.values()})
    cases = [case for line in sorted(chosen, key=lambda item: item.idx) for case in cases_of(meeting, line, names)]
    return cases


def validator_row(meetings: list[Meeting], seed: int) -> dict[str, float]:
    """The share of good citations the real validator keeps, and of each kind of fabrication it refuses."""
    rng = random.Random(seed)
    seen: dict[str, int] = {}
    right: dict[str, int] = {}
    # AMI's gold evidence only: the hand-made slices' lines are too short to cut a quote from.
    for meeting in [meeting for meeting in meetings if meeting.slice == "minutes"]:
        by_index = {line.idx: line for line in meeting.lines}
        for kind, entry in truth_cases(meeting, rng):
            kept = bool(minutes.keep_derivations([entry], by_index, meeting.speakers))
            seen[kind] = seen.get(kind, 0) + 1
            right[kind] = right.get(kind, 0) + (kept == (kind == "good"))
    row = {"cases": float(sum(seen.values())), "good kept %": share(right.get("good", 0), seen.get("good", 0))}
    for kind in FABRICATIONS:
        row[f"{kind} refused %"] = share(right.get(kind, 0), seen.get(kind, 0))
    return {metric: round(value, 2) for metric, value in row.items()}


def add(counts: Counts, metric: str, value: float) -> None:
    counts[metric] = counts.get(metric, 0.0) + value


def raw_items(answer: str) -> list[dict[str, object]]:
    """The items of one item section's answer as the model sent them, before the validator."""
    try:
        said = json.loads(answer)
    except ValueError:
        return []
    found = said.get("items") if isinstance(said, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def raw_entries(item: dict[str, object]) -> list[RawDerivation]:
    entries = []
    found = item.get("derivedFrom")
    for entry in found if isinstance(found, list) else []:
        try:
            entries.append(RawDerivation.model_validate(entry))
        except ValueError:
            entries.append(RawDerivation(i=-1, speaker="", atMs=0, quote=""))
    return entries


def items_of(doc: MinutesOut, names: Sequence[str] = tuple(minutes.ITEMS)) -> list[Any]:
    return [item for name in names for item in getattr(doc, name)]


def cited_lines(meeting: Meeting, item: Any) -> set[int]:
    found = {line.idx for entry in item.derivedFrom if (line := line_of(meeting.lines, entry.lineId)) is not None}
    return found


# Each gold kind, and the sections whose items cover it: AMI's problems are our risks and open questions.
COVERS = {"actions": ("actionItems", "followUps"), "decisions": ("decisions",), "problems": ("risks", "openQuestions")}


def minutes_counts(meeting: Meeting, answers: list[Answer], doc: MinutesOut | None) -> Counts:
    """One meeting's tallies: each section's first answer's citations before the validator, the served document's
    after it, and the served document against the gold, its details (`services/details.py`) and its slice."""
    sections = [section for section, _ in answers]
    repaired = any(sections.count(section) > 1 for section in minutes.CALLS)
    counts: Counts = {"meetings": 1.0, "repaired": float(repaired), "failed": float(doc is None)}
    by_index = {line.idx: line for line in meeting.lines}
    firsts: dict[str, str] = {}
    for section, answer in answers:
        firsts.setdefault(section, answer)
    for section, answer in firsts.items():
        for item in raw_items(answer) if section in minutes.ITEMS else []:
            add(counts, "raw claims", 1)
            for entry in raw_entries(item):
                add(counts, "raw citations", 1)
                add(counts, "raw grounded", bool(minutes.keep_derivations([entry], by_index, meeting.speakers)))
    # People tokens (MEETINGS.md D41): those the model wrote that name a speaker of the meeting, and any that reach the
    # served minutes naming no one or written wrong ("{{p:3}", "{{ p:3 }}").
    numbers = {n for speaker in meeting.speakers.values() if (n := people.number_of(speaker)) is not None}
    for answer in firsts.values():
        for found in people.WRITTEN.finditer(answer):
            label, name = found.groups()
            add(counts, "tokens written", 1)
            add(counts, "tokens valid", name is not None or (label is not None and int(label) in numbers))
    if doc:
        for found in people.WRITTEN.finditer(doc.model_dump_json()):
            label, name = found.groups()
            add(counts, "served bad tokens", name is None and (label is None or int(label) not in numbers))
    add(counts, "sections", len(minutes.CALLS))
    add(counts, "sections incomplete", len(doc.incomplete) if doc else len(minutes.CALLS))
    # Scored as the app shows them: each person by their name (D41), so a token never counts against the gold.
    doc = people.rendered_doc(doc, meeting.speakers) if doc else None
    claims = items_of(doc) if doc else []
    served = [entry for claim in claims for entry in claim.derivedFrom]
    add(counts, "served claims", len(claims))
    add(counts, "served fabricated", sum(not grounded(meeting, entry) for entry in served))
    cited = {idx for claim in claims for idx in cited_lines(meeting, claim)}
    abstract = " ".join(item.text for item in meeting.gold if item.kind == "abstract")
    if abstract:
        add(counts, "summaries", 1)
        add(counts, "rouge", rouge_l(doc.summary, abstract) if doc else 0.0)
    for kind, names in COVERS.items():
        ours = [item.text for item in items_of(doc, names)] if doc else []
        golds = [item.text for item in meeting.gold if item.kind == kind]
        add(counts, f"gold {kind}", len(golds))
        add(counts, f"{kind} covered", sum(any(overlap_f1(text, gold) >= COVERED for text in ours) for gold in golds))
        add(counts, f"ours {kind}", len(ours) if golds else 0)
        add(counts, f"{kind} matched", sum(any(overlap_f1(text, gold) >= COVERED for gold in golds) for text in ours))
    for item in meeting.gold:
        if item.kind in COVERS and item.lines:
            add(counts, "gold items", 1)
            add(counts, "gold cited", bool(item.lines & cited))
    if doc:
        units = details.inventory(meeting.lines, meeting.speakers)
        add(counts, "details", len(units))
        left = details.missing(doc, units, meeting.lines)
        add(counts, "details kept", len(units) - len(left))
        add(counts, "commitments", sum(unit.kind == "commitment" for unit in units))
        add(
            counts,
            "commitments kept",
            sum(unit.kind == "commitment" for unit in units)
            - len([unit for unit in left if unit.kind == "commitment"]),
        )
        add(counts, "agenda topics", len(doc.agenda))
        add(counts, "topics with section", len(doc.agenda) - len(details.topics_missing(doc)))
        add(counts, "probe items", len(items_of(doc, [name for name in minutes.ITEMS if name != "agenda"])))
        # What the app's date formatter reads: every YYYY-MM-DD due a real day (26 Oct 2026).
        dues = [item.due for item in items_of(doc, ["actionItems", "followUps"])]
        add(counts, "invalid dues", sum(not minutes.real_date(due) for due in dues))
    clashes = [item.lines for item in meeting.gold if item.kind == "contradiction"]
    found = [cited_lines(meeting, item) for item in doc.contradictions] if doc else []
    add(counts, "gold contradictions", len(clashes))
    add(counts, "contradictions found", sum(any(gold <= lines for lines in found) for gold in clashes))
    # Precision only where the gold says what the contradictions are: AMI has no contradiction gold.
    add(counts, "contradiction items", len(found) if clashes else 0)
    add(counts, "contradictions true", sum(any(gold <= lines for gold in clashes) for lines in found if clashes))
    return counts


def minutes_rates(counts: Counts) -> dict[str, float]:
    """The tallies as the report's metrics; gold with nothing to cite or cover counts as fully cited and covered, and
    a slice with no items to judge counts as fully precise."""
    get = counts.get
    rates = {
        "citation precision %": share(get("raw grounded", 0), get("raw citations", 0)),
        "served fabricated": get("served fabricated", 0),
        # Nothing written is not 100%: it misses the gate as nan, as eval_speakers' n/a does.
        "token validity %": share(get("tokens valid", 0), get("tokens written", 0), math.nan),
        "served bad tokens": get("served bad tokens", 0),
        "claims kept %": share(get("served claims", 0), get("raw claims", 0)),
        "gold items cited %": share(get("gold cited", 0), get("gold items", 0), 100.0),
        "action coverage %": share(get("actions covered", 0), get("gold actions", 0), 100.0),
        "action precision %": share(get("actions matched", 0), get("ours actions", 0), 100.0),
        "decision coverage %": share(get("decisions covered", 0), get("gold decisions", 0), 100.0),
        "decision precision %": share(get("decisions matched", 0), get("ours decisions", 0), 100.0),
        "problem coverage %": share(get("problems covered", 0), get("gold problems", 0), 100.0),
        "problem precision %": share(get("problems matched", 0), get("ours problems", 0), 100.0),
        "summary ROUGE-L %": get("rouge", 0) / get("summaries", 1),
        "detail recall %": share(get("details kept", 0), get("details", 0), 100.0),
        "commitments covered %": share(get("commitments kept", 0), get("commitments", 0), 100.0),
        "topics covered %": share(get("topics with section", 0), get("agenda topics", 0), 100.0),
        "probe items": get("probe items", 0),
        "invalid due dates": get("invalid dues", 0),
        "contradiction recall %": share(get("contradictions found", 0), get("gold contradictions", 0), 100.0),
        "contradiction precision %": share(get("contradictions true", 0), get("contradiction items", 0), 100.0),
        "sections incomplete %": share(get("sections incomplete", 0), get("sections", 0)),
        "repaired %": share(get("repaired", 0), get("meetings", 0)),
        "failed %": share(get("failed", 0), get("meetings", 0)),
    }
    return {metric: round(value, 2) for metric, value in rates.items()}


def qa_counts(
    meeting: QMeeting, question: Question, found: list[TranscriptLine], response: AskResponse | None
) -> Counts:
    """One question's tallies: whether retrieval reached the gold span, where the served citations point, whether
    they are the line's words, and the answer against the gold answer."""

    def in_span(idx: int) -> bool:
        return any(first <= idx <= last for first, last in question.spans)

    counts: Counts = {"questions": 1.0, "failed": float(response is None)}
    add(counts, "retrieved", any(in_span(line.idx) for line in found))
    if response is None:
        return counts
    add(counts, "not in meeting", not response.supported)
    add(counts, "rouge", rouge_l(response.answer, question.answer))
    for citation in response.citations:
        line = line_of(meeting.lines, citation.lineId)
        add(counts, "citations", 1)
        add(counts, "in span", line is not None and in_span(line.idx))
        add(counts, "served fabricated", line is None or not contains(line.text, citation.quote))
    return counts


# Questions about the meeting as a whole (the founder's Ask bug, 26 Oct 2026), English and Hinglish, each with the
# kind of minutes item that answers it.
META = (
    ("What is still undecided?", "undecided"),
    ("Who owns what?", "owners"),
    ("What are the risks?", "risks"),
    ("Summarize the meeting.", "summary"),
    ("Abhi kya decide nahi hua?", "undecided"),
    ("Kaun kya karega?", "owners"),
    ("Risks kya hain?", "risks"),
    ("Meeting ka summary batao.", "summary"),
)
META_SLICES = ("minutes", "hinglish", "private")


def meta_items(doc: MinutesOut, kind: str) -> list[Any]:
    """The minutes' items that answer a meta question of `kind`."""
    if kind == "undecided":
        return [*doc.openQuestions, *(topic for topic in doc.agenda if topic.status != "covered")]
    if kind == "owners":
        return list(doc.actionItems)
    if kind == "risks":
        return list(doc.risks)
    return items_of(doc)


def meta_gold(doc: MinutesOut, kind: str, lines: list[TranscriptLine]) -> tuple[bool, set[int]]:
    """Whether the minutes answer the question, and the lines an answer should cite: those their items cite (any
    line, for a summary)."""
    if kind == "summary":
        return bool(doc.summary), {line.idx for line in lines}
    items = meta_items(doc, kind)
    by_id = {line.id: line.idx for line in lines}
    span = {by_id[entry.lineId] for item in items for entry in item.derivedFrom if entry.lineId in by_id}
    return bool(items), span


def meta_counts(meeting: Meeting, doc: MinutesOut, kind: str, response: AskResponse | None, calls: int) -> Counts:
    """One meta question's tallies: answerable and refused, a second call over the whole transcript, and where the
    served citations point."""
    answerable, span = meta_gold(doc, kind, meeting.lines)
    supported = response is not None and response.supported
    counts: Counts = {"meta": 1.0, "answerable": float(answerable), "fallbacks": float(calls > 1)}
    add(counts, "wrongly refused", answerable and not supported)
    for citation in response.citations if response is not None else []:
        line = line_of(meeting.lines, citation.lineId)
        add(counts, "citations", 1)
        add(counts, "in span", line is not None and line.idx in span)
        add(counts, "served fabricated", line is None or not contains(line.text, citation.quote))
    return counts


def meta_rates(counts: Counts) -> dict[str, float]:
    get = counts.get
    rates = {
        "wrongly not in meeting %": share(get("wrongly refused", 0), get("answerable", 0)),
        "served fabricated": get("served fabricated", 0),
        "cited in span %": share(get("in span", 0), get("citations", 0), 100.0),
        "fallback %": share(get("fallbacks", 0), get("meta", 0)),
        "meta questions": get("meta", 0),
    }
    return {metric: round(value, 2) for metric, value in rates.items()}


def meta_asked(meeting: Meeting, doc: MinutesOut, question: str) -> qa.Asked:
    """The question as the server's Ask gathers it, before the sections by meaning (`qa.focused`)."""
    sections = qa.sections_of(doc)
    notes = [section.text for section in sections]
    return qa.Asked(qa.candidates(question, [], meeting.lines), meeting.speakers, notes, meeting.lines, sections)


def stored_of(vectors: list[list[float]]) -> qa.Stored:
    """The meeting's Ask sections' vectors as the embed task stores them, ranked as the route's read ranks them."""

    async def nearest(vector: list[float]) -> list[int]:
        scores = [sum(a * b for a, b in zip(row, vector, strict=True)) for row in vectors]
        return sorted(range(len(vectors)), key=lambda n: -scores[n])[: qa.NEAREST]

    return nearest


async def meta_live(
    meeting: Meeting,
    doc: MinutesOut,
    question: str,
    tiers: Tiers,
    embeddings: Embeddings,
    vectors: list[list[float]],
) -> tuple[dict[str, Any], AskResponse | None]:
    """The server's Ask on a meta question, live, with the minutes' Ask sections already embedded (`vectors`, as the
    embed task stores them): its record (the candidate lines and the model's answers, for the replay) and its
    response."""
    asked = await qa.focused(embeddings, question, meta_asked(meeting, doc, question), stored_of(vectors))
    recorder, recorded = recording(tiers)
    try:
        response: AskResponse | None = await qa.answer(recorded, question, asked)
    except ProviderFailed:
        response = None
    record = {"candidates": [line.idx for line in asked.lines], "answers": [answer for _, answer in recorder.outputs]}
    return record, response


async def meta_replayed(
    meeting: Meeting, doc: MinutesOut, question: str, record: dict[str, Any]
) -> tuple[AskResponse | None, int]:
    """A recorded meta answer through the same Ask, with the candidate lines the live run sent; the calls it took."""
    base = meta_asked(meeting, doc, question)
    by_idx = {line.idx: line for line in meeting.lines}
    lines = [by_idx[idx] for idx in record["candidates"] if idx in by_idx]
    asked = qa.Asked(lines, base.speakers, base.notes, base.everything, base.sections)
    provider = Replayed("recorded", [("", answer) for answer in record["answers"]])
    try:
        response: AskResponse | None = await qa.answer(
            Tiers(polish=provider, command=provider, fallback=None), question, asked
        )
    except ProviderFailed:
        response = None
    return response, len(record["answers"]) - len(provider.outputs)


def qa_rates(counts: Counts) -> dict[str, float]:
    get = counts.get
    rates = {
        "citations in span %": share(get("in span", 0), get("citations", 0)),
        "served fabricated": get("served fabricated", 0),
        "answer ROUGE-L %": get("rouge", 0) / get("questions", 1),
        "not in meeting %": share(get("not in meeting", 0), get("questions", 0)),
        "retrieval hit %": share(get("retrieved", 0), get("questions", 0)),
        "failed %": share(get("failed", 0), get("questions", 0)),
    }
    return {metric: round(value, 2) for metric, value in rates.items()}


def tally(rows: list[Counts]) -> Counts:
    total: Counts = {}
    for row in rows:
        for metric, value in row.items():
            add(total, metric, value)
    return total


type Answer = tuple[str, str]  # the section asked for ("" for a question) and the model's answer


def section_of(turns: list[Turn]) -> str:
    """The section a minutes request asks for ("details" for the completion), or "" for any other request."""
    try:
        said = json.loads(turns[1][1])
    except IndexError, ValueError:
        return ""
    return str(said.get("section", "")) if isinstance(said, dict) else ""


class Recorder:
    """A provider that keeps every answer it passes on, for the metrics and the replay tier."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.engine = provider.engine
        self.outputs: list[Answer] = []

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        answer = await self.provider.complete(turns, max_tokens, timeout, reason)
        self.outputs.append((section_of(turns), answer[0]))
        return answer

    async def aclose(self) -> None:
        pass


class Replayed:
    """A provider that answers each request with the next answer recorded for its section, so a section the model
    never answered live fails here as it did there; with none left it fails as a provider would."""

    def __init__(self, engine: str, outputs: list[Answer]) -> None:
        self.engine = engine
        self.outputs = list(outputs)

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        section = section_of(turns)
        found = next((n for n, (asked, _) in enumerate(self.outputs) if asked == section), None)
        if found is None:
            raise ProviderError(f"{self.engine}: no recorded answer")
        return self.outputs.pop(found)[1], Usage(0, 0)

    async def aclose(self) -> None:
        pass


def answered[T](made: list[tuple[str, list[T]]]) -> dict[str, list[T]]:
    """The items the model answered at least once; one it never reached (a refused call, a network failure) says
    nothing about the model, so it is not recorded."""
    kept = {key: answers for key, answers in made if answers}
    return kept


def recording(tiers: Tiers) -> tuple[Recorder, Tiers]:
    recorder = Recorder(tiers.polish)
    return recorder, Tiers(polish=recorder, command=recorder, fallback=tiers.fallback)


async def written_minutes(
    meeting: Meeting, tiers: Tiers, saves: list[float] | None = None
) -> tuple[MinutesOut, str | None]:
    """The meeting's minutes through the real `minutes.run`, every section from the start, as the worker runs it,
    with the code it would fail with (`minutes.failure`); each save's time goes in `saves`."""
    stamps = saves if saves is not None else []

    async def saved(_: MinutesOut) -> None:
        stamps.append(time.monotonic())

    doc, _ = await minutes.run(
        tiers, meeting.name, [], meeting.lines, meeting.speakers, minutes.empty([*minutes.CALLS]), saved
    )
    return doc, minutes.failure(doc)


async def minutes_of(
    meeting: Meeting, tiers: Tiers, saves: list[float] | None = None
) -> tuple[list[Answer], MinutesOut | None]:
    """The meeting's minutes, with the model's answers by section; None when the worker would fail them."""
    recorder, recorded = recording(tiers)
    doc, code = await written_minutes(meeting, recorded, saves)
    return recorder.outputs, None if code else doc


async def answer_of(meeting: QMeeting, question: Question, tiers: Tiers) -> tuple[list[str], AskResponse | None]:
    """The server's Ask without the database or the minutes' sections (QMSum has none): the real ranking and the one
    `qa.answer` call, with the model's answers; None when it failed."""
    recorder, recorded = recording(tiers)
    found = qa.candidates(question.query, [], meeting.lines)
    asked = qa.Asked(lines=found, speakers=meeting.speakers, notes=[], everything=meeting.lines)
    said = recorder.outputs
    try:
        response = await qa.answer(recorded, question.query, asked)
    except ProviderFailed:
        return [answer for _, answer in said], None
    return [answer for _, answer in said], response


async def within[T](budget: Budget, jobs: Sequence[Callable[[], Awaitable[T]]], concurrency: int) -> list[T]:
    """The jobs, `concurrency` at a time, each started only while the budget has room; the ones that ran, in order."""
    gate = asyncio.Semaphore(concurrency)
    done: list[T] = []

    async def run(job: Callable[[], Awaitable[T]]) -> None:
        async with gate:
            if budget.reached():
                return
            budget.items += 1
            done.append(await job())

    await asyncio.gather(*(run(job) for job in jobs))
    return done


def replayed(engine: str, outputs: list[Answer]) -> Tiers:
    provider = Replayed(engine, outputs)
    return Tiers(polish=provider, command=provider, fallback=None)


async def recorded_rows(
    meetings: list[Meeting], qmeetings: list[QMeeting], recorded: dict[str, dict[str, Any]]
) -> Rows:
    """The recorded answers through the real paths again, scored: one row per meeting and an aggregate per eval."""
    rows: Rows = {}
    by_slice: dict[str, list[Counts]] = {}
    meta_all: list[Counts] = []
    for meeting in meetings:
        said = recorded["minutes"].get(meeting.name, [])
        # A recording from before minutes v2 (one answer for the whole document) says nothing about these calls.
        if not said or not all(isinstance(entry, list | tuple) for entry in said):
            continue
        answers, doc = await minutes_of(meeting, replayed("recorded", [(section, answer) for section, answer in said]))
        by_slice.setdefault(meeting.slice, []).append(minutes_counts(meeting, answers, doc))
        rows[f"{meeting.slice} {meeting.name}"] = minutes_rates(by_slice[meeting.slice][-1])
        for n, (question, kind) in enumerate(META):
            record = recorded.get("meta", {}).get(f"{meeting.name}:{n}")
            if doc is not None and record is not None:
                response, calls = await meta_replayed(meeting, doc, question, record)
                meta_all.append(meta_counts(meeting, doc, kind, response, calls))
    qa_all = []
    for meeting in qmeetings:
        counts = []
        for n, question in enumerate(meeting.questions):
            key = f"{meeting.name}:{n}"
            if key not in recorded["qa"]:
                continue
            said = [("", answer) for answer in recorded["qa"][key]]
            _, response = await answer_of(meeting, question, replayed("recorded", said))
            found = qa.candidates(question.query, [], meeting.lines)
            counts.append(qa_counts(meeting, question, found, response))
        if counts:
            qa_all += counts
            rows[f"qa {meeting.name}"] = qa_rates(tally(counts))
    for name, counts in by_slice.items():
        rows[name] = minutes_rates(tally(counts))
    if qa_all:
        rows["qa"] = qa_rates(tally(qa_all))
    if meta_all:
        rows["meta"] = meta_rates(tally(meta_all))
    return rows


def fake_row(meetings: list[Meeting]) -> dict[str, float]:
    fake = make_provider("fake:meetings", {})
    tiers = Tiers(polish=fake, command=fake, fallback=None)
    ami = [meeting for meeting in meetings if meeting.slice == "minutes"]
    counts = [minutes_counts(meeting, *asyncio.run(minutes_of(meeting, tiers))) for meeting in ami]
    return minutes_rates(tally(counts))


def missed(row: dict[str, float], gates: dict[str, Gate]) -> list[str]:
    """Each gated metric the row misses, with its value and its gate."""
    found = []
    for metric, (op, limit) in gates.items():
        value = row.get(metric, float("nan"))
        ok = value >= limit if op == ">=" else value <= limit
        if not ok:
            found.append(f"{metric} {value:.2f} (gate {op} {limit:.2f})")
    return found


def gate_misses(rows: Rows) -> list[str]:
    found = [f"{name}: {miss}" for name, gates in GATES.items() if name in rows for miss in missed(rows[name], gates)]
    return found


def fixture_meetings(path: Path) -> list[Meeting]:
    """Hand-made or exported meetings from a JSON file, or every JSON file of a folder: a list of {"name", "slice",
    "speakers": [name], "lines": [[speaker, startMs, endMs, text]], "gold": [{"kind", "text", "lines": [i]}]}."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    found = []
    for file in files:
        for made in json.loads(file.read_text(encoding="utf-8")):
            name = made["name"]
            # Numbered as diarization numbers them (the corpus letter A, B…), named as the fixture names them.
            cast = {who: made_speaker(name, chr(ord("A") + n), who) for n, who in enumerate(made["speakers"])}
            said = [
                made_line(name, n, cast[who], start, end, text)
                for n, (who, start, end, text) in enumerate(made["lines"])
            ]
            gold = [Gold(item["kind"], item["text"], frozenset(item["lines"])) for item in made.get("gold", [])]
            speakers = {person.id: person for person in cast.values()}
            found.append(Meeting(name=name, lines=said, speakers=speakers, gold=gold, slice=made["slice"]))
    return found


def corpus() -> tuple[list[Meeting], list[QMeeting]]:
    """AMI's test meetings, then the hand-made slices; QMSum's for Ask."""
    manifest = json.loads(MANIFEST.read_text())
    archive = fetch(json.loads(AMI_MANIFEST.read_text())["annotations"])
    meetings = [ami_meeting(archive, name) for name in manifest["meetings"]]
    qmeetings = qmsum_meetings(fetch(manifest["qmsum"], QMSUM_CACHE))
    return [*meetings, *fixture_meetings(SLICES)], qmeetings


def read_recorded() -> dict[str, dict[str, Any]]:
    found = {}
    for part in ("minutes", "qa", "meta"):
        path = RECORDED / f"{part}.json"
        found[part] = json.loads(path.read_text()) if path.exists() else {}
    return found


AGGREGATES = ("validator", "fake minutes", "minutes", "probes", "contradictions", "hinglish", "qa", "meta", "speed")


def report(rows: Rows, out: Path) -> None:
    write(out, rows)
    print(table({name: rows[name] for name in AGGREGATES if name in rows}))
    print(f"results: {out}")


def replay_rows() -> Rows:
    """The free tier's aggregate rows on the recordings as they stand."""
    meetings, qmeetings = corpus()
    rows: Rows = {"validator": validator_row(meetings, SEED), "fake minutes": fake_row(meetings)}
    rows |= asyncio.run(recorded_rows(meetings, qmeetings, read_recorded()))
    return {name: rows[name] for name in AGGREGATES if name in rows}


def rebase(speed: dict[str, float]) -> None:
    """A clean live run's baseline: the replay of the answers it just recorded (a new sample of the model, so the
    old replay baseline no longer describes them) and its speed row."""
    write(BASELINE, replay_rows() | {"speed": speed})
    print(f"baseline rebased on the new recordings: {BASELINE}")


def replay(save_baseline: bool) -> int:
    meetings, qmeetings = corpus()
    rows: Rows = {"validator": validator_row(meetings, SEED), "fake minutes": fake_row(meetings)}
    rows |= asyncio.run(recorded_rows(meetings, qmeetings, read_recorded()))
    report(rows, OUT)
    results = {name: rows[name] for name in AGGREGATES if name in rows}
    failed = gate_misses(results)
    if failed:
        print("gates missed: " + "; ".join(failed))
    if not BASELINE.exists() and not failed:
        write(BASELINE, results)
        print(f"baseline recorded: {BASELINE}")
        return 0
    if not BASELINE.exists():
        return 1
    # The speed row is the live run's own (no replay times anything); it is kept as it is.
    baseline = json.loads(BASELINE.read_text())
    speed = {"speed": baseline.pop("speed")} if "speed" in baseline else {}
    worse = regressions(baseline, results, HIGHER)
    if save_baseline and (worse or failed):
        raise SystemExit("Not saved: " + "; ".join(worse + failed))
    if save_baseline:
        write(BASELINE, results | speed)
        print(f"baseline saved: {BASELINE}")
        return 0
    if worse:
        print("worse than the baseline: " + "; ".join(worse))
    if worse or failed:
        return 1
    print("no metric worse than the baseline; every gate met")
    return 0


def series_of(meeting: str) -> str:
    """The AMI series a meeting is a part of: ES2004a to ES2004d are one design task in four meetings."""
    return meeting[:-1]


# Live speed against its baseline: a time may grow by a quarter before it counts as worse (the spread between two
# runs on 25 Oct 2026 was under that), a share may drop 3 points (the sampling spread of 350 search queries). The
# query count is no measure.
SPREAD = 1.25
SHARE_SPREAD = 3.0
UNMEASURED = ("search queries",)


def speed_worse(baseline: dict[str, float], row: dict[str, float]) -> list[str]:
    """Each speed metric worse than the baseline's, past its spread."""
    worse = []
    for metric, before in baseline.items():
        now = row.get(metric, float("nan"))
        timed = metric.endswith(" ms")
        if metric in UNMEASURED:
            continue
        if (timed and not now <= before * SPREAD) or (not timed and not now >= before - SHARE_SPREAD):
            worse.append(f"{metric} {before:.2f} -> {now:.2f}")
    return worse


def p95(times: list[float]) -> float:
    """The 95th percentile, nearest rank; NaN with nothing timed."""
    if not times:
        return float("nan")
    ordered = sorted(times)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


async def search_row(
    meetings: list[Meeting], docs: dict[str, MinutesOut], tiers: Tiers, embeddings: Embeddings
) -> dict[str, float]:
    """The server's search on the live minutes: every meeting's sections embedded once, then every gold item of
    every served meeting (abstract, action, decision, problem sentences: about 350) as a query, timed through what a
    request does (embed the query, the nearest `3 x limit` sections, the model's re-rank), four at a time. A hit is
    the query's own meeting among the first 5 (recall@5), or any of its AMI series' four (series recall@5). One query
    per meeting (20) moved series recall between 65 and 85 % run to run; 350 holds it to a couple of points."""
    sections = [(name, kind, idx, body) for name, doc in docs.items() for kind, idx, body in minutes.chunks_of(doc)]
    if not sections:
        return {}
    vectors = await embeddings.embed([body for *_, body in sections])
    asked = [(meeting.name, gold.text) for meeting in meetings if meeting.name in docs for gold in meeting.gold]
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(name: str, query: str) -> tuple[float, bool, bool]:
        async with gate:
            started = time.monotonic()
            query_vector = (await embeddings.embed([query]))[0]
            scores = [sum(a * b for a, b in zip(vector, query_vector, strict=True)) for vector in vectors]
            nearest = sorted(range(len(sections)), key=lambda n: -scores[n])
            rows = [
                {
                    "meeting_id": uuid5(NAMESPACE_URL, sections[n][0]),
                    "recording_id": uuid5(NAMESPACE_URL, sections[n][0]),
                    "title": sections[n][0],
                    "kind": sections[n][1],
                    "snippet": sections[n][3],
                    "owner": None,
                    "score": 0.0,
                }
                for n in nearest[:15]
            ]
            try:
                hits = await meeting_index.reranked(tiers, query, 5, rows)
            except ProviderFailed:
                # A re-rank the model didn't answer is the route's 502: no hit, a miss for both recalls.
                hits = []
            took = (time.monotonic() - started) * 1000
        siblings = {uuid5(NAMESPACE_URL, other) for other in docs if series_of(other) == series_of(name)}
        own = any(hit.meetingId == uuid5(NAMESPACE_URL, name) for hit in hits)
        return took, own, any(hit.meetingId in siblings for hit in hits)

    answered = await asyncio.gather(*(one(name, query) for name, query in asked))
    return {
        "queries": float(len(asked)),
        "recall@5 %": share(sum(own for _, own, _ in answered), len(asked)),
        "series recall@5 %": share(sum(series for _, _, series in answered), len(asked)),
        "p95 ms": p95([took for took, _, _ in answered]),
    }


async def ask_live(
    meetings: list[Meeting], qmeetings: list[QMeeting], tiers: Tiers, budget: Budget, times: dict[str, list[float]]
) -> tuple[dict[str, dict[str, Any]], dict[str, MinutesOut]]:
    """Each meeting's minutes and each question's answer from the model while the budget lasts, as recordings, with
    each item's wall time; and the minutes served, for the search pass."""
    docs: dict[str, MinutesOut] = {}

    async def one_minutes(meeting: Meeting) -> tuple[str, list[Answer]]:
        started = time.monotonic()
        saves: list[float] = []
        answers, doc = await minutes_of(meeting, tiers, saves)
        if meeting.slice == "minutes":
            times["minutes"].append((time.monotonic() - started) * 1000)
            times["first section"].append(((saves or [time.monotonic()])[0] - started) * 1000)
            if doc is not None:
                docs[meeting.name] = doc
        print(f"{meeting.name}: {len(answers)} answers, {'served' if doc else 'failed'}")
        return meeting.name, answers

    async def one_answer(meeting: QMeeting, n: int) -> tuple[str, list[str]]:
        started = time.monotonic()
        answers, _ = await answer_of(meeting, meeting.questions[n], tiers)
        times["qa"].append((time.monotonic() - started) * 1000)
        return f"{meeting.name}:{n}", answers

    minute_jobs = [lambda meeting=meeting: one_minutes(meeting) for meeting in meetings]
    qa_jobs = [
        lambda meeting=meeting, n=n: one_answer(meeting, n)
        for meeting in qmeetings
        for n in range(len(meeting.questions))
    ]
    made_minutes = await within(budget, minute_jobs, CONCURRENCY)
    # Questions one at a time, as a person asks them: their wall times are the MOM-06 budget's.
    made_answers = await within(budget, qa_jobs, 1)
    return {"minutes": answered(made_minutes), "qa": answered(made_answers)}, docs


def run_live(budget_usd: float | None, ledger: Path, limit: int | None, save_baseline: bool = False) -> int:
    budget_usd = require_budget(budget_usd, "The live tier")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY: the live tier asks the server's polish model (docs/providers.md).")
    config = load(os.environ)
    if config.polish not in PRICES:
        raise SystemExit(f"{config.polish} has no price in config/pricing.py, so its spend could not be counted.")
    meetings, qmeetings = corpus()
    meetings, qmeetings = meetings[:limit], qmeetings[:limit]
    budget = Budget(budget_usd, ledger_total(ledger))
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    tiers = Tiers(polish=metered, command=metered, fallback=None)
    print(
        f"engine {config.polish}; {len(meetings)} meetings, {sum(len(item.questions) for item in qmeetings)} questions"
    )
    note = "crashed"

    times: dict[str, list[float]] = {"minutes": [], "first section": [], "qa": []}
    embeddings = make_embeddings(config.cloud.embed if config.cloud else EMBED, dict(config.keys))
    speed: dict[str, float] = {}

    async def asked() -> dict[str, dict[str, Any]]:
        try:
            made, docs = await ask_live(meetings, qmeetings, tiers, budget, times)
            tiny, _ = await smoke_outcome(tiny_meeting(), tiers)
            speed["tiny meeting ended"] = float(tiny == "ready" or tiny.startswith("failed:"))
            found = await search_row(meetings, docs, tiers, embeddings)
            speed.update({"minutes p95 ms": p95(times["minutes"]), "qa p95 ms": p95(times["qa"])})
            speed["first section p95 ms"] = p95(times["first section"])
            speed.update({f"search {name}": value for name, value in found.items()})
            return made
        finally:
            await provider.aclose()
            await embeddings.aclose()

    try:
        made = asyncio.run(asked())
        note = "budget reached" if budget.reached() else "ok"
    finally:
        append_ledger(ledger, budget.spent, budget.items, f"eval-minutes-live {config.polish}", note)
        print(f"$ this run: {budget.spent:.4f}; ledger total: {budget.before + budget.spent:.4f}")
    recorded = read_recorded()
    for part in ("minutes", "qa"):
        recorded[part] |= made[part]
        RECORDED.mkdir(parents=True, exist_ok=True)
        (RECORDED / f"{part}.json").write_text(json.dumps(recorded[part], indent=1, sort_keys=True) + "\n")
    rows = asyncio.run(recorded_rows(meetings, qmeetings, made)) | {"speed": speed}
    report(rows, OUT.with_name("eval-minutes-live.json"))
    failed = gate_misses(rows)
    if failed:
        print("gates missed: " + "; ".join(failed))
    # Speed only moves forward: against the baseline's speed row, which a clean live run with --save-baseline moves.
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    worse = speed_worse(baseline.get("speed", {}), speed)
    if worse:
        print("speed worse than the baseline: " + "; ".join(worse))
    elif save_baseline and not failed:
        rebase(speed)
    failed += worse
    finished = len(made["minutes"]) == len(meetings) and len(made["qa"]) == sum(len(m.questions) for m in qmeetings)
    if not finished:
        print(f"budget reached after {len(made['minutes'])} meetings and {len(made['qa'])} questions")
    return 0 if finished and not failed else 1


# The founder's own failing case (25 Oct 2026): a 3 s recording, one speaker, a line or two.
SMOKE_LINES = ((300, 1_400, "Okay, testing the meeting recorder."), (1_600, 2_900, "One, two, three, done."))
# A minutes run or a question that has not ended by then is stuck.
SMOKE_DEADLINE_S = 240


# The same in Hinglish, as Devanagari lines: its minutes must still be English (MOM-05, D26).
HINGLISH_LINES = ((300, 1_600, "ठीक है, मीटिंग रिकॉर्डर टेस्ट कर रहे हैं।"), (1_800, 3_000, "एक, दो, तीन, हो गया।"))


def tiny_meeting(hinglish: bool = False) -> Meeting:
    name = "smoke-hinglish" if hinglish else "smoke"
    said = HINGLISH_LINES if hinglish else SMOKE_LINES
    speaker = made_speaker(name, "A", "Founder")
    lines = [made_line(name, n, speaker, start, end, text) for n, (start, end, text) in enumerate(said)]
    made = Meeting(name=name, lines=lines, speakers={speaker.id: speaker}, gold=[])
    return made


def english(doc: MinutesOut) -> bool:
    """No Devanagari in anything the model wrote: the minutes are always English (D26); quotes may be any script."""
    written = [doc.title or "", doc.summary, doc.mom, *(claim.text for claim in items_of(doc, COVERS["actions"]))]
    written += [claim.text for claim in items_of(doc, ["decisions", "openQuestions", "risks", "contradictions"])]
    written += [topic.topic for topic in items_of(doc, ["agenda", "nextAgenda"])]
    return not any("\u0900" <= char <= "\u097f" for text in written for char in text)


async def smoke_outcome(meeting: Meeting, tiers: Tiers) -> tuple[str, list[DerivationOut]]:
    """What the worker would store for the meeting: `ready` with what it serves, or `failed:<code>` as
    `generate_minutes` fails it; `stuck` when it never ends, `not english` when its minutes aren't."""
    try:
        doc, code = await asyncio.wait_for(written_minutes(meeting, tiers), SMOKE_DEADLINE_S)
    except TimeoutError:
        return "stuck", []
    if code:
        return f"failed:{code}", []
    served = [entry for claim in items_of(doc) for entry in claim.derivedFrom]
    return ("ready" if english(doc) else "not english"), served


def run_smoke(budget_usd: float | None, ledger: Path) -> int:
    """After any change to the providers, the prompts, minutes or qa: the real model on one degenerate meeting and
    one question, under a cent. It fails when the meeting is left anywhere but ready or failed with a code, or
    when anything served is fabricated."""
    budget_usd = require_budget(budget_usd, "The smoke run")
    config = load(os.environ)
    budget = Budget(budget_usd, ledger_total(ledger))
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    tiers = Tiers(polish=metered, command=metered, fallback=None)
    meeting = tiny_meeting()

    async def asked() -> tuple[str, list[DerivationOut], AskResponse | None, str]:
        try:
            outcome, served = await smoke_outcome(meeting, tiers)
            hinglish, _ = await smoke_outcome(tiny_meeting(hinglish=True), tiers)
            question = qa.Asked(
                lines=qa.candidates("What was tested?", [], meeting.lines), speakers=meeting.speakers, notes=[]
            )
            try:
                said = await asyncio.wait_for(qa.answer(tiers, "What was tested?", question), SMOKE_DEADLINE_S)
            except ProviderFailed, TimeoutError:
                said = None
            return outcome, served, said, hinglish
        finally:
            await provider.aclose()

    outcome, served, said, hinglish = asyncio.run(asked())
    append_ledger(ledger, budget.spent, budget.items, f"eval-minutes-smoke {config.polish}", outcome)
    fabricated = sum(not grounded(meeting, entry) for entry in served)
    cited = [] if said is None else said.citations
    fabricated += sum(line_of(meeting.lines, citation.lineId) is None for citation in cited)
    asked_ok = said is not None
    print(
        f"smoke on {config.polish}: minutes {outcome}, {len(served)} served, {fabricated} fabricated; "
        f"Hinglish minutes {hinglish}; "
        f"question {'answered' if asked_ok else 'failed'} (supported {bool(said and said.supported)}); "
        f"$ {budget.spent:.4f}"
    )
    ended = all(end == "ready" or end.startswith("failed:") for end in (outcome, hinglish))
    return 0 if ended and asked_ok and fabricated == 0 else 1


def private_report(meeting: Meeting, doc: MinutesOut | None, row: dict[str, float], times: dict[str, float]) -> str:
    """One private meeting's report, for its owner only (it quotes their meeting): the metrics, each detail the
    minutes left out, and the minutes as served."""
    parts = [f"# {meeting.name}", "", f"{len(meeting.lines)} lines.", "", "| metric | value |", "| --- | --- |"]
    parts += [f"| {metric} | {value} |" for metric, value in {**row, **times}.items()]
    if doc is None:
        return "\n".join([*parts, "", "No minutes: both the summary and the MoM failed."]) + "\n"
    units = details.inventory(meeting.lines, meeting.speakers)
    left = details.missing(doc, units, meeting.lines)
    parts += ["", f"## Details left out ({len(left)} of {len(units)})", ""]
    parts += [f"- line {unit.idx}, {unit.kind}: {unit.text}" for unit in left] or ["None."]
    parts += ["", "## Agenda topics without a section", ""]
    parts += [f"- {topic}" for topic in details.topics_missing(doc)] or ["None."]
    parts += ["", "## The minutes as served", "", "```json", doc.model_dump_json(indent=1), "```"]
    return "\n".join(parts) + "\n"


# The private slice's gates: the owner's own meetings must lose no commitment and serve no fabrication.
PRIVATE_GATES: dict[str, Gate] = {
    "commitments covered %": (">=", 100.0),
    "served fabricated": ("<=", 0.0),
    # The founder's Ask bug (26 Oct 2026): his meetings' meta questions, live.
    "ask wrongly not in meeting %": ("<=", 2.0),
    "ask served fabricated": ("<=", 0.0),
}


def private_misses(done: Sequence[tuple[Meeting, MinutesOut | None, dict[str, float], Any]]) -> list[tuple[str, str]]:
    """Each private meeting that served no minutes or missed a private gate, with what it missed."""
    found = [(meeting.name, "no minutes") for meeting, doc, _, _ in done if doc is None]
    # A meeting with no minutes asks nothing: its Ask gates have nothing to read (it fails as "no minutes").
    for meeting, _, row, _ in done:
        gates = {name: gate for name, gate in PRIVATE_GATES.items() if name in row}
        found += [(meeting.name, miss) for miss in missed(row, gates)]
    return found


def run_private(folder: Path, report: Path, budget_usd: float | None, ledger: Path) -> int:
    """The live tier on meetings exported to a folder outside the repository (`fixture_meetings`), reported to the
    owner in `report`; only metrics reach the terminal. It fails on a served fabrication or a meeting with no
    minutes."""
    budget_usd = require_budget(budget_usd, "The private slice")
    config = load(os.environ)
    found = fixture_meetings(folder)
    if not found:
        raise SystemExit(f'No exported meeting in {folder}: see docs/bench.md, "The private slice".')
    budget = Budget(budget_usd, ledger_total(ledger))
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    tiers = Tiers(polish=metered, command=metered, fallback=None)
    embeddings = make_embeddings(config.cloud.embed if config.cloud else EMBED, dict(config.keys))
    asks: dict[str, list[str]] = {}

    async def asked() -> list[tuple[Meeting, MinutesOut | None, dict[str, float], dict[str, float]]]:
        made = []
        try:
            for meeting in found:
                started, saves = time.monotonic(), []
                answers, doc = await minutes_of(meeting, tiers, saves)
                times = {"first section s": round((saves or [time.monotonic()])[0] - started, 1)}
                times["all sections s"] = round(time.monotonic() - started, 1)
                row = minutes_rates(minutes_counts(meeting, answers, doc))
                metas: list[Counts] = []
                vectors = [] if doc is None else await embeddings.embed([text for _, _, text in qa.ask_chunks(doc)])
                for question, kind in META:
                    if doc is None:
                        break
                    record, response = await meta_live(meeting, doc, question, tiers, embeddings, vectors)
                    metas.append(meta_counts(meeting, doc, kind, response, len(record["answers"])))
                    shown = response.answer if response is not None else "(the model failed)"
                    asks.setdefault(meeting.name, []).append(f"- {question} → {shown}")
                row |= {f"ask {name}": value for name, value in meta_rates(tally(metas)).items()} if metas else {}
                made.append((meeting, doc, row, times))
        finally:
            await provider.aclose()
            await embeddings.aclose()
        return made

    done = asyncio.run(asked())
    append_ledger(ledger, budget.spent, len(done), f"eval-minutes-private {config.polish}", "ok")
    parts = [private_report(*item) + "\n## Ask\n\n" + "\n".join(asks.get(item[0].name, [])) for item in done]
    report.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(table({meeting.name: {**row, **times} for meeting, _, row, times in done}))
    print(f"report (private, quotes the meetings): {report}; $ {budget.spent:.4f}")
    # The founder's bar (26 Oct 2026): every commitment an action item or judged none, nothing fabricated.
    bad = [name for name, row in private_misses(done)]
    if bad:
        print("private gate missed: " + ", ".join(bad))
    return 1 if bad else 0


def run_ask(budget_usd: float | None, ledger: Path) -> int:
    """Ask and search, live, on the recorded minutes (replayed free; only the embeddings and the Ask and re-rank calls
    are live): the meta questions (`META`) on the AMI and Hinglish meetings, recorded for the replay; every QMSum
    question through Ask, one at a time; the search pass. Gated on the `meta` row and the speed row's search and Ask
    gates; no minutes are written."""
    budget_usd = require_budget(budget_usd, "An Ask run")
    config = load(os.environ)
    meetings, qmeetings = corpus()
    recorded = read_recorded()["minutes"]
    budget = Budget(budget_usd, ledger_total(ledger))
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    embeddings = make_embeddings(config.cloud.embed if config.cloud else EMBED, dict(config.keys))
    ami = [meeting for meeting in meetings if meeting.slice == "minutes" and recorded.get(meeting.name)]
    meta_meetings = [meeting for meeting in meetings if meeting.slice in META_SLICES and recorded.get(meeting.name)]
    made: dict[str, dict[str, Any]] = {}
    asked_qa: dict[str, list[str]] = {}
    meta_all: list[Counts] = []

    async def searched() -> dict[str, float]:
        tiers = Tiers(polish=metered, command=metered, fallback=None)
        times = []
        meta_times: dict[bool, list[float]] = {True: [], False: []}
        for meeting in meta_meetings:
            said = [(section, answer) for section, answer in recorded[meeting.name]]
            _, doc = await minutes_of(meeting, replayed("recorded", said))
            if doc is None:
                continue
            # Embedded once, untimed, as the embed task does when the minutes land.
            vectors = await embeddings.embed([text for _, _, text in qa.ask_chunks(doc)])
            for n, (question, kind) in enumerate(META):
                started = time.monotonic()
                record, response = await meta_live(meeting, doc, question, tiers, embeddings, vectors)
                meta_times[len(record["answers"]) > 1].append((time.monotonic() - started) * 1000)
                made[f"{meeting.name}:{n}"] = record
                meta_all.append(meta_counts(meeting, doc, kind, response, len(record["answers"])))
        for qmeeting in qmeetings:
            for n, question in enumerate(qmeeting.questions):
                started = time.monotonic()
                said, _ = await answer_of(qmeeting, question, tiers)
                times.append((time.monotonic() - started) * 1000)
                if said:
                    asked_qa[f"{qmeeting.name}:{n}"] = said
        docs: dict[str, MinutesOut] = {}
        for meeting in ami:
            said = [(section, answer) for section, answer in recorded[meeting.name]]
            _, doc = await minutes_of(meeting, replayed("recorded", said))
            if doc is not None:
                docs[meeting.name] = doc
        try:
            found = await search_row(ami, docs, tiers, embeddings)
            speeds = {"qa p95 ms": p95(times), **{f"search {name}": value for name, value in found.items()}}
            speeds["meta p95 ms"] = p95(meta_times[False]) if meta_times[False] else 0.0
            speeds["meta fallback p95 ms"] = p95(meta_times[True]) if meta_times[True] else 0.0
            return speeds
        finally:
            await provider.aclose()
            await embeddings.aclose()

    row = asyncio.run(searched())
    append_ledger(ledger, budget.spent, len(made), f"eval-minutes-ask {config.polish}", "ok")
    meta_row = meta_rates(tally(meta_all))
    # The answers become the replay's: Ask's meta questions, and QMSum's through the new two-step Ask.
    for part, fresh in (("meta", made), ("qa", asked_qa)):
        stored = read_recorded()[part] | fresh
        (RECORDED / f"{part}.json").write_text(json.dumps(stored, indent=1, sort_keys=True) + "\n")
    print(table({"meta": meta_row, "speed": row}))
    gates = {name: gate for name, gate in GATES["speed"].items() if name in row}
    failed = missed(row, gates) + missed(meta_row, GATES["meta"])
    if failed:
        print("gates missed: " + "; ".join(failed))
    print(f"$ this run: {budget.spent:.4f}; ledger total: {budget.before + budget.spent:.4f}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save-baseline", action="store_true", help="move the baseline to this run if none is worse")
    parser.add_argument("--live", action="store_true", help="the real model (costs money; needs --budget-usd)")
    parser.add_argument("--budget-usd", type=float, help="live: send nothing that would take the ledger past this")
    parser.add_argument("--ledger", type=Path, default=LEDGER, help="live: the JSONL spend ledger bench.py shares")
    parser.add_argument("--limit", type=int, help="live: the first N meetings of each set, for a quick look")
    parser.add_argument("--smoke", action="store_true", help="the real model on one tiny meeting and one question")
    parser.add_argument("--private", type=Path, help="the real model on exported meetings in DIR, outside the repo")
    parser.add_argument("--report", type=Path, help="private: where the owner's report goes (DIR/private-eval.md)")
    parser.add_argument("--ask", action="store_true", help="live Ask and search on the recorded minutes (cheap)")
    options = parser.parse_args(argv)
    if options.ask:
        return run_ask(options.budget_usd, options.ledger)
    if options.private:
        report = options.report or options.private / "private-eval.md"
        return run_private(options.private, report, options.budget_usd, options.ledger)
    if options.smoke:
        return run_smoke(options.budget_usd, options.ledger)
    if options.live:
        return run_live(options.budget_usd, options.ledger, options.limit, options.save_baseline)
    return replay(options.save_baseline)


if __name__ == "__main__":
    sys.exit(main())
