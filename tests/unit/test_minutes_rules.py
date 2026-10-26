"""The minutes' deterministic rules (services/minutes.py, MEETINGS.md 7.2): the skeleton a quote is checked
against, the derivation truth table, the drop of a claim nothing supports, the one correction retry, and the
embedded sections."""

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.base import Usage
from snapbak_backend.db.models import Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import ActionItemOut, MinutesOut
from snapbak_backend.services import folding, minutes
from snapbak_backend.services.minutes import RawDerivation


def speaker(named: str | None = None) -> Speaker:
    made = Speaker(id=uuid4(), recording_id=uuid4(), label="Speaker 1", vendor_label="A", name=named)
    return made


def line(text: str, idx: int = 0, start: int = 1_000, end: int = 4_000, who: Speaker | None = None) -> TranscriptLine:
    person = who or speaker()
    made = TranscriptLine(
        id=uuid4(),
        recording_id=uuid4(),
        speaker_id=person.id,
        idx=idx,
        start_ms=start,
        end_ms=end,
        text=text,
        words=[],
    )
    return made


def entry(i: int, said: str, at: int, quote: str) -> RawDerivation:
    made = RawDerivation(i=i, speaker=said, atMs=at, quote=quote)
    return made


def test_mom05_skeleton_keeps_the_words_of_any_script_and_drops_punctuation() -> None:
    assert folding.skeleton("Ship it, Friday!!") == "ship it friday"
    assert folding.skeleton("Priya said —  'no-go'") == "priya said no go"
    assert folding.skeleton("यहाँ दिखता है।") == folding.skeleton("यहां दिखता है")
    assert folding.skeleton("यहाँ दिखता है") != ""
    assert folding.skeleton("…!? —") == ""


def test_mom05_derivation_ok_accepts_a_verbatim_quote_with_right_speaker_and_time() -> None:
    who = speaker()
    made = line("We ship the pricing page next Friday.", who=who)
    ok = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "ship the pricing page"), made, who)
    assert ok


def test_mom05_derivation_ok_ignores_case_and_punctuation_in_the_quote() -> None:
    who = speaker()
    made = line("We ship the pricing page next Friday.", who=who)
    ok = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "SHIP — the PRICING, page!"), made, who)
    assert ok


def test_mom05_derivation_ok_refuses_a_fabricated_quote() -> None:
    who = speaker()
    made = line("We ship the pricing page next Friday.", who=who)
    ok = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "definitely unspoken words"), made, who)
    assert not ok


def test_mom05_derivation_ok_refuses_a_time_outside_the_line() -> None:
    who = speaker()
    made = line("We ship the pricing page next Friday.", who=who)
    ok = minutes.derivation_ok(entry(0, "Speaker 1", 9_000, "ship the pricing page"), made, who)
    assert not ok


def test_mom05_derivation_ok_refuses_another_speaker() -> None:
    who = speaker()
    made = line("We ship the pricing page next Friday.", who=who)
    ok = minutes.derivation_ok(entry(0, "Priya", 2_000, "ship the pricing page"), made, who)
    assert not ok


def test_mom05_derivation_ok_matches_the_chosen_name_not_the_label() -> None:
    named = speaker("Priya")
    made = line("We ship it.", who=named)
    ok = minutes.derivation_ok(entry(0, "Priya", 2_000, "ship it"), made, named)
    other = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "ship it"), made, named)
    assert ok and not other


def test_mom05_derivation_ok_matches_a_devanagari_quote_truly() -> None:
    who = speaker()
    made = line("हम शुक्रवार को ज़रूर ship करेंगे।", who=who)
    verbatim = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "शुक्रवार को ज़रूर"), made, who)
    # A nukta or a chandrabindu spelled the other way is the same word (MEETINGS.md 7.2, Devanagari folded).
    variant = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "जरूर ship करेंगे"), made, who)
    assert verbatim and variant


def test_mom05_derivation_ok_refuses_fabricated_devanagari() -> None:
    who = speaker()
    made = line("हम शुक्रवार को ज़रूर ship करेंगे।", who=who)
    ok = minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "सोमवार को रद्द"), made, who)
    assert not ok


def test_mom05_derivation_ok_refuses_a_quote_with_no_words() -> None:
    who = speaker()
    made = line("यहाँ दिखता है", who=who)
    for quote in ("", "।", "— ...!"):
        assert not minutes.derivation_ok(entry(0, "Speaker 1", 2_000, quote), made, who), quote


