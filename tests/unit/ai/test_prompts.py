import json
import re

import pytest

from snapbak_backend.ai.guardrail.judge import Reason, Reject, check, sounds_alike
from snapbak_backend.ai.guardrail.tokens import words
from snapbak_backend.ai.prompts.command import COMMAND_SYSTEM
from snapbak_backend.ai.prompts.examples import EXAMPLES
from snapbak_backend.ai.prompts.polish import SYSTEM, correction
from snapbak_backend.schemas.command import CommandRequest
from snapbak_backend.schemas.polish import PolishRequest
from snapbak_backend.transformers.command import command_turns
from snapbak_backend.transformers.polish import polish_turns


def request(**changes: object) -> PolishRequest:
    fields = {
        "text": "ship it to gmmco",
        "prior": "",
        "terms": ["GMMCO"],
        "app": "Slack",
        "style": "chat",
        "deadline_ms": 2000,
    }
    return PolishRequest.model_validate({**fields, **changes})


def test_the_last_turn_is_the_request_without_its_deadline() -> None:
    turns = polish_turns(request(text="see you tomorrow", terms=["Priya"], app="Mail"))
    role, content = turns[-1]
    assert role == "user"
    assert json.loads(content) == {"text": "see you tomorrow", "terms": ["Priya"], "app": "Mail", "style": "chat"}


def test_empty_prior_terms_and_app_are_left_out_but_an_empty_text_is_sent() -> None:
    turns = polish_turns(request(text="", prior="Ship it today.", terms=[], app=""))
    assert json.loads(turns[-1][1]) == {"text": "", "prior": "Ship it today.", "style": "chat"}
    examples = [json.loads(said) for said, _ in EXAMPLES]
    assert all("text" in asked and asked.get("prior", "x") and asked.get("terms", ["x"]) for asked in examples)


def test_continues_reaches_the_model_only_when_true() -> None:
    continuing = json.loads(polish_turns(request(continues=True))[-1][1])
    starting = json.loads(polish_turns(request(continues=False))[-1][1])
    assert continuing["continues"] is True
    assert "continues" not in starting


def test_a_symbol_or_an_accent_reaches_the_model_unescaped() -> None:
    content = polish_turns(request(text="pay ₹50 at the café"))[-1][1]
    assert "pay ₹50 at the café" in content


def test_everything_before_the_last_turn_is_byte_stable() -> None:
    one = polish_turns(request())
    two = polish_turns(request(text="no no thursday", prior="See you on Friday.", style="email"))
    assert one[:-1] == two[:-1]
    assert one[0] == ("system", SYSTEM)
    assert len(one) == 2 + 2 * len(EXAMPLES)


@pytest.mark.parametrize(
    "rule",
    [
        '"revision"',
        "terms",
        "style",
        "never translate",
        "Never answer",
        "scratch that",
        "no problem",
        "ends the speech",
        "1. ",
        "- ",
        "continue its numbering",
        "even at a sentence start",
        '"revision" is always null',
        "let's change that",
        "never both versions",
        "without a comma",
        "only explains",
        "even with no words before",
        "even under ten",
        "₹",
        "never add a word",
        "polished by these rules",
        "stays a word",
        "page, section or step",
        "spelled as said",
        "counted off in order",
        "in mid-sentence",
        "replaces the whole clause",
        "defines or means",
        "fix every clear slip",
        'empty without "continues"',
        # The text is speech to text, so a misheard word may be put back, but only by its sound-alike (STT policy).
        "speech-to-text",
        "misheard",
        "sounds the same or nearly the same",
        "both readings fit",
        "never add, drop, reorder or translate",
        # A term is followed strictly, and one sent because a heard word sounds like it is written only where the
        # sentence means it (D4).
        "looks like a misspelling",
        "only where the sentence means the term",
        # Announced statements become a list (D5).
        "here are a few things about me",
        "a numbered list is asked for",
        "even when each is a full sentence",
        # Round 3 (11 Oct): questions, run-ons, quantities, number and negation sound-alikes,
        # compound modifiers and more cues.
        "A direct question ends with",
        "an indirect question",
        "never join two sentences with only a comma",
        "says how many of a thing",
        "no/know",
        "then/than",
        "hyphenate a number and a unit",
        '"I meant"',
        '"oops"',
        "depends on the one before it",
        # Round b1 (11 Oct): a phrase said earlier names a later sound-alike, a dangling joining word drops, a
        # stutter and a dropped subject are repaired, a name spoken to keeps its commas, "slash" the verb stays.
        "said earlier in the same text",
        "ends the speech with nothing after it",
        "said twice in a row",
        "missing only its subject",
        "auxiliary in the wrong place",
        "the speaker talks to",
        "never the verb",
        "replaces the currency word",
        "ten past three",
        "as an apology",
        "start at the end of",
        "names the thing",
        'only where "new paragraph"',
        # 14 Oct: a lone subject the speech ends on drops, and a noun naming the items announces a list.
        "said alone after the last finished sentence",
        "a plural noun that names the items",
    ],
)
def test_the_rules_cover_each_feature(rule: str) -> None:
    assert rule in SYSTEM


