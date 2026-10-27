"""`just eval-speakers`: the speaker identification loop scored (MEETINGS.md D42, PRD MOM-08; docs/bench.md).

Two slices, every speaker anonymised to "Speaker N" as a fresh transcript is:

- `ami`: the minutes eval's 20 AMI test meetings (CC BY 4.0), diarization broken the way it breaks (DiarizationLM's
  deg2ref, simulated): the speaker who talks most split in two, their second half a new speaker, and 5% of the lines
  given to another speaker (seed 7). The pyannote rerun is simulated from the truth, 3% of its lines heard wrong,
  this scores the rules end to end, not pyannote's voices, which only real audio can (`just test-cloud`).
  Scored, from the model's answer and the rerun agreeing: merge precision (a merge joins two halves of one person)
  and recall (the split joined), move precision (a moved line lands on who said it) and recall (flipped lines put
  back), and name precision against the names the participants introduce themselves by
  (`bench/speakers/ami-names.json`); a name on a speaker who never says theirs is unverifiable, reported, not scored.
  The `ami voiceprints` row adds simulated voiceprints, scored with noise around pyannote's documented example (86
  for a voice's own speakers, 16 for another's) through the real merge rule, so a split can be missed and two people
  joined wrongly: merge precision and split recall are gated there.
- `names`: hand-made meetings (`bench/speakers/names.json`) with gold names and their traps: a name said once, someone
  mentioned who never speaks, a host handing over, a caller not in the room, no names at all. Scored: name precision,
  false names (applied to a speaker the meeting never names), recall of the names said on two lines or more, and
  suggestion precision.

Both report cue validity: the share of the model's cues that are a line's words verbatim, holding the name, before
the rules drop the rest. A name rests only on cues that check out, whatever this share. A rate with nothing to count
is n/a, never a perfect score.

`uv run scripts/eval_speakers.py` is the replay tier: the answers recorded live (`bench/speakers/recorded.json`)
through the real rules (`speakers.proposal_of`, `speakers.decide`). Free. `... --live --budget-usd USD [--ledger
PATH]` asks the server's polish model, charges every call to the bench ledger, records the answers and scores them.
A run that misses a gate in `GATES` exits 1. Only metrics and meeting IDs are printed, never a line or a name.
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from eval_minutes import (
    LEDGER,
    MANIFEST,
    Meeting,
    Recorder,
    ami_meeting,
    fetch,
    made_line,
    within,
)
from eval_transcript import MANIFEST as AMI_MANIFEST

from bench import Budget, Metered, append_ledger, ledger_total, require_budget
from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.config.settings import load
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.services import speaker_loop, speakers

ROOT = Path(__file__).resolve().parent.parent
NAMES = ROOT / "bench" / "speakers" / "names.json"
AMI_NAMES = ROOT / "bench" / "speakers" / "ami-names.json"
RECORDED = ROOT / "bench" / "speakers" / "recorded.json"
EARNINGS = ROOT / "bench" / "speakers" / "earnings21.json"
EARNINGS_CACHE = Path.home() / ".snapbak-bench" / "earnings21"
# The operator naming the analyst who asks next: "Our next question comes from (the line of) Jane Doe with Acme".
ASKS_NEXT = re.compile(
    r"(?i:(?:question|line)\s+(?:comes|is coming|will come)\s+from\s+(?:the\s+line\s+of\s+)?)"
    r"([A-Z][a-z'\-]+(?:\s[A-Z][a-z'\-]+){0,2})"
)
SEED = 7
# The share of lines diarization gave to another speaker, and the share the simulated rerun hears wrong.
FLIPPED = 0.05
MISHEARD = 0.03
# The spread of the simulated voiceprint scores, on pyannote's 0-100 scale.
VOICE_NOISE = 12.0
CONCURRENCY = 4
# The speakers call's p95 in ms: what MOM-04's 3 minutes leave after the minutes' own 140 s.
CALL_P95_MS = 40_000
# A slice's gates: a floor on a rate or a cap on a count, and whether an n/a (nothing counted) misses it. A
# precision with nothing counted passes (the loop changed nothing, so nothing wrong); the names slice's recall and
# precision must be measured, so a model that names nobody fails.
GATES: dict[str, dict[str, tuple[str, float, bool]]] = {
    "ami": {
        "cue validity": (">=", 0.95, False),
        "name precision": (">=", 0.98, False),
        "false names": ("<=", 0, True),
        "merge precision": (">=", 0.95, False),
        "move precision": (">=", 0.9, True),
    },
    # Simulated voiceprints with noise: the merge rule's precision and recall where a voiceprint is the evidence.
    "ami voiceprints": {
        "merge precision": (">=", 0.95, True),
        "split recall": (">=", 0.8, True),
    },
    # Long calls: the model mis-quotes more cues there (an invalid cue is dropped before any rule, so it costs recall,
    # never precision). The recall and cue floors sit below the lowest of the live runs on 27 Oct (recall 0.230,
    # 0.150, 0.170; cues 0.769, 0.815) by a margin for the model's run-to-run spread, as the minutes' gates do.
    "earnings": {
        "cue validity": (">=", 0.7, True),
        "name precision": (">=", 0.98, True),
        "false names": ("<=", 0, True),
        "sure name recall": (">=", 0.12, True),
    },
    "names": {
        "cue validity": (">=", 0.95, True),
        "name precision": (">=", 0.98, True),
        "false names": ("<=", 0, True),
        "sure name recall": (">=", 0.5, True),
    },
}


@dataclass
class Case:
    """One meeting as the loop sees it, with the truth to score against: each line's true speaker (`truth`, by
    idx), each speaker's real name (`gold`, None when the meeting never says it) and the ones named on two lines or
    more (`sure`)."""

    name: str
    slice: str
    lines: list[TranscriptLine]
    known: dict[UUID, Speaker]
    truth: dict[int, str] = field(default_factory=dict)
    gold: dict[UUID, str | None] = field(default_factory=dict)
    sure: set[UUID] = field(default_factory=set)
    # AMI: each true speaker letter's accepted names; the split speaker; the lines given to another speaker.
    known_names: dict[str, list[str]] = field(default_factory=dict)
    split: tuple[UUID, UUID] | None = None
    flipped: set[int] = field(default_factory=set)


def anonymous(meeting: str, n: int) -> Speaker:
    made = Speaker(
        id=uuid5(NAMESPACE_URL, f"{meeting}/eval-speaker/{n}"),
        recording_id=uuid5(NAMESPACE_URL, meeting),
        label=f"Speaker {n}",
        vendor_label=f"S{n}",
        name=None,
    )
    return made


def broken(meeting: Meeting, rng: random.Random) -> Case:
    """An AMI meeting as a diarization that went wrong: anonymised, the most talkative speaker split in two at their
    middle line, and FLIPPED of the lines given to another speaker."""
    said = {line.idx: meeting.speakers[line.speaker_id].vendor_label for line in meeting.lines}
    order = list(dict.fromkeys(said[line.idx] for line in meeting.lines))
    loudest = Counter(said.values()).most_common(1)[0][0]
    theirs = [line.idx for line in meeting.lines if said[line.idx] == loudest]
    split = set(theirs[len(theirs) // 2 :])
    voices = {letter: anonymous(meeting.name, n) for n, letter in enumerate(order, 1)}
    half = anonymous(meeting.name, len(order) + 1)
    flipped = set(rng.sample(sorted(said), int(len(said) * FLIPPED)))
    lines = []
    for line in meeting.lines:
        speaker = half if line.idx in split else voices[said[line.idx]]
        if line.idx in flipped:
            speaker = rng.choice([each for each in voices.values() if each is not speaker])
        made = made_line(meeting.name, line.idx, speaker, line.start_ms, line.end_ms, line.text)
        made.rev = 1  # as stored: no user edit
        lines.append(made)
    known = {each.id: each for each in [*voices.values(), half]}
    names = json.loads(AMI_NAMES.read_text())["series"].get(meeting.name[:6], {})
    made = Case(meeting.name, "ami", lines, known, truth=said, known_names=names)
    made.split = (voices[loudest].id, half.id)
    made.flipped = flipped
    return made


def named_cases() -> list[Case]:
    """The hand-made meetings with gold names."""
    cases = []
    for raw in json.loads(NAMES.read_text())["meetings"]:
        voices = [anonymous(raw["name"], n) for n in range(1, len(raw["gold"]) + 1)]
        lines = [
            made_line(raw["name"], idx, voices[who], idx * 5_000, idx * 5_000 + 4_000, text)
            for idx, (who, text) in enumerate(raw["lines"])
        ]
        for line in lines:
            line.rev = 1  # as stored: no user edit
        gold = {voice.id: name for voice, name in zip(voices, raw["gold"], strict=True)}
        sure = {voices[n].id for n in raw["sure"]}
        cases.append(Case(raw["name"], "names", lines, {each.id: each for each in voices}, gold=gold, sure=sure))
    return cases


def turns_of(path: Path) -> list[tuple[str, str]]:
    """An Earnings-21 transcript (one token a row: token, speaker, ..., punctuation, case) as its speakers' turns."""
    turns: list[tuple[str, list[str]]] = []
    with path.open() as opened:
        rows = csv.reader(opened, delimiter="|")
        next(rows)
        for row in rows:
            token, who, punctuation, shape = row[0], row[1], row[4], row[5]
            word = (token.capitalize() if shape == "UC" else token.upper() if shape == "CA" else token) + punctuation
            if turns and turns[-1][0] == who:
                turns[-1][1].append(word)
            else:
                turns.append((who, [word]))
    made = [(who, " ".join(words)) for who, words in turns]
    return made


