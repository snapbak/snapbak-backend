import asyncio
import gzip
import json
import re
from collections import Counter
from pathlib import Path

import gen_corpus_v2 as gen
import pytest

import bench
from snapbak_backend.ai.chain import Call, Tiers
from snapbak_backend.ai.prompts.examples import EXAMPLES
from snapbak_backend.ai.providers.base import Turn, Usage


def test_an_exact_match_scores_every_mark_and_capital_it_needed() -> None:
    assert bench.score("Hey Sam, ship it.", ["Hey Sam, ship it."], "hey sam ship it") == bench.Score(True, 2, 2, 2, 2)


def test_a_missing_comma_counts_once_against_punctuation() -> None:
    assert bench.score("Hey Sam ship it.", ["Hey Sam, ship it."], "hey sam ship it") == bench.Score(True, 2, 1, 2, 2)


def test_punctuation_and_casing_count_only_where_the_words_align() -> None:
    assert bench.score("hello there, Sam.", ["Hello Sam."], "hello sam") == bench.Score(False, 1, 1, 2, 1)


def test_the_closest_expected_output_is_used() -> None:
    expected = ["Meet me at nine.", "Meet me at 9 am."]
    assert bench.score("Meet me at 9 am.", expected, "meet me at nine am") == bench.Score(True, 1, 1, 1, 1)


def test_of_alternatives_with_the_same_words_the_one_with_the_closest_marks_is_used() -> None:
    expected = ["Pehle main office jaunga, phir client se milunga.", "Pehle main office jaunga phir client se milunga."]
    said = "pehle main office jaunga phir client se milunga"
    assert bench.score(expected[1], expected, said) == bench.Score(True, 1, 1, 1, 1)


def test_a_hyphenated_word_matches_the_same_words_written_apart() -> None:
    said = "we should ship it with an read only mode"
    scored = bench.score("We should ship it with a read-only mode.", ["We should ship it with a read only mode."], said)
    assert scored.words_right


def test_list_numbers_and_bullets_align_with_their_marks() -> None:
    output = "We need:\n1. The API\n- The UI"
    said = "we need one the api bullet the ui"
    assert bench.score(output, ["We need:\n1. The API\n- The UI"], said) == bench.Score(True, 2, 2, 5, 5)
    assert bench.score("We need 1 The API", ["We need:\n1. The API"], "we need one the api") == bench.Score(
        True, 2, 0, 3, 3
    )


def test_hinglish_and_hindi_marks_count_and_devanagari_has_no_case() -> None:
    assert bench.score("Kal milte hain.", ["Kal milte hain."], "kal milte hain") == bench.Score(True, 1, 1, 1, 1)
    assert bench.score("कल मिलते हैं।", ["कल मिलते हैं।"], "कल मिलते हैं") == bench.Score(True, 1, 1, 0, 0)
    assert bench.score("कल मिलते हैं", ["कल मिलते हैं।"], "कल मिलते हैं") == bench.Score(True, 1, 0, 0, 0)
    assert bench.score("Call tomorrow.", ["कल मिलते हैं।"], "कल मिलते हैं") == bench.Score(False, 0, 0, 0, 0)


def test_marks_and_case_the_input_already_had_are_not_counted() -> None:
    said = "Hey Sam, ship it"
    assert bench.score("Hey Sam, ship it", ["Hey Sam, ship it."], said) == bench.Score(True, 1, 0, 0, 0)
    assert bench.score("Hey Sam, ship it.", ["Hey Sam, ship it."], said) == bench.Score(True, 1, 1, 0, 0)
    assert bench.score("hey sam ship it.", ["Hey Sam, ship it."], "hey sam ship it") == bench.Score(True, 2, 1, 2, 0)


def test_a_word_the_input_never_had_counts_only_for_the_mark_or_capital_it_needs() -> None:
    assert bench.score("She goes home.", ["She goes home."], "she go home") == bench.Score(True, 1, 1, 1, 1)
    assert bench.score("Ship it Monday.", ["Ship it Monday."], "ship it") == bench.Score(True, 1, 1, 2, 2)
    assert bench.score("टास्क पूरे कर लिए।", ["टास्क पूरे कर लिए।"], "टास्क पूरा कर लिया") == bench.Score(True, 1, 1, 0, 0)


def test_a_mark_added_or_changed_where_none_was_needed_counts_against_punctuation() -> None:
    # Round 1 (11 Oct): over-punctuating an already punctuated text was invisible to the punctuation gate.
    said = "The deploy is done. The logs are clean."
    scored = bench.score("The deploy, is done? The logs, are clean!", [said], said)
    assert scored == bench.Score(True, 4, 0, 0, 0)
    assert bench.score(said, [said], said) == bench.Score(True, 0, 0, 0, 0)


def test_an_opening_quote_or_bracket_is_a_mark_too() -> None:
    said = "leah told the partner quote our burn stays flat end quote"
    expected = ['Leah told the partner, "Our burn stays flat."']
    assert bench.score('Leah told the partner, "Our burn stays flat."', expected, said) == bench.Score(True, 3, 3, 2, 2)
    assert bench.score('Leah told the partner, Our burn stays flat."', expected, said) == bench.Score(True, 3, 2, 2, 2)


def test_curly_and_straight_quotes_are_the_same_word_and_mark() -> None:
    curly = "Nia said, \u201cChoose the safer option,\u201d so I picked the steel one."
    straight = 'Nia said, "Choose the safer option," so I picked the steel one.'
    assert bench.score(straight, [curly], curly).words_right
    assert bench.exact(straight, [curly])


def test_report_does_not_grade_a_run_with_over_5_percent_provider_errors(capsys: pytest.CaptureFixture[str]) -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    answered = [result(entry, "She goes home.") for _ in range(18)]
    failed = [bench.Result(entry, entry.text, False, "-", 100, [], True) for _ in range(2)]
    assert bench.report("fake", answered + failed) is False
    out = capsys.readouterr().out
    assert "NOT RUN" in out
    assert "grades:" not in out


def test_a_chunked_item_splits_at_the_first_cue_after_three_words() -> None:
    assert bench.split_at_cue("Let's deploy this on Tuesday, actually Wednesday.") == (
        "Let's deploy this on Tuesday,",
        "actually Wednesday.",
    )
    assert bench.split_at_cue("No, send it now please") is None
    assert bench.split_at_cue("okay no, send it now please") is None
    assert bench.split_at_cue("कल मीटिंग रखते हैं नहीं नहीं परसों") == ("कल मीटिंग रखते हैं", "नहीं नहीं परसों")
    assert bench.split_at_cue("ship the build tonight") is None


def item(slice_: str, kind: str, text: str, expected: str, prior: str = "") -> bench.Item:
    return bench.Item(slice_, kind, text, prior, [], "en", "other", [expected])


def result(entry: bench.Item, text: str, tier: str = "small", calls: list[Call] | None = None) -> bench.Result:
    return bench.Result(entry, text, False, tier, 100, calls or [], False)


def test_a_model_that_changes_nothing_fails_the_punctuation_and_case_gates() -> None:
    entries = [
        item("formatting", "run-on", "hey sam can you ship it", "Hey Sam, can you ship it?"),
        item("formatting", "run-on", "Kal milte hain", "Kal milte hain."),
        item("grammar", "tense", "she go home", "She goes home."),
    ]
    scored = bench.gates([result(entry, entry.text) for entry in entries])
    assert scored["punctuation"] == 0
    assert scored["capitalization"] == 0


def test_report_fails_when_one_gate_is_under_its_bar_and_passes_when_all_clear() -> None:
    good = [result(item("grammar", "tense", "she go home", "She goes home."), "She goes home.")]
    bad = [result(item("grammar", "tense", "she go home", "She goes home."), "She go home.")]
    assert bench.report("fake", good) is True
    assert bench.report("fake", bad) is False


def test_clean_text_unchanged_counts_traps_only() -> None:
    traps = [
        result(item("grammar", "trap correct sentence", "Ship it.", "Ship it."), "Ship it."),
        result(item("grammar", "trap correct sentence", "Ship it now.", "Ship it now."), "Ship now."),
    ]
    assert bench.gates(traps)["clean text unchanged"] == 50.0


def test_grammar_kept_counts_correct_grammar_rows_left_correct_and_grades_grammar() -> None:
    # Round 1 (11 Oct): breaking an already correct sentence never reached the grammar gate.
    kept = [
        result(
            item("hinglish-grammar", "trap Hinglish", "Mera cousin kal aayega.", "Mera cousin kal aayega."),
            "Mera cousin kal aayega.",
        ),
        result(item("grammar", "trap correct sentence", "We shipped it.", "We shipped it."), "We ship it."),
        result(item("formatting", "trap correct sentence", "Ship it.", "Ship it."), "Ship it now."),
    ]
    scored = bench.gates(kept)
    assert scored["grammar kept"] == 50.0
    assert "grammar kept" in bench.AREAS["Grammar"]


def test_money_prices_each_tier_per_thousand_dictations() -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    small = Call("openai:gpt-4.1-nano", Usage(1000, 20), "ok")
    retried = [Call("openai:gpt-4.1-nano", Usage(1000, 20), "unheard"), Call("openai:gpt-4.1", Usage(1000, 20), "ok")]
    dollars = bench.money([result(entry, "", "small", [small]), result(entry, "", "big", retried)])
    # OpenAI calls are billed at the priority estimate, twice the list price (13 Oct).
    assert dollars == pytest.approx({"small": 0.216, "big": 2.16})


def test_report_prints_the_share_of_prompt_tokens_served_from_the_cache(capsys: pytest.CaptureFixture[str]) -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    calls = [Call("openai:gpt-6-luna", Usage(1000, 20, 900), "ok"), Call("openai:gpt-6-luna", Usage(1000, 20, 0), "ok")]
    bench.report("fake", [result(entry, "She goes home.", calls=calls)])
    assert "prompt cache 45.0 %" in capsys.readouterr().out


def test_money_prices_cached_prompt_tokens_at_the_cached_price() -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    cached = Call("openai:gpt-4.1-nano", Usage(1000, 20, 1000), "ok")
    dollars = bench.money([result(entry, "", "small", [cached])])
    assert dollars == pytest.approx({"small": 0.066, "big": 0.0})