def test_every_example_is_a_valid_request_and_a_json_answer() -> None:
    for said, cleaned in EXAMPLES:
        PolishRequest.model_validate({"prior": "", "terms": [], "app": "", **json.loads(said), "deadline_ms": 2000})
        answer = json.loads(cleaned)
        assert set(answer) == {"revision", "text"}


def test_every_example_answer_passes_the_guardrail() -> None:
    for said, cleaned in EXAMPLES:
        asked = json.loads(said)
        answer = json.loads(cleaned)
        revised = answer["revision"] is not None
        heard = f"{asked['prior']} {asked['text']}" if revised else asked["text"]
        wrote = f"{answer['revision']} {answer['text']}" if revised else answer["text"]
        assert check(heard, wrote, asked.get("terms", [])) is None, said


def test_no_example_leans_on_one_customer_term() -> None:
    assert "GMMCO" not in json.dumps(EXAMPLES)


def misheard_fixes(said: str, written: str) -> list[tuple[str, str]]:
    """The said words an answer replaced with a word that sounds like them."""
    heard, wrote = words(said), words(written)
    lost = [word for word in heard if word not in wrote]
    added = [word for word in wrote if word not in heard]
    return [(one, other) for one in lost for other in added if sounds_alike([one], [other])]


def test_the_examples_put_back_a_misheard_word_and_keep_a_right_one() -> None:
    fixed = []
    kept = []
    for said, cleaned in EXAMPLES:
        asked, answer = json.loads(said), json.loads(cleaned)
        fixed += misheard_fixes(asked["text"], answer["text"])
        right = [word for word in ("taste", "lift", "nimble") if word in words(asked["text"])]
        kept += [word for word in right if word in words(answer["text"])]
    assert fixed
    assert kept


def test_the_examples_keep_a_term_that_looks_like_a_misspelling_and_write_a_term_for_its_sound() -> None:
    answers = [(json.loads(said), json.loads(cleaned)["text"]) for said, cleaned in EXAMPLES]
    termed = [(asked, text) for asked, text in answers if asked.get("terms")]
    kept = [term for asked, text in termed for term in asked["terms"] if term in text and term in asked["text"]]
    sounded = [term for asked, text in termed for term in asked["terms"] if term in text and term not in asked["text"]]
    assert kept and sounded


def test_the_examples_turn_announced_statements_into_a_list() -> None:
    answers = [(json.loads(said)["text"], json.loads(cleaned)["text"]) for said, cleaned in EXAMPLES]
    listed = [text for said, text in answers if "\n- " in text and "bullet" not in said and " then " not in said]
    assert listed


def test_the_examples_number_a_counted_list_ending_in_and_three() -> None:
    answers = [(json.loads(said)["text"], json.loads(cleaned)["text"]) for said, cleaned in EXAMPLES]
    assert any(re.search(r"\band three\b", said, re.IGNORECASE) and "\n3. " in text for said, text in answers)


def test_the_examples_cover_a_revision_and_carry_no_language() -> None:
    revisions = [cleaned for _, cleaned in EXAMPLES if json.loads(cleaned)["revision"] is not None]
    assert revisions
    assert not any("language" in json.loads(said) for said, _ in EXAMPLES)


def test_the_examples_take_english_words_back_in_mid_sentence() -> None:
    answers = [(json.loads(said)["text"], json.loads(cleaned)["text"].lower()) for said, cleaned in EXAMPLES]
    cues = (" i mean ", " rather ")
    taken = [text for said, text in answers for cue in cues if cue in said and cue.strip() not in text]
    assert len(taken) == 2


def test_the_examples_split_a_run_on_and_keep_a_request_as_dictation() -> None:
    # Round 3: "then" with its own subject starts a sentence, and a request to the assistant comes back as dictation.
    answers = [(json.loads(said)["text"], json.loads(cleaned)["text"]) for said, cleaned in EXAMPLES]
    assert any(" then we " in said and ". Then we " in text for said, text in answers)
    assert any("tell me a joke" in said and text.startswith("Tell me a joke") for said, text in answers)


