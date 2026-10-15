import gzip
import json
import unicodedata
from pathlib import Path

import pytest
from gen_corpus_v2 import gold_refused

from snapbak_backend.ai.guardrail.corrections import last_cue
from snapbak_backend.ai.guardrail.judge import Polished, Reason, Reject, changed, check, judge, tidy, verdict
from snapbak_backend.ai.guardrail.tokens import align, tokens

CORPUS = Path(__file__).parents[3] / "bench" / "cleanup"

ACCEPTED = [
    # A digit Whisper wrote may become its word, as the prompt asks for numbers up to ten (founder, 11 Oct).
    (
        "Ishaan do 1 thing. Get in touch with Aran directly for this and whatever he says then call archana.",
        "Ishaan, do one thing. Get in touch with Aran directly for this, and whatever he says, then call Archana.",
    ),
    ("we have 2 options", "We have two options."),
    # A stutter: a word said twice in a row may lose one copy (founder, 11 Oct).
    ("It's it's finished finished.", "It's finished."),
    ("we we shipped shipped it", "We shipped it."),
    ("the the build is green", "The build is green."),
    (
        "hey team i pushed the fix for the login issue can someone test it",
        "Hey team, I pushed the fix for the login issue. Can someone test it?",
    ),
    (
        "we need three things first the api second the ui third the docs",
        "We need three things:\n1. The API\n2. The UI\n3. The docs",
    ),
    ("for the trip bullet towels bullet a charger", "For the trip:\n- Towels\n- A charger"),
    ("it costs twenty five dollars", "It costs $25."),
    ("we ship on march third at nine am", "We ship on March 3 at 9 am."),
    ("mail john at procol dot in today", "Mail john@procol.in today."),
    (
        "the endpoint is slash users slash id and it returns json",
        "The endpoint is /users/:id and it returns JSON.",
    ),
    (
        "hey team comma i pushed the fix full stop can someone test it question mark",
        "Hey team, I pushed the fix. Can someone test it?",
    ),
    ("I has already send the document yesterday.", "I had already sent the document yesterday."),
    ("she don't know that the files was moved", "She doesn't know that the files were moved."),
    ("they wasn't ready", "They weren't ready."),
    ("I has already send the document", "I had already sent the document."),
    ("ship it please", "Ship it."),
    ("sure i can take the review", "Sure, I can take the review."),
    ("deploy on tuesday actually wednesday", "Deploy on Wednesday."),
    ("we celebrate at our place wait at grandmas place", "We celebrate at Grandma's place."),
    ("Send this to Jam Co. No, send this to GMMCO.", "Send this to GMMCO."),
    ("send this to jam co no send this to GMMCO", "Send this to GMMCO."),
    ("kevin seen the error", "Kevin saw the error."),
    ("i already send it and they pay it", "I already sent it and they paid it."),
    ("my notes goes missing", "My notes go missing."),
    ("we has a idea", "We have an idea."),
    ("he were late, they was early", "He was late, they were early."),
    ("firstly the budget secondly the hiring", "1. The budget\n2. The hiring"),
    ("order pizza for lunch. actually let's get sushi", "Let's get sushi."),
    ("Book the flight to Delhi tomorrow. Scratch that, call Priya.", "Call Priya."),
    ("We should send this to the frontend team. No, don't send it yet.", "Don't send it yet."),
    ("please ship the build to staging today thanks", "Ship the build to staging today."),
    ("the api path is slash v one slash notes slash note id", "The API path is /v1/notes/:noteId"),
    ("unit seven b is on the fourth floor", "Unit 7B is on the 4th floor."),
    ("we need a one hundred rupee budget", "We need a ₹100 budget."),
    ("ill keep it so i dont lose it", "I'll keep it so I don't lose it."),
    ("the oncall flagged it", "The on-call flagged it."),
    ("i can't go but i will not stay", "I cannot go, but I will not stay."),
    ("the folder is slash c o r p", "The folder is /corp."),
    ("it lives under slash users slash tmp let's change that slash users slash prod", "It lives under /users/prod."),
    ("the no fault clause is missing", "The no-fault clause is missing."),
    ("save it to session underscore a17", "Save it to session_a17."),
    ("send report today", "Send the report today."),
    ("the deposit is twelve thousand two hundred dollars", "The deposit is $12,200."),
    ("call me at nine one one or at ten forty five", "Call me at 911 or at 10:45."),
    ("i buyed a laptop", "I bought a laptop."),
    ("we thinked so", "We thought so."),
    ("he has did it", "He has done it."),
    ("the mouses are broken", "The mice are broken."),
    ("i stop the build yesterday", "I stopped the build yesterday."),
    ("ok. fifty five dollars", "Ok. $55."),
    ("fifty five dollars", "$55."),
    ("call at ten after three", "Call at 3:10."),
    ("dial 555-0147 now", "Dial 555-0147 now."),
    ("pick up the kids by half past three", "Pick up the kids by 3:30."),
    (
        "agenda bullet one simplify onboarding bullet two keep dark mode",
        "Agenda:\n- Simplify onboarding\n- Keep dark mode",
    ),
    ("kevin seen error in log", "Kevin saw the error in the log."),
    ("Ship it today team. Also loop in Priya on the budget doc. Scratch that.", "Ship it today team."),
    ("i drafted the update for finance actually for operations", "I drafted the update for operations."),
    ("ill send it", "I'll send it."),
    ("i will sent it", "I will send it."),
    ("she eated lunch", "She ate lunch."),
    ("he drinked the water", "He drank the water."),
    ("i sleeped late", "I slept late."),
    ("first call sam and second email ana", "1. Call Sam\n2. Email Ana"),
    ("first call sam second email ana", "1. Call Sam\n2. Email Ana"),
    ("first the api second the api docs", "1. The API\n2. The API docs"),
    ("first add the airfare second add the hotel", "1. Add the airfare\n2. Add the hotel"),
    ("first set the cap to five second expand to ten percent", "1. Set the cap to 5\n2. Expand to 10%"),
    ("the first build failed the second build passed", "The first build failed, the second build passed."),
    (
        "the plan has two steps one call the vendor two update the sheet",
        "The plan has two steps:\n1. Call the vendor\n2. Update the sheet",
    ),
    (
        "send the reminder to emily at northstar dot io, no, emily at northstar dot com, actually sales at northstar "
        "dot com",
        "Send the reminder to sales@northstar.com.",
    ),
    ("meet at five no six", "Meet at 6."),
    ("ship the hotfix to staging no to production", "Ship the hotfix to production."),
    ("put the notes in drive no in notion", "Put the notes in Notion."),
    ("i have 2 no 3 kids", "I have 3 kids."),
    ("book 3 tickets no 4", "Book 4 tickets."),
    ("keep it with me no in your bag", "Keep it in your bag."),
    ("leave the keys with ravi no at the desk", "Leave the keys at the desk."),
    ("no no we meet friday", "We meet Friday."),
    ("send the report to ravi no don't do this", "Don't do this."),
    ("send the report to ravi no don't send it yet", "Don't send it yet."),
    ("ship it today, no, tomorrow", "Ship it tomorrow."),
    ("don't send it, no wait, send it", "Send it."),
    ("do not merge the pr scratch that merge the pr", "Merge the PR."),
    ("don't cancel the meeting i mean cancel the meeting", "Cancel the meeting."),
    ("the copy should say no busywork rather no distractions", "The copy should say no distractions."),
    ("we ship it yesterday", "We shipped it yesterday."),
    ("call mom", "Call Mom."),
    ("your welcome", "You're welcome."),
    ("there car is here", "Their car is here."),
    ("this is better then that", "This is better than that."),
    ("who's file is this", "Whose file is this?"),
    ("its a good plan", "It's a good plan."),
    ("it almost work", "It almost works."),
    ("i have send it", "I have sent it."),
    ("he has went home", "He has gone home."),
    ("we have shipped it", "We shipped it."),
    ("she have paid the invoice", "She has paid the invoice."),
]


@pytest.mark.parametrize(("said", "wrote"), ACCEPTED)
def test_formatting_grammar_and_corrections_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