class Scripted:
    engine = "openai:gpt-4.1"

    def __init__(self, answer: dict) -> None:
        self.answer = answer

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        return json.dumps(self.answer), Usage(10, 2)

    async def aclose(self) -> None:
        pass


def test_a_full_retraction_of_the_prior_is_scored_without_it() -> None:
    entry = item("backtracking-chunked", "cue no", "No, send it to Ravi.", "Send it to Ravi.", "Send the deck to Sam.")
    provider = Scripted({"revision": "", "text": "Send it to Ravi."})
    tiers = Tiers(small=provider, big=provider, fallback=None)
    outcome = asyncio.run(bench.run(entry, tiers, asyncio.Semaphore(1)))
    assert outcome is not None
    assert outcome.text == "Send it to Ravi."
    assert bench.score(outcome.text, entry.expected).words_right


def test_a_rejected_full_retraction_that_was_right_counts_as_a_false_reject() -> None:
    entry = item("backtracking-chunked", "cue no", "No, send it to Ravi.", "Send it to Ravi.", "Send the deck to Sam.")
    output = json.dumps({"revision": "", "text": "Send it to Ravi."})
    rejected = result(entry, entry.text, calls=[Call("openai:gpt-4.1", Usage(1, 1), "unheard", output)])
    assert bench.falsely_rejected(rejected)


def test_a_first_chunk_answered_as_a_revision_is_scored_as_the_server_judges_it() -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    only_revision = json.dumps({"revision": "She goes home.", "text": ""})
    rejected = result(entry, entry.text, calls=[Call("openai:gpt-4.1-nano", Usage(1, 1), "unheard", only_revision)])
    assert bench.falsely_rejected(rejected)
    with_text = json.dumps({"revision": "She goes home.", "text": "She go home."})
    rejected = result(entry, entry.text, calls=[Call("openai:gpt-4.1-nano", Usage(1, 1), "unheard", with_text)])
    assert not bench.falsely_rejected(rejected)


def test_an_answer_to_a_folded_correction_is_scored_as_the_server_unfolds_it() -> None:
    prior = "We need a list. Let's meet on Friday at noon."
    entry = item(
        "backtracking-chunked",
        "cue",
        "no wait make it thursday",
        "We need a list. Let's meet on Thursday at noon.",
        prior,
    )
    folded = json.dumps({"revision": None, "text": "Let's meet on Thursday at noon."})
    rejected = result(entry, prior, calls=[Call("openai:gpt-4.1-mini", Usage(1, 1), "unheard", folded)])
    assert bench.falsely_rejected(rejected)


def corpus_rows() -> list[dict]:
    rows = []
    for path in sorted(bench.CORPUS.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            rows += [json.loads(line) for line in lines]
    return rows


LIST_ITEM = re.compile(r"^(?:\d+\. |- )(.+)$", re.MULTILINE)


def test_every_corpus_list_item_starts_with_a_capital_and_has_no_full_stop() -> None:
    items = [found for row in corpus_rows() for out in row["expected"] for found in LIST_ITEM.findall(out)]
    wrong = [text for text in items if text[0].islower() or text.endswith((".", "।"))]
    assert len(items) > 200
    assert wrong == []


def test_no_corpus_answer_is_a_lone_list_item() -> None:
    split = [row["id"] for row in corpus_rows() for out in row["expected"] if LIST_ITEM.fullmatch(out)]
    assert split == []


def test_a_dictated_question_ends_with_a_question_mark() -> None:
    question = re.compile(r"^(?:Can|Could|Would|Will) you\b[^?\n]*\.$", re.MULTILINE)
    wrong = [row["id"] for row in corpus_rows() for out in row["expected"] if question.search(out)]
    assert wrong == []


def test_spoken_times_and_percentages_are_written_as_digits() -> None:
    spoken = re.compile(
        r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve) (?:am|pm|baje)\b|\d प्रतिशत"
    )
    wrong = [row["id"] for row in corpus_rows() for out in row["expected"] if spoken.search(out)]
    assert wrong == []


def test_a_spoken_number_before_a_unit_of_time_or_money_is_written_as_digits() -> None:
    spoken = re.compile(
        r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten) "
        r"(?:days?|weeks?|months?|years?|hours?|minutes?|seconds?|dollars?|percent)\b"
        r"|\b(?:ek|do|teen|char|paanch|chhe|saat|aath|nau|das) (?:din|hafte|mahine|saal|ghante|minute)\b"
        r"|(?:एक|दो|तीन|चार|पांच|पाँच|छह|सात|आठ|नौ|दस) (?:दिन|हफ्ते|महीने|साल|घंटे|मिनट)"
    )
    wrong = [row["id"] for row in corpus_rows() for out in row["expected"] if spoken.search(out)]
    assert wrong == []


def test_money_takes_its_symbol_instead_of_a_currency_word() -> None:
    worded = re.compile(r"\d (?:(?:lakh|crore|लाख|करोड़) )?(?:rupees|rupaye|dollars|रुपये|रुपए)")
    wrong = [row["id"] for row in corpus_rows() for out in row["expected"] if worded.search(out)]
    assert wrong == []


def test_no_expected_output_stops_at_the_colon_before_its_list() -> None:
    cut = [row["id"] for row in corpus_rows() for out in row["expected"] if out.rstrip().endswith(":")]
    assert cut == []


def test_the_corpus_has_hinglish_and_hindi_lists_and_app_style_rows() -> None:
    rows = corpus_rows()
    lists = [row["kind"] for row in rows if any(LIST_ITEM.search(out) for out in row["expected"])]
    assert sum("Hinglish" in kind for kind in lists) >= 10
    assert sum("Hindi" in kind for kind in lists) >= 10
    traps = [row for row in rows if row["kind"].startswith("trap list words")]
    assert len(traps) >= 5
    assert not any(LIST_ITEM.search(out) for row in traps for out in row["expected"])
    styles = Counter(item.style for item in bench.load())
    assert all(styles[style] >= 5 for style in ("email", "code", "chat", "docs")), styles
    assert styles["other"] > 0
    # v2 kinds name Roman Hindi too ("Roman Hindi spelling variant kept"); a v2 row has its own language.
    languages = {item.language for item in bench.load() if "Hindi" in item.kind and not item.split}
    assert languages == {"hi"}


def test_every_language_has_cue_led_bullet_lists_and_cueless_sequences_that_stay_prose() -> None:
    bullet = re.compile(r"^- ", re.MULTILINE)
    rows = corpus_rows()
    for name in ("formatting", "hinglish-formatting", "hi-formatting"):
        slice_rows = [row for row in rows if row["id"].startswith(f"{name}-")]
        led = [row for row in slice_rows if row["kind"] == "cue-led bullet list"]
        assert len(led) >= 8, name
        assert all(len(bullet.findall(row["expected"][0])) >= 2 for row in led), name
        assert all(row["expected"][0].splitlines()[0].endswith(":") for row in led), name
        plain = [row for row in slice_rows if row["kind"] == "trap list words sequence without a cue"]
        assert len(plain) >= 5, name
        # "Do one thing" announces a list only when three or more steps follow (founder, 11 Oct).
        one_thing = re.compile(r"one thing|ek kaam|एक काम", re.IGNORECASE)
        assert sum(bool(one_thing.search(row["said"])) for row in led) >= 2, name
        assert sum(bool(one_thing.search(row["said"])) for row in plain) >= 2, name
        assert not any(bullet.search(out) or LIST_ITEM.search(out) for row in plain for out in row["expected"]), name


def test_every_language_has_digits_kept_or_written_as_words_and_digits_that_stay() -> None:
    # Whisper writes "do 1 thing"; either "1" or "one" is right (founder, 11 Oct), except before a unit or time.
    rows = corpus_rows()
    digit = re.compile(r"(?<![\d.:])\b([1-9]|10)\b(?![\d.:])")
    for name in ("formatting", "hinglish-formatting", "hi-formatting"):
        slice_rows = [row for row in rows if row["id"].startswith(f"{name}-")]
        either = [row for row in slice_rows if row["kind"] == "digit as said or as its word"]
        assert len(either) >= 5, name
        assert all(digit.search(row["said"]) for row in either), name
        assert all(any(digit.search(o) for o in row["expected"]) for row in either), name
        assert all(any(not digit.search(o) for o in row["expected"]) for row in either), name
        kept = [row for row in slice_rows if row["kind"] == "trap digit stays a digit"]
        assert len(kept) >= 2, name
        assert all(re.search(r"\d", out) for row in kept for out in row["expected"]), name


def test_a_rows_own_language_is_the_language_it_is_sent_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The founder's dictations are English speech the app tags Hinglish; the bench sends them as the app does.
    rows = [
        {"kind": "counted list", "said": "do 1 thing", "expected": ["Do one thing."], "language": "hinglish"},
        {"kind": "counted list", "said": "ship it", "expected": ["Ship it."]},
    ]
    with gzip.open(tmp_path / "founder.jsonl.gz", "wt") as out:
        out.writelines(json.dumps(row) + "\n" for row in rows)
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    assert [item.language for item in bench.load()] == ["hinglish", "en"]