def test_mom05_derivation_ok_refuses_a_quote_that_is_only_part_of_a_word() -> None:
    who = speaker()
    made = line("We ship it.", who=who)
    assert not minutes.derivation_ok(entry(0, "Speaker 1", 2_000, "hip"), made, who)


def test_mom05_keep_derivations_keeps_the_good_ones_and_drops_the_made_up() -> None:
    who = speaker()
    made = line("We ship it.", who=who)
    good = entry(0, "Speaker 1", 2_000, "ship it")
    bad = entry(0, "Speaker 1", 2_000, "never said this")
    kept = minutes.keep_derivations([good, bad, good, good, good, good, good], {0: made}, {who.id: who})
    # No cap on how many: every good one stands (minutes v2).
    assert len(kept) == 6 and kept[0].lineId == made.id


def cite(quote: str = "ship it", i: int = 0, said: str = "Speaker 1") -> dict[str, Any]:
    return {"i": i, "speaker": said, "atMs": 2_000 + i * 10_000, "quote": quote}


def clean(section: str, quote: str = "ship it") -> str:
    """A model-shaped answer for one section, every item hung on `quote` from line 0."""
    items = {
        "actionItems": [
            {"id": "act_1", "text": "Do it", "owner": "Speaker 1", "due": None, "derivedFrom": [cite(quote)]}
        ],
        "decisions": [
            {
                "id": "dec_1",
                "text": "We ship",
                "rationale": "it works",
                "alternatives": ["wait"],
                "derivedFrom": [cite(quote)],
            }
        ],
        "openQuestions": [{"id": "q_1", "text": "When?", "derivedFrom": [cite(quote)]}],
        "risks": [{"id": "risk_1", "kind": "blocker", "text": "Finance", "owner": None, "derivedFrom": [cite(quote)]}],
        "followUps": [{"id": "fu_1", "text": "Check", "owner": None, "due": None, "derivedFrom": [cite(quote)]}],
        "nextAgenda": [{"id": "next_1", "topic": "Tiers", "derivedFrom": [cite(quote)]}],
        "agenda": [{"id": "ag_1", "topic": "Shipping", "status": "covered", "derivedFrom": [cite(quote)]}],
        "contradictions": [
            {"id": "con_1", "text": "Ships, then waits", "derivedFrom": [cite(quote), cite("we wait", 1)]}
        ],
    }
    texts = {
        "summary": {"title": "Shipping it", "summary": "We ship it."},
        "mom": {"mom": "### Shipping\n\nWe ship it."},
    }
    return json.dumps(texts[section] if section in texts else {"items": items[section]})


class Sections:
    """A provider that answers each section from its own script and keeps the order of the sections asked."""

    def __init__(self, scripts: dict[str, list[str]] | None = None, down: set[str] | None = None) -> None:
        self.engine = "fake:sections"
        self.scripts = scripts or {}
        self.down = down or set()
        self.asked: list[str] = []
        self.turns: list[list[tuple[str, str]]] = []
        self.budgets: list[int] = []

    async def complete(self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = "") -> Any:
        from snapbak_backend.ai.providers.base import ProviderError

        section = json.loads(next(content for role, content in turns if role == "user"))["section"]
        self.asked.append(section)
        self.turns.append(list(turns))
        self.budgets.append(max_tokens)
        if section in self.down:
            raise ProviderError(f"fake:sections {section} down")
        script = self.scripts.get(section)
        said = script.pop(0) if script else (clean(section) if section != "details" else json.dumps({}))
        return said, Usage(0, 0)

    async def aclose(self) -> None:
        pass


TWO_LINES: list[TranscriptLine] = []


def lines_and_who() -> tuple[list[TranscriptLine], Speaker]:
    who = speaker()
    made = [
        line("We ship it.", idx=0, start=1_000, end=4_000, who=who),
        line("No, we wait.", idx=1, start=11_000, end=14_000, who=who),
    ]
    return made, who


def run_with(provider: Sections, doc: MinutesOut | None = None) -> tuple[MinutesOut, list[list[str]]]:
    lines, who = lines_and_who()
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]
    saved: list[list[str]] = []

    async def save(now: MinutesOut) -> None:
        saved.append(list(now.pending))

    start = doc or minutes.empty(list(minutes.CALLS))
    made, _ = asyncio.run(minutes.run(tiers, "Weekly", [], lines, {who.id: who}, start, save))
    return made, saved


