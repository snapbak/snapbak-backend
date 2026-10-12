"""Polish quality, latency and cost per engine on the checked corpus sample (docs/bench.md).

`uv run scripts/bench.py SPEC [SPEC ...] [--split dev|heldout] [--limit N | --per-slice N | --heldout N]
[--failures PATH] [--ledger PATH --budget-usd USD]`. A SPEC is `provider:model` (both tiers that model) or
`small+big` (the real escalation). Each corpus item goes through the server's own `polish()` with its tiers, and is
scored against the gates overall, per language, per length and per slice, then graded per area. Needs the providers'
keys in the environment (`just bench` loads `.env`). With `--ledger`, each run appends what it spent to that JSONL
file, and with `--budget-usd` it stops sending once the ledger's total and its own spend reach the budget.
"""

import argparse
import asyncio
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path

from snapbak_backend.ai.chain import MARGIN_MS, Call, ProviderFailed, Tiers, elapsed_ms
from snapbak_backend.ai.guardrail.judge import Reject, check, in_address, judge
from snapbak_backend.ai.guardrail.numbers import NUMBER_NAMES
from snapbak_backend.ai.guardrail.respell import SPELLING_KEY
from snapbak_backend.ai.guardrail.shape import in_style
from snapbak_backend.ai.providers.base import Provider, Turn, Usage
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.ai.routing import CUE, LETTER
from snapbak_backend.config.pricing import PRICES, cost
from snapbak_backend.config.settings import KEYS
from snapbak_backend.schemas.polish import MAX_PRIOR_WORDS, PolishRequest
from snapbak_backend.services.polish import polish
from snapbak_backend.transformers.polish import fold, unfold

CORPUS = Path(__file__).resolve().parent.parent / "bench" / "cleanup"
CONCURRENCY = 16
# What the app gives the last chunk from when it is sent (LAST_MS in the app's dictation.rs); a short hold is one
# last chunk.
DEADLINE_MS = 20_000
LANGUAGES = {"hinglish": "hinglish", "hi": "hi"}
SEED = 7
NUMBER_WORDS = {word for names in NUMBER_NAMES.values() for word in names.split()}
DEV_PER_SLICE = 40
# A run with more provider errors than this is not graded: its gates would score raw input as model output.
MAX_FAILED_PERCENT = 5

# Gate -> (bar %, higher is better).
GATES = {
    "backtracking": (95, True),
    "punctuation": (95, True),
    "grammar": (95, True),
    "grammar kept": (99, True),
    "capitalization": (98, True),
    "clean text unchanged": (99, True),
    "dictionary terms kept": (99, True),
    "false rejects": (3, False),
    "hinglish and hindi": (95, True),
    "list shape": (95, True),
    "false lists": (1, False),
    "misheard words fixed": (95, True),
    "words not over-fixed": (99, True),
    "sentence correctness": (95, True),
    "dictionary strict": (99.5, True),
    "term twins kept": (99, True),
    "spoken forms to terms": (97, True),
    "no unheard content": (0.5, False),
    "script kept": (99.5, True),
    "no reply": (99.5, True),
    "numbers right": (97, True),
    "length parity": (95, True),
    "paragraphs kept": (99, True),
}
# Gates whose bar holds in each language too, and the bar sentence correctness holds in every language x length cell
# with this many rows; length parity also fails this many points under the sentence bucket.
PER_LANGUAGE = {"grammar"}
CELL_BAR, CELL_ROWS = 93, 30
PARITY_GAP = 2
LENGTHS = ("1-4", "sentence", "2-5", "paragraph", "multi")
CELLS = ("en", "hinglish", "hi", *LENGTHS)
# Area -> its gates (docs/bench.md, Grades). Hinglish and Hindi also takes every gate's hinglish and hi cell, and
# Grammar every language cell of the grammar gate.
AREAS = {
    "Misheard words": ["misheard words fixed", "words not over-fixed"],
    "Grammar": ["grammar", "grammar kept"],
    "Punctuation and casing": ["punctuation", "capitalization"],
    "Sentence correctness": ["sentence correctness", "length parity"],
    "Formatting": ["list shape", "false lists", "numbers right", "paragraphs kept"],
    "Corrections": ["backtracking"],
    "Dictionary": ["dictionary terms kept", "dictionary strict", "term twins kept", "spoken forms to terms"],
    "Safety": ["clean text unchanged", "no unheard content", "script kept", "no reply", "false rejects"],
    "Hinglish and Hindi": ["hinglish and hindi"],
}
REPLY_BAIT = {"assistant-bait", "reply-bait"}