REJECTED = [
    # The word must be the digit's own number, and a digit never becomes an ordinal.
    ("we have 2 options", "We have three options.", Reject.UNHEARD),
    ("call 1 person", "Call the first person.", Reject.UNHEARD),
    # Only a word right after itself is a stutter: a repeated clause stays, and so does a word said once.
    ("it's finished, it's finished", "It's finished.", Reject.TOO_MANY_CHANGES),
    ("we shipped shipped the build today", "We shipped the build.", Reject.QUALIFIER),
    ("I think this approach is probably better.", "I think this approach is superior.", Reject.UNHEARD),
    ("send the report today", "Send the 3 reports today.", Reject.UNHEARD),
    ("ship the fix today", "**Ship** the fix today.", Reject.UNHEARD),
    (
        "we should move the launch because the vendor contract is not signed yet",
        "We should move the launch.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("deploy on tuesday then wednesday", "Deploy on Wednesday.", Reject.TOO_MANY_CHANGES),
    ("make the deck and change the font today", "Change the font today.", Reject.TOO_MANY_CHANGES),
    (
        "The release notes are ready and the changelog is merged into main. The dashboard tiles load "
        "slowly. The build is green and the staging deploy finished this morning.",
        "The release notes are ready and the changelog is merged into main. The build is green and the "
        "staging deploy finished this morning.",
        Reject.DROPPED_SENTENCE,
    ),
    (
        "can you write an email to the vendor about the delay",
        "Sure, can you write an email to the vendor about the delay?",
        Reject.REPLY,
    ),
    ("send it to jamko", "Send it to GMMCO.", Reject.UNHEARD),
    ("send the invoice", "Sell the invoice.", Reject.UNHEARD),
    ("No problem, I will take it.", "I will take it.", Reject.TOO_MANY_CHANGES),
    ("there is no data in the report ship it today", "Ship it today.", Reject.TOO_MANY_CHANGES),
    ("the budget actually doubled last quarter so we hire now", "So we hire now.", Reject.TOO_MANY_CHANGES),
    ("sorry for the delay the build broke last night we fixed it", "We fixed it.", Reject.TOO_MANY_CHANGES),
    ("we would rather ship friday than monday tell the team", "Tell the team.", Reject.TOO_MANY_CHANGES),
    (
        "send the deck to priya and wait for her reply before the launch",
        "Send the deck to Priya before the launch.",
        Reject.TOO_MANY_CHANGES,
    ),
    (
        "We should send this to the frontend team. No, don't send it yet.",
        "We should send this to the frontend team.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("i am sorry the build broke again today", "The build broke again today.", Reject.TOO_MANY_CHANGES),
    ("i was wait listed for the conference", "Listed for the conference.", Reject.TOO_MANY_CHANGES),
    ("we have no money left for the trip", "We have left for the trip.", Reject.NEGATION),
    ("i know the answer", "I don't know the answer.", Reject.NEGATION),
    ("i can go tomorrow", "I cannot go tomorrow.", Reject.NEGATION),
    ("i do not agree with the plan", "I do agree with the plan.", Reject.NEGATION),
    ("we will never ship this", "We will ship this.", Reject.NEGATION),
    ("the meeting is at five", "The meeting is at 6.", Reject.UNHEARD),
    ("pay him fifty dollars", "Pay him $500.", Reject.UNHEARD),
    ("tell the team to wait for the review", "The review.", Reject.TOO_MANY_CHANGES),
    ("ravi said sorry about the delay", "About the delay.", Reject.TOO_MANY_CHANGES),
    ("the client is sorry we missed the deadline", "We missed the deadline.", Reject.TOO_MANY_CHANGES),
    ("priya was actually promoted last week", "Promoted last week.", Reject.TOO_MANY_CHANGES),
    ("i would rather go home now and rest", "Go home now and rest.", Reject.TOO_MANY_CHANGES),
    ("make that call to the bank before noon", "Call to the bank before noon.", Reject.TOO_MANY_CHANGES),
    ("ship it", "Ship it to the and it or this with that for it in the.", Reject.TOO_MANY_CHANGES),
    ("call priya", "Call Priya and it.", Reject.TOO_MANY_CHANGES),
    ("invite alice and bob", "Invite Alice or Bob.", Reject.TOO_MANY_CHANGES),
    ("put it in the box", "Put it on the box.", Reject.TOO_MANY_CHANGES),
    ("meet at five not six", "Meet at 6, not 5.", Reject.UNHEARD),
    ("five people and six chairs", "6 people and 5 chairs.", Reject.UNHEARD),
    ("twenty five", "25 20", Reject.UNHEARD),
    ("the cost is five and six", "The cost is 56.", Reject.UNHEARD),
    ("accept the contract today", "Access the contract today.", Reject.UNHEARD),
    ("sign the contract now", "Sign the contrast now.", Reject.UNHEARD),
    ("send the mail to priya", "Send the main to Priya.", Reject.UNHEARD),
    ("the debit was approved", "The debt was approved.", Reject.UNHEARD),
    ("send a file to sam", "Send that file to Sam.", Reject.TOO_MANY_CHANGES),
    (
        "send the deck to sam and the notes to ravi scratch that the notes to priya",
        "The notes to Priya.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("we need five servers by friday", "We need servers by Friday.", Reject.TOO_MANY_CHANGES),
    ("call me at three", "Call me.", Reject.TOO_MANY_CHANGES),
    ("ship the second build to sam today", "Ship the build to Sam today.", Reject.TOO_MANY_CHANGES),
    ("ship it", "The ship is to the it a.", Reject.TOO_MANY_CHANGES),
    ("ship it now", "Ship the it to a now is.", Reject.TOO_MANY_CHANGES),
    ("we are ready they are done it is live", "We were ready, they were done, it was live.", Reject.TOO_MANY_CHANGES),
    ("Ravi will send the deck. Wait for it.", "For it.", Reject.TOO_MANY_CHANGES),
    ("Thanks for the update. Sorry for the delay on my side.", "For the delay on my side.", Reject.TOO_MANY_CHANGES),
    ("Send it to Sam. Sorry about that.", "About that.", Reject.TOO_MANY_CHANGES),
    ("We fly Monday. Wait until the visa comes.", "Until the visa comes.", Reject.TOO_MANY_CHANGES),
    ("Ship it today team. Also loop in Priya on the budget doc. Scratch that.", "", Reject.TOO_MANY_CHANGES),
    (
        "Ship it today team. Also loop in Priya on the budget doc. Scratch that.",
        "Also loop in Priya on the budget doc.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("ship it today team also loop in priya on the budget doc scratch that", "", Reject.TOO_MANY_CHANGES),
    ("i was first in line and second in the race", "I was:\n1. In line\n2. In the race", Reject.TOO_MANY_CHANGES),
    ("first of the month and second of the month", "1. Of the month\n2. Of the month", Reject.TOO_MANY_CHANGES),
    ("we shipped one or two fixes", "We shipped:\n1. Or\n2. Fixes", Reject.TOO_MANY_CHANGES),
    ("we need one to two days", "We need:\n1. To\n2. Days", Reject.TOO_MANY_CHANGES),
    ("the first build failed the second build passed", "1. Build failed\n2. Build passed", Reject.TOO_MANY_CHANGES),
    ("i bought two apples one red and two green", "I bought two apples:\n1. Red\n2. Green", Reject.TOO_MANY_CHANGES),
    ("i will send it", "I sent it.", Reject.TENSE),
    ("i will go there", "I go there.", Reject.TENSE),
    (
        "i will send the report and i will pay the vendor and i will call the client",
        "I sent the report and I paid the vendor and I called the client.",
        Reject.TENSE,
    ),
    ("give one pill at night then give two pills at night", "Give two pills at night.", Reject.TOO_MANY_CHANGES),
    ("send it no wait send it", "Don't send it.", Reject.NEGATION),
    ("i have 2 no kids", "I have 2 kids.", Reject.NEGATION),
    ("don't send it, no wait, send it", "Don't send it.", Reject.TOO_MANY_CHANGES),
    ("don't send it", "Send it.", Reject.NEGATION),
    (
        "the copy should say no busywork rather no distractions",
        "The copy should say distractions.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("tell the vendor no pickup after six pm", "Tell the pickup after 6 pm.", Reject.NEGATION),
    ("send the report to ravi", "", Reject.DROPPED_SENTENCE),
    ("there is no data in the report don't ship it", "Don't ship it.", Reject.TOO_MANY_CHANGES),
    ("call priya today", "Call Priya.", Reject.QUALIFIER),
    ("maybe we ship friday", "We ship Friday.", Reject.QUALIFIER),
    ("charge the customer twice", "Charge the customer.", Reject.QUALIFIER),
    ("delete all files", "Delete files.", Reject.QUALIFIER),
    ("the bug is probably fixed", "The bug is fixed.", Reject.QUALIFIER),
    ("we must ship before friday", "We ship before Friday.", Reject.QUALIFIER),
    ("we almost lost the client", "We lost the client.", Reject.QUALIFIER),
    ("we rarely ship on friday", "We ship on Friday.", Reject.QUALIFIER),
    ("it is definitely ready", "It is ready.", Reject.QUALIFIER),
    ("we shipped it", "We ship it.", Reject.TENSE),
    ("i have sent the file to priya", "I send the file to Priya.", Reject.TENSE),
    ("we have finished the review", "We finish the review.", Reject.TENSE),
    ("she has paid the invoice", "She pays the invoice.", Reject.TENSE),
    ("i went home", "I go home.", Reject.TENSE),
    ("we ship the build", "We shipped the build.", Reject.TENSE),
    ("he is coming", "He was coming.", Reject.TENSE),
    ("i send it tomorrow", "I sent it tomorrow.", Reject.TENSE),
    ("your file is ready", "Their file is ready.", Reject.UNHEARD),
    ("send report", "I send the report.", Reject.TOO_MANY_CHANGES),
    ("pay it", "Pay for it.", Reject.TOO_MANY_CHANGES),
]


@pytest.mark.parametrize(("said", "wrote", "reason"), REJECTED)
def test_unheard_words_rewrites_dropped_sentences_and_replies_fail(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


def test_decomposed_and_precomposed_unicode_are_the_same_word() -> None:
    said = unicodedata.normalize("NFD", "the café is open")
    wrote = "The caf\u00e9 is open."
    assert "\u0301" in said and "\u0301" not in wrote
    assert check(said, wrote, []) is None


def test_the_last_cue_is_the_longest_one_ending_last() -> None:
    assert last_cue(["today", "no", "no"]) == (1, 3)


def test_a_lone_cue_cannot_erase_the_prior() -> None:
    prior = "Hi team, the launch moved to Monday and legal signed off on the contract."
    judged = judge(prior, "actually", [], "", "")
    assert judged == Polished(text="actually", revision=None, rejected=Reject.TOO_MANY_CHANGES)


def test_an_everyday_wait_for_cannot_erase_the_prior() -> None:
    judged = judge("Ravi will send the deck.", "wait for it", [], "", "For it.")
    assert judged.rejected == Reject.TOO_MANY_CHANGES


def test_a_chunk_that_only_scratches_the_last_sentence_revises_the_prior() -> None:
    prior = "Ship it today team. Also loop in Priya on the budget doc."
    judged = judge(prior, "scratch that", [], "Ship it today team.", "")
    assert judged == Polished(text="", revision="Ship it today team.", rejected=None)


def test_a_retraction_of_the_whole_prior_is_kept() -> None:
    prior = "We should send this to the frontend team."
    judged = judge(prior, "no, don't send it yet", [], "", "Don't send it yet.")
    assert judged == Polished(text="Don't send it yet.", revision="", rejected=None)


@pytest.mark.parametrize("path", sorted(CORPUS.glob("*.jsonl.gz")), ids=lambda path: path.name)
def test_the_guardrail_accepts_the_corpus_gold_answers(path: Path) -> None:
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        rows = [json.loads(line) for line in lines]
    rejected = [row["id"] for row in rows if gold_refused(row)]
    assert len(rejected) * 100 <= len(rows), rejected


LONE_NEGATORS = {"no"}


def without_a_negation(text: str) -> str | None:
    """The text with its first lone "no" and the word before it dropped, as a model that misread
    a negation as a correction would write it; `None` when it has none."""
    found = text.split(" ")
    for i in range(1, len(found) - 1):
        word = found[i].lower()
        doubled = found[i - 1].lower().strip(",") in LONE_NEGATORS or found[i + 1].lower() in LONE_NEGATORS
        if word in LONE_NEGATORS and not doubled and found[i - 1][-1].isalpha():
            return " ".join(found[: i - 1] + found[i + 1 :])
    return None


def test_dropping_a_real_negation_with_the_word_before_it_is_rejected_across_the_corpus() -> None:
    mutated = []
    for path in sorted(CORPUS.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            for row in map(json.loads, lines):
                wrote = without_a_negation(row["expected"][0])
                if wrote is not None:
                    mutated.append((row["id"], check(row["said"], wrote, row["dictionary"])))
    accepted = [row_id for row_id, reason in mutated if reason is None]
    assert len(mutated) >= 80
    assert len(accepted) * 100 <= len(mutated), accepted


def test_a_term_may_appear_though_the_user_said_it_differently() -> None:
    assert check("send it to jamko", "Send it to GMMCO.", ["GMMCO"]) is None


def test_tidy_puts_term_spelling_back_and_trims_line_ends() -> None:
    tidied = tidy("Gmmco signed, and IOS ships Friday.  \n- Priya ", ["GMMCO", "iOS", "Priya"])
    assert tidied == "GMMCO signed, and iOS ships Friday.\n- Priya"


def test_tidy_keeps_a_sentence_start_capital_on_a_lowercase_term() -> None:
    text = "Npm is fast. Yarn works, Pnpm too? pnpm.\n1. Yarn"
    tidied = tidy(text, ["npm", "yarn", "pnpm"])
    assert tidied == "Npm is fast. Yarn works, pnpm too? pnpm.\n1. Yarn"


def test_tidy_lowercases_a_lowercase_term_inside_a_sentence() -> None:
    assert tidy("Use NPM and Yarn more.", ["npm", "yarn"]) == "Use npm and yarn more."


def test_tidy_leaves_a_term_inside_another_word_alone() -> None:
    assert tidy("The iOSX build and ios ship.", ["iOS"]) == "The iOSX build and iOS ship."


PRIOR = "Let's meet on Friday at noon."
FIX = "no wait make it thursday not friday"


def test_a_revision_is_judged_with_the_prior_text() -> None:
    judged = judge(PRIOR, FIX, [], "Let's meet on Thursday at noon.", "")
    assert judged == Polished(text="", revision="Let's meet on Thursday at noon.", rejected=None)


def test_a_revision_with_an_unheard_word_is_rejected_whole() -> None:
    judged = judge(PRIOR, FIX, [], "Let's meet on Thursday at lunch.", "")
    assert judged == Polished(text=FIX, revision=None, rejected=Reject.UNHEARD)


def test_a_rejected_answer_keeps_the_request_text() -> None:
    judged = judge("", "ship it", [], None, "Sure, shipping it.")
    assert judged == Polished(text="ship it", revision=None, rejected=Reject.REPLY)


def test_an_accepted_answer_is_tidied() -> None:
    judged = judge("", "ship it to gmmco", ["GMMCO"], None, "Ship it to Gmmco.  ")
    assert judged == Polished(text="Ship it to GMMCO.", revision=None, rejected=None)


def test_a_revision_without_prior_text_or_equal_to_it_is_dropped() -> None:
    assert judge("", "ship it", [], "Ship it.", "Ship it.").revision is None
    assert judge(PRIOR, "and bring snacks", [], PRIOR, "And bring snacks.").revision is None


def test_a_first_chunk_answered_only_as_a_revision_is_judged_as_its_text() -> None:
    judged = judge("", "she go home", [], "She goes home.", "")
    assert judged == Polished(text="She goes home.", revision=None, rejected=None)
    unheard = judge("", "she go home", [], "She goes home now.", "")
    assert unheard == Polished(text="she go home", revision=None, rejected=Reject.UNHEARD)


def test_a_negation_dropped_across_chunks_is_rejected() -> None:
    judged = judge(
        "I will not come on Friday.", "and send the report", [], "I will come on Friday.", "And send the report."
    )
    assert judged.rejected == Reject.NEGATION


def test_a_negative_instruction_taken_back_across_chunks_is_kept() -> None:
    judged = judge("Don't send it.", "no wait send it", [], "Send it.", "")
    assert judged == Polished(text="", revision="Send it.", rejected=None)


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the tests are passing", "The tests were passing."),
    ],
)
def test_a_tense_swap_costs_one_change(said: str, wrote: str) -> None:
    said_tokens = tokens(said)
    said_words = [word for word, _ in said_tokens]
    wrote_words = [word for word, _ in tokens(wrote)]
    marks = [mark for _, mark in said_tokens]
    count, _, _, _ = changed(said_words, marks, wrote_words, align(said_words, wrote_words), set())
    assert count == 1


@pytest.mark.parametrize(
    ("said", "wrote", "polished"),
    [
        ("i will try again on hotel wifi", "I will try again on hotel Wi-Fi.", "I will try again on hotel wifi."),
    ],
)
def test_a_word_said_whole_loses_the_hyphen_the_model_put_in(said: str, wrote: str, polished: str) -> None:
    assert judge("", said, [], None, wrote) == Polished(text=polished, revision=None, rejected=None)


@pytest.mark.parametrize(
    ("said", "wrote", "polished"),
    [
        # 14 Oct 2026 (English only): "5 countries" came back "five countries" in two runs of two.
        (
            "For the beta, we support 5 countries and 2 languages.",
            "For the beta, we support five countries and two languages.",
            "For the beta, we support 5 countries and 2 languages.",
        ),
        ("2 tickets are confirmed", "Two tickets are confirmed.", "2 tickets are confirmed."),
        # A number said as a word stays a word, and so does one the speaker said both ways.
        ("we support five countries", "We support five countries.", "We support five countries."),
        ("send 1 copy to no one else", "Send one copy to no one else.", "Send one copy to no one else."),
    ],
)
def test_a_digit_already_written_stays_a_digit(said: str, wrote: str, polished: str) -> None:
    assert judge("", said, [], None, wrote) == Polished(text=polished, revision=None, rejected=None)


@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("send a follow up email", "Send a follow-up email.", []),
        ("page the oncall", "Page the on-call.", ["on-call"]),
    ],
)
def test_a_hyphen_between_words_said_apart_or_in_a_term_stays(said: str, wrote: str, terms: list[str]) -> None:
    assert judge("", said, terms, None, wrote) == Polished(text=wrote, revision=None, rejected=None)


def test_a_revision_loses_the_hyphen_the_model_put_in_a_word_said_whole() -> None:
    judged = judge("Ping the team.", "no wait ping the oncall", [], "Ping the on-call.", "")
    assert judged == Polished(text="", revision="Ping the oncall.", rejected=None)


JOINS = [
    # A piece that goes on with the sentence before it keeps the word joining it (founder, 11 Oct: "visually
    # appealing How is it going to show me the data?").
    (
        "2. How well is it going to be, visually appealing",
        "and how is it going to show me the data?",
        "How is it going to show me the data?",
        Reject.DROPPED_JOIN,
    ),
    (
        "2. How well is it going to be, visually appealing",
        "and how is it going to show me the data?",
        "and how is it going to show me the data?",
        None,
    ),
    ("We shipped it.", "and then we celebrated", "Then we celebrated.", None),
]


@pytest.mark.parametrize(("prior", "text", "output", "reason"), JOINS)
def test_a_piece_that_continues_a_sentence_keeps_its_joining_word(
    prior: str, text: str, output: str, reason: Reject | None
) -> None:
    assert judge(prior, text, [], None, output).rejected == reason


@pytest.mark.parametrize(
    ("prior", "text", "output", "expected"),
    [
        ("2. visually appealing", "and How is it going", "And how is it going?", "and how is it going?"),
        ("We shipped it.", "and then we left", "And then we left.", "And then we left."),
    ],
)
def test_a_joining_word_that_goes_on_with_a_sentence_comes_back_lowercase(
    prior: str, text: str, output: str, expected: str
) -> None:
    assert judge(prior, text, [], None, output).text == expected


# A word the recognizer misheard may become the word that sounds the same and was meant (STT policy, 11 Oct): one
# said word for one written, two for one ("a head" ahead) or one for two ("alot" a lot).
MISHEARD = [
    ("we need to taste the build before friday", "We need to test the build before Friday.", []),
    ("it was a waist of time", "It was a waste of time.", []),
    ("check weather it works", "Check whether it works.", []),
    ("one peace is missing from the box", "One piece is missing from the box.", []),
    ("open the write file first", "Open the right file first.", []),
    ("take a brake after lunch", "Take a break after lunch.", []),
    ("we are a head of the launch", "We are ahead of the launch.", []),
    ("i for got the keys at home", "I forgot the keys at home.", []),
    ("please except the offer by friday", "Please accept the offer by Friday.", []),
    ("you should of told me earlier", "You should have told me earlier.", []),
    ("i no the florist", "I know the florist.", []),
    ("the clerk new my number", "The clerk knew my number.", []),
    ("send male today", "Send mail today.", []),
    ("it past review", "It passed review.", []),
    ("witch date works", "Which date works?", []),
    ("the service can cash the results", "The service can cache the results.", []),
    ("we pay invoices through zero", "We pay invoices through Xero.", ["Xero"]),
    ("book a ride on lift tonight", "Book a ride on Lyft tonight.", ["Lyft"]),
    ("the lift is broken again", "The lift is broken again.", ["Lyft"]),
    ("ask procall for the quote", "Ask Procol for the quote.", ["Procol"]),
]


@pytest.mark.parametrize(("said", "wrote", "terms"), MISHEARD)
def test_a_misheard_word_may_become_the_sound_alike_that_was_meant(said: str, wrote: str, terms: list[str]) -> None:
    assert check(said, wrote, terms) is None


MISHEARD_REFUSED = [
    ("taste the build", "Check the build.", [], Reject.UNHEARD),
    ("we need fifteen chairs", "We need 50 chairs.", [], Reject.UNHEARD),
    ("i can come", "I can't come.", [], Reject.NEGATION),
    ("tell sean the plan", "Tell Shawn the plan.", [], Reject.UNHEARD),
    ("tell sean the plan", "Tell Shaun the plan.", [], Reject.UNHEARD),
    ("Lakshmi will join the call", "Laxmi will join the call.", ["Lakshmi"], Reject.UNHEARD),
    ("we use Xero for invoices", "We use zero for invoices.", ["Xero"], Reject.UNHEARD),
    ("i will send it", "I sent it.", [], Reject.TENSE),
    ("book a ride on lift tonight", "Book a ride on Lyft tonight.", [], Reject.UNHEARD),
    ("send it", "Send it to Procol.", ["Procol"], Reject.UNHEARD),
    ("taste the waist of the peace", "Test the waste of the piece.", [], Reject.TOO_MANY_CHANGES),
    ("deploy it on Kubernetes today", "Deploy it on Kubernete today.", ["Kubernetes"], Reject.TERM),
    ("we have no time left", "We have now time left.", [], Reject.UNHEARD),
]


@pytest.mark.parametrize(("said", "wrote", "terms", "reason"), MISHEARD_REFUSED)
def test_a_sound_alike_never_changes_a_fact_a_name_or_a_term(
    said: str, wrote: str, terms: list[str], reason: Reject
) -> None:
    assert check(said, wrote, terms) == reason


LISTS_AND_NUMBERS = [
    # "And three" counts the last item off, like "one" and "two" before it (founder-020, 11 Oct).
    (
        "Put these in a numbered list. One, milk. Two, eggs. And three, bread.",
        "Put these in a numbered list:\n1. Milk\n2. Eggs\n3. Bread",
    ),
    # An item after the counted ones takes the next number, and a dangling last word goes (founder-021).
    (
        "i need to do the following first call the bank second pay the rent. Pick up the dry cleaning. And",
        "I need to do the following:\n1. Call the bank\n2. Pay the rent\n3. Pick up the dry cleaning",
    ),
    # Items asked for in a numbered list are numbered though not counted (D5).
    (
        "I want the following in a numbered list. Buy milk. Call mom. Pay the rent.",
        "I want the following in a numbered list:\n1. Buy milk\n2. Call mom\n3. Pay the rent",
    ),
    (
        "here are a few things about me. i lead the design team. i love long hikes. i live in pune.",
        "Here are a few things about me:\n- I lead the design team\n- I love long hikes\n- I live in Pune",
    ),
    ("the truck reaches quarter to six", "The truck reaches 5:45."),
    ("thirteen interviews at 3pm", "13 interviews at 3 pm."),
]


@pytest.mark.parametrize(("said", "wrote"), LISTS_AND_NUMBERS)
def test_counted_lists_fractions_and_agreement_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the truck reaches quarter to six", "The truck reaches 6:45."),
        (
            "I want the following in a numbered list. Buy milk. Call mom.",
            "I want the following:\n1. Buy milk\n5. Call mom",
        ),
        ("one red and two green", "1. Red\n2. Green"),
    ],
)
def test_a_wrong_fraction_or_list_number_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote", "terms", "expected"),
    [
        ("ship it today", "Okay, ship it today.", [], Reason(Reject.UNHEARD, ("okay",))),
        ("we shipped it", "We ship it.", [], Reason(Reject.TENSE, ("shipped",))),
        ("i can come", "I can't come.", [], Reason(Reject.NEGATION, ("can't",))),
        ("deploy it on Kubernetes", "Deploy it on Kubernete.", ["Kubernetes"], Reason(Reject.TERM, ("Kubernetes",))),
        ("sure i can take it", "Sure, I can take it.", [], None),
    ],
)
def test_a_refusal_names_its_rule_and_the_words(said: str, wrote: str, terms: list[str], expected: Reason) -> None:
    assert verdict(said, wrote, terms) == expected


