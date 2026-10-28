"""The minutes and Q&A eval (docs/bench.md, "Minutes and Q&A evals"): its AMI and QMSum parsers on tiny excerpts,
its scorers on hand-worked examples, the validator truth table, the fake-provider plumbing and the live tier's guard
rails. No test here downloads anything or calls a model.

`tests/fixtures/meetings-eval/ami/` is adapted from meeting ES2004a of the AMI Meeting Corpus (CC BY 4.0,
amiproject.org): a few of speaker B's words, one of A's, their dialogue acts, and the abstractive summary sentences
they link to. `qmsum.jsonl` follows the shape of Yale-LILY/QMSum (MIT)."""

import asyncio
import json
import random
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import eval_minutes as ev
import pytest

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.schemas.cloud import ActionItemOut, AskResponse, CitationOut, ClaimOut, DerivationOut, MinutesOut
from snapbak_backend.services import minutes, qa, recordings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "meetings-eval"


def zipped(folder: Path, path: Path) -> Path:
    """The folder's files as a zip at `path`, at their paths inside it: the AMI annotations' layout."""
    with zipfile.ZipFile(path, "w") as made:
        for file in sorted(folder.rglob("*.xml")):
            made.write(file, file.relative_to(folder).as_posix())
    return path


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return zipped(FIXTURES / "ami", tmp_path / "ami.zip")


def meeting_of(archive: Path) -> ev.Meeting:
    return ev.ami_meeting(archive, "ES2004a")


def test_mom05_ami_lines_are_our_transcript_lines_with_role_names(archive: Path) -> None:
    meeting = meeting_of(archive)
    said = [(line.idx, line.start_ms, line.end_ms, line.text) for line in meeting.lines]
    assert said == [
        (0, 17_880, 18_150, "Yeah."),
        (1, 91_960, 93_560, "Okay, this is our agenda."),
        (2, 95_680, 98_390, "We will do some stuff."),
    ]
    names = [recordings.speaker_of(meeting.speakers[line.speaker_id]) for line in meeting.lines]
    assert names == ["User Interface", "Project Manager", "Project Manager"]


def test_mom05_gold_sentences_carry_the_lines_their_dialogue_acts_cover(archive: Path) -> None:
    gold = {(item.kind, item.text): item.lines for item in meeting_of(archive).gold}
    assert gold == {
        ("abstract", "The Project Manager presented the agenda."): frozenset({1}),
        ("actions", "The group will do some stuff for the next meeting."): frozenset({2}),
        ("problems", "Nobody knew the agenda."): frozenset(),
    }


def test_mom05_a_gold_na_sentence_is_no_gold() -> None:
    assert ev.empty_gold("NA.")
    assert ev.empty_gold(" n/a ")
    assert ev.empty_gold("None.")
    assert not ev.empty_gold("None of them agreed.")


def test_mom05_rouge_l_is_the_f_measure_of_the_longest_common_subsequence() -> None:
    assert ev.rouge_l("the cat sat on the mat", "the cat on the mat") == pytest.approx(100 * 10 / 11)
    assert ev.rouge_l("The Cat.", "the cat") == 100.0
    assert ev.rouge_l("", "the cat") == 0.0
    assert ev.rouge_l("dog", "the cat") == 0.0
    assert ev.rouge_l("x a", "a y") == 50.0


def test_mom05_overlap_f1_scores_content_words_only() -> None:
    ours = "Prepare for the functional design meeting"
    gold = "The group will prepare for the functional design meeting to discuss the components"
    assert ev.overlap_f1(ours, gold) == pytest.approx(100 * 8 / 11)
    assert ev.overlap_f1("the and of", "the and of") == 0.0