def test_mom05_each_section_is_its_own_call_in_order_and_saved_as_it_lands() -> None:
    """Title and summary first (the first value fast), then each section in turn, one call each, every one stored
    and published before the next starts (minutes v2)."""
    provider = Sections()
    doc, saved = run_with(provider)
    assert provider.asked[: len(minutes.CALLS)] == list(minutes.CALLS)
    assert saved[0] == list(minutes.CALLS[1:]) and saved[len(minutes.CALLS) - 1] == []
    assert (doc.title, doc.summary) == ("Shipping it", "We ship it.")
    assert doc.decisions[0].rationale == "it works" and doc.decisions[0].alternatives == ["wait"]
    assert doc.risks[0].kind == "blocker" and doc.agenda[0].status == "covered"
    assert len(doc.contradictions[0].derivedFrom) == 2
    assert (doc.pending, doc.incomplete) == ([], [])


def test_mom05_every_call_has_64000_output_tokens_and_no_reasoning_label() -> None:
    provider = Sections()
    run_with(provider)
    assert set(provider.budgets) == {64_000}


def test_mom05_a_section_with_a_made_up_quote_is_repaired_alone() -> None:
    """The repair is that section's own: its first answer and what was refused in it."""
    provider = Sections({"decisions": [clean("decisions", "never said this"), clean("decisions")]})
    doc, _ = run_with(provider)
    assert provider.asked.count("decisions") == 2
    repair = provider.turns[provider.asked.index("decisions") + 1]
    assert repair[-2][0] == "assistant" and "never said this" in repair[-2][1]
    assert "dec_1" in repair[-1][1]
    # The repair names the model's own id; the item kept carries the server's (grading 4).
    assert len(doc.decisions) == 1 and doc.decisions[0].id.startswith("dec_")


def test_mom05_a_section_that_fails_is_marked_and_the_others_stand() -> None:
    provider = Sections({"risks": ["not json", "still not json"]})
    doc, _ = run_with(provider)
    assert doc.incomplete == [minutes.Incomplete(section="risks", code="bad_answer")]
    assert doc.risks == [] and doc.followUps and doc.contradictions
    assert doc.summary == "We ship it."


def test_mom05_a_section_whose_model_is_down_is_tried_three_times_then_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(minutes, "RETRY_S", (0, 0))
    provider = Sections(down={"followUps"})
    doc, _ = run_with(provider)
    assert provider.asked.count("followUps") == minutes.TRIES
    assert doc.incomplete == [minutes.Incomplete(section="followUps", code="provider")]


def test_mom05_a_resumed_run_starts_at_the_first_unfinished_section() -> None:
    """Crash-safe: what was stored stays, and only the pending sections are asked."""
    done, _ = run_with(Sections())
    half = done.model_copy(update={"pending": ["risks", "contradictions"], "risks": [], "contradictions": []})
    provider = Sections()
    doc, _ = run_with(provider, half)
    assert [section for section in provider.asked if section != "details"] == ["risks", "contradictions"]
    assert doc.actionItems == done.actionItems and doc.risks and doc.contradictions


def test_mom05_a_regenerate_keeps_the_user_s_items_and_edits() -> None:
    done, _ = run_with(Sections())
    mine = ActionItemOut(id="act_user_1", text="Call the bank", derivedFrom=[], byUser=True)
    edited = done.model_copy(
        update={
            "actionItems": [*done.actionItems, mine],
            "summary": "My own summary.",
            "userEdited": ["summary"],
            "pending": list(minutes.CALLS),
        }
    )
    doc, _ = run_with(Sections({"actionItems": [clean("actionItems").replace("Do it", "Do it again")]}), edited)
    assert [item.text for item in doc.actionItems] == ["Do it again", "Call the bank"]
    assert doc.summary == "My own summary."


def test_mom05_a_contradiction_needs_two_lines_that_check_out() -> None:
    one = json.dumps({"items": [{"id": "con_1", "text": "x", "derivedFrom": [cite(), cite("never said", 1)]}]})
    doc, _ = run_with(Sections({"contradictions": [one, one]}))
    assert doc.contradictions == []