MARKS = ".,;:!?।\"')\u2026\u2014\u2013"
# An em or en dash between words is a mark ending the word before it (round b3 fix).
DASH = re.compile("([\u2014\u2013])")
# Marks that open a quote or an aside, scored like the marks that end a word.
OPENING = "\"'("
STRAIGHT = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
# A full stop ending a list item: optional, as the founder decided (D3, 11 Oct).
ITEM_STOP = re.compile(r"^(\s*(?:\d+\.|[-•]) .*?)[.।]$", re.MULTILINE)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
HYPHEN = re.compile(r"(?<=[\wऀ-ॿ])-(?=[\wऀ-ॿ])")
# Spellings a careful editor writes either way (round b2 fix, 11 Oct): chandrabindu or anusvara, a nukta, a Latin
# accent ("entrée" entree). Each is read one way before scoring.
CHANDRABINDU = str.maketrans({"\N{DEVANAGARI SIGN CANDRABINDU}": "\N{DEVANAGARI SIGN ANUSVARA}"})
NUKTA_OR_ACCENT = re.compile("[\N{DEVANAGARI SIGN NUKTA}\u0300-\u036f]")
# Marks a careful editor may write either way (round 3, 11 Oct): a comma before the last "and" of a series or
# between clauses, a comma after a short opening phrase of time or place, a semicolon or a full stop between two
# clauses, and "a.m." for "am". Each is read one way before scoring.
SERIES_COMMA = re.compile(r",(?= (?:and|aur|और) )")
OPENING_COMMA = re.compile(
    r"(^|[.?!।]\s+)((?:on|in|at|after|before|by|during|since|until|last|next|this|yesterday|today|tomorrow)"
    r"(?: [^\s,.?!।]+){0,3}),",
    re.IGNORECASE,
)
# Round b1: a comma before a clause of reason or purpose, and after a sentence's opening "so", "okay" or "well".
REASON_COMMA = re.compile(r",(?= (?:because|kyunki|kyonki|क्योंकि|taaki|ताकि|isliye|इसलिए) )", re.IGNORECASE)
# Round b6: also after an opening sentence adverb or greeting word ("Also,", "Finally,", "Hello, Amit").
OPENER_COMMA = re.compile(
    r"(^|[.?!।]\s+)(so|okay|ok|well|bas|बस|also|finally|actually|anyway|hi|hello|hey|haan|arre|waise|sorry|सॉरी|अरे"
    r"|namaste|नमस्ते|dekho|देखो),(?= )",
    re.IGNORECASE | re.MULTILINE,
)
SEMICOLON = re.compile(r"; (\w)")
# Round b2 fix: a title's full stop ("Ms." Ms), a full stop or a comma before a joining word ("dungi. Lekin" dungi,
# lekin), and a comma before a sequence's "phir", after a reporting verb, or between an "agar" or "jab" clause and
# its "to".
TITLE_STOP = re.compile(r"\b(Dr|Mr|Mrs|Ms|Prof|St)\.(?=\s)")
JOINED_SENTENCE = re.compile(r"[.;।]\s+(But|And|So|Then|Phir|Lekin|Par|Magar|Warna|Aur|लेकिन|पर|मगर|वरना|और|फिर)(?=\s)")
SEQUENCE_COMMA = re.compile(r"[,;](?= (?:phir|then|फिर) )", re.IGNORECASE)
# Round b3 fix: a comma before a contrast word ("It works, but the fan is loud.") is optional too.
CONTRAST_COMMA = re.compile(r",(?= (?:but|par|lekin|magar|warna|लेकिन|पर|मगर|वरना) )", re.IGNORECASE)
# Round b5: also a reporting verb said with its auxiliary ("bol rahe the", "कहते हैं") and "note kiya", "bataya".
REPORTED_COMMA = re.compile(
    r"((?:^|\s)(?:bola|boli|bole|kaha|poocha|pucha|bataya|likha|note kiya|कहा|बोला|बोली|बोले|पूछा|बताया|लिखा"
    r"|lagta hai|lagta h|laga|socha|लगता है|लगा|सोचा|said|says|asked|told me|told us|wrote"
    r"|(?:bol|pooch|puch|keh|kah) (?:raha|rahi|rahe|rha|rhi|rhe)(?: (?:hai|hain|h|tha|thi|the))?"
    r"|keh(?:te|ti|ta) (?:hai|hain|h|tha|thi|the)"
    r"|(?:बोल|पूछ|कह) (?:रहा|रही|रहे)(?: (?:है|हैं|था|थी|थे))?|कह(?:ते|ती|ता) (?:है|हैं|था|थी|थे))),(?= )",
    re.IGNORECASE,
)
# Round b5: a comma before the "to" or "तो" that opens a result clause, with or without "agar" before it.
RESULT_COMMA = re.compile(r",(?= (?:to|तो) )")
# Founder, 12 Oct: "Ek kaam karo:" or "Ek kaam karo," before the task.
TASK_MARK = re.compile(r"((?:^|[.?!।]\s+)(?:ek kaam (?:karo|kro)|एक काम करो|do one thing)):(?= )", re.IGNORECASE)
# Round b5: an email's greeting on a line of its own (rule 2) or run on into the first sentence; read run on.
# Round b6: a comma after the greeting word ("Hey, Leo,") and a title's full stop in the name ("Hi Ms. Rao,") too.
GREETING = re.compile(
    r"\A(hi|hello|hey|dear),?\s+((?:(?:Dr|Mr|Ms|Mrs|Prof)\.\s+)?[^\s,.!?:][^\n,.!?:]{0,30}),[ \t]*\n+[ \t]*(\S+)",
    re.IGNORECASE,
)
# Round b6: an email's sign-off set off by a blank line or one line break, with or without a full stop after a lone
# sign-off word, is one layout.
SIGN_OFF = re.compile(
    r"\n\n?((?:thanks|thank you|best|regards|best regards|warm regards|kind regards|cheers|dhanyavaad|shukriya)"
    r"(?:,\n[^\n]{1,30}|\.)?)\Z",
    re.IGNORECASE,
)
IF_THEN_COMMA = re.compile(r"((?:^|\s)(?:agar|jab|अगर|जब)\s[^.?!।,\n]*),(?= (?:to|तो) )", re.IGNORECASE)
MERIDIEM = re.compile(r"(?<=\d)(\s?)([ap])\.m\.|(?<=\d)(\s?)([AP])M\b")
LIST_LINE = re.compile(r"^(?:(\d+\.)|[-•]) ", re.MULTILINE)
# Round b3 fix: Roman Hindi has no one spelling ("me" "mein", "yaha" "yahan"); each word is read as its group's first
# spelling, as the guardrail's respell does.
LATIN_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Score:
    """Words must match; punctuation and case count only where the expected output differs from the input."""

    words_right: bool
    punctuation_needed: int
    punctuation_right: int
    case_needed: int
    case_right: int


@dataclass(frozen=True)
class Item:
    slice: str
    kind: str
    text: str
    prior: str
    terms: list[str]
    language: str
    style: str
    expected: list[str]
    continues: bool = False
    id: str = ""
    length: str = ""
    split: str = ""
    fixes: tuple[tuple[str, str], ...] = ()
    keeps: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def said(self) -> str:
        return f"{self.prior} {self.text}".strip()


def tokens(text: str) -> list[tuple[str, str, str]]:
    """(lowercase word, its opening and trailing marks, the word as written) for each word; list dashes and bullets
    drop, curly quotes read as straight ones, and a hyphen inside a word splits it ("read-only" is "read only")."""
    found = []
    unhyphenated = HYPHEN.sub(" ", unicodedata.normalize("NFC", text).translate(STRAIGHT))
    unhyphenated = DASH.sub(r"\1 ", unhyphenated)
    for raw in unhyphenated.split():
        word = raw.strip("-•*\"'(").rstrip(MARKS)
        unbulleted = raw.lstrip("-•*")
        opening = unbulleted[: len(unbulleted) - len(unbulleted.lstrip(OPENING))]
        marks = opening + raw[len(raw.rstrip(MARKS)) :]
        if word:
            found.append((word.lower(), marks, word))
    return found