def test_a_rejected_polish_carries_its_reason() -> None:
    judged = judge("", "ship it today", [], None, "Okay, ship it today.")
    assert judged.reason == Reason(Reject.UNHEARD, ("okay",))
    assert judged == Polished(text="ship it today", revision=None, rejected=Reject.UNHEARD)


def test_a_dropped_joining_word_is_named() -> None:
    dropped = judge("2. visually appealing", "and how is it going", [], None, "How is it going?")
    assert dropped.reason == Reason(Reject.DROPPED_JOIN, ("and",))


# Round 1 (11 Oct): right answers the guardrail refused.
ACCEPTED_ROUND_1 = [
    # A lone "no" between two names, colours, sides, levels or family words takes the first back.
    (
        "the family wants to see amy no emily for the pain consult",
        "The family wants to see Emily for the pain consult.",
    ),
    ("send it to priya no rahul", "Send it to Rahul."),
    ("call mom no dad", "Call Dad."),
    ("use red no blue for the header", "Use blue for the header."),
    ("set it to high no low priority", "Set it to low priority."),
    (
        "the text block should align left no center and the note under it should say draft only actually internal "
        "draft",
        "The text block should align center and the note under it should say internal draft.",
    ),
    # "no wait" and "no actually" are strong cues: a new instruction after them replaces the one before.
    ("move it to the backlog no actually close it", "Close it."),
    ("approve it no wait reject it", "Reject it."),
    # Marks said as words, in pairs.
    ("she said open quote hi close quote", 'She said "hi."'),
    # A real negation stays, and so does a lone "no" before a negation phrase.
    ("the patient has no allergies actually no known allergies", "The patient has no known allergies."),
    ("june fifth no one objected", "June 5. No one objected."),
    # "not friday" names the version taken back.
    ("let's meet on friday at noon no wait make it thursday not friday", "Let's meet on Thursday at noon."),
]