def test_mom05_minutes_have_no_length_caps() -> None:
    """The founder's "lots of detail": a long summary and many items stand whole (only the 2 MB edit cap stays)."""
    long_summary = json.dumps({"title": "T", "summary": "word " * 3_000})
    many = json.dumps(
        {
            "items": [
                {
                    "id": f"act_{n}",
                    "text": "x " * 600,
                    "owner": None,
                    "due": "next Friday evening",
                    "derivedFrom": [cite()],
                }
                for n in range(80)
            ]
        }
    )
    doc, _ = run_with(Sections({"summary": [long_summary], "actionItems": [many]}))
    assert len(doc.summary) == 14_999 and len(doc.actionItems) == 80
    assert doc.actionItems[0].due == "next Friday evening"


def test_mom05_minutes_missing_a_detail_get_one_completion_call_that_adds_it() -> None:
    """The guard: the transcript's "11" is nowhere in the minutes, so one completion call works it in."""
    lines, who = lines_and_who()
    lines[0].text = "We ship it at 11."
    added = json.dumps({"summary": "We ship it at 11.", "mom": "### Shipping\n\nWe ship it at 11.", "actionItems": []})
    provider = Sections({"details": [added]})
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    doc, _ = asyncio.run(
        minutes.run(tiers, "Weekly", [], lines, {who.id: who}, minutes.empty(list(minutes.CALLS)), save)
    )
    assert provider.asked.count("details") == 1
    assert "11" in doc.summary


def test_mom05_minutes_that_kept_every_detail_make_no_completion_call() -> None:
    provider = Sections()
    run_with(provider)
    assert "details" not in provider.asked


def test_mom05_the_minutes_name_the_engine_that_answered() -> None:
    lines, who = lines_and_who()
    first, backup = Sections(down=set(minutes.CALLS)), Sections()
    backup.engine = "fake:backup"
    tiers = Tiers(polish=first, command=first, fallback=backup)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    _, engine = asyncio.run(minutes.run(tiers, "W", [], lines, {who.id: who}, minutes.empty(list(minutes.CALLS)), save))
    assert engine == "fake:backup"


class Slow(Sections):
    """Sections, each answered after `delay_s`."""

    delay_s = 0.0

    async def complete(self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = "") -> Any:
        await asyncio.sleep(self.delay_s)
        return await super().complete(turns, max_tokens, timeout, reason)


def run_hedged(first: Slow, backup: Sections) -> MinutesOut:
    lines, who = lines_and_who()
    tiers = Tiers(polish=first, command=first, fallback=backup)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    start = minutes.empty(list(minutes.CALLS))
    doc, _ = asyncio.run(minutes.run(tiers, "W", [], lines, {who.id: who}, start, save))
    return doc


def test_mom05_a_section_answering_in_its_usual_time_is_never_hedged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unattended batch work: a second model beside one answering in its usual time would double the cost."""
    monkeypatch.setattr(minutes, "SECTION_HEDGE_S", 0.2)
    first, backup = Slow(), Sections()
    first.delay_s = 0.01
    run_hedged(first, backup)
    assert backup.asked == []


def test_mom04_a_stuck_section_is_joined_by_the_fallback_well_before_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One run of eight took 264 s of minutes (26 Oct 2026): one call stuck until its 180 s deadline, past MOM-04's
    3 min. A section not answered in SECTION_HEDGE_S (well past the slowest measured, 24 s) gets the fallback beside
    it, and the first answer wins."""
    monkeypatch.setattr(minutes, "SECTION_HEDGE_S", 0.05)
    first, backup = Slow(), Sections()
    first.delay_s = 5.0
    doc = run_hedged(first, backup)
    assert backup.asked[0] == "summary" and doc.summary == "We ship it."
    assert minutes.SECTION_HEDGE_S < minutes.SECTION_DEADLINE_S


@pytest.mark.parametrize(("quote", "said"), [("काम", "यह कम है"), ("दिन", "वो दीन है"), ("कि", "यह की है")])
def test_mom05_a_quote_with_another_vowel_is_another_word(quote: str, said: str) -> None:
    """Folding keeps the vowel signs (matras): काम (work) is not कम (less). Only a nukta or a chandrabindu
    spelled another way is the same word (grading round 2)."""
    assert not folding.quoted(quote, said)