def test_mom05_the_independent_check_needs_line_speaker_time_and_whole_words(archive: Path) -> None:
    meeting = meeting_of(archive)
    line = meeting.lines[1]

    def made(quote: str, speaker: str = "Project Manager", at: int = 92_000) -> DerivationOut:
        return DerivationOut(lineId=line.id, speaker=speaker, atMs=at, quote=quote)

    assert ev.grounded(meeting, made("THIS IS our agenda"))
    assert not ev.grounded(meeting, made("this is our agend"))
    assert not ev.grounded(meeting, made("this is our agenda", speaker="User Interface"))
    assert not ev.grounded(meeting, made("this is our agenda", at=93_561))
    assert not ev.grounded(meeting, DerivationOut(lineId=uuid4(), speaker="Project Manager", atMs=92_000, quote="our"))


def test_mom05_the_validator_keeps_every_good_citation_and_refuses_every_fabrication(archive: Path) -> None:
    cases = ev.truth_cases(meeting_of(archive), random.Random(1))
    kinds = {kind for kind, _ in cases}
    assert kinds == {"good", "wrong line", "altered quote", "wrong speaker", "time outside", "unknown line"}
    row = ev.validator_row([meeting_of(archive)], 1)
    assert row["good kept %"] == 100.0
    assert all(row[f"{kind} refused %"] == 100.0 for kind in kinds - {"good"})