@pytest.mark.parametrize(("said", "wrote"), ACCEPTED_ROUND_1)
def test_round_1_right_answers_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 1 (11 Oct): wrong answers the guardrail let through.
REJECTED_ROUND_1 = [
    # A negation after the cue belongs to the replacement; a weak cue before "for" or "about" is a verb.
    ("send it no wait don't send it", "Send it.", Reject.TOO_MANY_CHANGES),
    (
        "the patient has no allergies actually no known allergies",
        "The patient has known allergies.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("i will wait for the report", "I will the report.", Reject.TOO_MANY_CHANGES),
    ("sorry about the delay", "About the delay.", Reject.TOO_MANY_CHANGES),
    ("june fifth no one objected", "June 5. One objected.", Reject.NEGATION),
    (
        "we need the sales snapshot for 2022 no for 2021 to compare",
        "We need the sales snapshot for 2022 to compare.",
        Reject.TOO_MANY_CHANGES,
    ),
    # Both versions kept around a cue.
    ("ship friday i mean monday", "Ship Friday, I mean Monday.", Reject.KEPT_CORRECTION),
    ("the meeting is monday no tuesday", "The meeting is Monday, no, Tuesday.", Reject.KEPT_CORRECTION),
    ("see amy no emily", "See Amy no Emily.", Reject.KEPT_CORRECTION),
    # A question made a statement, a mark word used as a noun eaten as a mark.
    ("is the deck ready", "The deck is ready.", Reject.QUESTION),
    ("the period of the contract is two years", "The. Of the contract is 2 years.", Reject.TOO_MANY_CHANGES),
    # Lists: one never announced, one missing an item.
    ("I woke up then made coffee then left", "- I woke up\n- Made coffee\n- Left", Reject.FALSE_LIST),
    ("bullet milk bullet eggs bullet bread", "- Milk\n- Eggs", Reject.DROPPED_ITEM),
    (
        "first verify the payment schedule second review late fee language third confirm invoice timing",
        "1. Verify the payment schedule\n3. Confirm invoice timing",
        Reject.DROPPED_ITEM,
    ),
    (
        "first verify the payment schedule second review late fee language third confirm invoice timing",
        "1. Verify the payment schedule\n2. Confirm invoice timing",
        Reject.DROPPED_SENTENCE,
    ),
    # A sentence left out whose words recur elsewhere, with a cue far from it.
    (
        "the vendor sent the contract to our legal team this morning and the finance team reviewed the pricing in "
        "the contract after lunch. the legal team wants a clean copy of the contract from the vendor. the vendor "
        "will ship the first order to the warehouse on monday and the finance team will pay the invoice next week "
        "actually the week after",
        "The vendor sent the contract to our legal team this morning, and the finance team reviewed the pricing in "
        "the contract after lunch. The vendor will ship the first order to the warehouse on Monday, and the finance "
        "team will pay the invoice the week after.",
        Reject.DROPPED_SENTENCE,
    ),
    # A meridiem never said.
    ("the meeting is from two to three pm", "The meeting is from 2 to 3 am.", Reject.QUALIFIER),
]


@pytest.mark.parametrize(("said", "wrote", "reason"), REJECTED_ROUND_1)
def test_round_1_wrong_answers_fail(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


def test_a_misspelled_term_is_refused_though_a_cue_follows_it() -> None:
    said = "ship the android build on thursday actually on friday morning"
    assert check(said, "Ship the Androi build on Friday morning.", ["Android"]) == Reject.TERM
    assert check(said, "Ship the Android build on Friday morning.", ["Android"]) is None


def test_a_term_taken_back_whole_by_a_cue_is_not_lost() -> None:
    assert check("i can have ben handle this rather nina", "I can have Nina handle this.", ["Ben"]) is None


def test_a_meridiem_said_apart_or_joined_is_heard() -> None:
    assert check("call me at nine a m before the showing", "Call me at 9 am before the showing.", []) is None
    assert check("thirteen interviews at 3pm", "13 interviews at 3 pm.", []) is None


# Round 3 (11 Oct): right misheard-word fixes the guardrail refused. A number word may be fixed for its sound-alike
# ("to tickets" two) though never into another number; a cue word may be written for its sound-alike ("weight"
# wait); silent letters don't count ("reign" rain); a split word may join though a part is a number ("ten your"
# tenure); and pairs speech to text confuses a syllable apart are known ("bones" bonus).
MISHEARD_ROUND_3 = [
    ("i bought to tickets for the show", "I bought two tickets for the show."),
    ("the queue is two long", "The queue is too long."),
    ("we have for options", "We have four options."),
    ("the meeting is at for pm", "The meeting is at 4 pm."),
    ("the team one the match", "The team won the match."),
    ("please weight in the lobby until your name is called", "Please wait in the lobby until your name is called."),
    ("the weather forecast says it will reign all weekend", "The weather forecast says it will rain all weekend."),
    ("the home loan has a ten your of twenty years", "The home loan has a tenure of 20 years."),
    ("the weather four cast says heavy rain on saturday", "The weather forecast says heavy rain on Saturday."),
    ("the referral bones for analyst hires is two thousand dollars", "The referral bonus for analyst hires is $2,000."),
    ("my doctor put me on a five day antibiotic chorus", "My doctor put me on a 5-day antibiotic course."),
    (
        "my grandmother was formally a teacher at the village school",
        "My grandmother was formerly a teacher at the village school.",
    ),
    ("we should negate a better rate with the landlord", "We should negotiate a better rate with the landlord."),
]


@pytest.mark.parametrize(("said", "wrote"), MISHEARD_ROUND_3)
def test_round_3_misheard_number_cue_and_near_sound_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A number never becomes another number, and a right word stays.
        ("we need fifteen chairs", "We need 50 chairs."),
        ("i want to go home", "I want two go home."),
        ("the debit was approved", "The debt was approved."),
        ("we have two options", "We have 3 options."),
        ("we have two options", "We have too options."),
    ],
)
def test_round_3_a_number_sound_alike_never_changes_a_count(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: right grammar fixes the guardrail refused. A past after a negated modal is a slip for the base form; a
# verb agrees with its own clause's subject, not with a "main" in another clause; a future time word allows "will"
# and a future form, a past one a past form; Indian English "from morning" becomes "since morning".
GRAMMAR_ROUND_3 = [
    ("he did not went yesterday", "He did not go yesterday."),
    ("she did not gave the keys", "She did not give the keys."),
    ("they could not found the bug", "They could not find the bug."),
    ("we will not sent the file today", "We will not send the file today."),
    ("she can't came today", "She can't come today."),
    ("next week we finished the audit", "Next week we will finish the audit."),
    ("tomorrow she joined the team", "Tomorrow she will join the team."),
    (
        "this issue is there from yesterday and nobody are looking at it",
        "This issue has been there since yesterday, and nobody is looking at it.",
    ),
    ("I am waiting for it from morning.", "I have been waiting for it since morning."),
    ("they are playing since morning at the terrace", "They have been playing since morning on the terrace."),
    ("i am working here since two years", "I have been working here for two years."),
    ("the retry wasteed the quota", "The retry wasted the quota."),
    (
        "on april fifth liam save twelve percent but his budget are off",
        "On April 5, Liam saved 12%, but his budget is off.",
    ),
    ("between you and i this is risky", "Between you and me, this is risky."),
    ("whom is coming to the party", "Who is coming to the party?"),
    ("less people came this time", "Fewer people came this time."),
]


@pytest.mark.parametrize(("said", "wrote"), GRAMMAR_ROUND_3)
def test_round_3_grammar_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        # A time word allows its own tense only.
        ("we finished the audit", "We will finish the audit.", Reject.UNHEARD),
        ("he did not go yesterday", "He went yesterday.", Reject.TOO_MANY_CHANGES),
    ],
)
def test_round_3_grammar_never_breaks_agreement_or_tense(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


# Round 3: correction cues the guardrail did not know ("I meant", "oops", "nope", "correction"), a lone "no" swapping a
# name at the start, "no not X", and an antonym said as the fix.
CORRECTIONS_ROUND_3 = [
    ("send it to anna sorry I meant maria", "Send it to Maria."),
    ("send it to anna oops to maria", "Send it to Maria."),
    ("send it to anna nope maria", "Send it to Maria."),
    ("priya no rahul will lead the call", "Rahul will lead the call."),
    ("the meeting is on tuesday no not tuesday wednesday", "The meeting is on Wednesday."),
    ("the client is happy no unhappy with the draft", "The client is unhappy with the draft."),
]


@pytest.mark.parametrize(("said", "wrote"), CORRECTIONS_ROUND_3)
def test_round_3_more_correction_cues_take_words_back(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# A pronoun the speaker puts right after a cue, a lone "no" too, is the one written (14 Oct).
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("send it to him no her", "Send it to her."),
        ("send it to him i mean her", "Send it to her."),
        ("he no she will lead the call", "She will lead the call."),
        ("tell the team that he no she will lead", "Tell the team that she will lead."),
        ("put it on his no her desk", "Put it on her desk."),
    ],
)
def test_a_pronoun_put_right_after_a_cue_is_written(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        # Without a cue, another person is never heard.
        ("send it to him", "Send it to her.", Reject.UNHEARD),
        # The pronoun taken back is never the one written.
        ("send it to him no her", "Send it to him.", Reject.TOO_MANY_CHANGES),
    ],
)
def test_a_pronoun_not_put_right_by_the_speaker_is_refused(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # The same words used plainly stay.
        ("the correction was filed on monday", "The was filed on Monday."),
        ("i meant every word of it", "Every word of it."),
        ("priya will not lead the call", "Priya will lead the call."),
    ],
)
def test_round_3_plain_uses_of_cue_words_stay(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: one content word left out with nothing written in its place is lost content, as a run of two is.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        (
            "my teacher asked for the draft by february fourteenth at five pm",
            "My asked for the draft by February 14 at 5 pm.",
        ),
        ("kevin wrote the introduction and rachel wrote the conclusion", "Kevin wrote the introduction, and wrote the"),
        (
            "please book the room for friday and send the agenda to the team",
            "Please book the room for Friday and send the agenda to the",
        ),
        (
            "add a delay before the webhook fires then retry after ten minutes",
            "Add a delay before the webhook fires, 10 minutes.",
        ),
    ],
)
def test_round_3_a_single_dropped_content_word_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) in (Reject.DROPPED_WORD, Reject.DROPPED_SENTENCE)


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # Words moved, grammar words dropped and number words written as digits are no loss.
        ("so what workflows will be", "So, what will the workflows be?"),
        ("this section needs a more clearer hierarchy", "This section needs a clearer hierarchy."),
        ("the note says to discuss regarding the results", "The note says to discuss the results."),
        ("the fee is twenty euros", "The fee is €20."),
    ],
)
def test_round_3_moved_grammar_and_number_words_are_not_dropped(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: a dictionary term never recases a plain word the model kept plain ("cut me some slack" with Slack, "let
# us know" with US), nor a word inside an email, a web address or a path; a variant casing of the term is still put
# back ("NGINX" Nginx, "ios" iOS).
@pytest.mark.parametrize(
    ("text", "terms", "tidied"),
    [
        ("Cut me some slack, please.", ["Slack"], "Cut me some slack, please."),
        ("Can you zoom in?", ["Zoom"], "Can you zoom in?"),
        ("Let us know by Friday.", ["US"], "Let us know by Friday."),
        ("It is done.", ["IT"], "It is done."),
        (
            "I will mark it done and bill the client.",
            ["Will", "Mark", "Bill"],
            "I will mark it done and bill the client.",
        ),
        ("The teaser earned real cred.", ["CRED"], "The teaser earned real cred."),
        ("Mail sales@northstar.com today.", ["Northstar"], "Mail sales@northstar.com today."),
        ("Open portal.snapbak.app now.", ["Snapbak"], "Open portal.snapbak.app now."),
        ("Call /api/events first.", ["API"], "Call /api/events first."),
        ("Restart NGINX and the ios app.", ["Nginx", "iOS"], "Restart Nginx and the iOS app."),
        ("Pay with PayTM.", ["Paytm"], "Pay with Paytm."),
        ("Use node.js here.", ["Node.js"], "Use Node.js here."),
    ],
)
def test_round_3_tidy_recases_only_term_variants(text: str, terms: list[str], tidied: str) -> None:
    assert tidy(text, terms) == tidied


# Round 3: a direct question written as a statement: one that opens with an auxiliary and its subject or a question
# word and an auxiliary.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("what time is the standup tomorrow", "What time is the standup tomorrow."),
        ("can you send me the deck", "Can you send me the deck."),
        ("where is the invoice", "Where is the invoice."),
    ],
)
def test_round_3_a_question_written_as_a_statement_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # An indirect question, an exclamation and a clause that opens with a question word stay statements.
        ("i wonder if the bank portal is slow today", "I wonder if the bank portal is slow today."),
        ("what a relief", "What a relief!"),
        ("when you get time call me", "When you get time, call me."),
        ("what i mean is we need more time", "What I mean is we need more time."),
    ],
)
def test_round_3_an_indirect_question_may_end_with_a_full_stop(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: an acknowledgement or sign-off the user never said ("Noted:", "Thanks!", a doubled "okay") is a
# reply, wherever it stands; said once, it may stay once.
@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        (
            "sonia wants the invoice revised to ninety nine dollars not ninety dollars",
            "Noted: Sonia wants the invoice revised to $99, not $90.",
            Reject.REPLY,
        ),
        (
            "your appointment is confirmed for thursday thank you for the forms",
            "Your appointment is confirmed for Thursday. Thank you for the forms. Thanks!",
            Reject.REPLY,
        ),
        ("ship the build today", "Done. Ship the build today.", Reject.REPLY),
    ],
)
def test_round_3_an_unsaid_acknowledgement_is_a_reply(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("okay ship it today", "Okay, ship it today."),
        ("send the report thanks", "Send the report. Thanks!"),
        ("is it done", "Is it done?"),
    ],
)
def test_round_3_an_acknowledgement_said_stays(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: "still", "already", "yet", "even", "again" and "exactly" say when or how much.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the ingress logs still show errors", "The ingress logs show errors."),
        ("we already paid the invoice", "We paid the invoice."),
    ],
)
def test_round_3_a_dropped_time_or_degree_word_is_a_qualifier(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.QUALIFIER


def test_round_3_a_qualifier_heard_split_is_that_qualifier() -> None:
    assert check("the smoke test failed a gain", "The smoke test failed again.", []) is None


# Round 3: a correction of a number keeps only the new number; both versions kept around a cue are refused
# whether written as words or digits, or with a determiner before each; and an "or" or "and" never stands in
# for a dropped cue.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the meeting is at three no four", "The meeting is at 3."),
        ("the price is fifty dollars wait sixty dollars", "The price is $50."),
        ("the price is fifty dollars wait sixty dollars", "The price is $50, wait, $60."),
        ("the meeting is at three no four", "The meeting is at 3, no, 4."),
        ("call the plumber no the electrician", "Call the plumber, no, the electrician."),
        ("the meeting is at three no four", "The meeting is at 3 or 4."),
        (
            "we can include the case study actually include the testimonial",
            "We can include the case study and the testimonial.",
        ),
    ],
)
def test_round_3_a_correction_keeps_only_the_new_version(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the meeting is at three no four", "The meeting is at 4."),
        ("the price is fifty dollars wait sixty dollars", "The price is $60."),
        ("call the plumber no the electrician", "Call the electrician."),
        ("book twenty five seats no thirty", "Book 30 seats."),
    ],
)
def test_round_3_a_number_correction_writes_the_new_number(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3 intake (b2): an ordinal written in digits ("12th") before a number cue read as no number and the guardrail
# crashed with an IndexError instead of judging the answer.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("we can send the plan by the 12th i mean the fourteenth", "We can send the plan by the 14th."),
        ("the review is on the 3rd no the fifth", "The review is on the 5th."),
    ],
)
def test_round_3_a_digit_ordinal_put_right_is_judged_without_crashing(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_3_a_digit_ordinal_in_the_prior_put_right_in_the_next_piece_is_judged() -> None:
    prior = "Hi Ms. Rao,\n\nWe can send the revised floor plan by the 12th."
    revised = "Hi Ms. Rao,\n\nWe can send the revised floor plan by the 14th."
    assert judge(prior, "i mean the fourteenth", [], revised, "").rejected is None


# Round 3: right number and address formats the guardrail refused: a day said "the third of march", a range of
# hundreds, a decimal lakh or crore, a phone number's "plus", minutes "to" an hour and "point eight".
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("send it by the third of march", "Send it by March 3."),
        ("the logs were uploaded on the twenty second of november", "The logs were uploaded on November 22."),
        (
            "we will spend between four hundred and five hundred dollars on the stall",
            "We will spend between $400 and $500 on the stall.",
        ),
        ("one lakh fifty thousand rupees", "₹1.5 lakh."),
        ("call me at plus nine one nine eight seven six five four three two one zero", "Call me at +91 98765 43210."),
        ("the call starts at ten to nine", "The call starts at 8:50."),
        (
            "the error rate dropped from two point four percent to point eight percent",
            "The error rate dropped from 2.4% to 0.8%.",
        ),
    ],
)
def test_round_3_dates_ranges_lakhs_phones_and_points_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("send it by the third of march", "Send it by March 4."),
        ("one lakh fifty thousand rupees", "₹1.6 lakh."),
        ("the call starts at ten to nine", "The call starts at 9:10."),
        ("call me at plus nine one nine eight seven", "Call me at +92 987."),
    ],
)
def test_round_3_a_wrong_date_amount_or_time_is_still_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: lists the speech never made: counts that say how many of a thing ("two engineers"), one item alone, a
# lead-in that announces nothing; and an answer that changes the order of what was said (list items, names).
@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        (
            "we hired one designer two engineers and three interns",
            "We hired:\n1. Designer\n2. Engineers\n3. Interns",
            Reject.FALSE_LIST,
        ),
        (
            "my name is ravi i live in pune i like chess",
            "My name is Ravi:\n- I live in Pune\n- I like chess",
            Reject.FALSE_LIST,
        ),
        ("first we need to hire more people", "1. We need to hire more people", Reject.FALSE_LIST),
        (
            "i need three things first milk second eggs third bread",
            "I need three things:\n1. Milk\n2. Bread\n3. Eggs",
            Reject.REORDERED,
        ),
        ("book a train from paris to lyon", "Book a train from Lyon to Paris.", Reject.REORDERED),
        ("priya will send the invoice to rohan", "Rohan will send the invoice to Priya.", Reject.REORDERED),
    ],
)
def test_round_3_a_list_never_made_or_an_order_changed_is_refused(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("One, milk. Two, eggs. Three, bread.", "1. Milk\n2. Eggs\n3. Bread"),
        (
            "some notes for the offsite we leave friday the venue has a pool we return sunday",
            "Some notes for the offsite:\n- We leave Friday\n- The venue has a pool\n- We return Sunday",
        ),
        ("so what workflows will be", "So, what will the workflows be?"),
    ],
)
def test_round_3_counted_or_announced_lists_and_moved_grammar_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: wrong formats the guardrail let through: a time, a currency, a decimal or a ratio changed, an address
# cut or changed, an "@" in prose, a spoken mark kept as words, a paragraph break said and dropped.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("meet on may twenty ninth at two pm", "Meet on May 29 at 9 pm."),
        ("the shop opens at quarter to five", "The shop opens at 4:15."),
        ("the fee is twenty euros", "The fee is $20."),
        ("the fee is twenty pounds", "The fee is ₹20."),
        ("growth was three point zero five percent", "Growth was 3.5%."),
        ("the ratio is three to one", "The ratio is 1:3."),
        ("mail priya dot sharma at acme dot co dot in", "Mail priya.sharma@acme.co."),
        ("mail priya underscore k at acme dot com", "Mail priya-k@acme.com."),
        ("open acme dot com slash pricing slash teams", "Open acme.com/teams/pricing."),
        ("we met at the office", "We met @ the office."),
        ("are you coming question mark", "Are you coming question mark?"),
        ("first line new paragraph second line", "First line. Second line."),
    ],
)
def test_round_3_a_changed_format_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("meet on may twenty ninth at two pm", "Meet on May 29 at 2 pm."),
        ("the shop opens at quarter to five", "The shop opens at 4:45."),
        ("the fee is twenty euros", "The fee is €20."),
        ("growth was three point zero five percent", "Growth was 3.05%."),
        ("the ratio is three to one", "The ratio is 3:1."),
        ("mail priya dot sharma at acme dot co dot in", "Mail priya.sharma@acme.co.in."),
        ("mail priya underscore k at acme dot com", "Mail priya_k@acme.com."),
        ("open acme dot com slash pricing slash teams", "Open acme.com/pricing/teams."),
        ("the endpoint is slash users slash id and it returns json", "The endpoint is /users/:id and it returns JSON."),
        ("first line new paragraph second line", "First line.\n\nSecond line."),
    ],
)
def test_round_3_the_right_format_passes(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: a sound-alike written where the right word was said: a grammar word after "the" ("the whether"), "except"
# where a verb goes ("will except"), a term after "the" or a subject ("the Lyft is broken", "I Reddit"); a strong cue
# dropped with both versions kept.
@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("the weather is nice today", "The whether is nice today.", []),
        ("i will accept the job", "I will except the job.", []),
        ("the lift is broken take the stairs", "The Lyft is broken, take the stairs.", ["Lyft"]),
        ("i read it yesterday", "I Reddit yesterday.", ["Reddit"]),
        ("approve it scratch that reject it", "Approve it. Reject it.", []),
    ],
)
def test_round_3_an_over_fix_a_lost_question_or_both_versions_are_refused(
    said: str, wrote: str, terms: list[str]
) -> None:
    assert check(said, wrote, terms) is not None


