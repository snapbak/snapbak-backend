"""The speaker identification loop's rules (services/speakers.py, MEETINGS.md D42, PRD MOM-08): the model proposes,
the pyannote rerun must agree, the user's own edits are never touched, and every applied change can be undone."""

import json
from uuid import uuid4

import pytest

from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import ActionItemOut, Cue, DecisionOut, DerivationOut, MinutesOut
from snapbak_backend.services import speakers
from snapbak_backend.transcription.merge import Turn


def speaker(n: int, name: str | None = None, source: str | None = None) -> Speaker:
    made = Speaker(
        id=uuid4(),
        recording_id=uuid4(),
        label=f"Speaker {n}",
        vendor_label="user" if source == "user" else "A",
        name=name,
        name_source=source if name else None,
    )
    return made


def line(idx: int, who: Speaker, text: str, start: int | None = None, rev: int = 1) -> TranscriptLine:
    at = idx * 10_000 if start is None else start
    words = [{"w": word, "s": at + n * 300, "e": at + n * 300 + 250} for n, word in enumerate(text.split())]
    made = TranscriptLine(
        id=uuid4(),
        recording_id=who.recording_id,
        speaker_id=who.id,
        idx=idx,
        start_ms=at,
        end_ms=words[-1]["e"],
        text=text,
        words=words,
        rev=rev,
    )
    return made


def answer(people: list[dict], moves: list[dict] | None = None, count: int = 0) -> str:
    return json.dumps({"count": count, "people": people, "moves": moves or []})


def cue(i: int, quote: str) -> dict:
    return {"i": i, "quote": quote}


# A housewarming: Speaker 1 is Joanne, Speakers 2 and 4 are both Noah's voice split in two, Speaker 3 is Ian.
ONE, TWO, THREE, FOUR = speaker(1), speaker(2), speaker(3), speaker(4)
SPEAKERS = {each.id: each for each in (ONE, TWO, THREE, FOUR)}
LINES = [
    line(0, TWO, "Joanne, did you get the mezuzah?"),
    line(1, ONE, "Yes Noah, I got it today."),
    line(2, FOUR, "Thanks Joanne, it looks great."),
    line(3, ONE, "Thank you."),
    line(4, THREE, "Mazel tov to you both."),
    line(5, ONE, "Thank you Ian."),
    line(6, THREE, "You're welcome."),
    line(7, TWO, "I will hang it tomorrow."),
]
BY_INDEX = {each.idx: each for each in LINES}


def proposal(text: str) -> speakers.Proposal:
    made = speakers.proposal_of(text, BY_INDEX, SPEAKERS)
    return made


def test_mom08_a_cue_counts_only_when_its_quote_is_the_line_s_words_and_holds_the_name() -> None:
    said = proposal(
        answer(
            [
                {
                    "p": 1,
                    "name": "Joanne",
                    "cues": [
                        cue(0, "Joanne, did you"),
                        cue(2, "Thanks Joanne"),
                        cue(1, "I got it today"),
                        cue(3, "Joanne"),
                        cue(9, "Joanne"),
                    ],
                    "same": [],
                }
            ]
        )
    )
    # Line 1's words are real but don't name her; line 3 doesn't say "Joanne", but line 2 beside it does, so that cue
    # is line 2's (two cues on one line still count once); line 9 doesn't exist.
    assert [each.i for each in said.people[0].cues] == [0, 2, 2]


def test_mom08_a_name_with_two_cues_on_two_lines_applies_as_guessed_one_cue_is_a_suggestion() -> None:
    said = proposal(
        answer(
            [
                {"p": 1, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(2, "Thanks Joanne")], "same": []},
                {"p": 3, "name": "Ian", "cues": [cue(5, "Thank you Ian")], "same": []},
                {"p": None, "name": "Rabbi Eden", "cues": [], "same": []},
            ]
        )
    )
    changes = speakers.decide(said, SPEAKERS, LINES, None)
    names = {(change.kind, change.speakerId, change.name, change.state) for change in changes}
    assert names == {("name", ONE.id, "Joanne", "applied"), ("name", THREE.id, "Ian", "suggested")}