def test_a_rows_prior_is_sent_as_the_text_before_the_piece(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A piece cut mid-sentence after a list item, as the app sent it (founder, 11 Oct).
    row = {"kind": "piece after a list item", "said": "and how", "expected": ["1. A and how"], "prior": "1. A"}
    with gzip.open(tmp_path / "founder.jsonl.gz", "wt") as out:
        out.write(json.dumps(row) + "\n")
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    assert [(item.prior, item.text) for item in bench.load()] == [("1. A", "and how")]


def test_the_founder_slice_holds_the_reported_dictations_and_the_decided_traps() -> None:
    # Every dictation the founder reported on 10 and 11 Oct, as the app sent it, with the answer the founder asked for.
    rows = [row for row in corpus_rows() if row["id"].startswith("founder-")]
    reported = [row for row in rows if not row["kind"].startswith("trap")]
    traps = [row for row in rows if row["kind"].startswith("trap")]
    assert len(reported) >= 9
    assert len(traps) >= 6
    assert all(row.get("note") for row in rows), "each row says which report or decision it comes from"


def test_per_slice_takes_the_same_seeded_sample_from_every_slice() -> None:
    entries = [item(name, "kind", f"{name} {number}", "") for name in ("grammar", "dictionary") for number in range(6)]
    picked = [entry.text for entry in bench.per_slice(entries, 2)]
    assert picked == ["grammar 1", "grammar 2", "dictionary 1", "dictionary 2"]
    assert [entry.text for entry in bench.per_slice(entries, 9)] == [entry.text for entry in entries]


def test_first_takes_the_first_items_of_every_slice_in_corpus_order() -> None:
    entries = [item(name, "kind", f"{name} {number}", "") for name in ("grammar", "dictionary") for number in range(3)]
    picked = [entry.text for entry in bench.first(entries, 2)]
    assert picked == ["grammar 0", "grammar 1", "dictionary 0", "dictionary 1"]


def test_misses_lists_each_result_that_missed_a_gate_with_what_it_missed() -> None:
    right = result(item("grammar", "tense", "she go home", "She goes home."), "She goes home.")
    wrong = result(item("grammar", "tense", "she go home", "She goes home."), "she goes home")
    missed = bench.misses([right, wrong])
    assert missed == [
        {
            "slice": "grammar",
            "kind": "tense",
            "prior": "",
            "text": "she go home",
            "output": "she goes home",
            "expected": ["She goes home."],
            "terms": [],
            "rejected": False,
            "falsely_rejected": False,
            "words_right": True,
            "punctuation": [0, 1],
            "case": [0, 1],
            "missing_terms": [],
            "wrong_list": False,
            "failed": False,
            "id": "",
            "split": "",
            "language": "en",
            "length": "",
            "exact": False,
            "fixes_missed": [],
            "keeps_lost": [],
            "invented": [],
            "refused": [],
        }
    ]


def test_misses_marks_a_provider_failure_scored_as_the_raw_input() -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    failed = bench.Result(entry, entry.text, False, "-", 100, [], True)
    missed = bench.misses([failed])
    assert [miss["failed"] for miss in missed] == [True]


def test_misses_lists_a_prose_answer_that_came_out_as_a_list() -> None:
    said = "First we eat, then we work."
    listed = result(item("formatting", "trap list words", said, said), "- First we eat\n- Then we work")
    missed = bench.misses([listed])
    assert [miss["wrong_list"] for miss in missed] == [True]


LISTED = "We need:\n1. The API\n2. The UI"


def test_list_shape_passes_only_the_same_items_one_per_line_of_the_same_kind() -> None:
    entry = item("formatting", "spoken numbered list", "we need first the api second the ui", LISTED)
    outputs = [
        LISTED,
        "We need:\n- The API\n- The UI",
        "We need:\n1. The API 2. The UI",
        "We need:\n1. The API\n2. The UI\n3. Docs",
        "We need the API and the UI.",
    ]
    prose = item("formatting", "run-on", "ship it", "Ship it.")
    results = [result(entry, output) for output in outputs] + [result(prose, "Ship it.")]
    assert bench.gates(results)["list shape"] == 20.0


def test_a_prose_answer_is_the_right_shape_when_one_expected_answer_is_prose() -> None:
    gold = ["We discuss three things: rent, water and lights.", "We discuss three things:\n- Rent\n- Water\n- Lights"]
    said = "we discuss three things rent water and lights"
    entry = bench.Item("formatting", "inline series", said, "", [], "en", "other", gold)
    prose = result(entry, "We discuss three things: rent, water and lights.")
    numbered = result(entry, "We discuss three things:\n1. Rent\n2. Water\n3. Lights")
    assert bench.gates([prose])["list shape"] == 100.0
    assert bench.gates([numbered])["list shape"] == 0.0
    assert bench.misses([prose]) == []


def test_a_bulleted_answer_matches_dash_or_dot_bullets() -> None:
    entry = item("formatting", "spoken bullet list", "trip bullet towel bullet charger", "Trip:\n- Towel\n- Charger")
    assert bench.gates([result(entry, "Trip:\n• Towel\n• Charger")])["list shape"] == 100.0


def test_false_lists_counts_prose_answers_that_came_out_as_a_list() -> None:
    listed = item("formatting", "spoken numbered list", "we need first the api second the ui", LISTED)
    prose = item("formatting", "trap list words", "I was first in line.", "I was first in line.")
    outputs = ["I was first in line.", "1. I was first in line", "• I was first in line", "I was first in line."]
    results = [result(prose, output) for output in outputs] + [result(listed, "We need the API and the UI.")]
    assert bench.gates(results)["false lists"] == 50.0


def test_report_fails_on_one_false_list_in_a_small_sample() -> None:
    said = "First we eat, then we work."
    entry = item("formatting", "trap list words", said, said)
    assert bench.report("fake", [result(entry, said)]) is True
    assert bench.report("fake", [result(entry, "- First we eat\n- Then we work")]) is False


def numbered(name: str, count: int) -> list[bench.Item]:
    return [item(name, "kind", f"{name} {n}", f"{name} {n}") for n in range(count)]


def test_heldout_takes_seeded_items_outside_the_dev_sample_of_every_slice() -> None:
    entries = numbered("grammar", 45) + numbered("dictionary", 43)
    picked = [entry.text for entry in bench.heldout(entries, 2)]
    assert picked == ["grammar 14", "grammar 15", "dictionary 5", "dictionary 14"]
    assert picked == [entry.text for entry in bench.heldout(entries, 2)]
    dev = {entry.text for entry in bench.per_slice(entries, 40)}
    assert dev.isdisjoint(entry.text for entry in bench.heldout(entries, 9))


def test_heldout_leaves_out_the_chunked_twin_of_a_dev_row() -> None:
    twins = [item("backtracking-chunked", "kind", f"chunk {n}", f"backtracking {n}") for n in range(1, 45)]
    entries = numbered("backtracking", 45) + twins
    dev = {entry.expected[0] for entry in bench.per_slice(entries, 40)}
    held = {entry.expected[0] for entry in bench.heldout(entries, 9)}
    assert held
    assert dev.isdisjoint(held)


TRAP_FILES = ("formatting", "hinglish-formatting", "hi-formatting")


def test_each_language_has_list_traps_that_keep_their_words_as_prose() -> None:
    for name in TRAP_FILES:
        with gzip.open(bench.CORPUS / f"{name}.jsonl.gz", "rt", encoding="utf-8") as lines:
            rows = [json.loads(line) for line in lines]
        traps = [row for row in rows if row["kind"] == "trap list words"]
        listed = [row["id"] for row in traps for out in row["expected"] if bench.list_shape(out)]
        reworded = [row["id"] for row in traps for out in row["expected"] if words(out) != words(row["said"])]
        assert len(traps) >= 30, name
        assert listed == []
        assert reworded == []


def words(text: str) -> list[str]:
    found = [word for word, _, _ in bench.tokens(text)]
    return found


def test_every_corpus_row_is_numbered_in_file_order() -> None:
    # v2 files keep the id each row was generated with (test_v2_ids_are_unique_sorted_and_name_slice_and_language).
    for path in sorted(bench.CORPUS.glob("*.jsonl.gz")):
        name = path.name.removesuffix(".jsonl.gz")
        if bench.is_v2(name):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            rows = [json.loads(line) for line in lines]
        wrong = [row["id"] for number, row in enumerate(rows) if row["id"] != f"{name}-{number:03d}"]
        assert wrong == [], name


def test_a_corpus_row_said_mid_sentence_is_asked_as_continuing() -> None:
    continuing = [entry for entry in bench.load() if entry.continues]
    assert {entry.language for entry in continuing} >= {"en", "hinglish", "hi"}
    assert all(bench.request_of(entry).continues for entry in continuing)
    assert not bench.request_of(item("grammar", "tense", "she go home", "She goes home.")).continues


def test_each_item_gets_the_deadline_the_app_gives_the_last_chunk() -> None:
    entry = item("grammar", "tense", "she go home", "She goes home.")
    assert bench.request_of(entry).deadline_ms == 20_000


class Late(Scripted):
    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        await asyncio.sleep(0.5)
        return await super().complete(turns, max_tokens, timeout, reason)


def test_an_answer_after_the_deadline_is_scored_as_the_text_sent_and_counted_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "DEADLINE_MS", 600)
    entry = item("grammar", "tense", "she go home", "She goes home.")
    provider = Late({"revision": None, "text": "She goes home."})
    tiers = Tiers(small=provider, big=provider, fallback=None)
    late = asyncio.run(bench.run(entry, tiers, asyncio.Semaphore(1)))
    assert late is not None
    assert late.failed
    assert late.text == "she go home"
    bench.report("fake", [late, result(entry, "She goes home.")])
    assert "timed out 50.0 %" in capsys.readouterr().out


STYLED_FILES = ["formatting", "hinglish-formatting", "hi-formatting"]


def test_each_language_has_items_for_every_app_style_and_chat_stays_one_line() -> None:
    for name in STYLED_FILES:
        with gzip.open(bench.CORPUS / f"{name}.jsonl.gz", "rt", encoding="utf-8") as lines:
            rows = [json.loads(line) for line in lines]
        styles = Counter(row["app_style"] for row in rows if "app_style" in row)
        assert min(styles[style] for style in ("email", "code", "chat", "docs")) >= 1, (name, styles)
        chat = [out for row in rows if row.get("app_style") == "chat" for out in row["expected"]]
        assert all("\n" not in out for out in chat), name


def test_homophone_slips_are_in_the_grammar_slice() -> None:
    with gzip.open(bench.CORPUS / "grammar.jsonl.gz", "rt", encoding="utf-8") as lines:
        kinds = Counter(json.loads(line)["kind"] for line in lines)
    assert kinds["homophone mix-ups"] >= 10


# The v2 corpus and its gates (docs/bench.md, The v2 corpus).