@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("check weather it works", "Check whether it works.", []),
        ("please except the offer by friday", "Please accept the offer by Friday.", []),
        ("book a ride on lift tonight", "Book a ride on Lyft tonight.", ["Lyft"]),
        ("we pay invoices through zero", "We pay invoices through Xero.", ["Xero"]),
        ("ask procall for the quote", "Ask Procol for the quote.", ["Procol"]),
        ("approve it scratch that reject it", "Reject it.", []),
        ("the travel in sure ance covers lost baggage", "The travel insurance covers lost baggage.", []),
        ("call shivon in lester", "Call Siobhan in Leicester.", ["Siobhan", "Leicester"]),
    ],
)
def test_round_3_a_fix_where_the_said_word_is_wrong_passes(said: str, wrote: str, terms: list[str]) -> None:
    assert check(said, wrote, terms) is None


# Round b1: a word the model respelled to another accepted spelling of the same word ("ok" "okay") is written as the
# speaker said it (rule 6), so it is never refused as dropped or unheard.
@pytest.mark.parametrize(
    ("said", "wrote", "written"),
    [
        ("ok so we ship friday", "Okay, so we ship Friday.", "Ok, so we ship Friday."),
    ],
)
def test_round_b1_a_respelled_word_is_written_as_said(said: str, wrote: str, written: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == written


# Round b1: a joining word that ends the whole speech with nothing after it ("... use it. But") drops,
# and the sentence it hung on gets its full stop (founder, 11 Oct).
@pytest.mark.parametrize(
    ("said", "wrote", "written"),
    [
        (
            "pack the charger and the cables. And",
            "Pack the charger and the cables. And",
            "Pack the charger and the cables.",
        ),
        (
            "send the invoice to the client today. So",
            "Send the invoice to the client today. So",
            "Send the invoice to the client today.",
        ),
    ],
)
def test_round_b1_a_dangling_final_joining_word_drops(said: str, wrote: str, written: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == written


# A subject said alone after a finished sentence, with nothing after it, starts nothing: the speaker stopped, or the
# recognizer heard it in the silence at the end. It drops in any chunk, kept by the model or not (founder, 14 Oct).
THREE_THINGS = (
    "I mean, like, whatever may be. Three things are going to be there, like. Gym then there is work and then there "
    "is going to be a personal project I."
)
THREE_THINGS_LISTED = (
    "I mean, like, whatever may be. Three things are going to be there, like:\n- Gym\n- There is work\n"
    "- There is going to be a personal project"
)
ASKED_ON_WEEKENDS = "They asked you What do you do outside work, and what do you do on weekends? You"
POLISHED_ON_WEEKENDS = "They asked you: What do you do outside work, and what do you do on weekends?"


@pytest.mark.parametrize(
    ("said", "wrote", "written"),
    [
        (ASKED_ON_WEEKENDS, f"{POLISHED_ON_WEEKENDS} You.", POLISHED_ON_WEEKENDS),
        (ASKED_ON_WEEKENDS, POLISHED_ON_WEEKENDS, POLISHED_ON_WEEKENDS),
        # "I" or "we" can't close a clause after a noun: it opened one the speaker never said.
        (THREE_THINGS, f"{THREE_THINGS_LISTED} I", THREE_THINGS_LISTED),
        ("send the deck to priya we", "Send the deck to Priya we.", "Send the deck to Priya."),
    ],
)
def test_a_lone_subject_ending_the_speech_drops(said: str, wrote: str, written: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == written


# A subject that answers "who", is asked, or ends a sentence it belongs to was meant.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("who is presenting first? you", "Who is presenting first? You."),
        ("is everyone ready? you?", "Is everyone ready? You?"),
        ("i will miss you", "I will miss you."),
        ("thank you", "Thank you."),
        ("she runs faster than i", "She runs faster than I."),
        ("the trip is for you and i", "The trip is for you and I."),
        ("rahul likes tea and so do i", "Rahul likes tea, and so do I."),
    ],
)
def test_a_subject_that_was_meant_stays(said: str, wrote: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.text == wrote


def test_speech_typed_back_with_no_mark_or_capital_is_unpolished() -> None:
    said = "thanks for the update ill review it tonight"
    judged = judge("", said, [], None, said)
    assert (judged.text, judged.rejected) == (said, Reject.UNPOLISHED)


def test_speech_given_its_marks_and_capitals_is_polished() -> None:
    said = "thanks for the update ill review it tonight"
    wrote = "Thanks for the update, I'll review it tonight."
    judged = judge("", said, [], None, wrote)
    assert (judged.text, judged.rejected) == (wrote, None)


# A plural noun that names the items, then the items, announces a list as "the following" does (founder, 14 Oct).
def test_a_noun_naming_the_items_announces_a_list() -> None:
    said = (
        "Like, there are going to be only the priorities. The only priorities that are going to be there are "
        "Personal project whenever I get time. Gym and then the walk. That's it."
    )
    wrote = (
        "Like, there are going to be only the priorities. The only priorities that are going to be there are:\n"
        "- Personal project whenever I get time\n- Gym\n- The walk\nThat's it."
    )
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == wrote


# "so" and "then" end a sentence too ("I told him so"): only after a full stop are they left dangling.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("salt and pepper", "Salt and pepper."),
        ("i told him so", "I told him so."),
        ("who are you sending it to", "Who are you sending it to?"),
        ("their team will call us then", "Their team will call us then."),
        # A question tag ends on its subject (R7 suite-growth-en-02231, 15 Oct).
        ("we are still on for lunch aren't we", "We are still on for lunch, aren't we?"),
        ("they booked the venue didn't they", "They booked the venue, didn't they?"),
    ],
)
def test_round_b1_a_word_that_ends_a_sentence_stays(said: str, wrote: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.text == wrote


# Round b1: "slash" said as a verb after its subject or a modal is never a path's "/".
@pytest.mark.parametrize(
    ("said", "wrote"),
    [("we slash cloud spend", "We /cloud spend."), ("diego should slash replicas", "Diego should /replicas.")],
)
def test_round_b1_slash_as_a_verb_is_never_a_path(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.FORMAT


# Round b2 fix (11 Oct): an accepted answer's list and marks are put in shape without a model: a bullet after
# numbered items takes the next number, a first item glued to the line before goes on its own line, the words
# before a list end with a colon, counting words left in a numbered item go, a "?" the speaker said on an item or
# the piece's end comes back.
@pytest.mark.parametrize(
    ("prior", "said", "wrote", "shaped"),
    [
        (
            "",
            "Okay i need to do the following first turn off the tv second Turn off the lamps. Keep my laptop in a "
            "place where I cannot use it. But",
            "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps\n- Keep my laptop in a "
            "place where I cannot use it.",
            "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps\n3. Keep my laptop in a "
            "place where I cannot use it.",
        ),
        (
            "",
            "Ok, so this is just a simple test that I am running. First it needs to be an audit set second I want "
            "to buy a chair. Third lets get eggs for the breakfast. It's it's finished finished.",
            "Ok, so this is just a simple test that I am running. 1. It needs to be an audit set\n2. I want to buy "
            "a chair\n3. Let's get eggs for the breakfast\nIt's finished.",
            "Ok, so this is just a simple test that I am running:\n1. It needs to be an audit set\n2. I want to buy "
            "a chair\n3. Let's get eggs for the breakfast\nIt's finished.",
        ),
        (
            "",
            "we packed three bags first the tent second the stove third the food",
            "We packed three bags.\n1. The tent\n2. The stove\n3. The food",
            "We packed three bags:\n1. The tent\n2. The stove\n3. The food",
        ),
        (
            "",
            "I want to know a few things. First, can you tell me how it works? Second, can I add this? Is over "
            "here. Third, how well will it perform with the long text?",
            "I want to know a few things:\n1. First, can you tell me how it works?\n2. Second, can I add this? It's "
            "over here\n3. Third, how well will it perform with the long text?",
            "I want to know a few things:\n1. Can you tell me how it works?\n2. Can I add this? It's over here\n3. "
            "How well will it perform with the long text?",
        ),
        (
            "So these are the things that I need to test:\n1. Is it going to show bullet points\n2. How well is it "
            "going to look",
            "and How is it going to show me the data?",
            "and how is it going to show me the data",
            "and how is it going to show me the data?",
        ),
    ],
)
def test_round_b2_an_accepted_answer_is_put_in_shape(prior: str, said: str, wrote: str, shaped: str) -> None:
    judged = judge(prior, said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == shaped


@pytest.mark.parametrize(
    "wrote",
    [
        "Steps:\n1. First aid kit\n2. Second hand books",
        "Is this right?\n- Yes",
        "Done.\n\nNext we eat.",
    ],
)
def test_round_b2_shaping_leaves_a_right_answer_alone(wrote: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped

    assert shaped("", wrote) == wrote


def test_round_b2_a_refused_piece_keeps_its_joining_word_in_lowercase() -> None:
    prior = "So these are the things:\n1. Is it fast\n2. How well is it going to look"
    judged = judge(
        prior, "and How is it going to show me the data?", [], None, "And how is it going to show me the charts?"
    )
    assert judged.rejected == Reject.UNHEARD
    assert judged.text == "and how is it going to show me the data?"


# Round b2 fix: two refusals put right without a model. A piece going on with a sentence that came back as a new list
# item or without its joining word gets the word back, in lowercase; a paragraph said but run on gets its blank
# line after the sentence the speaker said it after.
@pytest.mark.parametrize(
    ("prior", "said", "wrote", "fixed"),
    [
        (
            "So these are the things:\n1. Is it fast\n2. How well is it going to look",
            "and How is it going to show me the data?",
            "3. how is it going to show me the data?",
            "and how is it going to show me the data?",
        ),
        (
            "So these are the things:\n1. Is it fast\n2. How well is it going to look",
            "and How is it going to show me the data?",
            "3. and how is it going to show me the data?",
            "and how is it going to show me the data?",
        ),
        (
            "",
            "the trip was great new paragraph next year we want to go to japan",
            "The trip was great.\nNext year we want to go to Japan.",
            "The trip was great.\n\nNext year we want to go to Japan.",
        ),
        # Round b5: a run-on split by dropping its joining word gets the word back opening the sentence.
        (
            "",
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue. It will pick the next job.",
            "It will be a simple queue. And it will pick the next job.",
        ),
        (
            "",
            "we met the client today and they liked it new paragraph the next call is on friday",
            "We met the client today, and they liked it. The next call is on Friday.",
            "We met the client today, and they liked it.\n\nThe next call is on Friday.",
        ),
    ],
)
def test_round_b2_a_refusal_with_a_known_repair_is_put_right(prior: str, said: str, wrote: str, fixed: str) -> None:
    from snapbak_backend.schemas.polish import PolishRequest
    from snapbak_backend.transformers.polish import fold, read

    fields = {
        "text": said,
        "prior": prior,
        "terms": [],
        "app": "",
        "style": "chat",
        "deadline_ms": 20000,
    }
    request = PolishRequest.model_validate(fields)
    assert judge(prior, said, [], None, wrote).rejected is not None
    judged = read(request, fold(request), json.dumps({"revision": None, "text": wrote}))
    assert judged.rejected is None
    assert judged.text == fixed


# Round b2 fix: refused now. A short dictation's "then" dropped; a spoken number written as its word and its digit
# ("two 2-liter"); a revision that changes the prior's words though the piece takes nothing back.
@pytest.mark.parametrize(
    ("said", "wrote", "rule"),
    [
        ("see you then", "See you.", Reject.DROPPED_WORD),
        ("buy two liter bottles of oil", "Buy two 2-liter bottles of oil.", Reject.UNHEARD),
    ],
)
def test_round_b2_a_wrong_answer_is_refused(said: str, wrote: str, rule: Reject) -> None:
    assert check(said, wrote, []) == rule


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("we left. and", "We left."),
        ("buy two liter bottles of oil", "Buy 2 liter bottles of oil."),
        ("mail rahul at meera dot in", "Mail rahul@meera.in."),
    ],
)
def test_round_b2_a_right_answer_is_still_accepted(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round b2 fix: a blank line the speaker never asked for runs on, unless it sets off an email's greeting or sign-off;
# before a list it is one line break.
@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        (
            "hi my name is meera here are a few things about me i paint i cook",
            "Hi, my name is Meera.\n\nHere are a few things about me:\n- I paint\n- I cook",
            "Hi, my name is Meera. Here are a few things about me:\n- I paint\n- I cook",
        ),
        (
            "here are a few things about me i paint i cook",
            "Here are a few things about me:\n\n- I paint\n- I cook",
            "Here are a few things about me:\n- I paint\n- I cook",
        ),
        (
            "hi sam the deck is ready thanks meera",
            "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nMeera",
            "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nMeera",
        ),
        (
            "the trip was great new paragraph next year we go to japan",
            "The trip was great.\n\nNext year we go to Japan.",
            "The trip was great.\n\nNext year we go to Japan.",
        ),
    ],
)
def test_round_b2_an_unasked_paragraph_break_runs_on(said: str, wrote: str, shaped: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped as shape

    assert shape(said, wrote) == shaped


def test_round_b2_an_accepted_piece_lowercases_a_capital_the_recognizer_put_after_its_joining_word() -> None:
    prior = "So these are the things:\n1. Is it fast\n2. How well is it going to look"
    said = "and How is it going to show me the data?"
    judged = judge(prior, said, [], None, said)
    assert judged.text == "and how is it going to show me the data?"


def test_round_b3_a_number_out_of_order_names_only_that_number() -> None:
    found = verdict("send fifteen boxes and twenty bags today", "Send 20 boxes and 15 bags today.", [])
    assert found == Reason(Reject.UNHEARD, ("15",))


def test_round_b3_a_correction_that_says_the_negated_verb_again_takes_the_negation_back() -> None:
    # backtracking-500: the cue takes back the whole "don't send ..." clause by saying "send" again.
    prior = "don't send the invoice to the client yet"
    accepted = judge(prior, "no wait send it today", [], "", "Send the invoice to the client today.")
    assert accepted.rejected is None
    # A correction that restates only the object keeps the negation.
    kept = verdict("don't send it to Sam no wait to Ravi", "Send it to Ravi.", [])
    assert kept is not None and kept.rule == Reject.NEGATION


@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        (
            "Tom asked colon are we keeping the launch on Friday question mark period",
            "Tom asked: Are we keeping the launch on Friday?.",
            "Tom asked: Are we keeping the launch on Friday?",
        ),
        ("wait for it", "Wait for it...", "Wait for it..."),
        (
            "hi ana the invoice is ready cheers tom",
            "Hi Ana,\n\nThe invoice is ready.\n\nCheers,\nTom.",
            "Hi Ana,\n\nThe invoice is ready.\n\nCheers,\nTom",
        ),
    ],
)
def test_round_b3_stacked_end_marks_collapse_and_a_sign_off_name_takes_no_full_stop(
    said: str, wrote: str, shaped: str
) -> None:
    assert judge("", said, [], None, wrote).text == shaped


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A sound-alike written in its plural is still the sound-alike.
        ("the mechanic tightened both break on my car", "The mechanic tightened both brakes on my car."),
        # A compound written with a hyphen sounds as its words joined.
        ("brake glass access was not used", "Break-glass access was not used."),
    ],
)
def test_round_b4_a_sound_alike_may_take_a_plural_or_a_hyphen(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_b4_a_restated_sentence_may_keep_the_nouns_its_pronouns_stand_for() -> None:
    # backtracking-386: "give it to her" after the cue restates "give the medicine to Grandma"; the gold's own answer
    # keeps the nouns and the new time, and was refused twice, so the raw transcript shipped.
    said = "Give the medicine to Grandma after lunch, scratch that, give it to her before lunch."
    assert check(said, "Give the medicine to Grandma before lunch.", []) is None
    assert check(said, "Give it to her before lunch.", []) is None
    assert check(said, "Give the medicine to Grandma after lunch.", []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # suite-growth-en-00375: a name said once comes back as an invented sign-off.
        (
            "hi ms patel my son aarav was absent on tuesday thank you for your help",
            "Hi Ms. Patel,\n\nMy son Aarav was absent on Tuesday. Thank you for your help.\n\nAarav",
        ),
        # v2-backtracking-en-00013: a count said once is written twice.
        ("please include two liter bottles of olive oil", "Please include two two-liter bottles of olive oil."),
    ],
)
def test_round_b4_a_name_or_number_written_more_often_than_said_is_unheard(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.UNHEARD


def test_round_b4_a_question_mark_inside_a_closing_quote_takes_no_full_stop_after_it() -> None:
    # v2-numbers-en-00323: "?”." stacks two end marks.
    said = "Joaquin asked should I keep the old one"
    judged = judge("", said, [], None, "Joaquin asked, “Should I keep the old one?”.")
    assert judged.text == "Joaquin asked, “Should I keep the old one?”"


def test_round_b4_a_piece_with_no_correction_leaves_the_prior_as_typed() -> None:
    # founder-016: the prior was already typed; a piece that takes nothing back may not rewrite it.
    prior = "Things to test:\n1. Is it going to present the list in bullet points\n2. How well is it going to look"
    judged = judge(
        prior,
        "and how is it going to show me the data?",
        [],
        prior.replace("points", "points?"),
        "and how is it going to show me the data?",
    )
    assert judged.revision is None
    assert judged.rejected is None


ANNOUNCED = "Hi, I am Ravi. Here are a few things about my team. We ship on Fridays. We review code daily."


@pytest.mark.parametrize(
    ("said", "wrote", "rule"),
    [
        # founder-019, round b4: the recognizer's own stops after an announcement kept the statements as prose in
        # every run (D5).
        (ANNOUNCED, ANNOUNCED, Reject.UNLISTED),
        (
            ANNOUNCED,
            "Hi, I am Ravi. Here are a few things about my team:\n- We ship on Fridays\n- We review code daily",
            None,
        ),
        # One statement after it, or speech with no stops to count sentences by, is left to the model.
        (
            "Here are a few things about my team. We ship on Fridays.",
            "Here are a few things about my team. We ship on Fridays.",
            None,
        ),
        (
            "here are a few things about my team we ship on fridays we review code daily",
            "Here are a few things about my team. We ship on Fridays. We review code daily.",
            None,
        ),
        # Questions after it are asked, never listed.
        (
            "Here are a few things about the trip. Can you book it? Is it cheap?",
            "Here are a few things about the trip. Can you book it? Is it cheap?",
            None,
        ),
    ],
)
def test_round_b4_announced_statements_the_recognizer_stopped_are_a_list(
    said: str, wrote: str, rule: Reject | None
) -> None:
    assert check(said, wrote, []) == rule


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-numbers-en-00242: a decimal's two digits are one number, not a count written twice.
        (
            "keep the pipe length between two point two and two point four meters",
            "Keep the pipe length between 2.2 and 2.4 meters.",
        ),
        # A count that ends one sentence and opens the next was said twice.
        ("we need two two of them are broken", "We need two. Two of them are broken."),
    ],
)
def test_round_b4_a_count_beside_a_mark_is_not_written_twice(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("prior", "text", "revision"),
    [
        ("Should we meet on Friday", "question mark", "Should we meet on Friday?"),
        ("We met on Friday", "full stop", "We met on Friday."),
    ],
)
def test_round_b4_a_piece_that_says_a_mark_may_put_it_on_the_prior(prior: str, text: str, revision: str) -> None:
    judged = judge(prior, text, [], revision, "")
    assert judged.rejected is None
    assert judged.revision == revision


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # Round b5 (founder-022): a run-on split into sentences lost its spoken "and".
        (
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue. It will pick the next job.",
        ),
        ("the build failed so we rolled back", "The build failed. We rolled back."),
    ],
)
def test_round_b5_a_joining_word_dropped_at_a_sentence_split_is_refused(said: str, wrote: str) -> None:
    reason = verdict(said, wrote, [])
    assert reason is not None
    assert reason.rule == Reject.DROPPED_WORD


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        (
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue, and it will pick the next job.",
        ),
        # A joining word a correction takes back goes with it, and one said twice in a row is a stutter (founder-022).
        ("book the hall and no wait book the room", "Book the room."),
        ("it should run and And it should stop", "It should run. And it should stop."),
    ],
)
def test_round_b5_a_joining_word_kept_at_a_sentence_split_is_accepted(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_b5_words_put_on_lines_of_their_own_with_no_list_asked_are_a_false_list() -> None:
    # v2-punctuation-en-00034: a four-word run-on came back one word per line.
    assert check("tom cooks lisa cleans", "Tom\nCooks\nLisa\nCleans", []) == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("milk new line eggs new line bread", "Milk\nEggs\nBread"),
        ("hi sam the deck is ready thanks ravi", "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nRavi"),
    ],
)
def test_round_b5_lines_said_or_an_email_shape_are_no_false_list(said: str, wrote: str) -> None:
    assert check(said, wrote, []) != Reject.FALSE_LIST