def earnings_cases() -> list[Case]:
    """The Earnings-21 calls as the loop sees them, one line a turn, every speaker anonymised; gold: the analyst the
    operator names as asking next is the next turn's speaker when that is their first turn, other speakers
    unverifiable; `sure`: gold speakers whose
    first name is said on two lines or more."""
    cases = []
    for entry in json.loads(EARNINGS.read_text())["files"]:
        name = Path(entry["file"]).stem
        turns = turns_of(fetch(entry, EARNINGS_CACHE))
        order = list(dict.fromkeys(who for who, _ in turns))
        voices = {who: anonymous(name, n) for n, who in enumerate(order, 1)}
        lines = [
            made_line(name, idx, voices[who], idx * 30_000, idx * 30_000 + 29_000, text)
            for idx, (who, text) in enumerate(turns)
        ]
        for line in lines:
            line.rev = 1  # as stored: no user edit
        # The analyst asks next, speaking for the first time; a next speaker heard before (an executive answering
        # straight away) is not the one named.
        first_turn = {who: idx for idx, (who, _) in reversed(list(enumerate(turns)))}
        named: dict[str, set[str]] = {}
        for idx, (_, text) in enumerate(turns[:-1]):
            asker = turns[idx + 1][0]
            for found in ASKS_NEXT.finditer(text):
                if first_turn[asker] == idx + 1:
                    named.setdefault(asker, set()).add(found[1])
        analysts = {voices[who].id: names.pop() for who, names in named.items() if len(names) == 1}
        sure = {
            speaker
            for speaker, said in analysts.items()
            if sum(re.search(rf"\b{re.escape(said.split()[0])}\b", text) is not None for _, text in turns) >= 2
        }
        gold: dict[UUID, str | None] = dict(analysts)
        known = {each.id: each for each in voices.values()}
        cases.append(Case(name, "earnings", lines, known, gold=gold, sure=sure))
    return cases