def test_mom08_two_cues_from_the_same_line_are_one_cue() -> None:
    said = proposal(answer([{"p": 1, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(0, "Joanne, did")]}]))
    [change] = speakers.decide(said, SPEAKERS, LINES, None)
    assert change.state == "suggested"


def test_mom08_a_speaker_the_user_named_is_never_renamed_and_a_name_two_speakers_claim_is_only_suggested() -> None:
    mine = speaker(1, "Joane", "user")
    other = speaker(2)
    known = {mine.id: mine, other.id: other}
    lines = [line(0, other, "Joanne, did you get it?"), line(1, mine, "Thanks Joanne, yes.")]
    said = speakers.proposal_of(
        answer(
            [
                {"p": 1, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(1, "Thanks Joanne")]},
                {"p": 2, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(1, "Thanks Joanne")]},
            ]
        ),
        {each.idx: each for each in lines},
        known,
    )
    changes = speakers.decide(said, known, lines, None)
    assert [(change.speakerId, change.state) for change in changes] == [(other.id, "suggested")]


def clusters(**by_line: str) -> dict[int, str]:
    return {int(key[1:]): value for key, value in by_line.items()}


def test_mom08_a_merge_applies_only_when_the_model_and_the_rerun_agree() -> None:
    said = proposal(answer([{"p": 2, "name": None, "cues": [], "same": [4]}], count=3))
    # The rerun puts Speaker 2's and Speaker 4's lines in one voice.
    agreed = speakers.decide(
        said, SPEAKERS, LINES, clusters(l0="X", l1="Y", l2="X", l3="Y", l4="Z", l5="Y", l6="Z", l7="X")
    )
    assert [(c.kind, c.speakerId, c.intoId, c.state) for c in agreed] == [("merge", FOUR.id, TWO.id, "applied")]
    # It hears two voices: no merge, and no suggestion either.
    apart = speakers.decide(
        said, SPEAKERS, LINES, clusters(l0="X", l1="Y", l2="W", l3="Y", l4="Z", l5="Y", l6="Z", l7="X")
    )
    assert apart == []
    # No rerun (the count gave none): no merge.
    assert speakers.decide(said, SPEAKERS, LINES, None) == []


def test_mom08_the_merge_keeps_the_named_speaker_then_the_one_with_more_speech_and_never_two_names() -> None:
    named = speaker(5, "Noah", "vendor")
    plain = speaker(6)
    rival = speaker(7, "Ian", "vendor")
    known = {each.id: each for each in (named, plain, rival)}
    lines = [
        line(0, plain, "I will hang it tomorrow morning."),
        line(1, named, "Yes, of course."),
        line(2, rival, "Hi."),
    ]
    by_index = {each.idx: each for each in lines}
    voices = {0: "X", 1: "X", 2: "X"}
    said = speakers.proposal_of(answer([{"p": 6, "name": None, "cues": [], "same": [5, 7]}]), by_index, known)
    changes = speakers.decide(said, known, lines, voices)
    # Named beats unnamed, though Speaker 6 spoke most; of the two names, Noah spoke more; Noah and Ian never merge.
    assert [(c.speakerId, c.intoId, c.before) for c in changes] == [(plain.id, named.id, "Speaker 6")]


def test_mom08_a_speaker_the_user_named_is_never_merged_away() -> None:
    mine = speaker(1, "Joane", "user")
    plain = speaker(2)
    known = {mine.id: mine, plain.id: plain}
    lines = [line(0, mine, "Hello."), line(1, plain, "We said many words here today.")]
    said = speakers.proposal_of(
        answer([{"p": 2, "name": None, "same": [1]}]), {each.idx: each for each in lines}, known
    )
    [change] = speakers.decide(said, known, lines, {0: "X", 1: "X"})
    assert (change.speakerId, change.intoId) == (plain.id, mine.id)


def test_mom08_a_line_moves_only_when_the_model_and_the_rerun_agree_and_the_user_never_touched_it() -> None:
    edited = line(8, ONE, "Mine, edited.", rev=2)
    lines = [*LINES, edited]
    by_index = {each.idx: each for each in lines}
    # The model: line 7 is Joanne's (Speaker 1), and line 8 Ian's.
    said = speakers.proposal_of(
        answer(
            [], moves=[{"i": 7, "to": 1, "cue": cue(7, "I will hang it")}, {"i": 8, "to": 3, "cue": cue(8, "Mine")}]
        ),
        by_index,
        SPEAKERS,
    )
    voices = clusters(l0="X", l1="Y", l2="W", l3="Y", l4="Z", l5="Y", l6="Z", l7="Y", l8="Z")
    changes = speakers.decide(said, SPEAKERS, lines, voices)
    assert [(c.kind, c.lineIds, c.speakerId, c.intoId) for c in changes] == [("move", [LINES[7].id], TWO.id, ONE.id)]