def test_the_retracted_example_writes_a_self_contained_sentence() -> None:
    answers = [json.loads(cleaned)["text"] for _, cleaned in EXAMPLES]
    assert "Send it to Tidewell." not in answers


def test_the_examples_retract_a_whole_prior_sentence_and_continue_a_list() -> None:
    answers = [(json.loads(said), json.loads(cleaned)) for said, cleaned in EXAMPLES]
    assert any(asked.get("prior") and answer["revision"] == "" for asked, answer in answers)
    continued = [answer["text"] for asked, answer in answers if asked.get("prior", "").endswith("1. The API")]
    assert continued[0].startswith("2. ")


def test_command_turns_use_their_own_rules_and_the_request_without_deadline() -> None:
    asked = CommandRequest(
        selection="hey can u send the deck",
        instruction="make this formal",
        app="Mail",
        deadline_ms=6000,
    )
    turns = command_turns(asked)
    assert turns[0] == ("system", COMMAND_SYSTEM)
    assert json.loads(turns[-1][1]) == {
        "selection": "hey can u send the deck",
        "instruction": "make this formal",
        "app": "Mail",
    }


@pytest.mark.parametrize(
    ("reason", "quoted"),
    [
        (Reason(Reject.UNHEARD, ("okay",)), "you wrote 'okay', which the speaker never said"),
        (Reason(Reject.UNHEARD, ("okay", "and")), "you wrote 'okay' and 'and', which the speaker never said"),
        (Reason(Reject.DROPPED_JOIN, ("and",)), "you dropped the joining word 'and'"),
        (Reason(Reject.TENSE, ("shipped",)), "you changed the tense of 'shipped'"),
        (Reason(Reject.NEGATION, ("can't",)), "you changed a negation: 'can't'"),
        (Reason(Reject.QUALIFIER, ("only",)), "you dropped or changed 'only'"),
        (Reason(Reject.TERM, ("Kubernetes",)), "you changed the dictionary term 'Kubernetes'"),
        (Reason(Reject.DROPPED_SENTENCE, ("call", "priya")), "you left out a sentence the speaker said: 'call priya'"),
        (Reason(Reject.TOO_MANY_CHANGES, ("deck",)), "you changed or left out too many of the speaker's words"),
        (Reason(Reject.REPLY, ("sure",)), "you answered the text instead of polishing it"),
        (Reason(Reject.MALFORMED), "your answer was not the JSON object asked for"),
        (Reason(Reject.KEPT_CORRECTION, ("friday", "i", "mean", "monday")), "you kept both versions"),
        (Reason(Reject.QUESTION, ("is", "the", "deck")), "you ended the direct question 'is', 'the' and 'deck' with"),
        (Reason(Reject.FALSE_LIST, ("I woke up",)), "you made a list the speaker did not count off"),
        (Reason(Reject.DROPPED_ITEM, ("2 of 3 items",)), "your list lost '2 of 3 items'"),
        (Reason(Reject.DROPPED_WORD, ("teacher",)), "you left out 'teacher'"),
        (Reason(Reject.REORDERED, ("lyon", "paris")), "you changed the order of 'lyon' and 'paris'"),
        (Reason(Reject.FORMAT, ("$",)), "you wrote '$' otherwise than the speaker said it"),
        # A grammar fix the time word or the subject asks for is allowed: the turn never pushes the model to undo it.
        (Reason(Reject.TENSE, ("went",)), "a verb may change only to agree with a time word"),
        # A lone "no" may be a cue: the turn never pushes the model back to "X, no, Y".
        (Reason(Reject.NEGATION, ("no",)), "if the speaker used it to take words back, keep only the corrected"),
    ],
)
def test_a_correction_quotes_the_rule_and_the_words(reason: Reason, quoted: str) -> None:
    message = correction(reason)
    assert quoted in message
    assert message.startswith("That answer was refused")
    assert "only" in message and "JSON" in message


# Round b1 (11 Oct): the examples show a later sound-alike put right by a phrase said earlier, a fix in a 3-word
# phrase, an apology and "actually" kept, a question put in order, a name spoken to set off with commas and a term's
# plain word kept.
def test_the_examples_cover_round_b1_fixes() -> None:
    answers = {json.loads(said)["text"]: json.loads(cleaned)["text"] for said, cleaned in EXAMPLES}
    assert answers["the fan is running fine now just check if the fan is ruining at night"].endswith(
        "running at night."
    )
    assert answers["steal frame bikes"] == "Steel frame bikes."
    assert answers["sorry i am late the cab actually came early but i missed it"].startswith("Sorry,")
    assert "where will the files be?" in answers["so where the files will be they are in the shared drive"]
    assert answers["okay ravi invoice sent"] == "Okay, Ravi, invoice sent."
    assert "zoom in" in answers["please zoom in on the chart before our zoom call"]