def heard(case: Case, rng: random.Random) -> dict[int, str]:
    """The simulated rerun: each line in its true speaker's voice, MISHEARD of them in another's."""
    people = sorted(set(case.truth.values()))
    voices = {}
    for idx, person in case.truth.items():
        wrong = rng.random() < MISHEARD and len(people) > 1
        voices[idx] = rng.choice([each for each in people if each != person]) if wrong else person
    return voices


def one_voice(case: Case, rng: random.Random) -> list[frozenset[UUID]]:
    """The simulated voiceprints, through the real rule (`speakers.voice_groups`): pyannote's identify scores each
    true voice against each speaker's voiceprint, around 86 for that voice's own speakers and 16 for another's (its
    documented example), with noise (standard deviation VOICE_NOISE), so a split speaker can be missed and two people
    joined wrongly."""
    if case.slice != "ami":
        return []
    people = sorted({person for speaker in case.known if (person := majority(case, speaker)) is not None})
    labels = {f"p{n}": speaker for n, speaker in enumerate(case.known)}
    heard = []
    for person in people:
        scores = {
            label: min(100.0, max(0.0, rng.gauss(86 if majority(case, speaker) == person else 16, VOICE_NOISE)))
            for label, speaker in labels.items()
        }
        heard.append({"speaker": person, "confidence": scores})
    groups = speakers.voice_groups(heard, labels)
    return groups