def keyed(found: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The tokens with each Roman Hindi word read as its group's first spelling, for scoring."""
    return [(SPELLING_KEY.get(word, word), marks, raw) for word, marks, raw in found]


def canonical(text: str) -> str:
    """The text in NFC with straight quotes, and chandrabindu, a nukta and a Latin accent read one way."""
    decomposed = unicodedata.normalize("NFD", text.translate(STRAIGHT).translate(CHANDRABINDU))
    plain = NUKTA_OR_ACCENT.sub("", decomposed)
    composed = unicodedata.normalize("NFC", plain)
    return composed


def greeted(text: str) -> str:
    """The text with an email's greeting on a line of its own run on into the first sentence, whose first word loses
    its capital unless it is "I", and its sign-off one line break after the text."""
    found = GREETING.match(text) if "\n" in text else None
    if found is not None:
        word = found.group(3)
        first = word if word == "I" or word.startswith("I'") else word[:1].lower() + word[1:]
        text = f"{found.group(1)} {found.group(2)}, {first}{text[found.end() :]}"
    signed = SIGN_OFF.sub(lambda sign: "\n" + sign.group(1).removesuffix("."), text) if "\n" in text else text
    return signed


def unstopped(text: str) -> str:
    """The text with no full stop ending a list item, and each optional mark read one way."""
    bare = ITEM_STOP.sub(r"\1", greeted(text))
    serial = SERIES_COMMA.sub("", bare)
    reasoned = REASON_COMMA.sub("", serial)
    phrased = OPENING_COMMA.sub(r"\1\2", reasoned)
    opened = OPENER_COMMA.sub(r"\1\2", phrased)
    sequenced = SEQUENCE_COMMA.sub("", opened)
    stopped = SEMICOLON.sub(lambda found: ". " + found.group(1).upper(), sequenced)
    timed = MERIDIEM.sub(lambda found: meridiem(found, stopped), stopped)
    untitled = TITLE_STOP.sub(r"\1", timed)
    joined = JOINED_SENTENCE.sub(lambda found: ", " + found.group(1).lower(), untitled)
    # The comma a joined "and" leaves is optional like any other before "and" (round b4 fix).
    rejoined = SERIES_COMMA.sub("", joined)
    unsequenced = SEQUENCE_COMMA.sub("", rejoined)
    contrasted = CONTRAST_COMMA.sub("", unsequenced)
    reported = REPORTED_COMMA.sub(r"\1", contrasted)
    conditioned = IF_THEN_COMMA.sub(r"\1", reported)
    resulted = RESULT_COMMA.sub("", conditioned)
    tasked = TASK_MARK.sub(r"\1,", resulted)
    return tasked


def meridiem(found: re.Match[str], text: str) -> str:
    """ "am" or "pm" for "a.m.", "AM" and the like; "a.m." ending a sentence keeps its full stop."""
    space, letter = (found.group(1), found.group(2)) if found.group(2) else (found.group(3), found.group(4))
    rest = text[found.end() :]
    ends = found.group(2) is not None and (not rest.strip() or re.match(r"\s+[A-Z\n]", rest) is not None)
    return f"{space or ''}{letter.lower()}m{'.' if ends else ''}"


def score(output: str, expected: list[str], said: str = "") -> Score:
    """Words must match exactly; punctuation and casing are counted only on the words that align, and only where
    the expected mark or case differs from the input `said`. A word the input never had counts as said bare and
    lowercase. Of expected outputs with equally close words, the one whose marks are closest is used. A full stop
    after a list item is optional. A report scores each result once per cell, so scores are cached."""
    scored = scored_once(output, tuple(expected), said)
    return scored


@cache
def scored_once(output: str, answers: tuple[str, ...], said: str) -> Score:
    output = unstopped(canonical(unquoted(output, answers)))
    expected = [unstopped(canonical(out)) for out in answers]
    said = unstopped(canonical(said))
    wrote = keyed(tokens(output))
    wrote_words = [word for word, _, _ in wrote]

    def closeness(candidate: str) -> tuple[float, float]:
        candidate_words = [word for word, _, _ in keyed(tokens(candidate))]
        words = SequenceMatcher(a=wrote_words, b=candidate_words, autojunk=False).ratio()
        text = SequenceMatcher(a=output, b=candidate, autojunk=False).ratio()
        return words, text

    best_text = max(expected, key=closeness)
    best = keyed(tokens(best_text))
    best_words = [word for word, _, _ in best]
    heard = keyed(tokens(said))
    source: dict[int, tuple[str, str, str]] = {}
    heard_words = [word for word, _, _ in heard]
    for block in SequenceMatcher(a=best_words, b=heard_words, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            source[block.a + offset] = heard[block.b + offset]
    matcher = SequenceMatcher(a=wrote_words, b=best_words, autojunk=False)
    punctuation_needed = punctuation = case_needed = case = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            _, wrote_marks, wrote_raw = wrote[block.a + offset]
            best_word, best_marks, best_raw = best[block.b + offset]
            unheard = (best_word, "", best_word)
            _, said_marks, said_raw = source.get(block.b + offset, unheard)
            # A mark the input already had counts only when the output changed it: added, dropped or swapped.
            if said_marks != best_marks or wrote_marks != best_marks:
                punctuation_needed += 1
                punctuation += wrote_marks == best_marks
            if said_raw != best_raw:
                case_needed += 1
                case += same_case(wrote_raw, best_raw)
    right = wrote_words == best_words or unhyphenated_words(output) == unhyphenated_words(best_text)
    return Score(right, punctuation_needed, punctuation, case_needed, case)


def same_case(wrote: str, best: str) -> bool:
    """Whether a word is cased as expected; one Roman Hindi word spelled two ways compares its capitals only."""
    if wrote.lower() == best.lower():
        return wrote == best
    return wrote[:1].isupper() == best[:1].isupper() and wrote.isupper() == best.isupper()


def unhyphenated_words(text: str) -> list[str]:
    """The words with each hyphen inside a word read away ("sign-up" signup)."""
    joined = HYPHEN.sub("", canonical(text))
    found = [word for word, _, _ in keyed(tokens(joined))]
    return found


def normal(text: str) -> str:
    """The text as written, with spaces tidied, `canonical` and no full stop ending a list item."""
    straight = canonical(text)
    lines = [" ".join(line.split()) for line in straight.strip().splitlines()]
    tidied = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    keyed = LATIN_WORD.sub(spelled_one_way, tidied)
    bare = unstopped(keyed)
    return bare


def spelled_one_way(found: re.Match[str]) -> str:
    """A Roman Hindi word as its group's first spelling, keeping a capital it had."""
    word = found.group()
    key = SPELLING_KEY.get(word.lower())
    if key is None or key == word.lower():
        return word
    return key[0].upper() + key[1:] if word[0].isupper() else key


# Round b6: quote marks around a few words the expected answers never quote (UI text, a phrase mentioned) are
# optional, with the comma before them, a capital they open with and a mark they close over.
QUOTED = re.compile(r'(,?\s*)"([^"\n]{1,60}?)([,.]?)"')
QUOTED_WORDS = 5


def unquoted(output: str, expected: tuple[str, ...] | list[str]) -> str:
    """The output without quote marks around five words or fewer, when no expected answer has a quote mark."""
    straight = output.translate(STRAIGHT)
    if '"' not in straight or any('"' in answer.translate(STRAIGHT) for answer in expected):
        return output

    def bare(found: re.Match[str]) -> str:
        inner = found.group(2)
        if len(inner.split()) > QUOTED_WORDS:
            return found.group()
        starts = SENTENCE_OPEN.search(straight[: found.start()]) is not None
        first = inner if starts or inner[:2].isupper() else inner[:1].lower() + inner[1:]
        return f" {first}{found.group(3)}" if found.group(1) else f"{first}{found.group(3)}"

    return QUOTED.sub(bare, straight)


SENTENCE_OPEN = re.compile(r"(?:\A|[.?!।:\n])\s*$")


# Founder, 12 Oct: between two Hindi or Hinglish clauses a comma, a full stop or "।" is the writer's choice; the next
# word's capital goes with the mark. A "?" or "!" still counts.
CLAUSE_MARK = re.compile(r"(?<=[\wऀ-ॿ])[,.।] +(\S)")
# The comma before a result clause's "to" is already optional (RESULT_COMMA), so a full stop there is too.
RESULT_BREAK = re.compile(r" \| (to|तो)(?= )")


def exact(output: str, expected: list[str], language: str = "en") -> bool:
    """Whether the output is an expected answer in every word, mark and capital."""

    def read(text: str) -> set[str]:
        found = spellings(text)
        if language not in LANGUAGES.values():
            return found
        clauses = {
            RESULT_BREAK.sub(r" \1", CLAUSE_MARK.sub(lambda mark: " | " + mark.group(1).lower(), one)) for one in found
        }
        return clauses

    written = read(unquoted(output, expected))
    right = any(not written.isdisjoint(read(out)) for out in expected)
    return right


def spellings(text: str) -> set[str]:
    """The text `normal`, with a hyphen inside a word as written, as a space and read away."""
    found = {normal(text), normal(HYPHEN.sub(" ", canonical(text))), normal(HYPHEN.sub("", canonical(text)))}
    return found


def words_of(text: str) -> list[str]:
    found = [word for word, _, _ in tokens(text)]
    return found


def scored_words(text: str) -> list[str]:
    """The words as scoring reads them: each Roman Hindi word as its group's first spelling."""
    found = [word for word, _, _ in keyed(tokens(text))]
    return found


def holds(text_words: list[str], phrase: list[str]) -> bool:
    """Whether the words hold the phrase's words in a row."""
    size = len(phrase)
    found = any(text_words[start : start + size] == phrase for start in range(len(text_words) - size + 1))
    return size > 0 and found


def matched(a: list[str], b: list[str]) -> set[int]:
    """The indices of `b` whose word lines up with the same word in `a`."""
    blocks = SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks()
    found = {block.b + offset for block in blocks for offset in range(block.size)}
    return found


def placed(output: str, item: Item, phrase: str, changed: bool) -> bool:
    """Whether the output holds the phrase where an expected answer has it: where the answer changed what was said
    for a fix (`changed`), anywhere it has it for a kept word."""
    wrote = scored_words(output)
    said = scored_words(item.said)
    target = scored_words(phrase)
    size = len(target)
    for answer in item.expected:
        written = scored_words(answer)
        starts = [start for start in range(len(written) - size + 1) if written[start : start + size] == target]
        heard = matched(said, written)
        new = [start for start in starts if not set(range(start, start + size)) <= heard]
        aligned = matched(wrote, written)
        chosen = new if changed and new else starts
        if any(set(range(start, start + size)) <= aligned for start in chosen):
            return True
    return False


# Where a sentence or a list item starts: an all-lowercase term takes a capital there (rule 9, round b3 fix).
OPENS_SENTENCE = re.compile(r"(?:^|[.?!।:\n]|\n\s*(?:-|\d+\.))[\s\"'(]*$")


def term_count(text: str, term: str) -> int:
    """How often the term appears exactly, with its casing and accents, as a whole word; inside an email, a web
    address or a path, which read the same in any case, in either case (round b2 fix); an all-lowercase term also
    with a capital where a sentence starts (round b3 fix)."""
    pattern = re.compile(rf"(?<![{LETTER}]){re.escape(term)}(?![{LETTER}])", re.IGNORECASE)
    composed = unicodedata.normalize("NFC", text)
    found = pattern.finditer(composed)
    count = sum(term_at(composed, match, term) for match in found)
    return count


def term_at(text: str, match: re.Match[str], term: str) -> bool:
    if match.group() == term or in_address(text, match.start(), match.end()):
        return True
    opening = term.islower() and match.group() == term[:1].upper() + term[1:]
    return opening and OPENS_SENTENCE.search(text[: match.start()]) is not None


def known(item: Item) -> set[str]:
    """The words said, in an expected answer or in a term."""
    found = {word for text in [item.said, *item.expected, *item.terms] for word in scored_words(text)}
    return found


def invented(item: Item, output: str) -> list[str]:
    """Output words in no expected answer that were never said, and words written more often than said and than any
    expected answer holds them (round b5: one "क्या" written twice): a digit counts as said when a number was."""
    numbered = bool(re.search(r"\d", item.said)) or not NUMBER_WORDS.isdisjoint(scored_words(item.said))
    heard = known(item)
    folded = {canonical(word) for word in heard}
    written = scored_words(output)
    found = [
        word
        for word in written
        if word not in heard
        and canonical(word) not in folded
        and not (numbered and re.fullmatch(r"[\d.,:%₹$€£]+", word))
    ]
    said_times = Counter(scored_words(item.said))
    answer_times = [Counter(scored_words(answer)) for answer in item.expected]
    times = Counter(word for word in written if word in heard and not re.fullmatch(r"[\d.,:%₹$€£]+", word))
    found += [
        word
        for word, count in times.items()
        if count > max(said_times[word], *(answer[word] for answer in answer_times))
    ]
    return found


def expected_words(item: Item) -> set[str]:
    found = {word for answer in item.expected for word in words_of(answer)}
    return found


def wrong_script(item: Item, output: str) -> tuple[int, int]:
    """(words in a script, of them those in the other script than the speaker's that no answer or the speaker has).
    The speaker's script is the one most of the said words are in, whatever the language tag says (founder, 13 Oct)."""
    heard = known(item)
    said = words_of(item.said)
    devanagari = sum(bool(DEVANAGARI.search(word)) for word in said)
    other = LATIN if devanagari > len(said) - devanagari else DEVANAGARI
    scripted = [word for word in words_of(output) if LATIN.search(word) or DEVANAGARI.search(word)]
    wrong = sum(bool(other.search(word)) and word not in heard for word in scripted)
    return len(scripted), wrong


def list_shape(text: str) -> list[str]:
    """The kind of each line that starts a list item, "1." numbered or "-" bulleted; empty for prose."""
    shape = ["1." if found.group(1) else "-" for found in LIST_LINE.finditer(text)]
    return shape


def list_right(output: str, expected: list[str]) -> bool | None:
    """Whether a list answer has the same items, one per line, of the same kind; None when no answer is a list. Prose
    is right when one expected answer is prose."""
    shapes = [list_shape(out) for out in expected]
    if not any(shapes):
        return None
    right = list_shape(output) in shapes
    return right


def split_at_cue(said: str) -> tuple[str, str] | None:
    """`said` cut before its first correction cue that has at least three words ahead of it."""
    for found in CUE.finditer(said.lower()):
        before = said[: found.start()].strip()
        if len(before.split()) >= 3:
            return before, said[found.start() :].strip()
    return None


def paragraph_cuts(said: str, written: str) -> list[tuple[str, str]]:
    """A long dictation sent in pieces: for each written paragraph after the first (up to four in all), the written
    paragraphs before it as the prior and the said words after the ones they hold as the text."""
    paragraphs = [paragraph for paragraph in written.split("\n\n") if paragraph.strip()][:4]
    raw = said.split()
    owners = [index for index, word in enumerate(raw) for _ in tokens(word)]
    blocks = SequenceMatcher(a=words_of(written), b=words_of(said), autojunk=False).get_matching_blocks()
    cuts = []
    for count in range(1, len(paragraphs)):
        prior = "\n\n".join(paragraphs[:count])
        last = len(tokens(prior)) - 1
        ends = [block.b + last - block.a + 1 for block in blocks if block.a <= last < block.a + block.size]
        if ends and ends[0] < len(owners) and len(prior.split()) <= MAX_PRIOR_WORDS:
            cuts.append((prior, " ".join(raw[owners[ends[0]] :])))
    return cuts


def is_v2(name: str) -> bool:
    """Whether a slice file holds v2 rows: their own id, language, length and split (docs/bench.md)."""
    v2 = "v2-" in name or name in {"founder-variants", "suite-growth"}
    return v2


def derived(name: str, item: Item) -> list[Item]:
    """The chunked twins of a row: a correction cut before its cue, a long dictation cut at its paragraphs."""
    if name.endswith("backtracking") and not item.kind.startswith("trap"):
        cut = split_at_cue(item.text)
        prior = f"{item.prior} {cut[0]}".strip() if cut else ""
        if cut is None or len(prior.split()) > MAX_PRIOR_WORDS:
            return []
        return [replace(item, slice=f"{name}-chunked", text=cut[1], prior=prior)]
    if name.endswith("v2-long") and item.length == "multi" and not item.prior:
        pieces = paragraph_cuts(item.text, item.expected[0])
        return [replace(item, slice=f"{name}-chunked", text=text, prior=prior) for prior, text in pieces]
    return []


def load() -> list[Item]:
    items = []
    for path in sorted(CORPUS.glob("*.jsonl.gz")):
        name = path.name.removesuffix(".jsonl.gz")
        language = LANGUAGES.get(name.split("-")[0], "en")
        with gzip.open(path, "rt", encoding="utf-8") as rows:
            for line in rows:
                row = json.loads(line)
                kind = row["kind"]
                tagged = "hinglish" if "Hinglish" in kind else "hi" if "Hindi" in kind else language
                style = row.get("app_style", "other")
                item = Item(
                    name,
                    kind,
                    row["said"],
                    row.get("prior", ""),
                    [term for term in row.get("dictionary", []) if term],
                    row.get("language", tagged),
                    style,
                    # A row keeps the plain answer the guardrail judges; the app's style writes its lists.
                    [in_style(out, style) for out in row["expected"]],
                    row.get("continues", False),
                    id=row.get("id", ""),
                    length=row.get("length", ""),
                    split=row.get("split", ""),
                    fixes=tuple((fix["heard"], fix["written"]) for fix in row.get("fixes", [])),
                    keeps=tuple(row.get("keeps", [])),
                    tags=tuple(row.get("tags", [])),
                )
                items += [item, *derived(name, item)]
    return items


@dataclass
class Result:
    item: Item
    text: str
    rejected: bool
    tier: str
    ms: int
    calls: list[Call]
    failed: bool


@dataclass
class Budget:
    """What a run may spend: `limit` US$ in all, of which the ledger had spent `before` when it started. A call that
    failed reports no usage, though the provider may have billed it, so it is charged as the dearest call so far."""

    limit: float
    before: float
    spent: float = 0.0
    items: int = 0
    dearest: float = 0.0

    def reached(self) -> bool:
        return self.before + self.spent >= self.limit

    def charge(self, engine: str, usage: Usage | None) -> None:
        if usage is None:
            self.spent += self.dearest
            return
        dollars = cost(engine, usage.input, usage.output, usage.cached)
        self.dearest = max(self.dearest, dollars)
        self.spent += dollars


class Metered:
    """A provider whose every call, answered or failed, is charged to the budget as it ends."""

    def __init__(self, provider: Provider, budget: Budget) -> None:
        self.provider = provider
        self.budget = budget
        self.engine = provider.engine

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        try:
            answer = await self.provider.complete(turns, max_tokens, timeout, reason)
        except BaseException:
            self.budget.charge(self.engine, None)
            raise
        self.budget.charge(self.engine, answer[1])
        return answer

    async def aclose(self) -> None:
        await self.provider.aclose()


def ledger_total(path: Path) -> float:
    """The US$ every run in the ledger spent; a missing ledger has spent nothing."""
    if not path.exists():
        return 0.0
    lines = path.read_text(encoding="utf-8").splitlines()
    spent = sum(json.loads(line)["usd"] for line in lines if line.strip())
    return spent


def request_of(item: Item) -> PolishRequest:
    fields = {
        "text": item.text,
        "prior": item.prior,
        "terms": item.terms[:200],
        "app": "",
        "style": item.style,
        "language": item.language,
        "continues": item.continues,
        "deadline_ms": DEADLINE_MS,
    }
    request = PolishRequest.model_validate(fields)
    return request


async def run(item: Item, tiers: Tiers, gate: asyncio.Semaphore, budget: Budget | None = None) -> Result | None:
    """The item's result, or None when the budget was reached before it was sent."""
    request = request_of(item)
    budget = budget or Budget(math.inf, 0.0)
    async with gate:
        if budget.reached():
            return None
        started = time.monotonic()
        try:
            response, calls = await polish(request, tiers)
        except ProviderFailed:
            waited = elapsed_ms(started)
            budget.items += 1
            return Result(item, item.text, False, "-", waited, [], True)
        budget.items += 1
    joined = written(item.prior, response.revision, response.text)
    return Result(item, joined, response.rejected, response.tier, response.ms, calls, False)


# A chunk that starts a list item ("- ", "3. "), which the app puts on its own line (join in the app's dictation.rs).
ITEM_START = re.compile(r"(?:- |\d+\. )")


def written(prior: str, revision: str | None, text: str) -> str:
    """The whole text after a polish: the prior, or its revision ("" retracts it all), then the chunk, joined as the
    app joins them. With no prior the server drops a revision, unless it is the whole answer, so it does here too."""
    if not prior:
        answer = text or revision or ""
        return answer.strip()
    kept = prior.strip() if revision is None else revision.strip()
    # A break the prior had after the part a revision kept stays, as the app's assemble keeps it (round b3 fix).
    rest = prior.removeprefix(kept) if revision is not None and kept else ""
    space = rest[: len(rest) - len(rest.lstrip())]
    broken = "\n" in space
    seam = space if broken else "\n" if kept and ITEM_START.match(text.strip()) else " "
    return f"{kept}{seam}{text.strip()}".strip()


def falsely_rejected(result: Result) -> bool:
    for call in result.calls:
        if call.verdict in {reason.value for reason in Reject} and call.verdict != Reject.MALFORMED:
            answer = json.loads(call.output)
            request = request_of(result.item)
            revision, text = unfold(request, fold(request), answer.get("revision"), answer["text"])
            joined = written(result.item.prior, revision, text)
            if exact(joined, result.item.expected, result.item.language):
                return True
    return False


def misses(results: list[Result]) -> list[dict]:
    """Each result that missed a gate, with what it missed: [right, needed] for punctuation and case."""
    found = []
    for result in results:
        entry = result.item
        scored = score(result.text, entry.expected, entry.said)
        missing = [term for term in expected_terms(entry) if not term_count(result.text, term)]
        false_reject = falsely_rejected(result)
        styled = scored.punctuation_right < scored.punctuation_needed or scored.case_right < scored.case_needed
        list_answer = list_right(result.text, entry.expected)
        wrong_list = list_answer is False or (list_answer is None and bool(list_shape(result.text)))
        right = exact(result.text, entry.expected, entry.language)
        # An answer the gold accepts has every fix the gold accepts, also one a fix's single spelling misses, and so
        # does one with the words of an accepted answer (round b4 fix).
        fixes_missed = (
            []
            if right or scored.words_right
            else [heard for heard, written in entry.fixes if not placed(result.text, entry, written, True)]
        )
        keeps_lost = [keep for keep in entry.keeps if not placed(result.text, entry, keep, False)]
        unheard = invented(entry, result.text)
        clean = scored.words_right and not styled and not missing and not false_reject and not wrong_list
        if clean and right and not fixes_missed and not keeps_lost and not unheard:
            continue
        found.append(
            {
                "slice": entry.slice,
                "kind": entry.kind,
                "prior": entry.prior,
                "text": entry.text,
                "output": result.text,
                "expected": entry.expected,
                "terms": entry.terms,
                "rejected": result.rejected,
                "falsely_rejected": false_reject,
                "words_right": scored.words_right,
                "punctuation": [scored.punctuation_right, scored.punctuation_needed],
                "case": [scored.case_right, scored.case_needed],
                "missing_terms": missing,
                "wrong_list": wrong_list,
                "failed": result.failed,
                "id": entry.id,
                "split": entry.split,
                "language": entry.language,
                "length": entry.length,
                "exact": right,
                "fixes_missed": fixes_missed,
                "keeps_lost": keeps_lost,
                "invented": unheard,
                "refused": refused_answers(result),
            }
        )
    return found


def refused_answers(result: Result) -> list[dict[str, str]]:
    """Each answer the guardrail refused, with its verdict, so a grader can read what the model wrote."""
    refusals = {reason.value for reason in Reject} - {Reject.MALFORMED}
    found = []
    for call in result.calls:
        if call.verdict not in refusals:
            continue
        try:
            parsed = json.loads(call.output)
            answer, revision = parsed["text"], parsed.get("revision")
        except ValueError, KeyError, TypeError, AttributeError:
            answer, revision = call.output, None
        found.append({"verdict": call.verdict, "answer": answer, "revision": revision})
    return found


def percent(passed: int, total: int) -> float | None:
    return None if total == 0 else 100 * passed / total


def gates(results: list[Result]) -> dict[str, float | None]:
    scores = [score(result.text, result.item.expected, result.item.said) for result in results]
    pairs = list(zip(results, scores, strict=True))
    styled = scores
    traps = [s for r, s in pairs if r.item.kind.startswith("trap")]
    backtracking = [s for r, s in pairs if r.item.slice.endswith("backtracking") and not r.item.kind.startswith("trap")]
    backtracking += [s for r, s in pairs if r.item.slice.endswith("backtracking-chunked")]
    grammar = [s for r, s in pairs if r.item.slice.endswith("grammar") and not r.item.kind.startswith("trap")]
    grammar_traps = [s for r, s in pairs if r.item.slice.endswith("grammar") and r.item.kind.startswith("trap")]
    indic = [s for r, s in pairs if r.item.language in LANGUAGES.values()]
    with_terms = [r for r in results if any(term_count(e, t) for t in r.item.terms for e in r.item.expected)]
    kept = [r for r in with_terms if all(term_count(r.text, t) for t in expected_terms(r.item))]
    lists = [list_right(r.text, r.item.expected) for r in results]
    listed = [right for right in lists if right is not None]
    prose = [bool(list_shape(r.text)) for r, right in zip(results, lists, strict=True) if right is None]
    fixes = [
        exact(r.text, r.item.expected, r.item.language)
        or score(r.text, r.item.expected, r.item.said).words_right
        or placed(r.text, r.item, written, True)
        for r in results
        for _, written in r.item.fixes
    ]
    keeps = [placed(r.text, r.item, keep, False) for r in results for keep in r.item.keeps]
    keeps += [set(words_of(r.text)) <= expected_words(r.item) for r in results if r.item.kind.startswith("trap")]
    exacts = [exact(r.text, r.item.expected, r.item.language) for r in results]
    strict = [r for r in results if any(term_count(e, t) for t in r.item.terms for e in r.item.expected)]
    twins = [r for r in results if any(not any(term_count(e, t) for e in r.item.expected) for t in r.item.terms)]
    spoken = [r for r in strict if any(not term_count(r.item.said, t) for t in r.item.terms)]
    scripts = [wrong_script(r.item, r.text) for r in results]
    baits = [
        r for r in results if REPLY_BAIT & set(r.item.tags) or "reply" in r.item.kind or "assistant" in r.item.kind
    ]
    numbers = [s for r, s in pairs if r.item.slice.endswith("v2-numbers")]
    paragraphs = [r for r in results if any("\n\n" in e for e in r.item.expected)]
    paragraphed = [
        any(greeted(r.text).count("\n\n") == greeted(e).count("\n\n") for e in r.item.expected) for r in paragraphs
    ]
    return {
        "backtracking": percent(sum(s.words_right for s in backtracking), len(backtracking)),
        "punctuation": percent(sum(s.punctuation_right for s in styled), sum(s.punctuation_needed for s in styled)),
        "grammar": percent(sum(s.words_right for s in grammar), len(grammar)),
        "grammar kept": percent(sum(s.words_right for s in grammar_traps), len(grammar_traps)),
        "capitalization": percent(sum(s.case_right for s in styled), sum(s.case_needed for s in styled)),
        "clean text unchanged": percent(sum(s.words_right for s in traps), len(traps)),
        "dictionary terms kept": percent(len(kept), len(with_terms)),
        "false rejects": percent(sum(falsely_rejected(r) for r in results), len(results)),
        "hinglish and hindi": percent(sum(s.words_right for s in indic), len(indic)),
        "list shape": percent(sum(listed), len(listed)),
        "false lists": percent(sum(prose), len(prose)),
        "misheard words fixed": percent(sum(fixes), len(fixes)),
        "words not over-fixed": percent(sum(keeps), len(keeps)),
        "sentence correctness": percent(sum(exacts), len(exacts)),
        "dictionary strict": percent(sum(terms_right(r) for r in strict), len(strict)),
        "term twins kept": percent(sum(twins_kept(r) for r in twins), len(twins)),
        "spoken forms to terms": percent(sum(terms_right(r) for r in spoken), len(spoken)),
        "no unheard content": percent(sum(bool(invented(r.item, r.text)) for r in results), len(results)),
        "script kept": percent(sum(total - wrong for total, wrong in scripts), sum(total for total, _ in scripts)),
        "no reply": percent(sum(not invented(r.item, r.text) for r in baits), len(baits)),
        "numbers right": percent(sum(s.words_right for s in numbers), len(numbers)),
        "length parity": parity(pairs),
        "paragraphs kept": percent(sum(paragraphed), len(paragraphed)),
    }


def expected_terms(item: Item) -> list[str]:
    """The terms some expected answer holds."""
    found = [term for term in item.terms if any(term_count(out, term) for out in item.expected)]
    return found


def terms_right(result: Result) -> bool:
    """Whether the output holds every term an expected answer holds, as often and spelled exactly as it does."""
    terms = result.item.terms
    wrote = [term_count(result.text, term) for term in terms]
    right = any(wrote == [term_count(out, term) for term in terms] for out in result.item.expected)
    return right


def twins_kept(result: Result) -> bool:
    """Whether the output leaves out every term that no expected answer holds (the speaker said a real word)."""
    absent = [t for t in result.item.terms if not any(term_count(out, t) for out in result.item.expected)]
    kept = not any(term_count(result.text, term) for term in absent)
    return kept


def words_right_by_length(pairs: list[tuple[Result, Score]]) -> dict[str, float | None]:
    found = {}
    for length in LENGTHS:
        scored = [s.words_right for r, s in pairs if r.item.length == length]
        found[length] = percent(sum(scored), len(scored))
    return found


def parity(pairs: list[tuple[Result, Score]]) -> float | None:
    """Words right in the weaker of the paragraph and multi-paragraph buckets."""
    by_length = words_right_by_length(pairs)
    long = [value for length in ("paragraph", "multi") if (value := by_length[length]) is not None]
    weakest = min(long, default=None)
    return weakest


def cell(results: list[Result], name: str) -> list[Result]:
    """The results of a language or a length bucket."""
    picked = [r for r in results if name in {r.item.language, r.item.length}]
    return picked


def under(value: float | None, bar: float, higher: bool) -> bool:
    return value is not None and (value < bar if higher else value > bar)


def failures(results: list[Result]) -> list[str]:
    """Each bar a run misses: a gate overall, a per-language gate in a language, sentence correctness in a language x
    length cell of 30 rows or more, or the long buckets more than 2 points under the sentence bucket."""
    found = []
    overall = gates(results)
    for gate, (bar, higher) in GATES.items():
        if under(overall[gate], bar, higher):
            found.append(f"{gate} {overall[gate]:.1f} {'<' if higher else '>'} {bar}")
    for gate in sorted(PER_LANGUAGE):
        bar, higher = GATES[gate]
        for language in CELLS[:3]:
            value = gates([r for r in results if r.item.language == language])[gate]
            if under(value, bar, higher):
                found.append(f"{gate} {language} {value:.1f} {'<' if higher else '>'} {bar}")
    for language in CELLS[:3]:
        for length in LENGTHS:
            picked = [r for r in results if r.item.language == language and r.item.length == length]
            value = percent(sum(exact(r.text, r.item.expected, r.item.language) for r in picked), len(picked))
            if len(picked) >= CELL_ROWS and under(value, CELL_BAR, True):
                found.append(f"sentence correctness {language} {length} {value:.1f} < {CELL_BAR}")
    pairs = [(r, score(r.text, r.item.expected, r.item.said)) for r in results]
    sentence = words_right_by_length(pairs)["sentence"]
    long = overall["length parity"]
    if sentence is not None and long is not None and sentence - long > PARITY_GAP:
        found.append(f"length parity {long:.1f} is {sentence - long:.1f} points under sentence")
    return found


def grade(value: float | None, bar: float, higher: bool) -> float | None:
    """0 to 10: 9.5 at the bar, 10 at perfect, half a point off per point short (docs/bench.md, Grades)."""
    if value is None:
        return None
    if higher:
        over = 9.5 + 0.5 * (value - bar) / (100 - bar) if value >= bar else 9.5 - 0.5 * (bar - value)
    else:
        over = 9.5 + 0.5 * (bar - value) / bar if value <= bar else 9.5 - 0.5 * (value - bar)
    graded = max(0.0, over)
    return graded


def grades(results: list[Result]) -> dict[str, float | None]:
    """Each area's grade, its weakest gate (Hinglish and Hindi also its weakest gate cell in those languages, Grammar
    its weakest language), and the overall grade, the weakest area."""
    overall = gates(results)
    by_language = {language: gates([r for r in results if r.item.language == language]) for language in CELLS[:3]}
    found: dict[str, float | None] = {}
    for area, names in AREAS.items():
        values = [grade(overall[name], *GATES[name]) for name in names]
        if area == "Grammar":
            values += [grade(by_language[language]["grammar"], *GATES["grammar"]) for language in CELLS[:3]]
        if area == "Hinglish and Hindi":
            values += [grade(by_language[lang][name], *GATES[name]) for lang in ("hinglish", "hi") for name in GATES]
        graded = [value for value in values if value is not None]
        found[area] = min(graded, default=None)
    areas = [value for value in found.values() if value is not None]
    found["overall"] = min(areas, default=None)
    return found


def money(results: list[Result]) -> dict[str, float]:
    per_tier = {"small": 0.0, "big": 0.0}
    for result in results:
        for number, call in enumerate(result.calls):
            tier = "big" if result.tier == "big" and number == len(result.calls) - 1 else "small"
            per_tier[tier] += cost(call.engine, call.usage.input, call.usage.output, call.usage.cached)
    return {tier: 1000 * dollars / max(len(results), 1) for tier, dollars in per_tier.items()}


def gold_refused(item: Item) -> bool:
    """Whether the guardrail refuses an item's first gold answer, judged as the server judges a piece after its
    prior: a model giving the gold would be refused and the text typed as said."""
    answer = item.expected[0]
    if not item.prior:
        return check(item.text, answer, item.terms) is not None
    # The gold goes on from the prior only past a whole word of it ("Jose" is no prior of "Joseph").
    if answer.startswith(item.prior) and not answer[len(item.prior) : len(item.prior) + 1].isalnum():
        judged = judge(item.prior, item.text, item.terms, None, answer.removeprefix(item.prior).strip())
    else:
        judged = judge(item.prior, item.text, item.terms, answer, "")
    return judged.rejected is not None


def report(spec: str, results: list[Result]) -> bool:
    failed = sum(result.failed for result in results)
    print(f"\n{spec}: {len(results)} items, {failed} provider failures")
    # Gold the guardrail refuses is flagged, never dropped: the bench still measures it (round 3, 11 Oct).
    refused = [result.item.id for result in results if gold_refused(result.item)]
    if refused:
        print(
            f"  guardrail refuses {len(refused)} gold answer{'s' if len(refused) > 1 else ''}: "
            + ", ".join(refused[:10])
        )
    if failed == len(results):
        print("  every request failed: check the key, credits and model name")
        return False
    # A timeout is a measured outcome (the app types the text as said); an error answer is not.
    errors = sum(result.failed and result.ms < DEADLINE_MS - MARGIN_MS for result in results)
    if errors * 100 > MAX_FAILED_PERCENT * len(results):
        print(f"  NOT RUN: {100 * errors / len(results):.1f} % provider errors; no gate is graded on raw input")
        return False
    print(f"  {'gate':<24}{'bar':>7}" + "".join(f"{name:>10}" for name in ["all", *CELLS]))
    table = {name: gates(cell(results, name)) for name in CELLS}
    table["all"] = gates(results)
    missed = failures(results)
    for gate, (bar, higher) in GATES.items():
        values = [table[name][gate] for name in ["all", *CELLS]]
        cells = "".join(f"{'-':>10}" if value is None else f"{value:>10.1f}" for value in values)
        fails = any(failure.startswith(f"{gate} ") for failure in missed)
        print(f"  {gate:<24}{('≥' if higher else '≤') + str(bar):>7}{cells}{'  FAILS' if fails else ''}")
    for failure in missed:
        print(f"  fails: {failure}")
    for name in sorted({result.item.slice for result in results}):
        values = gates([r for r in results if r.item.slice == name])
        shown = ", ".join(f"{gate} {value:.1f}" for gate, value in values.items() if value is not None)
        print(f"  {name}: {shown}")
    graded = grades(results)
    print(
        "  grades: " + ", ".join(f"{area} {'-' if value is None else f'{value:.2f}'}" for area, value in graded.items())
    )
    latencies = sorted(result.ms for result in results if not result.failed)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0
    big = sum(result.tier == "big" for result in results)
    late = sum(result.failed and result.ms >= DEADLINE_MS - MARGIN_MS for result in results)
    print(
        f"  latency ms: p50 {statistics.median(latencies):.0f}, p95 {p95}; timed out {100 * late / len(results):.1f} %;"
        f" big tier {100 * big / len(results):.1f} %"
    )
    dollars = money(results)
    usages = [call.usage for result in results for call in result.calls]
    cached = percent(sum(usage.cached for usage in usages), sum(usage.input for usage in usages)) or 0.0
    print(
        f"  $ per 1,000 dictations: small {dollars['small']:.3f}, big {dollars['big']:.3f}; prompt cache {cached:.1f} %"
    )
    return not missed


def tiers_of(spec: str, budget: Budget) -> Tiers | None:
    """The spec's tiers, metered; the real escalation (`small+big`) has the small model as its fallback, as the
    server's founder pair does (round b5). None, with the reason printed, when a provider can't be made."""
    small, _, big = spec.partition("+")
    keys = {provider: os.environ.get(name, "") for provider, name in KEYS.items()}
    try:
        small_provider = Metered(make_provider(small, keys), budget)
        big_provider = Metered(make_provider(big or small, keys), budget)
    except ValueError as error:
        print(f"\n{spec}: cannot run: {error}")
        return None
    fallback = small_provider if big else None
    return Tiers(small=small_provider, big=big_provider, fallback=fallback)


async def bench(spec: str, items: list[Item], budget: Budget) -> list[Result] | None:
    """The results of the items sent before the budget was reached."""
    tiers = tiers_of(spec, budget)
    if tiers is None:
        return None
    # An engine with no price would spend without counting against the budget.
    unpriced = [
        p.engine for p in (tiers.small, tiers.big) if p.engine not in PRICES and not p.engine.startswith("fake:")
    ]
    if unpriced and budget.limit < math.inf:
        print(f"\n{spec}: cannot run: no price for {unpriced[0]} in config/pricing.py")
        return None
    gate = asyncio.Semaphore(CONCURRENCY)
    try:
        results = await asyncio.gather(*(run(item, tiers, gate, budget) for item in items))
    finally:
        await tiers.small.aclose()
        await tiers.big.aclose()
    sent = [result for result in results if result is not None]
    return sent


def slices(items: list[Item]) -> list[list[Item]]:
    """The items of each slice, in corpus order."""
    by_slice: dict[str, list[Item]] = {}
    for item in items:
        by_slice.setdefault(item.slice, []).append(item)
    groups = list(by_slice.values())
    return groups


def first(items: list[Item], count: int) -> list[Item]:
    """The first `count` items of each slice."""
    picked = [item for group in slices(items) for item in group[:count]]
    return picked


def per_slice(items: list[Item], count: int) -> list[Item]:
    """The same seeded `count` items from each slice, in corpus order, so runs compare like with like."""
    picked = []
    for group in slices(items):
        chosen = random.Random(SEED).sample(range(len(group)), min(count, len(group)))
        picked += [group[index] for index in sorted(chosen)]
    return picked


def heldout(items: list[Item], count: int) -> list[Item]:
    """The same seeded `count` items from each slice, drawn only from rows outside the `--per-slice 40` dev sample
    (a chunked twin of a dev row is the same row), so tuning on dev never sees them."""
    dev = {tuple(item.expected) for item in per_slice(items, DEV_PER_SLICE)}
    picked = []
    for group in slices(items):
        rest = [item for item in group if tuple(item.expected) not in dev]
        chosen = random.Random(SEED).sample(range(len(rest)), min(count, len(rest)))
        picked += [rest[index] for index in sorted(chosen)]
    return picked


def in_split(items: list[Item], split: str) -> list[Item]:
    """The items of the dev or held-out split: a v2 row by its own `split`, an older row by the seeded
    `--per-slice 40` dev sample (a chunked twin goes with its row)."""
    old = [item for item in items if not item.split]
    dev = {tuple(item.expected) for item in per_slice(old, DEV_PER_SLICE)}
    wanted = split == "dev"
    picked = [
        item for item in items if (item.split == split if item.split else (tuple(item.expected) in dev) == wanted)
    ]
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specs", nargs="+")
    parser.add_argument("--split", choices=["dev", "heldout"], help="only the dev or the held-out rows")
    sampling = parser.add_mutually_exclusive_group()
    sampling.add_argument("--limit", type=int, help="the first N items of each slice")
    sampling.add_argument("--per-slice", type=int, help="a seeded sample of N items from each slice")
    sampling.add_argument("--heldout", type=int, help="N seeded items per slice outside the --per-slice 40 sample")
    parser.add_argument("--failures", type=Path, help="write each missed item as a JSON line here")
    parser.add_argument("--ledger", type=Path, help="append this run's spend to this JSONL ledger")
    parser.add_argument("--budget-usd", type=float, help="send nothing more once the ledger and this run reach it")
    options = parser.parse_args()
    items = load()
    if options.split is not None:
        items = in_split(items, options.split)
    if options.limit is not None:
        items = first(items, options.limit)
    if options.per_slice is not None:
        items = per_slice(items, options.per_slice)
    if options.heldout is not None:
        items = heldout(items, options.heldout)
    counts = {name: sum(item.slice == name for item in items) for name in sorted({item.slice for item in items})}
    print(f"{len(items)} items: " + ", ".join(f"{name} {count}" for name, count in counts.items()))
    before = ledger_total(options.ledger) if options.ledger else 0.0
    limit = math.inf if options.budget_usd is None else options.budget_usd
    budget = Budget(limit, before)
    outcomes = []
    missed = []
    note = "crashed"
    try:
        for spec in options.specs:
            results = asyncio.run(bench(spec, items, budget))
            if results == []:
                print(f"\n{spec}: nothing sent")
            outcomes.append(bool(results) and report(spec, results or []))
            missed += [{"spec": spec, **miss} for miss in misses(results or [])]
        note = "budget reached" if budget.reached() else "ok"
    except BaseException as error:
        note = f"crashed: {type(error).__name__}"
        raise
    finally:
        spent(options, budget, note)
    if options.failures is not None:
        lines = [json.dumps(miss, ensure_ascii=False) + "\n" for miss in missed]
        options.failures.write_text("".join(lines), encoding="utf-8")
    if budget.reached():
        print(f"budget reached: ${budget.before + budget.spent:.2f} of ${budget.limit:.2f}")
        return 1
    return 0 if all(outcomes) else 1


def spent(options: argparse.Namespace, budget: Budget, note: str) -> None:
    """Appends the run's line to the ledger, when there is one, and prints what the run and the ledger spent."""
    if options.ledger is not None:
        entry = {"usd": budget.spent, "items": budget.items, "spec": " ".join(options.specs), "note": note}
        with options.ledger.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(entry) + "\n")
    print(f"$ this run: {budget.spent:.4f}; ledger total: {budget.before + budget.spent:.4f}")


if __name__ == "__main__":
    sys.exit(main())