def test_mom05_a_validator_that_kept_a_fabrication_shows_in_the_row(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(minutes, "derivation_ok", lambda *_: True)
    row = ev.validator_row([meeting_of(archive)], 1)
    assert row["wrong speaker refused %"] == 0.0
    assert row["good kept %"] == 100.0


def answers(derivations: list[dict[str, object]], action: str = "Do some stuff") -> list[tuple[str, str]]:
    """A recorded run: each section's answer, in CALLS order, the action items the only items."""
    made: list[tuple[str, str]] = [
        ("summary", json.dumps({"title": "Agenda", "summary": "The Project Manager presented the agenda."})),
        ("mom", json.dumps({"mom": "### Agenda\n\nThe User Interface said yeah."})),
    ]
    item = {"id": "act_1", "text": action, "owner": None, "due": None, "derivedFrom": derivations}
    for section in minutes.CALLS[2:]:
        made.append((section, json.dumps({"items": [item] if section == "actionItems" else []})))
    return made


GOOD = {"i": 2, "speaker": "Project Manager", "atMs": 96_000, "quote": "do some stuff"}
MADE_UP = {"i": 2, "speaker": "Project Manager", "atMs": 96_000, "quote": "do other stuff"}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(minutes, "RETRY_S", (0, 0))


def replay_of(recorded: list[tuple[str, str]]) -> Tiers:
    replayed = ev.Replayed("openai:gpt-6-luna", recorded)
    return Tiers(polish=replayed, command=replayed, fallback=None)


def test_mom05_minutes_counts_score_the_first_answers_and_the_served_document(archive: Path) -> None:
    meeting = meeting_of(archive)
    said, doc = asyncio.run(ev.minutes_of(meeting, replay_of(answers([GOOD, MADE_UP]))))
    assert doc is not None
    row = ev.minutes_rates(ev.minutes_counts(meeting, said, doc))
    assert row["citation precision %"] == 50.0
    assert row["served fabricated"] == 0.0
    assert row["gold items cited %"] == 100.0
    assert row["action coverage %"] == 100.0
    assert row["action precision %"] == 100.0
    assert row["summary ROUGE-L %"] == 100.0
    assert row["repaired %"] == 0.0
    assert row["failed %"] == 0.0
    assert row["sections incomplete %"] == 0.0
    assert row["decision coverage %"] == 100.0  # no gold decision: nothing to cover
    assert row["problem coverage %"] == 0.0  # a gold problem nobody raised as a risk or a question


def test_mom05_detail_recall_and_topics_covered_score_the_served_document(archive: Path) -> None:
    """ES2004a's excerpt holds three details: the names User Interface and Project Manager, and "We will" (a
    commitment the action item cites). The minutes name both; their one agenda topic has its `### ` section."""
    meeting = meeting_of(archive)
    said, doc = asyncio.run(ev.minutes_of(meeting, replay_of(answers([GOOD]))))
    assert doc is not None
    row = ev.minutes_rates(ev.minutes_counts(meeting, said, doc))
    assert row["detail recall %"] == 100.0
    assert row["topics covered %"] == 100.0  # no agenda: nothing to cover
    unnamed = doc.model_copy(update={"mom": "### Agenda", "actionItems": []})
    assert ev.minutes_rates(ev.minutes_counts(meeting, said, unnamed))["detail recall %"] == pytest.approx(
        100 / 3, 0.01
    )


def test_mom05_a_served_fabrication_is_counted_never_hidden(archive: Path) -> None:
    meeting = meeting_of(archive)
    said, doc = asyncio.run(ev.minutes_of(meeting, replay_of(answers([GOOD]))))
    assert doc is not None
    bad = doc.actionItems[0].derivedFrom[0].model_copy(update={"quote": "do other stuff"})
    forged = doc.model_copy(
        update={"risks": [], "actionItems": [doc.actionItems[0].model_copy(update={"derivedFrom": [bad]})]}
    )
    assert ev.minutes_rates(ev.minutes_counts(meeting, said, forged))["served fabricated"] == 1.0


def test_mom05_failed_minutes_count_as_failed_and_cite_nothing(archive: Path) -> None:
    row = ev.minutes_rates(ev.minutes_counts(meeting_of(archive), [("summary", "not json")], None))
    assert row["failed %"] == 100.0
    assert row["citation precision %"] == 0.0
    assert row["gold items cited %"] == 0.0
    assert row["sections incomplete %"] == 100.0


def test_mom05_the_fake_provider_runs_every_section_and_its_repair_and_serves_no_fabrication(archive: Path) -> None:
    meeting = meeting_of(archive)
    fake = Fake("meetings")
    said, doc = asyncio.run(ev.minutes_of(meeting, Tiers(polish=fake, command=fake, fallback=None)))
    assert [section for section, _ in said][:4] == ["summary", "mom", "actionItems", "actionItems"]
    assert doc is not None
    row = ev.minutes_rates(ev.minutes_counts(meeting, said, doc))
    assert row["served fabricated"] == 0.0
    assert row["citation precision %"] == pytest.approx(100 * 8 / 9, 0.01)  # the first action item's one fabrication
    assert row["repaired %"] == 100.0
    assert "definitelyunspoken" not in doc.model_dump_json()


def test_mom05_recorded_answers_replay_through_the_same_path_by_section(archive: Path) -> None:
    meeting = meeting_of(archive)
    recorded = answers([GOOD])
    # Recorded out of order: the replay hands each section its own answers.
    said, doc = asyncio.run(ev.minutes_of(meeting, replay_of([*recorded[2:], *recorded[:2]])))
    assert doc is not None
    assert said == recorded
    assert [item.derivedFrom[0].quote for item in doc.actionItems] == ["do some stuff"]


def test_mom05_a_replay_with_no_answer_left_fails_like_a_provider(archive: Path) -> None:
    said, doc = asyncio.run(ev.minutes_of(meeting_of(archive), replay_of([])))
    assert (said, doc) == ([], None)


def test_mom05_a_section_is_named_by_the_request_it_answers() -> None:
    turns = minutes.prompt.turns("risks", "Weekly", [], [])
    assert ev.section_of(turns) == "risks"
    assert ev.section_of([("system", "x"), ("user", "not json")]) == ""


def fixture(name: str) -> ev.Meeting:
    return next(meeting for meeting in ev.fixture_meetings(ev.SLICES) if meeting.name == name)


def test_mom05_the_hand_made_slices_load_with_their_gold() -> None:
    found = ev.fixture_meetings(ev.SLICES)
    assert {meeting.slice for meeting in found} == {"probes", "contradictions", "hinglish"}
    clash = [gold for meeting in found for gold in meeting.gold if gold.kind == "contradiction"]
    assert clash and all(len(gold.lines) == 2 for gold in clash)
    assert all(not meeting.gold for meeting in found if meeting.slice == "probes")


def test_mom05_a_probe_with_items_counts_them(archive: Path) -> None:
    """A meeting with nothing in it must give empty sections: every item a probe's minutes hold counts."""
    meeting = fixture("probe-fillers")
    doc = minutes.empty([]).model_copy(update={"summary": "Small talk.", "mom": "### Small talk"})
    assert ev.minutes_rates(ev.minutes_counts(meeting, [], doc))["probe items"] == 0.0
    line = meeting.lines[0]
    cite = DerivationOut(
        lineId=line.id,
        speaker=recordings.speaker_of(meeting.speakers[line.speaker_id]),
        atMs=line.start_ms,
        quote=line.text,
    )
    made = doc.model_copy(
        update={"actionItems": [ActionItemOut(id="act_1", text="Do it", owner=None, due=None, derivedFrom=[cite])]}
    )
    assert ev.minutes_rates(ev.minutes_counts(meeting, [], made))["probe items"] == 1.0


def test_mom05_a_contradiction_counts_when_it_cites_both_gold_lines() -> None:
    meeting = fixture("contradiction-launch-day")
    gold = next(gold for gold in meeting.gold if gold.kind == "contradiction")

    def cite(idx: int) -> DerivationOut:
        line = meeting.lines[idx]
        who = recordings.speaker_of(meeting.speakers[line.speaker_id])
        return DerivationOut(lineId=line.id, speaker=who, atMs=line.start_ms, quote=line.text)

    first, second = sorted(gold.lines)
    other = next(line.idx for line in meeting.lines if line.idx not in gold.lines)
    found = ClaimOut(id="con_1", text="Both days", derivedFrom=[cite(first), cite(second)])
    wrong = ClaimOut(id="con_2", text="Not one", derivedFrom=[cite(first), cite(other)])
    doc = minutes.empty([]).model_copy(update={"summary": "s", "mom": "m", "contradictions": [found, wrong]})
    row = ev.minutes_rates(ev.minutes_counts(meeting, [], doc))
    assert row["contradiction recall %"] == 100.0
    assert row["contradiction precision %"] == 50.0


def test_mom05_the_private_slice_reads_a_folder_of_exported_meetings(tmp_path: Path) -> None:
    exported = {
        "name": "private-1",
        "slice": "private",
        "speakers": ["Founder"],
        "lines": [["Founder", 0, 1_000, "We ship on Friday."]],
    }
    (tmp_path / "private-1.json").write_text(json.dumps([exported]))
    found = ev.fixture_meetings(tmp_path)
    assert [(meeting.name, meeting.slice, len(meeting.lines)) for meeting in found] == [("private-1", "private", 1)]


@pytest.fixture
def qmsum() -> ev.QMeeting:
    return ev.qmsum_meetings(FIXTURES / "qmsum.jsonl")[0]


def test_mom06_qmsum_turns_become_lines_and_specific_queries_keep_their_spans(qmsum: ev.QMeeting) -> None:
    assert [line.text for line in qmsum.lines] == [
        "Welcome everybody .",
        "The remote it is red .",
        "Red remote , okay .",
        "Lunch then .",
    ]
    assert [line.idx for line in qmsum.lines] == [0, 1, 2, 3]
    assert qmsum.questions == [ev.Question("What did the group say about the remote?", "It is red.", ((1, 2),))]
    assert recordings.speaker_of(qmsum.speakers[qmsum.lines[1].speaker_id]) == "Marketing"


def test_mom06_candidates_are_the_servers_whole_word_ranking_in_transcript_order(qmsum: ev.QMeeting) -> None:
    found = qa.candidates("What about the REMOTE?", [], qmsum.lines)
    assert [line.idx for line in found] == [1, 2]  # "the" is a stopword, so "Lunch then" is no longer found
    assert [line.idx for line in qa.candidates("a b", [], qmsum.lines)] == [0, 1, 2, 3]


def test_mom06_qa_counts_score_span_precision_rouge_and_not_in_this_meeting(qmsum: ev.QMeeting) -> None:
    question = qmsum.questions[0]
    lines = qmsum.lines
    cited = AskResponse(
        answer="It is red.",
        citations=[CitationOut(lineId=lines[1].id, quote="it is red"), CitationOut(lineId=lines[3].id, quote="Lunch")],
        supported=True,
    )
    row = ev.qa_rates(ev.qa_counts(qmsum, question, lines[1:3], cited))
    assert row["citations in span %"] == 50.0
    assert row["answer ROUGE-L %"] == 100.0
    assert row["not in meeting %"] == 0.0
    assert row["retrieval hit %"] == 100.0
    assert row["served fabricated"] == 0.0
    unsupported = AskResponse(answer="Not in this meeting.", citations=[], supported=False)
    row = ev.qa_rates(ev.qa_counts(qmsum, question, lines[3:], unsupported))
    assert row["not in meeting %"] == 100.0
    assert row["retrieval hit %"] == 0.0


def test_mom06_a_qa_citation_whose_quote_is_not_the_line_is_a_served_fabrication(qmsum: ev.QMeeting) -> None:
    lines = qmsum.lines
    forged = AskResponse(
        answer="Blue.", citations=[CitationOut(lineId=lines[1].id, quote="it is blue")], supported=True
    )
    assert ev.qa_rates(ev.qa_counts(qmsum, qmsum.questions[0], lines, forged))["served fabricated"] == 1.0


def test_mom06_the_fake_provider_answers_a_question_through_the_real_ask_in_one_call(qmsum: ev.QMeeting) -> None:
    fake = Fake("meetings")
    answers, response = asyncio.run(
        ev.answer_of(qmsum, qmsum.questions[0], Tiers(polish=fake, command=fake, fallback=None))
    )
    assert len(answers) == 1
    assert response is not None
    assert response.supported
    assert ev.qa_rates(ev.qa_counts(qmsum, qmsum.questions[0], [], response))["served fabricated"] == 0.0


def test_mom05_gates_name_each_metric_that_missed() -> None:
    gates = {"served fabricated": ("<=", 0.0), "citation precision %": (">=", 90.0)}
    assert ev.missed({"served fabricated": 0.0, "citation precision %": 95.0}, gates) == []
    assert ev.missed({"served fabricated": 1.0, "citation precision %": 80.0}, gates) == [
        "served fabricated 1.00 (gate <= 0.00)",
        "citation precision % 80.00 (gate >= 90.00)",
    ]


def test_mom05_live_refuses_to_start_without_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(SystemExit, match="--budget-usd"):
        ev.main(["--live"])


def test_mom05_live_refuses_to_start_without_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        ev.main(["--live", "--budget-usd", "1"])


def test_mom05_live_items_stop_once_the_budget_is_reached(tmp_path: Path) -> None:
    budget = ev.Budget(limit=1.0, before=0.9)
    seen: list[int] = []

    async def item(n: int) -> int:
        seen.append(n)
        budget.spent += 0.06
        return n

    jobs = [lambda n=n: item(n) for n in range(5)]
    done = asyncio.run(ev.within(budget, jobs, 1))
    assert done == [0, 1]
    assert seen == [0, 1]


def test_mom05_live_records_only_items_the_model_answered() -> None:
    made = ev.answered([("ES2004a", [("summary", "{}")]), ("ES2004b", [])])
    assert made == {"ES2004a": [("summary", "{}")]}


def test_mom06_p95_is_the_nearest_rank() -> None:
    assert ev.p95([float(n) for n in range(1, 101)]) == 95.0
    assert ev.p95([7.0]) == 7.0


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [("meetings", "ready"), ("junk", "failed:bad_answer"), ("fail", "failed:provider")],
)
def test_mom04_the_tiny_meeting_always_ends_ready_or_failed_with_a_code(mode: str, outcome: str) -> None:
    fake = Fake(mode)
    meeting = ev.tiny_meeting()
    ended, served = asyncio.run(ev.smoke_outcome(meeting, Tiers(polish=fake, command=fake, fallback=None)))
    assert ended == outcome
    assert all(ev.grounded(meeting, entry) for entry in served)