def raw_cues(answer: str) -> int:
    """How many cues the model gave, before any was checked."""
    try:
        said = json.loads(answer)
    except ValueError:
        return 0
    people = said.get("people") if isinstance(said, dict) else None
    moves = said.get("moves") if isinstance(said, dict) else None
    cues = sum(len(person.get("cues") or []) for person in people or [] if isinstance(person, dict))
    return cues + sum(1 for move in moves or [] if isinstance(move, dict) and move.get("cue"))


def majority(case: Case, speaker: UUID) -> str | None:
    """Who a speaker of the broken transcript mostly is."""
    counted = Counter(case.truth[line.idx] for line in case.lines if line.speaker_id == speaker)
    return counted.most_common(1)[0][0] if counted else None


def same_person(gold: str | None, name: str | None) -> bool:
    return gold is not None and name is not None and gold.split()[0].casefold() == name.split()[0].casefold()


def gold_of(case: Case, speaker: UUID) -> list[str] | None:
    """The names a speaker may rightly get; None when the truth doesn't say (an AMI speaker who never says theirs)."""
    if case.slice == "names":
        gold = case.gold.get(speaker)
        return [] if gold is None else [gold]
    if case.slice == "earnings":
        # Only the analysts the operator names are known; an executive's name is unverifiable.
        gold = case.gold.get(speaker)
        return None if gold is None else [gold]
    letter = majority(case, speaker)
    return case.known_names.get(letter or "")


def scored(case: Case, answer: str, rng: random.Random, prints: bool = False) -> Counter[str]:
    """The case's counts for one answer, through the real rules; with the simulated voiceprints when `prints`."""
    proposal = speakers.proposal_of(answer, {line.idx: line for line in case.lines}, case.known)
    voices = heard(case, rng) if case.slice == "ami" else None
    changes = speakers.decide(proposal, case.known, case.lines, voices, one_voice(case, rng) if prints else [])
    counts: Counter[str] = Counter()
    counts["cues given"] += raw_cues(answer)
    counts["cues kept"] += sum(len(person.cues) for person in proposal.people) + len(proposal.moves)
    for change in changes:
        if change.kind == "name":
            gold = gold_of(case, change.speakerId)
            if gold is None:
                counts[f"names {change.state} unverifiable"] += 1
                continue
            right = any(same_person(each, change.name) for each in gold)
            counts[f"names {change.state}"] += 1
            counts[f"names {change.state} right"] += right
            counts["false names"] += change.state == "applied" and not right
            counts["sure named"] += change.state == "applied" and right and change.speakerId in case.sure
        elif change.kind == "merge" and change.intoId is not None:
            counts["merges"] += 1
            counts["merges right"] += majority(case, change.speakerId) == majority(case, change.intoId)
            counts["splits joined"] += case.split is not None and {change.speakerId, change.intoId} == set(case.split)
        elif change.kind == "move" and change.intoId is not None:
            line = next(line for line in case.lines if line.id == change.lineIds[0])
            counts["moves"] += 1
            counts["moves right"] += case.truth.get(line.idx) == majority(case, change.intoId)
            counts["flips fixed"] += line.idx in case.flipped and case.truth.get(line.idx) == majority(
                case, change.intoId
            )
    counts["sure"] += len(case.sure)
    counts["splits"] += case.split is not None
    counts["flips"] += len(case.flipped)
    return counts


def rate(counts: Counter[str], right: str, total: str) -> float | None:
    """The share right, or None (n/a) with nothing to count."""
    return counts[right] / counts[total] if counts[total] else None


# Each rate: the count right, and the count it is out of (printed beside it, so a rate on four is read as such).
RATES = {
    "cue validity": ("cues kept", "cues given"),
    "name precision": ("names applied right", "names applied"),
    "sure name recall": ("sure named", "sure"),
    "suggestion precision": ("names suggested right", "names suggested"),
    "merge precision": ("merges right", "merges"),
    "split recall": ("splits joined", "splits"),
    "move precision": ("moves right", "moves"),
    "flip recall": ("flips fixed", "flips"),
}


def rates(counts: Counter[str]) -> dict[str, float | None]:
    made: dict[str, float | None] = {metric: rate(counts, right, total) for metric, (right, total) in RATES.items()}
    made["false names"] = float(counts["false names"])
    made["names unverifiable"] = float(counts["names applied unverifiable"])
    return made