def v2item(
    text: str,
    expected: str | list[str],
    *,
    slice_: str = "v2-grammar",
    kind: str = "slip",
    language: str = "en",
    length: str = "sentence",
    terms: tuple[str, ...] = (),
    fixes: tuple[tuple[str, str], ...] = (),
    keeps: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    split: str = "heldout",
    prior: str = "",
) -> bench.Item:
    answers = [expected] if isinstance(expected, str) else expected
    return bench.Item(
        slice_,
        kind,
        text,
        prior,
        list(terms),
        language,
        "other",
        answers,
        id=f"{slice_}-{language}-00001",
        length=length,
        split=split,
        fixes=fixes,
        keeps=keeps,
        tags=tags,
    )


def test_a_full_stop_after_a_list_item_is_optional() -> None:
    # Founder decision D3 (11 Oct): both forms are right; getting the list is what matters.
    expected = ["We need:\n1. The API\n2. The UI"]
    said = "we need one the api two the ui"
    assert bench.score("We need:\n1. The API.\n2. The UI.", expected, said) == bench.score(expected[0], expected, said)
    assert bench.exact("We need:\n1. The API.\n2. The UI.", expected)
    assert bench.exact("हमें चाहिए:\n- दूध।\n- चाय।", ["हमें चाहिए:\n- दूध\n- चाय"])
    assert not bench.exact("We need. The API.", ["We need the API."])


def test_sentence_correctness_needs_every_word_mark_and_capital() -> None:
    entry = v2item("hey sam can you ship it", "Hey Sam, can you ship it?")
    outputs = [
        "Hey Sam, can you ship it?",
        "Hey  Sam,  can you ship it? ",
        "Hey Sam can you ship it?",
        "hey Sam, can you ship it?",
    ]
    scored = bench.gates([result(entry, output) for output in outputs])
    assert scored["sentence correctness"] == 50.0


def test_misheard_words_fixed_counts_each_fix_at_its_aligned_place() -> None:
    one = v2item("please taste the build", "Please test the build.", fixes=(("taste", "test"),))
    two = v2item(
        "the waist bin is by the break room",
        "The waste bin is by the break room.",
        fixes=(("waist", "waste"), ("brake", "break")),
    )
    elsewhere = v2item(
        "run the test then taste the build", "Run the test, then test the build.", fixes=(("taste", "test"),)
    )
    results = [
        result(one, "Please test the build."),
        result(two, "The waist bin is by the break room."),
        result(elsewhere, "Run the test, then taste the build."),
    ]
    assert bench.gates(results)["misheard words fixed"] == 50.0


def test_words_not_over_fixed_counts_kept_words_and_foreign_words_in_traps() -> None:
    kept = v2item(
        "we went for a wine tasting",
        "We went for a wine tasting.",
        kind="trap homophone used right",
        keeps=("tasting",),
    )
    trap = v2item("the lift is broken", "The lift is broken.", kind="trap term twin", terms=("Lyft",))
    results = [result(kept, "We went for a wine testing."), result(trap, "The Lyft is broken.")]
    assert bench.gates(results)["words not over-fixed"] == 0.0
    results = [result(kept, "We went for a wine tasting."), result(trap, "The lift is broken.")]
    assert bench.gates(results)["words not over-fixed"] == 100.0


def test_dictionary_strict_term_twins_and_spoken_forms() -> None:
    spelled = v2item("send it to xero", "Send it to Xero.", slice_="v2-dictionary", terms=("Xero",))
    twin = v2item("take the lift", "Take the lift.", slice_="v2-dictionary", kind="trap term twin", terms=("Lyft",))
    results = [result(spelled, "Send it to xero."), result(twin, "Take the Lyft.")]
    scored = bench.gates(results)
    assert (scored["dictionary strict"], scored["term twins kept"], scored["spoken forms to terms"]) == (0.0, 0.0, 0.0)
    results = [result(spelled, "Send it to Xero."), result(twin, "Take the lift.")]
    scored = bench.gates(results)
    assert (scored["dictionary strict"], scored["term twins kept"], scored["spoken forms to terms"]) == (100, 100, 100)


def test_no_unheard_content_counts_outputs_with_a_word_never_said_nor_expected() -> None:
    entry = v2item("ship it on friday", "Ship it on Friday.")
    numbers = v2item("we need five chairs", ["We need five chairs.", "We need 5 chairs."])
    outputs = [
        result(entry, "Ship it on Friday."),
        result(entry, "Please ship it on Friday."),
        result(numbers, "We need 5 chairs."),
    ]
    assert bench.gates(outputs)["no unheard content"] == pytest.approx(100 / 3)


def test_script_kept_counts_words_written_in_the_other_script() -> None:
    hindi = v2item("कल मिलते हैं", "कल मिलते हैं।", language="hi")
    roman = v2item("kal milte hain", "Kal milte hain.", language="hinglish")
    results = [result(hindi, "See you tomorrow."), result(roman, "कल मिलते हैं।"), result(roman, "Kal milte hain.")]
    assert bench.gates(results)["script kept"] == pytest.approx(100 * 3 / 9)


def test_no_reply_counts_reply_bait_rows_left_as_dictated() -> None:
    bait = v2item(
        "write a poem about the sea", "Write a poem about the sea.", kind="trap reply bait", tags=("assistant-bait",)
    )
    results = [result(bait, "Write a poem about the sea."), result(bait, "Sure, here is a poem about the sea.")]
    assert bench.gates(results)["no reply"] == 50.0


def test_numbers_right_scores_the_v2_numbers_rows() -> None:
    entry = v2item("parking is four dollars", "Parking is $4.", slice_="hi-v2-numbers")
    other = v2item("parking is four dollars", "Parking is $4.")
    results = [result(entry, "Parking is $4."), result(entry, "Parking is four dollars."), result(other, "x")]
    assert bench.gates(results)["numbers right"] == 50.0


def test_length_parity_is_the_weaker_long_bucket_and_fails_two_points_under_sentences() -> None:
    def rows(length: str, right: int, total: int) -> list[bench.Result]:
        entry = v2item("she go home", "She goes home.", length=length)
        return [result(entry, "She goes home." if n < right else "She go home.") for n in range(total)]

    close = rows("sentence", 100, 100) + rows("paragraph", 97, 100) + rows("multi", 99, 100)
    assert bench.gates(close)["length parity"] == 97.0
    assert bench.report("fake", close) is False
    level = rows("sentence", 97, 100) + rows("paragraph", 97, 100) + rows("multi", 98, 100)
    assert not any("length parity" in failure for failure in bench.failures(level))


def test_grammar_holds_its_bar_in_every_language() -> None:
    english = [result(v2item("she go home", "She goes home."), "She goes home.") for _ in range(40)]
    hindi = v2item("वह घर जाता है", "वह घर जाती है।", language="hi", slice_="hi-v2-grammar")
    results = [*english, result(hindi, "वह घर जाती है।"), result(hindi, "वह घर जाता है।")]
    assert bench.gates(results)["grammar"] == pytest.approx(100 * 41 / 42)
    failures = bench.failures(results)
    assert "grammar hi 50.0 < 95" in failures
    assert not any(failure.startswith("grammar en") for failure in failures)


def test_sentence_correctness_holds_93_in_every_language_and_length_cell_of_30_rows() -> None:
    english = [result(v2item("ship it", "Ship it."), "Ship it.") for _ in range(300)]
    hindi = v2item("कल मिलते हैं", "कल मिलते हैं।", language="hi", length="1-4")
    small = [result(hindi, "कल मिलते हैं।" if n < 27 else "कल मिलते हैं") for n in range(30)]
    assert "sentence correctness hi 1-4 90.0 < 93" in bench.failures(english + small)
    assert not any("sentence correctness" in failure for failure in bench.failures(english + small[1:]))


def test_report_prints_each_gate_overall_per_language_and_per_length(capsys: pytest.CaptureFixture[str]) -> None:
    entry = v2item("kal milte hain", "Kal milte hain.", language="hinglish", length="1-4")
    bench.report("fake", [result(entry, "Kal milte hain.")])
    header = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("gate"))
    assert header.split() == [
        "gate",
        "bar",
        "all",
        "en",
        "hinglish",
        "hi",
        "1-4",
        "sentence",
        "2-5",
        "paragraph",
        "multi",
    ]


def test_a_gate_grades_by_how_far_it_is_over_or_under_its_bar() -> None:
    assert bench.grade(97, 95, True) == pytest.approx(9.7)
    assert bench.grade(100, 95, True) == pytest.approx(10.0)
    assert bench.grade(90, 95, True) == pytest.approx(7.0)
    assert bench.grade(0.25, 0.5, False) == pytest.approx(9.75)
    assert bench.grade(2.5, 0.5, False) == pytest.approx(8.5)
    assert bench.grade(None, 95, True) is None


def test_an_area_grades_as_its_weakest_gate_and_the_overall_as_its_weakest_area() -> None:
    right = [result(v2item("she go home", "She goes home."), "She goes home.") for _ in range(19)]
    wrong = result(v2item("she go home", "She goes home."), "She go home.")
    grades = bench.grades([*right, wrong])
    assert grades["Grammar"] == pytest.approx(9.5)
    assert grades["Sentence correctness"] == pytest.approx(9.5)
    assert grades["overall"] == min(grade for area, grade in grades.items() if area != "overall" and grade is not None)


def test_split_takes_v2_rows_by_their_field_and_old_rows_by_the_dev_seed() -> None:
    old = numbered("grammar", 45)
    v2 = [v2item(f"row {n}", f"Row {n}.", split="dev" if n % 3 == 0 else "heldout") for n in range(6)]
    dev = bench.in_split(old + v2, "dev")
    held = bench.in_split(old + v2, "heldout")
    assert [entry.text for entry in dev if entry.split] == ["row 0", "row 3"]
    assert {entry.text for entry in dev if not entry.split} == {entry.text for entry in bench.per_slice(old, 40)}
    assert {entry.text for entry in held if not entry.split} == {entry.text for entry in bench.heldout(old, 45)}
    assert len(dev) + len(held) == len(old) + len(v2)