def test_mom08_the_rerun_relabels_each_line_by_the_voice_holding_most_of_its_words() -> None:
    turns = [Turn("V1", 0, 10_400), Turn("V2", 10_400, 30_000), Turn("V3", 30_000, 99_000)]
    voices = speakers.relabel(LINES[:4], turns)
    # Line 1's words run 10,000 to 11,750, its first in V1's turn and the rest in V2's: V2's.
    assert voices == {0: "V1", 1: "V2", 2: "V2", 3: "V3"}


def test_mom08_a_second_round_runs_only_on_a_big_change() -> None:
    move = speakers.Change(id="c", kind="move", state="applied", speakerId=ONE.id, intoId=TWO.id)
    merge = move.model_copy(update={"kind": "merge"})
    many_lines = [LINES[0].id]
    assert not speakers.big([move.model_copy(update={"lineIds": many_lines})], 10)
    assert speakers.big([move.model_copy(update={"lineIds": [uuid4(), uuid4()]})], 10)
    assert speakers.big([merge, merge], 100) and not speakers.big([merge], 100)


def test_mom08_only_sections_that_cite_a_moved_line_are_written_again_and_the_texts_when_any_moved() -> None:
    cited = DerivationOut(lineId=LINES[7].id, speaker="Speaker 2", atMs=70_000, quote="I will hang it")
    other = DerivationOut(lineId=LINES[1].id, speaker="Speaker 1", atMs=10_000, quote="Yes Noah")
    doc = MinutesOut.model_validate(
        {
            "summary": "s",
            "mom": "m",
            "openQuestions": [],
            "decisions": [],
            "actionItems": [ActionItemOut(id="a", text="Hang it", derivedFrom=[cited])],
            "followUps": [ActionItemOut(id="f", text="Thank", derivedFrom=[other])],
        }
    )
    assert speakers.touched(doc, {LINES[7].id}) == ["summary", "mom", "actionItems"]
    assert speakers.touched(doc, set()) == []


def test_mom08_two_speakers_the_user_named_alike_are_still_never_merged() -> None:
    first, second = speaker(1, "Noah", "user"), speaker(2, "Noah", "user")
    known = {first.id: first, second.id: second}
    lines = [line(0, first, "Hello there."), line(1, second, "Hello again.")]
    said = speakers.proposal_of(
        answer([{"p": 1, "name": None, "same": [2]}]), {each.idx: each for each in lines}, known
    )
    assert speakers.decide(said, known, lines, {0: "X", 1: "X"}) == []


def test_mom08_undoing_a_merge_rewrites_the_sections_naming_the_kept_speaker_or_citing_its_lines() -> None:
    kept_line = DerivationOut(lineId=LINES[0].id, speaker="Noah", atMs=0, quote="Joanne")
    doc = MinutesOut.model_validate(
        {
            "summary": "{{p:2}} hosts.",
            "mom": "### Party\n\n{{p:1}} came.",
            "openQuestions": [],
            "decisions": [DecisionOut(id="d", text="Ask {{p:2}}", derivedFrom=[])],
            "actionItems": [ActionItemOut(id="a", text="Hang it", derivedFrom=[kept_line])],
        }
    )
    merge = speakers.Change(id="c", kind="merge", state="applied", speakerId=FOUR.id, intoId=TWO.id, lineIds=[uuid4()])
    assert speakers.undone_sections(doc, merge, 2) == ["summary", "decisions"]
    move = merge.model_copy(update={"kind": "move", "lineIds": [LINES[0].id]})
    assert speakers.undone_sections(doc, move, 2) == ["summary", "mom", "actionItems"]