def shown(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def missed(row: dict[str, float | None], gates: dict[str, tuple[str, float, bool]]) -> list[str]:
    failed = []
    for metric, (side, bound, measured) in gates.items():
        value = row[metric]
        if value is None:
            if measured:
                failed.append(f"{metric} n/a (gate {side} {bound}, nothing counted)")
            continue
        if (side == ">=" and value < bound) or (side == "<=" and value > bound):
            failed.append(f"{metric} {value:.3f} (gate {side} {bound})")
    return failed


def cases() -> list[Case]:
    rng = random.Random(SEED)
    manifest = json.loads(MANIFEST.read_text())
    archive = fetch(json.loads(AMI_MANIFEST.read_text())["annotations"])
    ami = [broken(ami_meeting(archive, name), rng) for name in manifest["meetings"]]
    return [*ami, *named_cases(), *earnings_cases()]


async def answer_of(case: Case, tiers: Tiers) -> tuple[str, str, float]:
    """The case's name, the model's answer for it as the loop asks (no draft: the transcript alone; empty when the
    model never answered) and how long the call took, in ms."""
    recorder = Recorder(tiers.polish)
    asked = Tiers(polish=recorder, command=recorder, fallback=None)
    started = time.monotonic()
    await speaker_loop.proposed(asked, case.lines, case.known, None, [])
    took = (time.monotonic() - started) * 1000
    answer = recorder.outputs[-1][1] if recorder.outputs else ""
    return case.name, answer, took


def report(made: list[Case], answers: dict[str, str]) -> int:
    rng, printed = random.Random(SEED), random.Random(SEED)
    totals: dict[str, Counter[str]] = {
        "ami": Counter(),
        "names": Counter(),
        "earnings": Counter(),
        "ami voiceprints": Counter(),
    }
    for case in made:
        if case.name in answers:
            totals[case.slice] += scored(case, answers[case.name], rng)
            if case.slice == "ami":
                totals["ami voiceprints"] += scored(case, answers[case.name], printed, prints=True)
    failed = []
    for name, counts in totals.items():
        row = rates(counts)
        shown_row = [
            f"{metric} {shown(value)}" + (f" (n {counts[RATES[metric][1]]})" if metric in RATES else "")
            for metric, value in row.items()
        ]
        print(f"{name}: " + ", ".join(shown_row))
        failed += [f"{name} {miss}" for miss in missed(row, GATES.get(name, {}))]
    for miss in failed:
        print(f"gate missed: {miss}")
    return 1 if failed else 0


def replay() -> int:
    if not RECORDED.exists():
        raise SystemExit(f"no recorded answers yet: run --live once ({RECORDED})")
    answers = json.loads(RECORDED.read_text())
    return report(cases(), answers)


def run_live(budget_usd: float | None, ledger: Path) -> int:
    budget_usd = require_budget(budget_usd, "The live tier")
    config = load(os.environ)
    budget = Budget(budget_usd, ledger_total(ledger))
    provider = make_provider(config.polish, config.keys, config.temperature)
    metered = Metered(provider, budget)
    tiers = Tiers(polish=metered, command=metered, fallback=None)
    made = cases()

    async def asked() -> list[tuple[str, str, float]]:
        try:
            jobs = [lambda case=case: answer_of(case, tiers) for case in made]
            return await within(budget, jobs, CONCURRENCY)
        finally:
            await provider.aclose()

    made_answers = asyncio.run(asked())
    answers = {name: answer for name, answer, _ in made_answers if answer}
    took = sorted(ms for _, answer, ms in made_answers if answer)
    append_ledger(ledger, budget.spent, budget.items, f"eval-speakers {config.polish}", f"{len(answers)} meetings")
    if len(answers) < len(made):
        # A run the model didn't answer in full (offline, refused) never replaces the recorded answers.
        print(f"only {len(answers)} of {len(made)} meetings answered: the recorded answers are kept")
        return 1
    RECORDED.write_text(json.dumps(answers, indent=2, ensure_ascii=False) + "\n")
    print(f"$ this run: {budget.spent:.4f}; ledger total: {budget.before + budget.spent:.4f}")
    # MOM-04's 3 minutes from stop to minutes hold the minutes' own p95 gate (140 s, eval_minutes.py) and this call.
    p95 = took[min(len(took) - 1, int(len(took) * 0.95))]
    print(f"speakers call p95 ms {p95:.0f} (gate <= {CALL_P95_MS})")
    slow = p95 > CALL_P95_MS
    return report(made, answers) or int(slow)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    options = parser.parse_args()
    return run_live(options.budget_usd, options.ledger) if options.live else replay()


if __name__ == "__main__":
    sys.exit(main())