def write_corpus(tmp_path: Path, name: str, rows: list[dict]) -> None:
    with gzip.open(tmp_path / f"{name}.jsonl.gz", "wt", encoding="utf-8") as out:
        out.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def test_load_reads_a_v2_rows_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {
        "id": "v2-stt-homophones-hinglish-00001",
        "kind": "homophone in context",
        "said": "ye taste karo",
        "prior": "Build ready hai.",
        "dictionary": ["Xero"],
        "expected": ["Build ready hai. Ye test karo."],
        "language": "hinglish",
        "length": "1-4",
        "fixes": [{"heard": "taste", "written": "test"}],
        "keeps": ["ready"],
        "tags": ["homophone"],
        "style": "raw",
        "split": "dev",
        "note": "context",
    }
    write_corpus(tmp_path, "hinglish-v2-stt-homophones", [row])
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    [entry] = bench.load()
    assert (entry.slice, entry.id, entry.language, entry.length, entry.split) == (
        "hinglish-v2-stt-homophones",
        "v2-stt-homophones-hinglish-00001",
        "hinglish",
        "1-4",
        "dev",
    )
    assert (entry.prior, entry.terms, entry.fixes, entry.keeps, entry.tags) == (
        "Build ready hai.",
        ["Xero"],
        (("taste", "test"),),
        ("ready",),
        ("homophone",),
    )


def test_every_backtracking_slice_gets_a_chunked_twin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {
        "kind": "cue nahi nahi",
        "said": "meeting kal rakhte hain nahi nahi parso",
        "dictionary": [],
        "expected": ["Meeting parso rakhte hain."],
    }
    trap = {**row, "kind": "trap real negation"}
    write_corpus(tmp_path, "hinglish-backtracking", [row, trap])
    write_corpus(tmp_path, "hi-v2-backtracking", [{**row, "said": "मीटिंग कल रखते हैं नहीं नहीं परसों", "split": "dev"}])
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    chunked = [
        (entry.slice, entry.prior, entry.text, entry.split) for entry in bench.load() if entry.slice.endswith("chunked")
    ]
    assert chunked == [
        ("hi-v2-backtracking-chunked", "मीटिंग कल रखते हैं", "नहीं नहीं परसों", "dev"),
        ("hinglish-backtracking-chunked", "meeting kal rakhte hain", "nahi nahi parso", ""),
    ]


def test_a_chunked_correction_counts_for_the_backtracking_gate() -> None:
    entry = item(
        "hi-v2-backtracking-chunked", "cue", "nahi nahi parso", "Meeting parso rakhte hain.", "meeting kal rakhte hain"
    )
    assert bench.gates([result(entry, "Meeting kal rakhte hain.")])["backtracking"] == 0.0


def test_a_long_multi_row_runs_as_paragraph_pieces_after_the_written_paragraphs() -> None:
    said = "first we met the team then we ate new paragraph next we flew home and slept new paragraph last we rested"
    written = "First, we met the team, then we ate.\n\nNext, we flew home and slept.\n\nLast, we rested."
    assert bench.paragraph_cuts(said, written) == [
        (
            "First, we met the team, then we ate.",
            "new paragraph next we flew home and slept new paragraph last we rested",
        ),
        ("First, we met the team, then we ate.\n\nNext, we flew home and slept.", "new paragraph last we rested"),
    ]


def test_v2_long_multi_rows_load_as_a_chunked_slice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {
        "kind": "long dictation",
        "said": "we met the team new paragraph we flew home",
        "dictionary": [],
        "expected": ["We met the team.\n\nWe flew home."],
        "length": "multi",
        "split": "heldout",
    }
    write_corpus(tmp_path, "v2-long", [row, {**row, "length": "paragraph"}])
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    chunked = [(entry.slice, entry.prior, entry.text) for entry in bench.load() if entry.slice.endswith("chunked")]
    assert chunked == [("v2-long-chunked", "We met the team.", "new paragraph we flew home")]


def v2_files() -> dict[str, list[dict]]:
    found = {}
    for path in sorted(bench.CORPUS.glob("*.jsonl.gz")):
        name = path.name.removesuffix(".jsonl.gz")
        if bench.is_v2(name):
            with gzip.open(path, "rt", encoding="utf-8") as lines:
                found[name] = [json.loads(line) for line in lines]
    return found


V2_FIELDS = {"id", "feature", "kind", "said", "dictionary", "expected", "language", "length", "tags", "domain", "style"}
V2_FIELDS |= {"split", "note"}
ROW_ID = re.compile(r"^(?P<area>[a-z0-9-]+)-(?P<language>en|hinglish|hi)-\d{5}$")


def test_the_v2_corpus_has_every_language_and_length() -> None:
    files = v2_files()
    rows = [row for found in files.values() for row in found]
    assert {row["language"] for row in rows} == {"en", "hinglish", "hi"}
    assert {row["length"] for row in rows} == set(bench.LENGTHS)
    for name in ("v2-stt-nearsound", "hinglish-v2-stt-nearsound", "hi-v2-stt-nearsound", "founder-variants"):
        assert files.get(name), name


def test_v2_rows_have_their_fields_language_and_a_length_matching_the_words_said() -> None:
    wrong = []
    for name, rows in v2_files().items():
        prefix = name.split("-")[0]
        for row in rows:
            language = bench.LANGUAGES.get(prefix, "en")
            fields_missing = V2_FIELDS - row.keys()
            low, high = gen.LENGTH_WORDS[row["length"]]
            answers = row["expected"]
            if (
                fields_missing
                or not row["kind"]
                or not row["note"]
                or row["split"] not in {"dev", "heldout"}
                or (name not in gen.MIXED and row["language"] != language)
                or not low <= len(row["said"].split()) <= high
                or not 1 <= len(answers) <= 3
                or len(set(answers)) != len(answers)
                or len(row.get("prior", "").split()) > 300
            ):
                wrong.append(row["id"])
    assert wrong == []


def test_v2_ids_are_unique_sorted_and_name_slice_and_language() -> None:
    seen: set[str] = set()
    for name, rows in v2_files().items():
        ids = [row["id"] for row in rows]
        assert ids == sorted(ids), name
        area = name.removeprefix("hinglish-").removeprefix("hi-")
        for row in rows:
            found = ROW_ID.match(row["id"])
            assert found is not None and found["area"] == area and found["language"] == row["language"], row["id"]
        assert seen.isdisjoint(ids), name
        seen |= set(ids)


def test_v2_split_is_fixed_by_the_row_id_and_every_cell_has_dev_rows() -> None:
    for name, rows in v2_files().items():
        moved = [row["id"] for row in rows if gen.dev_by_id(row["id"]) and row["split"] != "dev"]
        assert moved == [], name
        cells = Counter((row["language"], row["length"]) for row in rows)
        dev = Counter((row["language"], row["length"]) for row in rows if row["split"] == "dev")
        short = {cell: (dev[cell], count) for cell, count in cells.items() if dev[cell] < min(5, count)}
        assert short == {}, name
        said = {split: {row["said"] for row in rows if row["split"] == split} for split in ("dev", "heldout")}
        assert said["dev"].isdisjoint(said["heldout"]), name


def test_no_two_corpus_rows_say_the_same_text() -> None:
    said = Counter(" ".join(words(row["said"])) for row in corpus_rows() if "split" in row)
    old = {" ".join(words(row["said"])) for row in corpus_rows() if "split" not in row}
    assert [text for text, count in said.items() if count > 1 or text in old] == []


def test_v2_dictionary_terms_are_kept_exactly_in_every_expected_answer() -> None:
    wrong = []
    for rows in v2_files().values():
        for row in rows:
            for term in row["dictionary"]:
                counts = {bench.term_count(out, term) for out in row["expected"]}
                if len(counts) > 1:
                    wrong.append((row["id"], term))
    assert wrong == []


def test_v2_fixes_are_heard_and_written_and_keeps_survive_in_every_answer() -> None:
    wrong = []
    for rows in v2_files().values():
        for row in rows:
            heard = " ".join(words(row.get("prior", "") + " " + row["said"]))
            for fix in row.get("fixes", []):
                written = words(fix["written"])
                if " ".join(words(fix["heard"])) not in heard or not any(
                    bench.holds(words(out), written) for out in row["expected"]
                ):
                    wrong.append((row["id"], fix["heard"]))
            for keep in row.get("keeps", []):
                if not all(bench.holds(words(out), words(keep)) for out in row["expected"]):
                    wrong.append((row["id"], keep))
    assert wrong == []


def test_v2_trap_rows_change_only_formatting() -> None:
    wrong = []
    for rows in v2_files().values():
        for row in rows:
            if row["kind"].startswith("trap"):
                said = f"{row.get('prior', '')} {row['said']}"
                if not all(gen.formatting_only(said, out, row["dictionary"]) for out in row["expected"]):
                    wrong.append(row["id"])
    assert wrong == []


def test_formatting_only_allows_marks_case_digits_terms_and_spoken_marks() -> None:
    assert gen.formatting_only("its seven dollars new paragraph dont go", "It's $7.\n\nDon't go.", [])
    assert gen.formatting_only("ask seer sha", "Ask Saoirse.", ["Saoirse"])
    assert not gen.formatting_only("send three no four", "Send four.", [])
    assert not gen.formatting_only("she go home", "She goes home.", [])


def test_v2_rows_are_hand_reviewed_in_at_least_2_percent_per_file() -> None:
    for name, rows in v2_files().items():
        reviewed = sum("hand" in row["note"].lower() for row in rows)
        assert reviewed * 100 >= 2 * len(rows), name


def test_no_prompt_example_shares_an_8_word_ngram_with_any_row() -> None:
    examples = set()
    for turn in (turn for pair in EXAMPLES for turn in pair):
        fields = json.loads(turn)
        for name in ("text", "prior", "revision"):
            examples |= ngrams(fields.get(name) or "")
    shared = [
        row["id"]
        for row in corpus_rows()
        if any(not examples.isdisjoint(ngrams(text)) for text in [row["said"], row.get("prior", ""), *row["expected"]])
    ]
    assert examples
    assert shared == []


def ngrams(text: str) -> set[tuple[str, ...]]:
    found = words(text)
    grams = {tuple(found[i : i + 8]) for i in range(len(found) - 7)}
    return grams