def named_by(text_by: list[tuple[Speaker, str]], person: dict) -> list[speakers.Change]:
    lines = [line(idx, who, text) for idx, (who, text) in enumerate(text_by)]
    known = {who.id: who for who, _ in text_by}
    said = speakers.proposal_of(answer([person]), {each.idx: each for each in lines}, known)
    return speakers.decide(said, known, lines, None)


def test_mom08_a_speaker_saying_a_name_names_themselves_only_by_introducing_themselves() -> None:
    me, you = speaker(1), speaker(2)
    intro = [(me, "Hi, my name is uh Danny Wolfs."), (you, "Hello."), (me, "Okay, I'm Danny Wolfs, project lead.")]
    [took] = named_by(intro, {"p": 1, "name": "Danny Wolfs", "cues": [cue(0, "Danny Wolfs"), cue(2, "Danny Wolfs")]})
    assert took.state == "applied"
    # Talking about someone else, or to them, is no name for the speaker (TS3003d, TS3007d).
    about = [(me, "This is what me and Richard came up with."), (you, "Nice."), (me, "Richard's argument was good.")]
    assert named_by(about, {"p": 1, "name": "Richard", "cues": [cue(0, "me and Richard"), cue(2, "Richard's")]}) == []


def test_mom08_a_name_said_to_someone_names_the_speaker_next_to_it_never_one_merely_mentioned() -> None:
    host, maya, other = speaker(1), speaker(2), speaker(3)
    addressed = [
        (host, "Over to you, Maya."),
        (maya, "Thanks, sales grew."),
        (other, "Okay. Maya, how much is new?"),
        (maya, "About half."),
    ]
    [took] = named_by(addressed, {"p": 2, "name": "Maya", "cues": [cue(0, "you, Maya"), cue(2, "Maya, how")]})
    assert took.state == "applied"
    # Named while not next to it: no cue for them.
    far = [(host, "Thanks, Maya."), (other, "Right."), (host, "Fine."), (maya, "Hi.")]
    assert named_by(far, {"p": 2, "name": "Maya", "cues": [cue(0, "Thanks, Maya")]}) == []
    # Named in the third person, though next to it: no cue either.
    mentioned = [(host, "Noah is on the phone."), (maya, "Okay."), (host, "Noah said go ahead.")]
    assert named_by(mentioned, {"p": 2, "name": "Noah", "cues": [cue(0, "Noah is"), cue(2, "Noah said")]}) == []


def test_mom08_a_name_said_between_two_other_speakers_names_neither() -> None:
    """Crosstalk (AMI TS3007d): "Daniel." with one speaker before it and another after is no cue for either."""
    asker, before, after = speaker(1), speaker(2), speaker(3)
    said = [
        (before, "Yes."),
        (asker, "Daniel."),
        (after, "I'm the interface expert."),
        (before, "Right."),
        (asker, "Okay, Daniel."),
        (after, "Sure."),
    ]
    assert named_by(said, {"p": 3, "name": "Daniel", "cues": [cue(1, "Daniel."), cue(4, "Okay, Daniel.")]}) == []


def test_mom08_one_voice_matching_two_speakers_voiceprints_makes_them_one_voice() -> None:
    labels = {"p1": ONE.id, "p2": TWO.id, "p4": FOUR.id}
    heard = [
        {"speaker": "SPEAKER_00", "match": "p2", "confidence": {"p1": 10, "p2": 88, "p4": 81}},
        {"speaker": "SPEAKER_01", "match": "p1", "confidence": {"p1": 91, "p2": 12, "p4": 70}},
    ]
    assert speakers.voice_groups(heard, labels) == [frozenset({TWO.id, FOUR.id})]
    assert speakers.voice_groups([{"speaker": "S", "confidence": "bad"}, "nothing"], labels) == []