def test_a_sentence_written_more_often_than_said_is_refused() -> None:
    said = "can you resend the file"
    assert check(said, "Can you resend the file? Can you resend the file?", []) == Reject.REPEATED


def test_a_sentence_said_twice_may_be_written_twice() -> None:
    said = "can you resend the file can you resend the file"
    assert check(said, "Can you resend the file? Can you resend the file?", []) is None


def test_round_b5_too_many_changes_names_no_number_written_as_digits() -> None:
    # Round b5: the refusal named every number word written as digits, hiding the moved words.
    said = "send the twelve invoices to priya and the three receipts to amit by friday"
    reason = verdict(said, "Send priya 12 invoices and amit 3 receipts by Friday.", [])
    assert reason is not None
    assert reason.rule == Reject.TOO_MANY_CHANGES
    assert {"twelve", "three"}.isdisjoint(reason.words)
    assert "invoices" in reason.words


def read_answer(said: str, wrote: str, terms: list[str] | None = None, prior: str = "") -> Polished:
    from snapbak_backend.schemas.polish import PolishRequest
    from snapbak_backend.transformers.polish import fold, read

    fields = {"text": said, "prior": prior, "terms": terms or [], "app": "", "style": "chat"}
    request = PolishRequest.model_validate({**fields, "deadline_ms": 20000})
    answer = json.dumps({"revision": None, "text": wrote}, ensure_ascii=False)
    return read(request, fold(request), answer)