def test_the_guardrail_accepts_995_permille_of_the_v2_gold_answers() -> None:
    # Gate 11: a refused gold row is listed; the next step fixes the gold or the guardrail.
    rows = [row for found in v2_files().values() for row in found]
    refused = [row["id"] for row in rows if gen.gold_refused(row)]
    assert len(refused) * 1000 <= 5 * len(rows), refused


# Targets per v2 area and language (founder, 11 Oct): each file holds at least 90 % of its target.
V2_TARGETS = {
    "v2-stt-homophones": (550, 350, 350),
    "v2-stt-nearsound": (300, 250, 250),
    "v2-grammar": (750, 600, 600),
    "v2-punctuation": (600, 500, 450),
    "v2-lists": (350, 300, 250),
    "v2-numbers": (350, 250, 250),
    "v2-addresses": (250, 150, 150),
    "v2-backtracking": (500, 400, 350),
    "v2-dictionary": (500, 350, 300),
    "v2-traps": (400, 350, 300),
    "v2-long": (250, 200, 150),
}


def test_the_v2_corpus_meets_its_targets_per_area_and_language() -> None:
    files = v2_files()
    short = []
    for area, targets in V2_TARGETS.items():
        for prefix, target in zip(("", "hinglish-", "hi-"), targets, strict=True):
            count = len(files.get(f"{prefix}{area}", []))
            if count * 10 < target * 9:
                short.append(f"{prefix}{area} {count}/{target}")
    assert short == []


FOUNDER_REPORTS = ("001", "002", "003", "004", "005")


def test_each_founder_report_has_its_exact_case_and_generic_variants() -> None:
    founder = [row for row in corpus_rows() if row["id"].startswith("founder-") and "split" not in row]
    variants = v2_files()["founder-variants"]
    for report in FOUNDER_REPORTS:
        assert any(f"report {report}" in row["note"] for row in founder), report
        found = [row for row in variants if f"report {report}" in row["note"]]
        assert 6 <= len(found) <= 10, report
        assert any(row["kind"].startswith("trap") for row in found), report
        assert len({row["language"] for row in found}) >= 2, report


def test_a_suite_growth_build_merges_into_one_file_for_every_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Generated rows from the inbox (gen-r*.jsonl) grow one mixed-language file, like founder-variants.
    monkeypatch.setattr(gen, "CORPUS", tmp_path)
    build = tmp_path / "suite-growth-r1"
    build.mkdir()
    rows = [
        {
            "id": "suite-growth-en-00001",
            "kind": "homophone",
            "said": "repeat the taste on monday",
            "dictionary": [],
            "expected": ["Repeat the test on Monday."],
            "language": "en",
            "length": "sentence",
            "note": "hand",
        },
        {
            "id": "suite-growth-hi-00001",
            "kind": "agreement",
            "said": "बारिश रुक गया तो चलेंगे",
            "dictionary": [],
            "expected": ["बारिश रुक गई तो चलेंगे।"],
            "language": "hi",
            "length": "sentence",
            "note": "hand",
        },
    ]
    (build / "rows.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    merged = gen.merge([build])
    assert {name: [row["id"] for row in found] for name, found in merged.items()} == {
        "suite-growth": ["suite-growth-en-00001", "suite-growth-hi-00001"]
    }


def test_formatting_only_lets_the_and_of_go_only_with_a_number() -> None:
    assert gen.formatting_only("on the twenty second of may", "On 22 May.", [])
    assert not gen.formatting_only("before sending invoice", "Before sending the invoice.", [])


# Round 3 (11 Oct): marks a careful editor may write either way score the same, a dropped paragraph break is a miss,
# rows whose kind names Hinglish are scored for marks like any other, and the report flags gold the guardrail
# refuses.
@pytest.mark.parametrize(
    ("output", "expected", "said"),
    [
        ("We need milk, eggs, and bread.", "We need milk, eggs and bread.", "we need milk eggs and bread"),
        ("On Monday I met Ravi.", "On Monday, I met Ravi.", "on monday i met ravi"),
        ("After dinner, we shared stories.", "After dinner we shared stories.", "after dinner we shared stories"),
        ("It ran clear. Then I left.", "It ran clear; then I left.", "it ran clear then i left"),
        ("The meeting is at 9 a.m.", "The meeting is at 9 am.", "the meeting is at nine am"),
        # Round b1: a comma before a clause of reason or purpose, and after an opening "so" or "okay".
        (
            "Bas ye likh do, taaki main bhej sakun.",
            "Bas ye likh do taaki main bhej sakun.",
            "bas ye likh do taaki main bhej sakun",
        ),
        ("We left early because it rained.", "We left early, because it rained.", "we left early because it rained"),
        ("रिपोर्ट भेजो क्योंकि कल मीटिंग है।", "रिपोर्ट भेजो, क्योंकि कल मीटिंग है।", "रिपोर्ट भेजो क्योंकि कल मीटिंग है"),
        ("So, just go ahead.", "So just go ahead.", "so just go ahead"),
        ("Okay let's start.", "Okay, let's start.", "okay let's start"),
    ],
)
def test_an_optional_mark_scores_the_same_either_way(output: str, expected: str, said: str) -> None:
    scored = bench.score(output, [expected], said)
    assert scored.words_right
    assert scored.punctuation_right == scored.punctuation_needed
    assert scored.case_right == scored.case_needed
    assert bench.exact(output, [expected])


def test_a_comma_that_is_needed_still_counts() -> None:
    assert bench.score("Hey Sam ship it.", ["Hey Sam, ship it."], "hey sam ship it").punctuation_right == 1


def test_a_dropped_paragraph_break_fails_the_paragraphs_gate() -> None:
    said = "first line new paragraph second line"
    kept = v2item(said, "First line.\n\nSecond line.", slice_="v2-punctuation")
    dropped = v2item(said, "First line.\n\nSecond line.", slice_="v2-punctuation")
    assert bench.gates([result(kept, "First line.\n\nSecond line.")])["paragraphs kept"] == 100
    assert bench.gates([result(dropped, "First line. Second line.")])["paragraphs kept"] == 0
    assert "paragraphs kept" in bench.AREAS["Formatting"]


def test_a_row_whose_kind_names_hinglish_counts_for_the_punctuation_gate() -> None:
    entry = item("formatting", "spoken bullet list (Hinglish)", "kal milte hain", "Kal milte hain.")
    assert bench.gates([result(entry, "kal milte hain")])["punctuation"] == 0


def test_the_report_flags_gold_the_guardrail_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    right = v2item("ship it today", "Ship it today.")
    refused = v2item("ship it today", "Okay, ship it today.")
    bench.report("fake", [result(right, "Ship it today."), result(refused, "Ship it today.")])
    assert "guardrail refuses 1 gold answer" in capsys.readouterr().out


def test_a_gold_that_puts_right_a_name_the_prior_ends_with_is_judged_as_a_revision() -> None:
    # Round 4: "Jose" in the prior and "Joseph" in the gold share letters, not a word; the gold revises the prior.
    prior, text = "Brake pads ka estimate Jose", "balki Joseph ko bhejna before nine am"
    gold = "Brake pads ka estimate Joseph ko bhejna before 9 am."
    item = bench.Item("hinglish-v2-backtracking-chunked", "correction", text, prior, [], "hinglish", "other", [gold])
    row = {"said": text, "prior": prior, "dictionary": [], "expected": [gold]}
    assert (bench.gold_refused(item), gen.gold_refused(row)) == (False, False)


# Round b1: a piece that goes on with a list item starts on its own line, as the app joins it (dictation.rs join).
@pytest.mark.parametrize(
    ("prior", "text", "joined"),
    [
        (
            "Aaj ke kaam:\n1. Bank jaana\n2. Bill bharna",
            "3. Gas book karna",
            "Aaj ke kaam:\n1. Bank jaana\n2. Bill bharna\n3. Gas book karna",
        ),
        ("For the trip:\n- Towels", "- A charger", "For the trip:\n- Towels\n- A charger"),
        ("We met at 3.", "Then we left.", "We met at 3. Then we left."),
        ("The total is", "10.5 dollars.", "The total is 10.5 dollars."),
    ],
)
def test_a_piece_starting_a_list_item_goes_on_a_new_line(prior: str, text: str, joined: str) -> None:
    assert bench.written(prior, None, text) == joined


# Round b2 fix (11 Oct): spellings a careful editor writes either way score the same: a hyphen between Devanagari
# words or inside a Latin compound ("sign-up" signup), chandrabindu or anusvara, a nukta, a Latin accent ("entrée"),
# a title's full stop ("Ms."), a full stop or a comma before "but", "lekin" and the like, and a comma before a
# sequence's "phir", after a reporting verb, between an "agar" clause and its "to", and after a longer opening phrase.
@pytest.mark.parametrize(
    ("output", "expected", "said"),
    [
        ("दो-तीन लोग आए।", "दो तीन लोग आए।", "दो तीन लोग आए"),
        ("एक छोटा-सा कमरा है।", "एक छोटा सा कमरा है।", "एक छोटा सा कमरा है"),
        ("फ्लाइट 2:30 बजे पहुँचेगी।", "फ्लाइट 2:30 बजे पहुंचेगी।", "फ्लाइट ढाई बजे पहुंचेगी"),
        ("मुझे ज़्यादा काम है।", "मुझे ज्यादा काम है।", "मुझे ज्यादा काम है"),
        (
            "Support will watch the sign-up flow.",
            "Support will watch the signup flow.",
            "support will watch the signup flow",
        ),
        ("Order the chicken entrée.", "Order the chicken entree.", "order the chicken entree"),
        ("Ms. Chen will send it.", "Ms Chen will send it.", "ms chen will send it"),
        ("Main bhej dungi. Lekin kal.", "Main bhej dungi, lekin kal.", "main bhej dungi lekin kal"),
        ("I called him. But he left.", "I called him, but he left.", "i called him but he left"),
        ("नारियल डाला, फिर मिर्च डाली।", "नारियल डाला फिर मिर्च डाली।", "नारियल डाला फिर मिर्च डाली"),
        ("Usne bola, kal aana.", "Usne bola kal aana.", "usne bola kal aana"),
        ("उसने कहा, कल आना।", "उसने कहा कल आना।", "उसने कहा कल आना"),
        ("Agar issue aaya, to ping karna.", "Agar issue aaya to ping karna.", "agar issue aaya to ping karna"),
        (
            "During the code review, I fixed it.",
            "During the code review I fixed it.",
            "during the code review i fixed it",
        ),
    ],
)
def test_a_spelling_or_mark_written_either_way_scores_the_same(output: str, expected: str, said: str) -> None:
    scored = bench.score(output, [expected], said)
    assert scored.words_right
    assert scored.punctuation_right == scored.punctuation_needed
    assert scored.case_right == scored.case_needed
    assert bench.exact(output, [expected])


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Checklist ready hai.", "Check list ready hai."),
        ("Send the bill.", "Send the bill, and call."),
        ("मैं कल आऊंगा।", "मै कल आऊंगा।"),
    ],
)
def test_a_real_word_change_still_fails(output: str, expected: str) -> None:
    assert not bench.score(output, [expected]).words_right