def test_mom05_the_qa_words_fold_as_the_quote_check_does() -> None:
    """Ask's ranking and the citation check read a word the same way, so a line Ask would cite is one it finds."""
    from snapbak_backend.services import qa

    assert qa.tokens("ज़रूर, यहाँ!") == folding.skeleton("जरूर यहां").split()


def test_mom05_minutes_fail_only_when_summary_and_minutes_both_failed() -> None:
    """A run with either text stands, partial (minutes v2); with neither, the summary's code is the run's."""
    doc = minutes.empty([])
    assert minutes.failure(doc) is None
    one = doc.model_copy(update={"incomplete": [minutes.Incomplete(section="mom", code="provider")]})
    assert minutes.failure(one) is None
    both = [
        minutes.Incomplete(section="summary", code="bad_answer"),
        minutes.Incomplete(section="mom", code="provider"),
    ]
    assert minutes.failure(doc.model_copy(update={"incomplete": both})) == "bad_answer"


def test_mom05_a_completion_that_loses_details_is_refused() -> None:
    """The guard only ever adds: an answer that keeps fewer details than the minutes had leaves them as they were."""
    lines, who = lines_and_who()
    lines[0].text = "We ship it at 11 with Ravi."
    worse = json.dumps({"summary": "Shipping.", "mom": "### Shipping", "actionItems": []})
    provider = Sections({"details": [worse]})
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    doc, _ = asyncio.run(
        minutes.run(tiers, "Weekly", [], lines, {who.id: who}, minutes.empty(list(minutes.CALLS)), save)
    )
    assert provider.asked.count("details") == 1
    assert doc.summary == "We ship it."


def commitment_lines() -> tuple[list[TranscriptLine], Speaker]:
    """The two shapes the founder's meeting dropped (26 Oct 2026), paraphrased: a group proposal another speaker
    pushed back on, and a constraint with a time."""
    who = speaker()
    made = [
        line("We ship it.", idx=0, start=1_000, end=4_000, who=who),
        line("Let's just take the train.", idx=1, start=5_000, end=8_000, who=who),
        line("We need to be back by seven.", idx=2, start=9_000, end=12_000, who=who),
    ]
    return made, who


def run_on(lines: list[TranscriptLine], who: Speaker, provider: Sections) -> MinutesOut:
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    start = minutes.empty(list(minutes.CALLS))
    doc, _ = asyncio.run(minutes.run(tiers, "Weekly", [], lines, {who.id: who}, start, save))
    return doc


def test_mom05_every_commitment_ends_an_action_item_or_judged_none() -> None:
    from snapbak_backend.services import details

    lines, who = commitment_lines()
    back = {"i": 2, "speaker": "Speaker 1", "atMs": 9_000, "quote": "be back by seven"}
    answer = {
        "summary": "We ship it.",
        "mom": "### Shipping\n\nWe ship it.",
        "actionItems": [
            {"id": "act_1", "text": "Be back by seven", "owner": None, "due": "by seven", "derivedFrom": [back]}
        ],
        "notCommitments": [{"i": 1, "reason": "a proposal another speaker pushed back on"}],
    }
    provider = Sections({"details": [json.dumps(answer)]})
    doc = run_on(lines, who, provider)
    units = details.inventory(lines, {who.id: who})
    assert [unit for unit in details.missing(doc, units, lines) if unit.kind == "commitment"] == []
    assert [item.text for item in doc.actionItems][-1] == "Be back by seven"
    assert [(item.lineId, item.reason) for item in doc.notCommitments] == [
        (lines[1].id, "a proposal another speaker pushed back on")
    ]


def test_mom05_the_completion_must_answer_for_every_missing_commitment() -> None:
    lines, who = commitment_lines()
    provider = Sections({"details": [json.dumps({"summary": "s", "mom": "m"})]})
    run_on(lines, who, provider)
    sent = json.loads(provider.turns[provider.asked.index("details")][1][1])
    assert sorted(item["i"] for item in sent["missing"] if item["kind"] == "commitment") == [1, 2]
    assert "notCommitments" in provider.turns[provider.asked.index("details")][0][1]