def test_mom08_a_voice_split_in_two_merges_on_its_voiceprints_unless_the_words_say_two_people() -> None:
    said = proposal(answer([]))
    [merge] = speakers.decide(said, SPEAKERS, LINES, None, same_voice=[frozenset({TWO.id, FOUR.id})])
    assert (merge.kind, merge.speakerId, merge.intoId, merge.state) == ("merge", FOUR.id, TWO.id, "applied")
    noah, ian = speaker(2, "Noah", "vendor"), speaker(4, "Ian", "vendor")
    known = {ONE.id: ONE, noah.id: noah, THREE.id: THREE, ian.id: ian}
    lines = [line(0, noah, "Hello."), line(1, ian, "Hi.")]
    assert speakers.decide(said, known, lines, None, same_voice=[frozenset({noah.id, ian.id})]) == []


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ({"people": [{"p": [1], "name": "Joanne"}]}, [(None, [])]),
        ({"people": [{"p": True, "name": "Joanne"}]}, [(None, [])]),
        ({"people": [{"p": 1, "name": 5, "cues": 5, "same": 5}]}, [(1, [])]),
        ({"people": [{"p": 2, "name": None, "same": [[4], {"p": 4}, True, 4]}]}, [(2, [4])]),
        ({"people": "everyone", "moves": {"i": 0}, "count": True}, []),
        ({"moves": [{"i": [0], "to": 1, "cue": cue(0, "Joanne")}, {"i": 0, "to": {"p": 1}, "cue": cue(0, "J")}]}, []),
    ],
)
def test_mom08_an_answer_of_the_wrong_shapes_keeps_only_what_is_the_shape_asked_for(
    said: dict, kept: list[tuple[int | None, list[int]]]
) -> None:
    found = proposal(json.dumps(said))
    assert [(person.p, person.same) for person in found.people] == kept
    assert (found.moves, found.count) == ({}, 0)


def test_mom08_a_name_another_speaker_already_has_is_only_suggested() -> None:
    rival = speaker(3, "Joanne", "vendor")
    known = {ONE.id: ONE, TWO.id: TWO, rival.id: rival, FOUR.id: FOUR}
    lines = [each for each in LINES if each.speaker_id != THREE.id]
    said = speakers.proposal_of(
        answer([{"p": 1, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(2, "Thanks Joanne")]}]), BY_INDEX, known
    )
    [change] = speakers.decide(said, known, lines, None)
    assert (change.speakerId, change.state) == (ONE.id, "suggested")


def test_mom08_a_name_the_loop_guessed_is_never_guessed_over() -> None:
    guessed = speaker(1, "Joan", "guessed")
    guessed.id = ONE.id
    known = {**SPEAKERS, ONE.id: guessed}
    said = speakers.proposal_of(
        answer([{"p": 1, "name": "Joanne", "cues": [cue(0, "Joanne"), cue(2, "Thanks Joanne")]}]), BY_INDEX, known
    )
    assert speakers.decide(said, known, LINES, None) == []


def test_mom08_an_empty_transcript_never_runs_a_second_round() -> None:
    assert not speakers.big([], 0)


def test_mom08_two_voice_groups_sharing_a_speaker_merge_both_into_it_never_in_a_chain() -> None:
    a, b, c = speaker(1), speaker(2), speaker(3)
    known = {each.id: each for each in (a, b, c)}
    # C talks most, then B: alone, the second group would keep C and merge B, which just kept A, into it.
    lines = [line(0, c, "I talk the most here today, really."), line(1, b, "I talk a bit."), line(2, a, "Hi.")]
    said = speakers.proposal_of(answer([]), {each.idx: each for each in lines}, known)
    changes = speakers.decide(said, known, lines, None, [frozenset({a.id, b.id}), frozenset({b.id, c.id})])
    assert sorted((c_.speakerId, c_.intoId) for c_ in changes) == sorted([(a.id, b.id), (c.id, b.id)])


def test_mom08_a_line_too_short_to_hear_its_voice_never_moves() -> None:
    a, b = speaker(1), speaker(2)
    known = {a.id: a, b.id: b}
    lines = [
        line(0, a, "Shall we ship on Friday then?"),
        line(1, a, "Yeah."),
        line(2, b, "We could ship it then, once the billing page and the FAQ are done."),
    ]
    by_index = {each.idx: each for each in lines}
    moves = [{"i": 1, "to": 2, "cue": cue(1, "Yeah.")}, {"i": 0, "to": 2, "cue": cue(0, "Shall we ship")}]
    said = speakers.proposal_of(answer([], moves), by_index, known)
    voices = {0: "B", 1: "B", 2: "B"}
    # "Yeah." is too short to judge by its voice; the full question moves.
    assert [change.lineIds for change in speakers.decide(said, known, lines, voices)] == [[lines[0].id]]