def test_a_term_inside_an_address_counts_in_either_case() -> None:
    entry = v2item(
        "resume careers at tech nova dot in pe bhejo",
        "Resume careers@TechNova.in pe bhejo.",
        slice_="suite-growth",
        language="hinglish",
        terms=("TechNova",),
    )
    scored = bench.gates([result(entry, "Resume careers@technova.in pe bhejo.")])
    assert (scored["dictionary strict"], scored["spoken forms to terms"]) == (100, 100)
    assert bench.term_count("Write to TechNova or technova.in", "TechNova") == 2
    assert bench.term_count("Write to technova today", "TechNova") == 0


def test_a_miss_names_the_answers_the_guardrail_refused() -> None:
    # Round b2 fix: graders could see only a refusal's verdict, never the text it refused.
    entry = v2item("ship it today", "Ship it today.")
    refused = Call("test:big", Usage(10, 5), "unheard", '{"revision": null, "text": "Ship it today, team."}')
    missed = bench.misses([result(entry, "ship it today", calls=[refused])])
    assert missed[0]["refused"] == [{"verdict": "unheard", "answer": "Ship it today, team.", "revision": None}]


def test_a_miss_names_the_revision_a_refused_answer_carried() -> None:
    # Round b3 fix: a refused correction may move its words into the revision, which graders could not see.
    entry = v2item("no wait friday", "Meet on Friday.", slice_="v2-backtracking")
    output = '{"revision": "Meet on Friday.", "text": ""}'
    refused = Call("test:big", Usage(10, 5), "dropped_word", output)
    missed = bench.misses([result(entry, "no wait friday", calls=[refused])])
    assert missed[0]["refused"] == [{"verdict": "dropped_word", "answer": "", "revision": "Meet on Friday."}]


def test_a_refused_answer_with_the_right_words_but_a_wrong_mark_is_no_false_reject() -> None:
    # Round b3 fix: an answer refused for ending a direct question with a full stop was counted a false reject.
    entry = item("formatting", "question", "can you send it", "Can you send it?")
    output = json.dumps({"revision": None, "text": "Can you send it."})
    rejected = result(entry, entry.text, calls=[Call("test:big", Usage(1, 1), "question", output)])
    assert not bench.falsely_rejected(rejected)


def test_dictionary_terms_kept_reads_a_term_in_an_address_in_either_case() -> None:
    # Round b3 fix: the gate and a miss's missing terms used a plain substring test, unlike the strict gate.
    entry = v2item(
        "send the claim to support at blue dart dot com",
        ["Send the claim to support@bluedart.com.", "Send the claim to support@BlueDart.com."],
        slice_="suite-growth",
        terms=("BlueDart",),
    )
    outcome = result(entry, "Send the claim to support@bluedart.com.")
    assert bench.gates([outcome])["dictionary terms kept"] == 100
    assert bench.misses([outcome]) == []


def test_an_all_lowercase_term_counts_with_a_capital_at_a_sentence_start() -> None:
    # Rule 9: an all-lowercase term takes a capital at a sentence start, so the gold's "Anita" is the term "anita".
    assert bench.term_count("Anita ne kaha. Phir anita gayi.", "anita") == 2
    assert bench.term_count("Call Anita today.", "anita") == 0
    entry = v2item("anita ne kaha", "Anita ne kaha.", slice_="v2-grammar", language="hinglish", terms=("anita",))
    scored = bench.gates([result(entry, "anita ne kaha.")])
    assert scored["term twins kept"] is None
    assert scored["dictionary strict"] == 100


def test_an_ellipsis_or_a_dash_is_a_mark_not_part_of_a_word() -> None:
    assert bench.words_of("I'm not sure\u2026 let me check") == ["i'm", "not", "sure", "let", "me", "check"]
    assert bench.words_of("a replacement\u2014actually, an inspection\u2014because") == [
        "a",
        "replacement",
        "actually",
        "an",
        "inspection",
        "because",
    ]


def test_a_semicolon_before_then_reads_as_a_comma_before_then() -> None:
    expected = ["I rinse the rice until the water runs clear; then I leave it in the bowl."]
    output = "I rinse the rice until the water runs clear, then I leave it in the bowl."
    assert bench.exact(output, expected)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("It works, but the fan is loud.", "It works but the fan is loud."),
        ("Kaam ho gaya, par bill nahi aaya.", "Kaam ho gaya par bill nahi aaya."),
        ("काम हो गया, लेकिन बिल नहीं आया।", "काम हो गया लेकिन बिल नहीं आया।"),
    ],
)
def test_a_comma_before_a_contrast_word_is_optional(output: str, expected: str) -> None:
    assert bench.exact(output, [expected])
    assert bench.exact(expected, [output])


def test_a_revision_keeps_the_break_the_prior_had_after_it_as_the_app_joins_it() -> None:
    # Round b3 fix: the app keeps a spoken break after the part a revision kept (assemble in dictation.rs); the
    # bench joined them with a space, so a correction after an email's greeting lost its paragraph.
    prior = "Hi Ms. Rao,\n\nWe can send the plan by the 12th."
    joined = bench.written(prior, "Hi Ms. Rao,", "We can send the plan by the 14th.")
    assert joined == "Hi Ms. Rao,\n\nWe can send the plan by the 14th."
    assert bench.written("Send it.", "Send it now.", "Thanks.") == "Send it now. Thanks."


def test_a_roman_hindi_word_scores_the_same_in_any_of_its_spellings() -> None:
    # Round b3 fix: Roman Hindi has no one spelling ("me" "mein", "yaha" "yahan" "yahaan"), as the guardrail knows.
    expected = ["Yaha se mall paas hai. Tum car parking me chhod do."]
    output = "Yahan se mall paas hai. Tum car parking mein chhod do."
    assert bench.score(output, expected).words_right
    entry = v2item(
        "yeah se mall paas hai tum car parking may chhod do",
        expected,
        slice_="suite-growth",
        language="hinglish",
        fixes=(("yeah", "yaha"), ("may", "me")),
    )
    assert bench.misses([result(entry, output)]) == []
    assert not bench.score("Main kal aaunga.", ["Main kal aaungi."]).words_right


def test_an_answer_the_gold_accepts_has_every_fix_the_gold_accepts() -> None:
    # Round b3 fix: founder-022 accepts "rules engine" but its fix names only "rule engine".
    entry = v2item(
        "a simple ruling engine",
        ["A simple rule engine.", "A simple rules engine."],
        slice_="founder",
        fixes=(("ruling engine", "rule engine"),),
    )
    outcome = result(entry, "A simple rules engine.")
    assert bench.misses([outcome]) == []
    assert bench.gates([outcome])["misheard words fixed"] == 100


def test_a_sentence_opened_with_and_scores_like_a_comma_before_and() -> None:
    # founder-022, round b4: ". And" is read as ", and", but the comma that made was never read as optional, so the
    # gold's own ", and" scored a miss.
    scored = bench.score("I went home. And she stayed.", ["I went home, and she stayed."], "i went home and she stayed")
    assert scored.punctuation_right == scored.punctuation_needed


def test_a_fix_is_made_when_the_words_match_an_answer_holding_another_form_of_it() -> None:
    # founder-022, round b4: "rules engine" is in the gold, but the fix's one spelling "rule engine" counted it missed.
    gold = ["It is a simple rule engine, and it works.", "It is a simple rules engine, and it works."]
    said = "it is a simple ruling engine and it works"
    entry = bench.Item("founder", "fix", said, "", [], "en", "other", gold, fixes=(("ruling engine", "rule engine"),))
    answer = result(entry, "It is a simple rules engine. And it works!")
    assert bench.gates([answer])["misheard words fixed"] == 100.0
    assert bench.misses([answer])[0]["fixes_missed"] == []


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # Round b5: a comma after a Hindi or Hinglish reporting verb is optional, as after "said".
        ("Sir bol rahe the, kal portal khula rehta hai.", "Sir bol rahe the kal portal khula rehta hai."),
        ("Mummy pooch rahi thi, kal meeting hai kya?", "Mummy pooch rahi thi kal meeting hai kya?"),
        ("पापा कहते हैं, मेहनत करो तो सब मिलता है।", "पापा कहते हैं मेहनत करो तो सब मिलता है।"),
        ("Kabir ne note kiya, backup plan ready tha.", "Kabir ne note kiya backup plan ready tha."),
        # A comma before the "to" or "तो" that opens a result clause.
        ("Rent bhej diya, to late fee nahi lagegi.", "Rent bhej diya to late fee nahi lagegi."),
        ("कैंडिडेट्स कल आएंगे, तो प्रोसेस फिर चलेगा।", "कैंडिडेट्स कल आएंगे तो प्रोसेस फिर चलेगा।"),
        # A comma after an opening "bas" or "बस".
        ("बस, मेल शांत टोन में भेजना।", "बस मेल शांत टोन में भेजना।"),
    ],
)
def test_round_b5_optional_hindi_and_hinglish_commas_score_either_way(output: str, expected: str) -> None:
    assert bench.exact(output, [expected])
    assert bench.exact(expected, [output])


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # Round b5: an email's greeting on a line of its own (rule 2) or run on into the first sentence are one form.
        ("Hi Farah,\n\nHere is the plan.\n\nWe start Monday.", "Hi Farah, here is the plan.\n\nWe start Monday."),
        ("Hi Maria,\n\nThe brief is attached.", "Hi Maria,\nThe brief is attached."),
        ("Hi team,\n\nQuick notes.\n\nThanks.", "Hi team, quick notes.\n\nThanks."),
    ],
)
def test_round_b5_an_emails_greeting_scores_on_its_own_line_or_inline(output: str, expected: str) -> None:
    assert bench.exact(output, [expected])
    assert bench.exact(expected, [output])