def test_mom05_a_judgment_on_a_line_with_no_missing_commitment_is_dropped() -> None:
    lines, who = commitment_lines()
    answer = {
        "summary": "We ship it.",
        "mom": "m",
        "notCommitments": [{"i": 0, "reason": "x"}, {"i": 9, "reason": "y"}],
    }
    doc = run_on(lines, who, Sections({"details": [json.dumps(answer)]}))
    assert doc.notCommitments == []


def test_mom05_an_uncovered_commitment_runs_the_guard_whatever_the_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(minutes, "DETAIL_TARGET", 0.0)
    lines, who = commitment_lines()
    provider = Sections()
    run_on(lines, who, provider)
    # The fake's guard answer covers nothing, so the second pass asks again for the commitments alone.
    assert provider.asked.count("details") == 2


def test_mom05_a_regenerate_of_the_action_items_judges_the_commitments_again() -> None:
    """A stored "no commitment" judgment would hide a line from the next guard, after an edit changed it."""
    from uuid import uuid4

    from snapbak_backend.schemas.cloud import NotCommitmentOut
    from snapbak_backend.services import meetings

    judged = minutes.empty([]).model_copy(update={"notCommitments": [NotCommitmentOut(lineId=uuid4(), reason="x")]})
    stored = judged.model_dump(mode="json")
    assert meetings.started(stored, ["actionItems"])["notCommitments"] == []
    assert meetings.started(stored, None)["notCommitments"] == []
    assert len(meetings.started(stored, ["risks"])["notCommitments"]) == 1


def test_mom05_commitments_the_first_completion_left_unanswered_get_one_more_pass() -> None:
    lines, who = commitment_lines()
    back = {"i": 2, "speaker": "Speaker 1", "atMs": 9_000, "quote": "be back by seven"}
    first = {
        "summary": "We ship it.",
        "mom": "### Shipping\n\nWe ship it.",
        "notCommitments": [{"i": 1, "reason": "x"}],
    }
    second = {
        "actionItems": [{"id": "act_9", "text": "Back by seven", "owner": None, "due": None, "derivedFrom": [back]}]
    }
    provider = Sections({"details": [json.dumps(first), json.dumps(second)]})
    doc = run_on(lines, who, provider)
    assert provider.asked.count("details") == 2
    sent = json.loads(provider.turns[len(provider.asked) - 1][1][1])
    assert [item["i"] for item in sent["missing"]] == [2]
    assert doc.actionItems[-1].text == "Back by seven" and doc.summary == "We ship it."


def test_mom05_a_null_title_from_the_model_is_no_title() -> None:
    """`str(None)` once made the meeting's title "None" (grading 3)."""
    answer = json.dumps({"title": None, "summary": "We ship it."})
    doc, _ = run_with(Sections({"summary": [answer]}))
    assert doc.title is None and doc.summary == "We ship it."


def test_mom05_text_in_another_script_is_refused_and_repaired() -> None:
    """MOM-05's "always English" checked by rule, not the prompt alone (grading 3)."""
    hindi = json.dumps({"title": "Shipping", "summary": "हम इसे भेजते हैं।"})
    provider = Sections({"summary": [hindi, clean("summary")]})
    doc, _ = run_with(provider)
    assert provider.asked.count("summary") == 2 and doc.summary == "We ship it."
    item = json.loads(clean("risks"))
    item["items"][0]["text"] = "भेजने का जोखिम"
    doc, _ = run_with(Sections({"risks": [json.dumps(item), json.dumps(item)]}))
    assert doc.risks == []


def test_mom05_a_user_s_item_whose_id_a_regenerate_reuses_gets_its_own() -> None:
    """An edited `act_1` kept beside a new AI `act_1` collided: one edit or delete hit both (grading 3)."""
    done, _ = run_with(Sections())
    mine = done.actionItems[0].model_copy(update={"text": "Mine now", "byUser": True})
    again, _ = run_with(Sections(), done.model_copy(update={"actionItems": [mine], "pending": ["actionItems"]}))
    ids = [item.id for item in again.actionItems]
    assert len(ids) == len(set(ids)) and [item.text for item in again.actionItems if item.byUser] == ["Mine now"]


def test_mom06_empty_sections_are_never_embedded() -> None:
    """An empty summary (its section failed, or the user cleared it) is a 400 from the embeddings API."""
    doc = minutes.empty([])
    assert minutes.chunks_of(doc) == []


