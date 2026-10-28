"""The speaker loop's eval scores what it claims (scripts/eval_speakers.py, MOM-08): a broken diarization is split and
flipped as documented, and each metric counts a right change as right and a wrong one as wrong."""

import json
import random
from uuid import uuid4

import eval_speakers as ev
from eval_minutes import Meeting, made_line

from snapbak_backend.db.models import Speaker


def meeting() -> Meeting:
    """Four AMI-like speakers: A talks most."""
    known = {
        letter: Speaker(id=uuid4(), recording_id=uuid4(), label=letter, vendor_label=letter, name=None)
        for letter in "ABCD"
    }
    said = "AAABACADAAABA"
    lines = [
        made_line("TS0000a", idx, known[letter], idx * 1_000, idx * 1_000 + 900, "So.")
        for idx, letter in enumerate(said)
    ]
    return Meeting("TS0000a", lines, {each.id: each for each in known.values()}, [])


def test_mom08_a_broken_diarization_splits_the_loudest_speaker_in_two_and_flips_its_share_of_lines() -> None:
    case = ev.broken(meeting(), random.Random(7))
    assert len(case.known) == 5 and case.split is not None
    first, half = case.split
    halves = {line.idx for line in case.lines if line.speaker_id == half}
    # A's second half (its lines 6, 8, 9, 10 and 12 of 0, 1, 2, 4, 6, 8, 9, 10, 12) is the new speaker, but for a
    # line flipped away.
    assert halves <= {6, 8, 9, 10, 12} and first in case.known
    assert len(case.flipped) == int(len(case.lines) * ev.FLIPPED)
    assert all(line.rev == 1 for line in case.lines)


NOTHING = json.dumps({"count": 0, "people": [], "moves": []})


def test_mom08_an_answer_that_proposes_nothing_merges_nothing_unless_voiceprints_are_simulated() -> None:
    case = ev.broken(meeting(), random.Random(7))
    assert ev.scored(case, NOTHING, random.Random(1))["merges"] == 0
    # Simulated voiceprints hear the two halves as one voice: right by construction, so never gated.
    printed = ev.scored(case, NOTHING, random.Random(1), prints=True)
    assert printed["merges"] == printed["merges right"] == printed["splits joined"] == 1


def test_mom08_nothing_counted_is_n_a_and_misses_only_a_gate_that_must_be_measured() -> None:
    rates = ev.rates(ev.scored(ev.broken(meeting(), random.Random(7)), NOTHING, random.Random(1)))
    assert rates["merge precision"] is None and rates["move precision"] is None
    # A precision with nothing counted passes, but for moves: MOM-08 moves lines, so an AMI run must measure them.
    assert ev.missed(rates, ev.GATES["ami"]) == ["move precision n/a (gate >= 0.9, nothing counted)"]
    [case] = [each for each in ev.named_cases() if each.name == "intro-both"]
    named = ev.rates(ev.scored(case, NOTHING, random.Random(1)))
    assert ev.missed(named, ev.GATES["names"]) == [
        "cue validity n/a (gate >= 0.95, nothing counted)",
        "name precision n/a (gate >= 0.98, nothing counted)",
        "sure name recall 0.000 (gate >= 0.5)",
    ]


def test_mom08_a_wrong_name_applied_is_a_false_name_and_misses_its_gates() -> None:
    [case] = [each for each in ev.named_cases() if each.name == "intro-both"]
    first = case.lines[0].speaker_id
    # The meeting names Speaker 1 "Joanne", but suppose the truth were "Ruth": the applied name is false.
    case.gold[first] = "Ruth"
    joanne = {"p": 1, "name": "Joanne", "cues": [{"i": 0, "quote": "I'm Joanne"}, {"i": 1, "quote": "Thanks Joanne"}]}
    counts = ev.scored(case, json.dumps({"count": 2, "people": [joanne], "moves": []}), random.Random(1))
    assert (counts["names applied"], counts["false names"]) == (1, 1)
    assert "false names 1.000 (gate <= 0)" in ev.missed(ev.rates(counts), ev.GATES["names"])


def test_mom08_a_name_is_right_against_gold_and_unverifiable_without_it() -> None:
    [case] = [each for each in ev.named_cases() if each.name == "intro-both"]
    answer = json.dumps(
        {
            "count": 2,
            "people": [
                {
                    "p": 1,
                    "name": "Joanne",
                    "cues": [{"i": 0, "quote": "I'm Joanne"}, {"i": 1, "quote": "Thanks Joanne"}],
                },
                {
                    "p": 2,
                    "name": "Ruth",
                    "cues": [{"i": 2, "quote": "Noah, can you"}, {"i": 4, "quote": "thank you Noah"}],
                },
            ],
            "moves": [],
        }
    )
    counts = ev.scored(case, answer, random.Random(1))
    rates = ev.rates(counts)
    # "Ruth" holds in no cue, so it never stands; Joanne is right.
    assert (counts["names applied"], counts["names applied right"], counts["false names"]) == (1, 1, 0)
    assert rates["name precision"] == 1.0
    assert [miss for miss in ev.missed(rates, ev.GATES["names"]) if "precision" in miss] == []


def test_mom08_long_calls_gate_their_cues_their_recall_and_their_names() -> None:
    """The earnings floors sit below the live runs (cues 0.769 and 0.815; recall 0.150 to 0.230): a model that
    mis-quotes most cues, or names almost no one, fails; a wrong name always does."""
    passing: dict[str, float | None] = {
        "cue validity": 0.75,
        "name precision": 1.0,
        "false names": 0.0,
        "sure name recall": 0.15,
    }
    assert ev.missed(passing, ev.GATES["earnings"]) == []
    worse: dict[str, float | None] = {**passing, "cue validity": 0.69, "sure name recall": 0.11, "false names": 1.0}
    assert ev.missed(worse, ev.GATES["earnings"]) == [
        "cue validity 0.690 (gate >= 0.7)",
        "false names 1.000 (gate <= 0)",
        "sure name recall 0.110 (gate >= 0.12)",
    ]