def test_mom08_a_host_handing_over_by_name_names_who_speaks_next() -> None:
    host, cfo = speaker(1), speaker(2)
    lines = [
        line(0, host, "With that, I'll turn the call over to our CFO, Brian D'Ambrosia."),
        line(1, cfo, "Thank you, Brett, and good morning everyone."),
        line(2, host, "Our next question comes from the line of Brian Nagel with Oppenheimer."),
    ]
    by_index = {each.idx: each for each in lines}
    handover = Cue(i=0, quote="turn the call over to our CFO, Brian D'Ambrosia")
    # Handed over on the line just before theirs: Brian is the CFO; the analyst named on the last line speaks no line.
    assert speakers.supports(handover, cfo, by_index, "Brian D'Ambrosia")
    assert not speakers.supports(Cue(i=2, quote="Brian Nagel"), cfo, by_index, "Brian Nagel")


def test_mom08_thanks_by_name_at_the_start_of_a_line_names_the_speaker_before_it() -> None:
    ceo, cfo, operator = speaker(1), speaker(2), speaker(3)
    lines = [
        line(0, ceo, "That covers the quarter; over to you for the numbers."),
        line(1, cfo, "Thank you, Brett, and good morning everyone."),
        line(2, operator, "Thank you. We will now begin the question and answer session."),
    ]
    by_index = {each.idx: each for each in lines}
    # Two others around the line, but the thanks opens it: it answers the line before.
    assert speakers.supports(Cue(i=1, quote="Thank you, Brett"), ceo, by_index, "Brett")
    assert not speakers.supports(Cue(i=1, quote="Thank you, Brett"), operator, by_index, "Brett")


def test_mom08_a_cue_quoted_from_the_line_beside_the_one_named_is_kept_at_its_own_line() -> None:
    found = proposal(answer([{"p": 1, "name": "Joanne", "cues": [cue(1, "Joanne, did you get")]}]))
    # The words are line 0's, not line 1's: the cue stands, at line 0.
    assert [(each.i, each.quote) for each in found.people[0].cues] == [(0, "Joanne, did you get")]


def test_mom08_a_cue_may_say_just_the_first_name() -> None:
    found = proposal(answer([{"p": 1, "name": "Joanne Smith", "cues": [cue(2, "Thanks Joanne"), cue(4, "Mazel tov")]}]))
    assert [each.i for each in found.people[0].cues] == [2]


def test_mom08_a_sentence_that_opens_on_a_name_calls_on_the_speaker_after_it() -> None:
    host, maya, arjun = speaker(1), speaker(2), speaker(3)
    lines = [
        line(0, maya, "About half of it, so six points."),
        line(1, host, "Great work, Maya. Arjun, you're up with hiring."),
        line(2, arjun, "Thanks. We hired four engineers and one designer."),
    ]
    by_index = {each.idx: each for each in lines}
    called = Cue(i=1, quote="Arjun, you're up with hiring.")
    assert speakers.supports(called, arjun, by_index, "Arjun")
    # Maya is spoken of, not called on, and she spoke before, not after.
    assert not speakers.supports(Cue(i=1, quote="Great work, Maya."), arjun, by_index, "Maya")


def test_mom08_a_name_guessed_this_round_keeps_its_speaker_in_a_merge_the_same_round() -> None:
    joanne, half = speaker(1), speaker(2)
    known = {joanne.id: joanne, half.id: half}
    # The half that talks most has no name; Speaker 1 introduces herself twice.
    lines = [
        line(0, joanne, "Hi, I'm Joanne, welcome."),
        line(1, half, "We should review the quarter and the plan for next year in full detail."),
        line(2, joanne, "As I said, I'm Joanne, the host."),
    ]
    by_index = {each.idx: each for each in lines}
    cues = [cue(0, "I'm Joanne"), cue(2, "I'm Joanne")]
    said = speakers.proposal_of(answer([{"p": 1, "name": "Joanne", "cues": cues}]), by_index, known)
    changes = speakers.decide(said, known, lines, None, [frozenset({joanne.id, half.id})])
    [merge] = [change for change in changes if change.kind == "merge"]
    # The named one is kept, though the other spoke more: the name is not merged away.
    assert (merge.speakerId, merge.intoId) == (half.id, joanne.id)