def test_mom05_a_regenerate_can_ask_only_for_sections_a_call_writes() -> None:
    """ "title" is written by the summary call: asking for it alone started an empty run (grading 3)."""
    from pydantic import ValidationError

    from snapbak_backend.schemas.cloud import RegenerateRequest

    assert RegenerateRequest.model_validate({"sections": ["risks"]}).sections == ["risks"]
    with pytest.raises(ValidationError):
        RegenerateRequest.model_validate({"sections": ["title"]})


def test_mom05_a_completion_that_drops_a_topic_s_heading_is_refused() -> None:
    """Hybrid run 1 (26 Oct 2026): the guard took answers that kept more details but lost `### ` headings, so topics
    covered fell from 86 to 58 %. It now takes an answer only when neither measure gets worse."""
    lines, who = lines_and_who()
    lines[0].text = "We ship it at 11."
    agenda = json.dumps({"items": [{"id": "ag_1", "topic": "Shipping", "status": "covered", "derivedFrom": [cite()]}]})
    mom = json.dumps({"mom": "### Shipping\n\nWe ship it."})
    lost = json.dumps({"summary": "We ship it at 11.", "mom": "We ship it at 11, no headings left.", "actionItems": []})
    provider = Sections({"agenda": [agenda], "mom": [mom], "details": [lost, lost]})
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    doc, _ = asyncio.run(
        minutes.run(tiers, "Weekly", [], lines, {who.id: who}, minutes.empty(list(minutes.CALLS)), save)
    )
    assert doc.mom.startswith("### Shipping")


def test_mom05_a_completion_whose_text_drops_a_heading_still_adds_its_action_items() -> None:
    lines, who = commitment_lines()
    agenda = json.dumps({"items": [{"id": "ag_1", "topic": "Shipping", "status": "covered", "derivedFrom": [cite()]}]})
    mom = json.dumps({"mom": "### Shipping\n\nWe ship it."})
    back = {"i": 2, "speaker": "Speaker 1", "atMs": 9_000, "quote": "be back by seven"}
    lost = {
        "summary": "We ship it.",
        "mom": "No headings left.",
        "actionItems": [{"id": "act_9", "text": "Back by seven", "owner": None, "due": None, "derivedFrom": [back]}],
        "notCommitments": [{"i": 1, "reason": "a proposal"}],
    }
    doc = run_on(lines, who, Sections({"agenda": [agenda], "mom": [mom], "details": [json.dumps(lost)] * 2}))
    assert doc.mom.startswith("### Shipping") and doc.actionItems[-1].text == "Back by seven"


def test_mom05_the_agenda_call_names_its_topics_by_the_minutes_headings() -> None:
    """The agenda was asked to name topics as the MoM's headings would, without seeing them: topics covered was 58
    to 71 % on AMI and 0 % on Hinglish (grading 3). The MoM is written first; the agenda call now gets its
    headings."""
    mom = json.dumps({"mom": "### Shipping date\n\nWe ship it.\n\n### Waiting\n\nNo, we wait."})
    provider = Sections({"mom": [mom]})
    run_with(provider)
    sent = json.loads(provider.turns[provider.asked.index("agenda")][1][1])
    assert sent["headings"] == ["Shipping date", "Waiting"]
    assert "headings" not in json.loads(provider.turns[provider.asked.index("risks")][1][1])
    assert '"headings"' in minutes.prompt.system_of("agenda")


def test_mom06_each_decision_is_embedded_as_its_own_section_with_its_why() -> None:
    """D38 as changed on 26 Oct 2026: search covers decisions too, each its own chunk, its rationale in it."""
    from snapbak_backend.schemas.cloud import DecisionOut

    made = DecisionOut(id="dec_1", text="Ship Friday", rationale="billing is ready", derivedFrom=[])
    bare = DecisionOut(id="dec_2", text="Drop the dark mode", derivedFrom=[])
    doc = minutes.empty([]).model_copy(update={"summary": "s", "decisions": [made, bare]})
    assert [row for row in minutes.chunks_of(doc) if row[0] == "decision"] == [
        ("decision", 0, "Ship Friday because billing is ready"),
        ("decision", 1, "Drop the dark mode"),
    ]