# Round b3 (11 Oct): the rules the graders found missing or too weak, each named once in the system prompt.
@pytest.mark.parametrize(
    "rule",
    [
        "Two end marks never stack",
        "hangs on a verb before it",
        "takes no comma",
        "keep every joining word said",
        "count across full stops",
        "The whole counting phrase goes",
        "asks and answers right away",
        'never leave "at" as a word',
        "Only a dictionary term itself is protected",
        "one to four words",
        "near-sound of a known compound",
        "fixed in place",
        "A time word sets the tense",
        "doubled intensifier",
        "smallest span",
        "quoted content",
        "with nothing restated after it",
        "matches a term except for capitals",
        "written the same way each time",
    ],
)
def test_the_rules_cover_round_b3_fixes(rule: str) -> None:
    assert rule in SYSTEM


def test_the_examples_cover_round_b3_fixes() -> None:
    answers = {json.loads(said)["text"]: json.loads(cleaned)["text"] for said, cleaned in EXAMPLES}
    assert answers["the oven is heating up now just checking if olive is heating up"].endswith(
        "the oven is heating up."
    )
    assert answers["the knew acme invoice is late"] == "The new Acme invoice is late."
    assert answers["plain fare"] == "Plane fare."
    assert answers["tomorrow we submitted the form"] == "Tomorrow we will submit the form."
    assert answers["the food was very very good"] == "The food was very very good."
    assert answers["give us a call when you land"] == "Give us a call when you land."
    assert (
        answers[
            "let's do one thing water the plants then we'll lock the shed. then we can leave for the station"
        ].count("\n- ")
        == 3
    )


def test_a_correction_says_it_is_no_dictation_and_asks_for_the_heard_word() -> None:
    # Round b3: twice the model polished the correction note itself as the dictation, and told only that a word was
    # never said, it guessed a second wrong spelling ("Buzurg", then "Budhurg").
    unheard = correction(Reason(Reject.UNHEARD, ("buzurg",)))
    assert "not dictation" in unheard
    assert "spelled as said" in unheard


def test_the_rules_and_examples_cover_round_b3_founder_fixes() -> None:
    # After a typed full stop "do one thing" opens no list (founder-006, founder-007), a repeated verb stays
    # (founder variants), words are never contracted (founder-022), a later capitalized near-sound repeats a
    # phrase said earlier (founder-023) and an announced statement keeps its "and" (founder-019).
    assert "what follows stays sentences" in SYSTEM
    assert "never contract or expand" in SYSTEM
    assert "said with its verb twice" in SYSTEM
    answers = {json.loads(said)["text"]: json.loads(cleaned)["text"] for said, cleaned in EXAMPLES}
    assert answers["the api is responding again now just a quick check to see if happy is rebounding"].endswith(
        "if the API is responding."
    )
    trip = answers["here are a few things about our trip we leave on friday we fly to goa and we stay near the beach"]
    assert trip.endswith("- We fly to Goa and we stay near the beach")


@pytest.mark.parametrize(
    "rule",
    [
        # Round b4 (dev): "pasture case" before eclairs became "pasta", "serial box" and "word bill" stayed.
        "fits every word around it",
        # suite-growth-en-05122: "next week we finish" gained "will".
        "a plan and stays",
        # Round b4: "brakes" for "break" was refused, and the retry kept "break".
        "keeps the number it was heard in",
        # v2-numbers-en-00142: a written "8" became a word.
        "A digit already written stays a digit",
        # founder-019: the recognizer's own full stops kept the announced statements as prose, in both runs.
        "even when the recognizer already ended each statement",
        # founder-020: "As follows:" was left as a fragment.
        "joins the sentence before it with a comma",
        # Round b4: a condition clause was moved to the front twice, and "then" was added.
        "never move a clause",
        # v2-backtracking-en-00268: "no pain during lunges" became "He had no pain".
        "verbless note",
        # suite-growth-en-05335: a question to an assistant lost its "?".
        "is still a question",
        # Round b4: a name that is also a word took a capital where it named no one.
        "only where it names the person",
    ],
)
def test_the_rules_cover_round_b4_fixes(rule: str) -> None:
    assert rule in SYSTEM