def test_round_b6_a_direct_question_left_with_a_full_stop_is_mended_without_a_model() -> None:
    # formatting-214: the first answer had every word right and only lacked its "?"; the correction echoed the
    # checker's note and the text went in as said.
    said = "can you polish this for ethan and mention the discount applies for five days only"
    wrote = "Can you polish this for Ethan and mention the discount applies for 5 days only."
    polished = read_answer(said, wrote, ["Ethan"])
    assert (polished.rejected, polished.text) == (None, wrote[:-1] + "?")


def test_round_b6_announced_statements_written_as_prose_are_listed_without_a_model() -> None:
    # founder-019 run 2: the words were right (Saksham, Procol) and only the D5 list was missing.
    said = (
        "Hi, my name is Aksham. Here are a few things about me. I'm a 27-year-old software engineer. I work at a "
        "company called Procall. I am leaving Procall on Monday and I'll be joining a new company called Hello Council."
    )
    wrote = (
        "Hi, my name is Saksham. Here are a few things about me. I'm a 27-year-old software engineer. I work at a "
        "company called Procol. I am leaving Procol on Monday and I'll be joining a new company called Hello Council."
    )
    listed = (
        "Hi, my name is Saksham. Here are a few things about me:\n- I'm a 27-year-old software engineer\n"
        "- I work at a company called Procol\n"
        "- I am leaving Procol on Monday and I'll be joining a new company called Hello Council"
    )
    polished = read_answer(said, wrote, ["Procol", "Saksham"])
    assert (polished.rejected, polished.text) == (None, listed)


def test_round_b6_a_list_counted_off_but_bulleted_is_numbered_without_a_model() -> None:
    said = "first call the bank second pay the rent third book the cab"
    polished = read_answer(said, "- Call the bank\n- Pay the rent\n- Book the cab")
    assert (polished.rejected, polished.text) == (None, "1. Call the bank\n2. Pay the rent\n3. Book the cab")


def test_round_b6_a_mended_answer_that_breaks_another_rule_stays_refused() -> None:
    said = "can you polish this for ethan"
    polished = read_answer(said, "Can you polish this for Ethan today.", ["Ethan"])
    assert polished.rejected == Reject.UNHEARD


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-lists-en-00018: "do one thing," then two steps joined by "then" stays one sentence.
        (
            "Dad please check Nia's backpack before dinner, her library card is probably in the side pocket. Do one "
            "thing, read the chapter with her, then sign the reading log. The teacher wants it on the blue folder",
            "Dad, please check Nia's backpack before dinner. Her library card is probably in the side pocket. Do one "
            "thing:\n- Read the chapter with her\n- Sign the reading log\nThe teacher wants it on the blue folder.",
        ),
    ],
)
def test_round_b6_two_steps_after_do_one_thing_with_a_comma_are_no_list(said: str, wrote: str) -> None:
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "terms", "wrote", "kept"),
    [
        # suite-growth-en-05292 and -03338: a term that is also a plain word, after a describing word.
        (
            "please pay the electricity bill before friday",
            ["Bill"],
            "Please pay the electricity Bill before Friday.",
            "Please pay the electricity bill before Friday.",
        ),
        ("this yoga asana is hard on the knees", ["Asana"], "This yoga Asana is hard on the knees.", None),
        # Where the term names the thing, it keeps its capital.
        ("ask bill to send the invoice", ["Bill"], "Ask Bill to send the invoice.", "Ask Bill to send the invoice."),
        ("put the task in asana", ["Asana"], "Put the task in Asana.", "Put the task in Asana."),
    ],
)
def test_round_b6_a_term_twin_after_a_describing_word_stays_the_plain_word(
    said: str, terms: list[str], wrote: str, kept: str | None
) -> None:
    polished = judge("", said, terms, None, wrote)
    assert polished.rejected is None
    assert polished.text == (kept or "This yoga asana is hard on the knees.")


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-stt-homophones-en-00006 and v2-punctuation-en-00014: a short dictation broken onto lines.
        ("signed buy Patel", "Signed by\nPatel"),
        ("thanks proposal attached", "Thanks.\n\nProposal attached."),
    ],
)
def test_round_b6_a_short_dictation_gets_no_line_break_never_said(said: str, wrote: str) -> None:
    assert judge("", said, [], None, wrote).rejected == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("hi sam see you tomorrow", "Hi Sam,\nSee you tomorrow."),
        ("thanks a lot cheers tom", "Thanks a lot.\n\nCheers,\nTom"),
    ],
)
def test_round_b6_a_short_greeting_or_sign_off_keeps_its_line(said: str, wrote: str) -> None:
    assert judge("", said, [], None, wrote).text == wrote


def test_round_b6_an_address_left_half_spoken_is_refused() -> None:
    # formatting-070: "dot" became "." but "at" stayed a word.
    found = verdict(
        "Can Priya send the revised agenda to Sam at acme dot io question mark",
        "Can Priya send the revised agenda to Sam at acme.io?",
        [],
    )
    assert found is not None and found.rule == Reject.FORMAT


@pytest.mark.parametrize(
    ("prior", "text", "wrote"),
    [
        # v2-backtracking-en-00114: a piece that opens with a
        # cue, answered with the cue still between the old and the new version.
        (
            "doctor chen asked me to confirm my follow up for next friday at three fifteen",
            "i mean at eight forty five because the lab closes early that day",
            "Doctor Chen asked me to confirm my follow up for next Friday at 3:15. I mean, at 8:45, because the lab "
            "closes early that day.",
        ),
    ],
)
def test_round_b6_a_cue_opening_a_piece_kept_in_the_answer_is_refused(prior: str, text: str, wrote: str) -> None:
    polished = read_answer(text, wrote, prior=prior)
    assert polished.rejected == Reject.KEPT_CORRECTION


@pytest.mark.parametrize(
    ("prior", "text", "wrote"),
    [
        # v2-backtracking-en-00082: an apology opening a piece stays.
        (
            "I dropped off Aarav at the clinic.",
            "Sorry for the confusion at check in the nurse said to wait for the lab slip",
            "I dropped off Aarav at the clinic. Sorry for the confusion at check-in. The nurse said to wait for the "
            "lab slip.",
        ),
        (
            "we should finish the report",
            "wait for the numbers from finance",
            "We should finish the report. Wait for the numbers from finance.",
        ),
    ],
)
def test_round_b6_a_plain_cue_word_opening_a_piece_stays(prior: str, text: str, wrote: str) -> None:
    assert read_answer(text, wrote, prior=prior).rejected is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # founder-003: three steps after "do one thing", joined by "then" across a full stop, make a list.
        (
            "Let's do one thing, call archana, then we'll talk to Arun. Then we can talk to Reliance team in the "
            "entire.",
            "Let's do one thing: call Archana, then we'll talk to Arun. Then we can talk to Reliance team in the "
            "entire.",
        ),
    ],
)
def test_round_b6_three_steps_after_do_one_thing_written_as_prose_are_refused(said: str, wrote: str) -> None:
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.UNLISTED


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("do one thing call priya then send her the file", "Do one thing: call Priya, then send her the file."),
    ],
)
def test_round_b6_two_steps_or_a_step_on_what_someone_says_stay_prose(said: str, wrote: str) -> None:
    assert verdict(said, wrote, []) is None


def test_round_b6_a_said_sentence_glued_into_a_list_item_is_refused() -> None:
    # founder-021 (round b6 A/B): "Keep my laptop ..." was run into item 2.
    said = (
        "Okay i need to do the following first turn off the tv second Turn off the lamps. Keep my laptop in a place "
        "where I cannot use it. But"
    )
    wrote = (
        "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps Keep my laptop in a place where "
        "I cannot use it."
    )
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.DROPPED_ITEM


def test_round_b6_asking_someone_what_to_do_is_no_direct_question() -> None:
    # founder-006 (round b6 A/B): the recognizer's "?" kept on an indirect question; mended to a full stop.
    said = (
        "Ishaan do 1 thing. Get in touch with Aran directly for this and whatever he says then call archana. Then you "
        "can ask Pankaj John what to do and what not to do?"
    )
    wrote = (
        "Ishaan, do 1 thing. Get in touch with Aran directly for this, and whatever he says, then call Archana. Then "
        "you can ask Pankaj John what to do and what not to do?"
    )
    polished = read_answer(said, wrote)
    assert (polished.rejected, polished.text) == (None, wrote[:-1] + ".")
    assert verdict("can you ask him what to do", "Can you ask him what to do?", []) is None


def test_here_is_what_we_do_announces_the_steps_after_it() -> None:
    # v2-lists-en-00053: "here's what we do" leads the steps in, as "the following" does (14 Oct, English only).
    said = "here's what we do reset the link then resend the label then ask her to test it"
    wrote = "Here's what we do:\n- Reset the link\n- Resend the label\n- Ask her to test it"
    assert check(said, wrote, []) is None


# Answers prompt rules 3, 5 and 6 call right, which the guardrail refused (14 Oct false-rejection audit): each refusal
# typed the raw speech for a correct polish.
RULE_RIGHT = [
    # An irregular verb said regularized, its last consonant doubled ("runned" ran).
    ("the kid runned to the bus", "The kid ran to the bus."),
    ("i forgetted the keys", "I forgot the keys."),
    ("we swimmed in the lake", "We swam in the lake."),
    ("she singed a song", "She sang a song."),
    # A past written the same as its base ("readed" read).
    ("i readed the doc", "I read the doc."),
    ("we cutted the cost", "We cut the cost."),
    # A pronoun in the object case opening a clause is its subject ("him is" he is), and one after a preposition the
    # other way ("to he" to him).
    ("him is the new lead", "He is the new lead."),
    ("her is coming tomorrow", "She is coming tomorrow."),
    ("them are late again", "They are late again."),
    ("us need more time", "We need more time."),
    ("give it to he today", "Give it to him today."),
    ("me and him went to the store", "He and I went to the store."),
    ("her and i fixed it", "She and I fixed it."),
    ("between you and i it is late", "Between you and me, it is late."),
    ("there going home", "They're going home."),
    ("your welcome", "You're welcome."),
    # A sound-alike before a time word, a clock time, an irregular plural, or a plural ("few ours" hours).
    ("we went their yesterday", "We went there yesterday."),
    ("i will see you at for", "I will see you at four."),
    ("we need too people", "We need two people."),
    ("ice packs help in the first few ours", "Ice packs help in the first few hours."),
    # A plural noun that names the items announces them ("the blockers are"), also with a "like" before the first.
    (
        "the blockers are the api is down then the tests are flaky then the designer is out",
        "The blockers are:\n- The API is down\n- The tests are flaky\n- The designer is out",
    ),
    (
        "the blockers are like the api is down then the tests are flaky",
        "The blockers are:\n- The API is down\n- The tests are flaky",
    ),
]


@pytest.mark.parametrize(("said", "wrote"), RULE_RIGHT)
def test_answers_the_prompt_rules_call_right_pass(said: str, wrote: str) -> None:
    assert verdict(said, wrote, []) is None


# The same fixes reaching past what the rule allows still fail: another verb, another gender, a homophone where the
# said word is right, a list never announced, a "like" dropped from inside an item.
RULE_WRONG = [
    ("the kid runned to the bus", "The kid walked to the bus.", Reject.UNHEARD),
    ("we swam in the lake", "We swim in the lake.", Reject.TENSE),
    ("i readed the doc", "I wrote the doc.", Reject.UNHEARD),
    ("him is the new lead", "She is the new lead.", Reject.UNHEARD),
    ("send it to their office", "Send it to there office.", Reject.UNHEARD),
    ("i want to go", "I want two go.", Reject.UNHEARD),
    ("i woke up then made coffee", "I woke up:\n- Made coffee", Reject.FALSE_LIST),
    ("we woke up then the kids made coffee", "We woke:\n- Up\n- The kids made coffee", Reject.FALSE_LIST),
    (
        "the blockers are the api is down then i like the tests",
        "The blockers are:\n- The API is down\n- I the tests",
        Reject.DROPPED_WORD,
    ),
]