def due_item(due: object) -> str:
    item = {"id": "act_1", "text": "Do it", "owner": None, "due": due, "derivedFrom": [cite()]}
    return json.dumps({"items": [item]})


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("2026-10-30", "2026-10-30"),
        ("the next eight weeks", "the next eight weeks"),
        ("by 11 p.m.", "by 11 p.m."),
        ("  ", None),
        (None, None),
    ],
)
def test_mom05_a_due_is_an_iso_date_or_the_deadline_as_said(said: object, kept: str | None) -> None:
    """An app formatter once threw on `due: "the next eight weeks"` and blanked the window (26 Oct 2026): a due is a
    real calendar date as YYYY-MM-DD, or the deadline exactly as said, or null."""
    doc, _ = run_with(Sections({"actionItems": [due_item(said)]}))
    assert doc.actionItems[0].due == kept


def test_mom05_a_due_that_looks_like_a_date_but_is_none_is_refused_and_repaired() -> None:
    provider = Sections({"actionItems": [due_item("2026-02-30"), due_item("2026-03-02")]})
    doc, _ = run_with(provider)
    assert provider.asked.count("actionItems") == 2
    assert doc.actionItems[0].due == "2026-03-02"


def test_mom05_each_call_knows_the_meeting_s_date_to_resolve_friday_against() -> None:
    lines, who = lines_and_who()
    provider = Sections()
    tiers = Tiers(polish=provider, command=provider, fallback=None)  # type: ignore[arg-type]

    async def save(now: MinutesOut) -> None:
        pass

    start = minutes.empty(list(minutes.CALLS))
    asyncio.run(minutes.run(tiers, "Weekly", [], lines, {who.id: who}, start, save, day="2026-10-26"))
    sent = json.loads(provider.turns[provider.asked.index("actionItems")][1][1])
    assert sent["date"] == "2026-10-26"
    for section in ("actionItems", "followUps"):
        system = minutes.prompt.system_of(section)
        assert "YYYY-MM-DD" in system and "resolved against the meeting's date" in system
        assert "exactly as said" in system


def test_mom05_the_server_gives_every_item_an_id_of_its_own_run() -> None:
    """Grading 4: the model's `act_1` repeats across runs and within a section. An edit resent after a conflict then
    matched another run's `act_1` and took its citations. Ids are the server's: unique in a run, new each run."""
    twice = json.dumps({"items": [json.loads(clean("decisions"))["items"][0]] * 2})
    first, _ = run_with(Sections({"decisions": [twice]}))
    second, _ = run_with(Sections())
    ids = [item.id for item in first.decisions]
    assert len(ids) == 2 and len(set(ids)) == 2 and all(item.startswith("dec_") for item in ids)
    assert not {item.id for item in first.actionItems} & {item.id for item in second.actionItems}


def test_mom05_an_edit_resent_over_a_newer_run_keeps_no_citation_it_did_not_have() -> None:
    old, _ = run_with(Sections())
    new, _ = run_with(Sections())
    stale = old.actionItems[0].model_copy(update={"text": "Ship pricing"})
    sent = new.model_copy(update={"actionItems": [stale]})
    saved = minutes.edited(new, sent)
    assert saved.actionItems[0].byUser and saved.actionItems[0].derivedFrom == []


def test_mom05_a_repeated_id_in_a_section_becomes_id_and_its_position() -> None:
    """The rule Studio applies to what it reads (grading 4): the second `act_1` in a section is `act_1_<position>`,
    its index there from 0, so a save never sends an id the server doesn't know."""
    done, _ = run_with(Sections())
    first = done.actionItems[0]
    twin = first.model_copy(update={"text": "Another"})
    sent = done.model_copy(update={"actionItems": [first, twin]})
    saved = minutes.edited(done, sent)
    assert [item.id for item in saved.actionItems] == [first.id, f"{first.id}_1"]
    assert saved.actionItems[0].derivedFrom == first.derivedFrom and saved.actionItems[1].byUser


def test_mom05_a_user_s_item_kept_beside_a_new_one_with_its_id_takes_the_position_rule() -> None:
    done, _ = run_with(Sections())
    mine = done.actionItems[0].model_copy(update={"text": "Mine now", "byUser": True})
    merged = minutes.merged(done, {"actionItems": [done.actionItems[0]]})
    merged = minutes.merged(merged.model_copy(update={"actionItems": [mine]}), {"actionItems": [done.actionItems[0]]})
    assert [item.id for item in merged.actionItems] == [mine.id, f"{mine.id}_1"]