def test_the_examples_cover_round_b4_fixes() -> None:
    answers = {json.loads(said)["text"]: json.loads(cleaned)["text"] for said, cleaned in EXAMPLES}
    week = answers["Here are a few things about my week. I start a new course. I visit my parents on Sunday."]
    assert week == "Here are a few things about my week:\n- I start a new course\n- I visit my parents on Sunday"
    assert answers["next month we move to pune and then we unpack"] == "Next month we move to Pune, and then we unpack."
    assert answers["the baker wiped the pasture shelf and stacked the croissants"] == (
        "The baker wiped the pastry shelf and stacked the croissants."
    )
    assert answers["hole wheat bread"] == "Whole wheat bread."
    assert answers["knee is better no swelling just stiffness in the morning"] == (
        "Knee is better. No swelling, just stiffness in the morning."
    )


def test_a_correction_keeps_every_other_fix() -> None:
    # Round b4: refused for one word ("brakes", "bhejenge"), the retry undid every fix and shipped the slip.
    assert "keep every other fix" in correction(Reason(Reject.UNHEARD, ("brakes",)))


def test_a_correction_asks_for_the_announced_list() -> None:
    told = correction(Reason(Reject.UNLISTED, ("Here are a few things about my team",)))
    assert "bulleted list" in told


@pytest.mark.parametrize(
    "rule",
    [
        # founder-variants-en-00014: "what details will be it should have ..." lost "It should have" twice.
        "never cut to a fragment",
        # v2-stt-nearsound-en-00269: "before july rent is due" was cut after "July".
        "never cuts a noun phrase",
        # v2-stt-homophones-en-00062: "river sight" became "site" though both readings fit.
        "a lovely sight from the hill",
        # v2-backtracking-en-00096: a rewritten prior kept "four thirty pm".
        "is polished like the text",
        # Round b4: "slack dev team" + "no wait sreya ..." lost the verb "Slack".
        "the prior's verb",
    ],
)
def test_the_rules_cover_round_b4_second_pass(rule: str) -> None:
    flat = " ".join(SYSTEM.split())
    assert " ".join(rule.split()) in flat


def test_the_examples_cover_round_b4_second_pass() -> None:
    answers = {json.loads(said)["text"]: json.loads(cleaned) for said, cleaned in EXAMPLES}
    menu = answers["so what the menu will be it should have soup and rice"]["text"]
    assert menu == "So, what will the menu be? It should have soup and rice."
    baker = answers["sorry the baker that the cake is ready"]
    assert baker == {"revision": "Text the baker that the cake is ready.", "text": ""}


@pytest.mark.parametrize(
    "rule",
    [
        # v2-punctuation-en-00034: a four-word run-on came back one word per line.
        "never go on lines of their own",
        # Round b5: one spoken word ended a question and opened the next.
        "Each spoken word is written once",
        # v2-punctuation-en-00189: a bare vocative became a greeting line.
        "never made a greeting line",
        # Round b5: a typed full stop after an opening label became a comma or colon.
        "a full stop already typed after it stays",
        # v2-lists-en-00004: the last "then" step was left out of the list.
        "the last one too",
        # v2-numbers-en-00131, -00336, -00320: a corrected count, a count beside money, an ordinal went to digits.
        "stays a word also after a correction",
        "an ordinal up to tenth that is no date stays a word",
        # v2-backtracking-en-00202: rupees written as dollars.
        "never $ for rupees",
        # v2-addresses-en-00005: a spoken extension and a product-named domain.
        "spoken extension joins it in lowercase",
        # v2-stt-nearsound-en-00084: a dropped-syllable near-sound.
        '"negate the price" negotiate',
        # v2-backtracking-en-00134: a time word dropped, "the" made "his".
        "never drop a time word",
        'never turn "the" into "his" or "her"',
        # backtracking-202, -253, suite-growth-en-04200: bare "no", the new
        # version's article, the head noun kept.
        'A bare "no" between two clauses said with the same words corrects',
        "with its own article",
        "the noun after it stays",
        # suite-growth-en-05323: a heard name sounding like a name term.
        "even when the heard spelling is a common name too",
    ],
)
def test_the_rules_cover_round_b5_fixes(rule: str) -> None:
    flat = " ".join(SYSTEM.split())
    assert " ".join(rule.split()) in flat


def test_the_examples_cover_round_b5_fixes() -> None:
    answers = {json.loads(said)["text"]: json.loads(cleaned)["text"] for said, cleaned in EXAMPLES}
    assert answers["tom cooks lisa cleans"] == "Tom cooks. Lisa cleans."
    assert answers["mom said call me when you reach"] == "Mom said call me when you reach."


def test_a_correction_asks_to_keep_a_dropped_joining_word() -> None:
    told = correction(Reason(Reject.DROPPED_WORD, ("and",)))
    assert "joining word 'and'" in told
    assert "open the next sentence with it" in told