def test_mom06_a_search_hit_on_a_sibling_part_of_the_series_counts_for_series_recall() -> None:
    """AMI's meetings come in four-part series on one design task: ES2004a to ES2004d (grading round 2)."""
    assert ev.series_of("ES2004c") == "ES2004"
    assert ev.series_of("TS3003a") == ev.series_of("TS3003d")


def test_mom04_live_speed_worse_than_its_baseline_is_named_and_spread_is_not() -> None:
    """A time gets a quarter of room for run-to-run spread, a share 3 points (350 queries' sampling); the query
    count is no measure."""
    base = {"minutes p95 ms": 9_000.0, "search series recall@5 %": 90.0, "search queries": 355.0}
    same = {"minutes p95 ms": 11_000.0, "search series recall@5 %": 87.5, "search queries": 300.0}
    assert ev.speed_worse(base, same) == []
    later = {"minutes p95 ms": 12_000.0, "search series recall@5 %": 86.0, "search queries": 355.0}
    worse = ev.speed_worse(base, later)
    assert worse == ["minutes p95 ms 9000.00 -> 12000.00", "search series recall@5 % 90.00 -> 86.00"]


def test_mom05_minutes_in_another_script_are_not_english() -> None:
    """MOM-05: the minutes are always English, whatever the meeting's language (D26)."""
    english = MinutesOut(summary="Pricing ships Friday.", mom="Done.", actionItems=[], openQuestions=[], decisions=[])
    assert ev.english(english)
    assert not ev.english(english.model_copy(update={"summary": "मूल्य शुक्रवार को"}))
    assert not ev.english(english.model_copy(update={"mom": "Okay. फिर मिलते हैं"}))