def test_round_b5_a_greeting_comma_never_joins_a_sentence_that_is_no_greeting() -> None:
    assert not bench.exact("Hi, Maya.\n\nI mean, it works.", ["Hi Maya, I mean, it works.\n\nThanks."])
    # A short note's vocative is no letter's greeting.
    assert bench.score("Hey Sam, ship it.", ["Hey Sam, ship it."], "hey sam ship it") == bench.Score(True, 2, 2, 2, 2)


def test_round_b5_paragraphs_kept_reads_a_greeting_line_as_either_form() -> None:
    entry = item(
        "v2-numbers",
        "letter",
        "hi sam here is the plan new paragraph we start monday",
        "Hi Sam, here is the plan.\n\nWe start Monday.",
    )
    result = bench.Result(entry, "Hi Sam,\n\nHere is the plan.\n\nWe start Monday.", False, "small", 1, [], False)
    assert bench.gates([result])["paragraphs kept"] == 100.0


def test_round_b5_a_word_written_more_often_than_said_or_expected_is_invented() -> None:
    # v2-numbers-hi-00069: one "क्या" became two; a word set missed it.
    entry = item("hi-v2-numbers", "question", "ड्राइवर पहुंचेगा क्या उसे एडवांस मिला", "ड्राइवर पहुंचेगा। क्या उसे एडवांस मिला?")
    assert bench.invented(entry, "ड्राइवर पहुंचेगा क्या? क्या उसे एडवांस मिला?") == ["क्या"]
    assert bench.invented(entry, "ड्राइवर पहुंचेगा। क्या उसे एडवांस मिला?") == []


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # Round b6: a comma after the greeting word, and a title's full stop in the name, are the same greeting.
        ("Hey, Leo,\n\nThe landlord replied.", "Hey Leo, the landlord replied."),
        ("Hi Ms. Fernandez,\n\nThe form is ready.", "Hi Ms Fernandez, the form is ready."),
        # A sign-off set off by a blank line or by one line break is one layout, with or without its full stop.
        ("Thanks again.\n\nBest,\nDaniel", "Thanks again.\nBest,\nDaniel"),
        ("Returns are free.\n\nThanks", "Returns are free.\nThanks."),
        # A comma after a sentence's opening adverb or greeting word is optional.
        ("Also, the code is ready.", "Also the code is ready."),
        ("Finally, we ship.", "Finally we ship."),
        ("Hello, Amit, can you come?", "Hello Amit, can you come?"),
    ],
)
def test_round_b6_optional_layout_and_commas_score_either_way(output: str, expected: str) -> None:
    assert bench.exact(output, [expected])
    assert bench.exact(expected, [output])


def test_round_b6_a_blank_line_before_a_sign_off_keeps_paragraphs() -> None:
    entry = item(
        "v2-backtracking",
        "letter",
        "hi sam new paragraph thanks again best daniel",
        "Hi Sam,\n\nThanks again.\nBest,\nDaniel",
    )
    result = bench.Result(entry, "Hi Sam,\n\nThanks again.\n\nBest,\nDaniel", False, "big", 1, [], False)
    assert bench.gates([result])["paragraphs kept"] == 100.0


def test_round_b6_quotes_around_a_few_words_are_optional_when_the_answer_has_none() -> None:
    said = "the dialog says scratch that request"
    assert bench.exact('The dialog says "scratch that request".', ["The dialog says scratch that request."])
    assert bench.score('The dialog says "Scratch that request".', ["The dialog says scratch that request."], said) == (
        bench.score("The dialog says scratch that request.", ["The dialog says scratch that request."], said)
    )
    # Quotes the answer holds, as for a spoken "open quote", are still needed.
    assert not bench.exact("He said hello.", ['He said "hello."'])


def test_a_rows_answer_is_written_for_its_app_style(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A row keeps the plain answer the guardrail judges; the bench expects it as the app's style writes it.
    plain = "Trip:\n- Towels\n- A charger"
    rows = [
        {"kind": "app style list", "said": "trip bullet towels bullet a charger", "expected": [plain], "app_style": s}
        for s in ("chat", "code", "email")
    ]
    with gzip.open(tmp_path / "platform.jsonl.gz", "wt") as out:
        out.writelines(json.dumps(row) + "\n" for row in rows)
    monkeypatch.setattr(bench, "CORPUS", tmp_path)
    assert [item.expected for item in bench.load()] == [
        ["Trip:\n• Towels\n• A charger"],
        ["Trip: Towels. A charger."],
        [plain],
    ]


def test_the_platform_slice_has_a_list_for_every_app_style_in_every_language_and_both_kinds_per_style() -> None:
    with gzip.open(bench.CORPUS / "platform.jsonl.gz", "rt", encoding="utf-8") as lines:
        rows = [json.loads(line) for line in lines]
    for style in ("chat", "email", "code", "docs"):
        mine = [row for row in rows if row["app_style"] == style]
        assert {row["language"] for row in mine} == {"en", "hinglish", "hi"}, style
        assert any(re.search(r"^1\. ", row["expected"][0], re.MULTILINE) for row in mine), style
        assert any(re.search(r"^- ", row["expected"][0], re.MULTILINE) for row in mine), style


@pytest.mark.parametrize(
    ("output", "expected", "language"),
    [
        # Founder, 12 Oct: between two Hindi or Hinglish clauses a comma, a full stop or "।" is the writer's choice.
        ("Cache नहीं दिख रहा है, मतलब tokens active रहेंगे।", "Cache नहीं दिख रहा है। मतलब tokens active रहेंगे।", "hi"),
        ("Wo signatory h. Uska board ready h.", "Wo signatory h, uska board ready h.", "hinglish"),
        ("आज के काम के बारे में, मुझे लगता है", "आज के काम के बारे में। मुझे लगता है", "hi"),
        # A comma after an opening word, after "lagta hai", and a colon or comma after "ek kaam karo".
        ("Sorry, I missed the call.", "Sorry I missed the call.", "en"),
        ("सॉरी, कल नहीं आ पाया।", "सॉरी कल नहीं आ पाया।", "hi"),
        ("मुझे लगता है, यह ठीक है।", "मुझे लगता है यह ठीक है।", "hi"),
        ("Mujhe lagta hai, ye sahi hai.", "Mujhe lagta hai ye sahi hai.", "hinglish"),
        ("Ek kaam karo: report likh do.", "Ek kaam karo, report likh do.", "hinglish"),
        # A full stop before a result clause's "to" or "तो", as a comma there is (round b5).
        (
            "Report likhni hai sales ke liye, to usme kya hoga?",
            "Report likhni hai sales ke liye. To usme kya hoga?",
            "hinglish",
        ),
        ("Do one thing: send the deck.", "Do one thing, send the deck.", "en"),
        # A comma after an English reporting verb, as after "bola" or "कहा" (round b5).
        ("Nurse Evans said, do not skip the dose.", "Nurse Evans said do not skip the dose.", "en"),
    ],
)
def test_marks_a_careful_editor_writes_either_way_are_read_one_way(output: str, expected: str, language: str) -> None:
    assert bench.exact(output, [expected], language)


@pytest.mark.parametrize(
    ("output", "expected", "language"),
    [
        # English sentences stay judged where they end.
        ("I called Sam. He said yes.", "I called Sam, he said yes.", "en"),
        # A question mark is never a comma or a full stop.
        ("क्या हुआ? ठीक है।", "क्या हुआ। ठीक है।", "hi"),
        # Words still count.
        ("Wo signatory h. Uska board ready h.", "Wo signatory tha, uska board ready h.", "hinglish"),
    ],
)
def test_marks_that_change_the_text_still_count(output: str, expected: str, language: str) -> None:
    assert not bench.exact(output, [expected], language)


def test_the_language_slice_keeps_the_inputs_language_whatever_the_tag_says() -> None:
    # Founder, 13 Oct: English stays English, Hindi stays Hindi, Hinglish keeps its own mix; the text decides.
    with gzip.open(bench.CORPUS / "language-kept.jsonl.gz", "rt", encoding="utf-8") as lines:
        rows = [json.loads(line) for line in lines]
    devanagari = re.compile(r"[ऀ-ॿ]")
    tagged = {(bool(devanagari.search(row["said"])), row["language"]) for row in rows}
    assert {(False, "hi"), (False, "hinglish"), (True, "en"), (True, "hi"), (False, "en")} <= tagged
    for row in rows:
        said_script = bool(devanagari.search(row["said"]))
        assert all(bool(devanagari.search(out)) == said_script for out in row["expected"]), row["id"]


def test_the_script_gate_judges_by_the_script_the_speaker_used_not_the_tag() -> None:
    # Founder, 13 Oct: the text decides the language; a Roman dictation the app tagged Hindi stays Roman.
    roman = bench.Item(
        "language-kept",
        "Roman mix kept under the hi tag",
        "bhai aaj ka plan kya hai",
        "",
        [],
        "hi",
        "other",
        ["Bhai, aaj ka plan kya hai?"],
    )
    devanagari = bench.Item(
        "language-kept", "Devanagari kept under the en tag", "कल मीटिंग है", "", [], "en", "other", ["कल मीटिंग है।"]
    )
    assert bench.wrong_script(roman, "Bhai, aaj ka plan kya hai?") == (6, 0)
    assert bench.wrong_script(roman, "भाई, आज का प्लान क्या है?") == (6, 6)
    assert bench.wrong_script(devanagari, "कल मीटिंग है।") == (3, 0)
    assert bench.wrong_script(devanagari, "Kal meeting hai.") == (3, 3)