@pytest.mark.parametrize(("said", "wrote", "reason"), RULE_WRONG)
def test_fixes_past_what_the_rule_allows_still_fail(said: str, wrote: str, reason: Reject) -> None:
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == reason


# A pronoun written for a said one of another person, number or gender changes who did it, though the new one was
# said elsewhere (14 Oct swap audit: "we" for "they" passed 89 % of the time). Only its case may change.
PRONOUN_SWAPS = [
    (
        "we should wait until the finance team closes the books",
        "They should wait until the finance team closes the books.",
        "they",
    ),
    (
        "please send the renewal reminder before you leave",
        "Please send the renewal reminder before they leave.",
        "they",
    ),
    ("him is the new lead", "They are the new lead.", "they"),
    (
        "i think we should ship it on friday because the team is ready",
        "I think they should ship it on Friday because the team is ready.",
        "they",
    ),
    (
        "our designers and their engineers meet on monday",
        "Their designers and their engineers meet on Monday.",
        "their",
    ),
    (
        "can you send me the deck so the team can review it with us",
        "Can you send us the deck so the team can review it with us?",
        "us",
    ),
    (
        "here are the next steps one i call the vendor two we sign the lease three they move in",
        "Here are the next steps:\n1. I call the vendor\n2. They sign the lease\n3. They move in",
        "they",
    ),
    (
        "we can ship the segmenter to staging today scratch that to production today",
        "They can ship the segmenter to production today.",
        "they",
    ),
    (
        "he said the invoice is ready and she will send it today",
        "She said the invoice is ready and she will send it today.",
        "she",
    ),
    (
        "tell him the meeting moved to friday and ask her to confirm",
        "Tell her the meeting moved to Friday and ask her to confirm.",
        "her",
    ),
    ("his laptop is broken so she will lend him hers", "Her laptop is broken, so she will lend him hers.", "her"),
    ("him is the new lead and she starts monday", "She is the new lead and she starts Monday.", "she"),
    (
        "here is the plan one he books the room two she sends the invites",
        "Here is the plan:\n1. She books the room\n2. She sends the invites",
        "she",
    ),
    (
        "call him no wait email him about the contract her team signed",
        "Email her about the contract her team signed.",
        "her",
    ),
    ("she wrote it herself and he checked it himself", "She wrote it himself and he checked it himself.", "himself"),
]


@pytest.mark.parametrize(("said", "wrote", "swapped"), PRONOUN_SWAPS)
def test_a_pronoun_of_another_person_or_gender_is_unheard(said: str, wrote: str, swapped: str) -> None:
    assert verdict(said, wrote, []) == Reason(Reject.UNHEARD, (swapped,))


def test_a_pronoun_swapped_in_a_later_chunk_is_unheard() -> None:
    said = "they said we can pay the deposit by friday"
    assert judge("We met the vendor.", said, [], None, "They said we can pay the deposit by Friday.").rejected is None
    swapped = judge("We met the vendor.", said, [], None, "We said we can pay the deposit by Friday.")
    assert swapped.reason == Reason(Reject.UNHEARD, ("we",))


def test_a_paragraph_that_opens_with_here_is_after_new_paragraph_is_no_reply() -> None:
    # v2-long-en-00203 chunked: "new paragraph here is what ..." was said, so "Here is" answers nothing.
    prior = "Take the van."
    said = "new paragraph here is what you need to do park in the short stay lot"
    judged = judge(prior, said, [], None, "\n\nHere is what you need to do: park in the short stay lot.")
    assert judged.rejected is None
    assert (
        judge(
            prior, "new paragraph park in the short stay lot", [], None, "Here is the text: park in the short stay lot."
        ).rejected
        == Reject.REPLY
    )


# 14 Oct 2026: "delete that" and "no, don't do this" take back what was said before them, as "scratch that" does;
# "don't do that" without the "no" is an everyday instruction.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("Send the report to Sam. Delete that. Send the deck to Priya.", "Send the deck to Priya."),
        ("book the room for monday, delete that, for tuesday", "Book the room for Tuesday."),
        ("Send it to Sam. No, don't do this. Send it to Priya.", "Send it to Priya."),
        ("Write the intro first. No, don't do that. Write the summary first.", "Write the summary first."),
        ("send it to sam no don't do that send it to priya", "Send it to Priya."),
    ],
)
def test_delete_that_and_dont_do_this_take_the_words_before_back(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # The same words used plainly stay.
        ("please delete that file from the drive", "Please file from the drive."),
        ("please cancel that order from the shop", "Please order from the shop."),
        ("we told them not to merge on friday and please don't do that again", "Please don't do that again."),
        ("Ship it today. Please don't do this in production.", "Please don't do this in production."),
        ("Ship it today. Don't do that, it breaks the build.", "Don't do that, it breaks the build."),
    ],
)
def test_delete_that_and_dont_do_this_used_plainly_take_nothing_back(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# 14 Oct 2026, the English bench's formatting misses that a rule mends after the guardrail accepts the words: the
# words before a list end with a colon, not a comma (formatting-066, -427); a line break the text came with stays one
# (formatting-327, -457); "NAME at DOMAIN dot TLD" sent to someone is one address (formatting-429, -460).
@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        (
            "mom bullet please check on the baby bullet please check on the laundry",
            "Mom,\n- Please check on the baby\n- Please check on the laundry",
            "Mom:\n- Please check on the baby\n- Please check on the laundry",
        ),
        (
            "anya first keep the title short second move the caption",
            "Anya,\n1. Keep the title short\n2. Move the caption",
            "Anya:\n1. Keep the title short\n2. Move the caption",
        ),
        (
            "the timeline begins in january and ends in june\nshow the final draft by friday",
            "The timeline begins in January and ends in June. Show the final draft by Friday.",
            "The timeline begins in January and ends in June.\nShow the final draft by Friday.",
        ),
        (
            "Budget note:\ntwenty percent down.\nMonthly payment about sixteen hundred dollars.",
            "Budget note: 20% down. Monthly payment about $1,600.",
            "Budget note:\n20% down.\nMonthly payment about $1,600.",
        ),
        (
            "For the mobile spec, direct feedback to ux at lanternworks dot io.",
            "For the mobile spec, direct feedback to UX at lanternworks.io.",
            "For the mobile spec, direct feedback to ux@lanternworks.io.",
        ),
        (
            "turn in the essay to noor at silverline dot school then copy the teacher",
            "Turn in the essay to Noor at silverline.school, then copy the teacher.",
            "Turn in the essay to noor@silverline.school, then copy the teacher.",
        ),
    ],
)
def test_an_accepted_answer_gets_its_list_colon_line_breaks_and_address(said: str, wrote: str, shaped: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == shaped


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # An email's greeting keeps its comma before a list.
        ("hi team bullet ship the fix bullet close the ticket", "Hi team,\n- Ship the fix\n- Close the ticket"),
        # A web address after "at" is no email, nor is one no one is sent anything at.
        ("the docs are at docs dot example dot org", "The docs are at docs.example.org."),
        ("book a slot at calendly dot com slash hirewell", "Book a slot at calendly.com/hirewell."),
        ("sign up at learnfast dot io", "Sign up at learnfast.io."),
        # A break said where the answer already breaks the line stays as it is.
        ("Hi Sam,\nthe build is green", "Hi Sam,\n\nThe build is green."),
        ("please buy milk\nalso eggs", "Please buy milk.\nAlso eggs."),
    ],
)
def test_shaping_leaves_greetings_web_addresses_and_kept_breaks_alone(said: str, wrote: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped

    assert shaped(said, wrote) == wrote


# 15 Oct 2026, the English bench's formatting misses that a rule mends after the guardrail accepts the words, each
# from the 11 stored runs: a spoken colon's next word stays lowercase (formatting-038); a colon the speaker never said,
# when they say their marks, becomes the comma they said or goes (formatting-226, -259); "Noor at noor@" and "UX@" as
# the speaker said the address (formatting-460, -429); a said lead's colon kept (formatting-457); an item's "and"
# kept and an item split mid-way joined back (formatting-145, -241); a first item glued after a comma (formatting-427);
# "mb" as "MB" (formatting-231); and a list's lead run on after the sentence before it (founder-variants-en-00001).
@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        (
            "Priya said colon kick off at ten thirty am comma break at noon comma wrap by four pm full stop",
            "Priya said: Kick off at 10:30 am, break at noon, wrap by 4 pm.",
            "Priya said: kick off at 10:30 am, break at noon, wrap by 4 pm.",
        ),
        (
            "Please show the receipt numbers for the last three payments comma 1042 comma 1043 comma 1044.",
            "Please show the receipt numbers for the last three payments: 1042, 1043, 1044.",
            "Please show the receipt numbers for the last three payments, 1042, 1043, 1044.",
        ),
        (
            "the spec for the billing page says show invoice history comma payment method comma and download receipt "
            "link period",
            "The spec for the billing page says: show invoice history, payment method, and download receipt link.",
            "The spec for the billing page says show invoice history, payment method, and download receipt link.",
        ),
        (
            "the review says Priya is right comma but the copy is long period",
            "The review says: Priya is right, but the copy is long.",
            "The review says Priya is right, but the copy is long.",
        ),
        # A speech said all in lowercase can't tell a name: the model's capital stays (decided 15 Oct).
        (
            "the review says priya is right comma but the copy is long period",
            "The review says: Priya is right, but the copy is long.",
            "The review says Priya is right, but the copy is long.",
        ),
        (
            "turn in the essay to noor at silverline dot school then copy the teacher",
            "Turn in the essay to Noor at noor@silverline.school, then copy the teacher.",
            "Turn in the essay to noor@silverline.school, then copy the teacher.",
        ),
        (
            "For the mobile spec, direct feedback to ux at lanternworks dot io.",
            "For the mobile spec, direct feedback to UX@lanternworks.io.",
            "For the mobile spec, direct feedback to ux@lanternworks.io.",
        ),
        (
            "Budget note:\ntwenty percent down.\nMonthly payment about sixteen hundred dollars.",
            "Budget note.\n20% down.\nMonthly payment about $1,600.",
            "Budget note:\n20% down.\nMonthly payment about $1,600.",
        ),
        (
            "for emily bullet the meeting recap should mention scope bullet risks bullet and next steps",
            "For Emily:\n- The meeting recap should mention scope\n- Risks\n- Next steps",
            "For Emily:\n- The meeting recap should mention scope\n- Risks\n- And next steps",
        ),
        (
            "for the renewal email bullet confirm pricing lock bullet include case study from lena",
            "For the renewal email:\n- Confirm pricing\n- Lock\n- Include case study from Lena",
            "For the renewal email:\n- Confirm pricing lock\n- Include case study from Lena",
        ),
        (
            "anya first keep the page title on one line second move the caption under the image third make the "
            "margins consistent",
            "Anya, 1. Keep the page title on one line\n2. Move the caption under the image\n3. Make the margins "
            "consistent",
            "Anya:\n1. Keep the page title on one line\n2. Move the caption under the image\n3. Make the margins "
            "consistent",
        ),
        (
            "keep each upload below two mb",
            "Keep each upload below 2 mb.",
            "Keep each upload below 2 MB.",
        ),
        (
            "Quick note for the new joiners. Here are a few things about the office. The cafeteria is on the ground "
            "floor. Parking is free for visitors.",
            "Quick note for the new joiners.\nHere are a few things about the office:\n- The cafeteria is on the "
            "ground floor\n- Parking is free for visitors",
            "Quick note for the new joiners. Here are a few things about the office:\n- The cafeteria is on the "
            "ground floor\n- Parking is free for visitors",
        ),
    ],
)
def test_an_accepted_answer_gets_its_marks_addresses_and_items_as_said(said: str, wrote: str, shaped: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == shaped


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A question after a spoken colon keeps its capital, and so does a word in an all-lowercase speech, which could
        # be a name.
        (
            "Tom asked colon are we keeping the site launch on Friday question mark period",
            "Tom asked: Are we keeping the site launch on Friday?",
        ),
        ("note colon bring id", "Note: Bring ID."),
        # A name the speaker said before the address stays.
        (
            "You can email Neha at neha at snipandsuds dot in for the Saturday slot",
            "You can email Neha at neha@snipandsuds.in for the Saturday slot.",
        ),
        # Items said after "bullet one", "bullet two" stay apart.
        (
            "Design review agenda, bullet one, simplify the onboarding steps; bullet two, keep the dark mode toggle "
            "visible.",
            "Design review agenda:\n- Simplify the onboarding steps\n- Keep the dark mode toggle visible",
        ),
        # A paragraph said before a list's lead stays, and so does a semicolon for a said comma.
        (
            "hi everyone thanks for coming new paragraph here are the three goals launch the app cut the tickets",
            "Hi everyone, thanks for coming.\n\nHere are the three goals:\n1. Launch the app\n2. Cut the tickets",
        ),
        (
            "we rolled back the deployment comma however the ingress logs still show errors",
            "We rolled back the deployment; however, the ingress logs still show errors.",
        ),
    ],
)
def test_shaping_leaves_said_capitals_names_and_marks_alone(said: str, wrote: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped

    assert shaped(said, wrote) == wrote