def test_mom05_the_hinglish_tiny_meeting_ends_ready_only_with_english_minutes() -> None:
    meeting = ev.tiny_meeting(hinglish=True)
    assert any("\u0900" <= char <= "\u097f" for line in meeting.lines for char in line.text)
    fake = Fake("meetings")
    ended, _ = asyncio.run(ev.smoke_outcome(meeting, Tiers(polish=fake, command=fake, fallback=None)))
    assert ended == "ready"


def test_mom05_the_private_slice_refuses_to_start_without_a_budget(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--budget-usd"):
        ev.main(["--private", str(tmp_path)])


def test_mom05_the_private_report_lists_the_details_the_minutes_left_out() -> None:
    meeting = fixture("hinglish-delhi-shopping")
    doc = minutes.empty([]).model_copy(update={"summary": "Rahul and Harini plan Sarojini Nagar.", "mom": "### Plan"})
    row = ev.minutes_rates(ev.minutes_counts(meeting, [], doc))
    made = ev.private_report(meeting, doc, row, {"all sections s": 1.0})
    assert "| detail recall % |" in made and "line 2, number: 500" in made
    assert "No minutes" in ev.private_report(meeting, None, row, {})


def test_mom05_the_private_slice_fails_on_any_commitment_left_uncovered() -> None:
    meeting = fixture("hinglish-delhi-shopping")
    doc = minutes.empty([]).model_copy(update={"summary": "s", "mom": "m"})
    row = ev.minutes_rates(ev.minutes_counts(meeting, [], doc))
    assert row["commitments covered %"] < 100.0
    (name, miss), *_ = ev.private_misses([(meeting, doc, row, {})])
    assert (name, miss.startswith("commitments covered %")) == ("hinglish-delhi-shopping", True)
    assert ev.private_misses([(meeting, None, {**row, "commitments covered %": 100.0}, {})]) == [
        ("hinglish-delhi-shopping", "no minutes")
    ]


def test_mom06_an_ask_run_refuses_to_start_without_a_budget() -> None:
    """Ask's meta questions, search and Ask's P95 on the recorded minutes, the live model only for those: cheap, and
    it still spends money."""
    with pytest.raises(SystemExit, match="--budget-usd"):
        ev.main(["--ask"])


def test_mom05_an_iso_looking_due_that_is_no_date_is_counted(archive: Path) -> None:
    """The eval checks what the app's date formatter will read: every YYYY-MM-DD due a real date (26 Oct 2026)."""
    meeting = meeting_of(archive)
    said, doc = asyncio.run(ev.minutes_of(meeting, replay_of(answers([GOOD]))))
    assert doc is not None
    assert ev.minutes_rates(ev.minutes_counts(meeting, said, doc))["invalid due dates"] == 0.0
    bad = doc.actionItems[0].model_copy(update={"due": "2026-13-45"})
    worse = doc.model_copy(update={"actionItems": [bad]})
    assert ev.minutes_rates(ev.minutes_counts(meeting, said, worse))["invalid due dates"] == 1.0


def meta_doc(meeting: ev.Meeting) -> Any:
    """ES2004a's excerpt with an open question on line 2 and nothing about risks."""
    line = meeting.lines[2]
    said = {"lineId": str(line.id), "speaker": "Project Manager", "atMs": line.start_ms, "quote": "do some stuff"}
    return MinutesOut.model_validate(
        {
            "summary": "The Project Manager presented the agenda.",
            "mom": "### Agenda",
            "actionItems": [],
            "decisions": [],
            "openQuestions": [{"id": "q_1", "text": "What stuff to do", "derivedFrom": [said]}],
        }
    )


def test_mom06_a_meta_question_is_answerable_when_the_minutes_hold_its_kind(archive: Path) -> None:
    """The founder's Ask bug (26 Oct 2026): "what's still undecided?" is answerable when the minutes have an open
    question or a topic not covered, and its span is the lines they cite; "what are the risks?" is not, with none."""
    meeting = meeting_of(archive)
    doc = meta_doc(meeting)
    assert ev.meta_gold(doc, "undecided", meeting.lines) == (True, {2})
    assert ev.meta_gold(doc, "risks", meeting.lines) == (False, set())
    assert ev.meta_gold(doc, "summary", meeting.lines) == (True, {0, 1, 2})


def test_mom06_an_answerable_meta_question_answered_not_here_is_wrongly_refused(archive: Path) -> None:
    meeting = meeting_of(archive)
    doc = meta_doc(meeting)
    refused = ev.meta_counts(meeting, doc, "undecided", qa.NOT_HERE, calls=2)
    rates = ev.meta_rates(refused)
    assert (rates["wrongly not in meeting %"], rates["fallback %"]) == (100.0, 100.0)
    line = meeting.lines[2]
    good = AskResponse(
        answer="What stuff to do.", citations=[CitationOut(lineId=line.id, quote="do some stuff")], supported=True
    )
    rates = ev.meta_rates(ev.meta_counts(meeting, doc, "undecided", good, calls=1))
    assert (rates["wrongly not in meeting %"], rates["cited in span %"], rates["served fabricated"]) == (
        0.0,
        100.0,
        0.0,
    )
    forged = good.model_copy(update={"citations": [CitationOut(lineId=line.id, quote="do other stuff")]})
    assert ev.meta_rates(ev.meta_counts(meeting, doc, "undecided", forged, calls=1))["served fabricated"] == 1.0


def test_mom06_a_recorded_meta_answer_replays_through_the_same_ask(archive: Path) -> None:
    """The live run records each meta answer with its candidate lines, so the free replay sends the same lines."""
    meeting = meeting_of(archive)
    doc = meta_doc(meeting)
    said = json.dumps({"answer": "What stuff to do.", "citations": [{"i": 2, "quote": "do some stuff"}]})
    response, calls = asyncio.run(
        ev.meta_replayed(meeting, doc, "What is still undecided?", {"candidates": [2], "answers": [said]})
    )
    assert response is not None and response.supported and calls == 1
