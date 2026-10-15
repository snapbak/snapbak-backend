"""Whether a model's polish can be trusted (docs/api.md, Guardrail).

The model may punctuate, case, format lists and numbers, fix small grammar slips, put back a word the recognizer
misheard with the one that sounds like it, and drop a retracted span after a correction cue. It may not add words the
user never said, change over 30 % of their words, drop a sentence, flip a negation or a tense, drop a qualifier
("only", "maybe", "today"), lose a dictionary term or answer them. A rejected polish leaves the request text as it
was, and its reason names the rule and the words, which the correction prompt quotes.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from itertools import pairwise

from snapbak_backend.ai.guardrail.corrections import (
    BINDERS,
    CUE_PHRASES,
    CUE_WORDS,
    NEGATION_PHRASES,
    NEGATORS,
    REPEATS,
    WEAK_CUES,
    last_cue,
    negations,
    retracts,
    same_kind,
    used_plainly,
)
from snapbak_backend.ai.guardrail.grammar import (
    FORMAT_WORDS,
    FORMS,
    FUNCTION_WORDS,
    INSERTABLE,
    MONTHS,
    PARTNERS,
    explained,
    form_fix,
    futures,
    joins,
    past_form,
    person,
    qualifiers,
    related,
    retensed,
    timed_forms,
    usage_fixed,
)
from snapbak_backend.ai.guardrail.numbers import (
    ANY_LIST_ITEM,
    BULLETS,
    CONNECTORS,
    DETERMINERS,
    DIGIT_RUN,
    FRACTION_WORDS,
    INDIAN_SCALES,
    ORDINALS,
    SCALES,
    SPELLED,
    VALUES,
    Place,
    digits,
    in_prose,
    is_number,
    list_items,
    number_runs,
    out_of_order,
    read_numbers,
    spoken_numbers,
)
from snapbak_backend.ai.guardrail.respell import as_heard, spoken, undangled
from snapbak_backend.ai.guardrail.shape import NEVER_NAMES, hosts_lowercase, names_as_terms, shaped
from snapbak_backend.ai.guardrail.tokens import (
    Edit,
    align,
    heard_as,
    syllables,
    tokens,
    word_set,
    words,
)
from snapbak_backend.ai.routing import CUE, LETTER, has_cue


class Reject(StrEnum):
    UNHEARD = "unheard"
    TOO_MANY_CHANGES = "too_many_changes"
    DROPPED_SENTENCE = "dropped_sentence"
    NEGATION = "negation"
    TENSE = "tense"
    QUALIFIER = "qualifier"
    REPLY = "reply"
    MALFORMED = "malformed"
    DROPPED_JOIN = "dropped_join"
    TERM = "term"
    DROPPED_ITEM = "dropped_item"
    KEPT_CORRECTION = "kept_correction"
    QUESTION = "question"
    FALSE_LIST = "false_list"
    REORDERED = "reordered"
    FORMAT = "format"
    DROPPED_WORD = "dropped_word"
    NOT_A_QUESTION = "not_a_question"
    NUMBERED = "numbered"
    REPEATED = "repeated"
    UNPOLISHED = "unpolished"
    UNLISTED = "unlisted"


@dataclass(frozen=True)
class Reason:
    """Why an answer was refused: the rule, and the words it is about as the guardrail reads them."""

    rule: Reject
    words: tuple[str, ...] = ()


@dataclass(frozen=True)
class Polished:
    text: str
    revision: str | None
    rejected: Reject | None
    reason: Reason | None = field(default=None, compare=False)


MAX_CHANGED_PERCENT = 30
# Content words left out with no cue taking them back: two in a run lose a phrase, an item or a sentence, and four
# in all lose content, however long the text (a repeated word elsewhere can't stand in for them).
LOST_RUN = 2
LOST_TOTAL = 4
# The words a piece cut at a pause may go on with the sentence before it by; dropped, the piece reads as a new
# sentence glued to the last ("visually appealing How is it going…", founder, 11 Oct).
JOINS = word_set("and then")
SENTENCE_ENDS = ".?!\n"
# A list item with nothing after its number.
EMPTY_ITEM = re.compile(r"^\s*\d+\.\s*$", re.MULTILINE)
SENTENCE_START = re.compile(r"(?:^|[.?!\n]|\n\s*-)\s*$")


REPLY_OPENERS = [
    "sure",
    "certainly",
    "of course",
    "absolutely",
    "here is",
    "here's",
    "here are",
    "i can help",
    "i'd be happy",
    "done",
]


OPENERS = [" ".join(words(opener)) for opener in REPLY_OPENERS]
# Acknowledgements and sign-offs an assistant adds anywhere ("Noted:", "Done.", "Thanks!"): written more often than
# said, they answer the user.
ACKNOWLEDGEMENTS = [
    tuple(words(phrase)) for phrase in "noted|got it|okay|ok|thanks|thank you|hope this helps|done|sure".split("|")
]
ACKNOWLEDGING = {phrase[0] for phrase in ACKNOWLEDGEMENTS}


OPENING = tuple(OPENERS)


# Raw speech of this many words or more is never typed back unchanged: it needs its marks and capitals.
RAW_WORDS = 4
RAW_MARKS = re.compile(r"[.,?!A-Z]")


def unpolished(said_text: str, wrote_text: str) -> bool:
    """An answer with no mark and no capital at all, as raw speech comes ("thanks for the update ill review it
    tonight"): nothing was polished."""
    if len(wrote_text.split()) < RAW_WORDS:
        return False
    latin = re.search(r"[a-z]", wrote_text) is not None
    return latin and RAW_MARKS.search(wrote_text) is None


def added_sentence(wrote_text: str, opcodes: list[Edit], heard: set[str]) -> tuple[str, ...] | None:
    """A sentence the answer ends with that the speaker never said, all of it added and some of it never said at all
    ("... in review? It is merged.", "Done."): an answer or a note to the user. Said words moved there are a reorder."""
    if not opcodes or opcodes[-1][0] != "insert":
        return None
    start = opcodes[-1][3]
    wrote_tokens = tokens(wrote_text)
    if start == 0 or start >= len(wrote_tokens) or not wrote_tokens[start][1]:
        return None
    added = tuple(word for word, _ in wrote_tokens[start:])
    unsaid = [word for word in added if word not in heard and not word.isdigit()]
    return added if unsaid else None


# A sentence or list line written more often than said ("Can you resend? Can you resend?").
SENTENCE_SPLIT = re.compile(r"[.?!\n]+")


def repeated(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    parts = [part for part in SENTENCE_SPLIT.split(wrote_text) if part.strip()]
    # One sentence repeats none (round b6: most answers, so their words aren't read again).
    if len(parts) < 2:
        return None
    counted = Counter(tuple(words(part)) for part in parts)
    twice = (found for found, times in counted.items() if times > 1 and len(found) > 1)
    return next((found for found in twice if occurrences(said, found) < counted[found]), None)


# Joining words a split run-on keeps (rule 2): written ", and" or opening the next sentence, never dropped (round b5:
# "...rules engine. It will select" lost "and").
SPLIT_JOINERS = word_set("and but so because then")
# A full stop, "?" or "!" ending a sentence inside the text, and a list item's marker after it.
SENTENCE_BREAK = re.compile(r"[.?!]+[\"'\u201d\u2019)]*\s+")
ITEM_MARKER = re.compile(r"(?:\d+\.|[-•])\s")


def joiner_drops(said: list[str], wrote_text: str, opcodes: list[Edit]) -> list[tuple[int, int]]:
    """(said index, where in the text the sentence it was dropped before starts) for each joining word said between
    two words the answer kept on either side of a new sentence, and left out there: a run-on split by dropping its
    "and" (round b5). A list item's start is no split, a joining word said twice in a row is a stutter, and a run of
    words left out, as a correction takes back, is another check's."""
    places = {
        written: start
        for op, start, end, written, _ in opcodes
        if op == "delete"
        and end - start == 1
        and said[start] in SPLIT_JOINERS
        and 0 < start < len(said) - 1
        and said[start] not in (said[start - 1], said[start + 1])
    }
    if not places:
        return []
    found = []
    for gap in SENTENCE_BREAK.finditer(wrote_text):
        if ITEM_MARKER.match(wrote_text, gap.end()):
            continue
        written = len(words(wrote_text[: gap.end()]))
        if written in places:
            found.append((places[written], gap.end()))
    return found


def split_joiner(said: list[str], wrote_text: str, opcodes: list[Edit]) -> str | None:
    """The first joining word a split run-on dropped (`joiner_drops`)."""
    drops = joiner_drops(said, wrote_text, opcodes)
    return said[drops[0][0]] if drops else None


# Words that lose the capital they had opening a sentence when a joining word opens it instead ("It" and it).
UNCAPPED = word_set("it its the a an this that these those there they we he she you his her their our my your")


def joiner_put_back(said_text: str, wrote_text: str) -> str | None:
    """The answer with each joining word it dropped at a sentence split put back opening that sentence ("...queue. And
    it will pick"), which rule 2 allows; None when there is none to put back."""
    said = words(plain_marks(said_text))
    drops = joiner_drops(said, wrote_text, align(said, words(wrote_text)))
    if not drops:
        return None
    mended = wrote_text
    for start, at in sorted(drops, key=lambda drop: -drop[1]):
        joiner = said[start]
        rest = mended[at:]
        head, _, tail = rest.partition(" ")
        if head.lower() in UNCAPPED:
            head = head.lower()
        opening = joiner[:1].upper() + joiner[1:]
        mended = f"{mended[:at]}{opening} {head} {tail}".rstrip() if tail else f"{mended[:at]}{opening} {head}"
    return mended


# Lines a list, an email's greeting and sign-off, or a spoken line break may make; nothing else breaks a line.
LINE_WORDS = word_set("line lines paragraph")
SHORT_LINE_WORDS = 2
SHORT_SPEECH = 8
SHORT_LINES = 3


def bare_lines(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    """Short lines with no list marker where the speech had no line at all ("tom cooks lisa cleans" Tom / Cooks /
    Lisa / Cleans, round b5): a list never asked for. A line ending with a comma, and the line after it, are a
    greeting or a sign-off."""
    if not LINE_WORDS.isdisjoint(said) or ANY_ITEM_LINE.search(wrote_text):
        return None
    lines = [line.strip() for line in wrote_text.splitlines() if line.strip()]
    # A short dictation breaks a line only around a greeting or a sign-off ("Signed by\nPatel" is one sentence,
    # round b6).
    unsigned = [i for i in range(1, len(lines)) if not lines[i - 1].endswith(",") and not lines[i].endswith(",")]
    if len(said) < SHORT_SPEECH and unsigned:
        return (lines[unsigned[0] - 1], lines[unsigned[0]])
    short = [
        line
        for i, line in enumerate(lines)
        if len(line.split()) <= SHORT_LINE_WORDS and not line.endswith(",") and not (i and lines[i - 1].endswith(","))
    ]
    return tuple(short[:2]) if len(short) >= SHORT_LINES else None


def replies(said: list[str], wrote: list[str]) -> bool:
    wrote_start = " ".join(wrote[:3])
    # Most answers open with no reply word: one check of them all.
    if not wrote_start.startswith(OPENING):
        return False
    # A paragraph or line said first opens nothing ("new paragraph here is what ...").
    opened = 2 if said[:1] == ["new"] and said[1:2] in (["paragraph"], ["line"]) else 0
    said_start = " ".join(said[opened : opened + 3])
    return any(wrote_start.startswith(o) and not said_start.startswith(o) for o in OPENERS)


def acknowledges(said: list[str], wrote: list[str]) -> tuple[str, ...] | None:
    """An acknowledgement written more often than said ("Noted:", "Thanks!", a doubled "okay"), named."""
    present = ACKNOWLEDGING.intersection(wrote)
    if not present:
        return None
    found = (phrase for phrase in ACKNOWLEDGEMENTS if phrase[0] in present)
    return next((phrase for phrase in found if occurrences(wrote, phrase) > occurrences(said, phrase)), None)


# Pairs a letter or a syllable apart that speech to text swaps all the same, and number words with a sound-alike
# ("to tickets" two, "two long" too, "for pm" 4 pm): a number word may be fixed for a word that sounds like it,
# never for another number.
CONFUSIONS = {
    frozenset(pair.split("/"))
    for pair in (
        "cash/cache of/have ask/axe specific/pacific suite/sweet patients/patience flour/flower hour/our quite/quiet "
        "aloud/allowed no/know to/two two/too to/too for/four one/won ate/eight bones/bonus chorus/course hours/ours "
        "formally/formerly negate/negotiate weight/wait"
    ).split()
}
# Words a misheard-word fix never changes: a negation, a correction cue or a promise. A pair above may still swap:
# "no" and "know" are the one negation pair speech to text confuses ("I no the florist" know). A cue word may be
# written for its sound-alike, though ("weight in the lobby" wait).
UNSWAPPABLE = NEGATORS | CUE_WORDS | word_set("will shall")
NEVER_WRITTEN = NEGATORS | word_set("will shall")
# A said run of up to three words may be one term's spoken form ("post gres q l" PostgreSQL).
LONGEST_SPOKEN_TERM = 3
# Two small words for one another ("in" "on", "is" "as") change what was meant; a fix needs a word that isn't one.
SMALL_WORDS = FUNCTION_WORDS | word_set("as by from into onto off up out but so if or nor")
# Words whose loss alone loses no content: grammar, spoken marks, cues and list counts, the words of a number or
# a time a symbol or digits replace ("twenty euros" €20, "half past three" 3:30, "point"), the joining words of a
# list ("then"), and words a grammar fix drops ("discuss about", "more clearer", "proceed ahead", "at the rate" @).
PLAIN_WORDS = (
    SMALL_WORDS
    | FORMAT_WORDS
    | CUE_WORDS
    | ORDINALS
    | BULLETS
    | word_set("plus double triple then equals point pound pounds euro euros ahead more much having rate please thanks")
)
# One misheard-word fix in ten said words is free; each past that costs two changes, as a rewrite would.
FIX_EVERY = 10
OVER_FIX_COST = 2

LONG_KEY = 4
LONG_JOIN = 5

type Swap = tuple[range, range]


@cache
def sound(word: str) -> tuple[str, int]:
    return heard_as(word), syllables(word)


def sounds_alike(heard: list[str], wrote: list[str]) -> bool:
    """Words for others the recognizer could have heard instead: the same sounds. A hyphen joins words without a
    sound ("brake glass" Break-glass)."""
    said, written = "".join(heard), "".join(wrote).replace("-", "")
    (key, beats), (other, other_beats) = sound(said), sound(written)
    # A longer word may be heard with a vowel sound more or less ("manger" manager, "fiscal" physical), and so may
    # words heard across a wrong word boundary ("get way" gateway); one short word may not ("debit" debt).
    loose = len(key) >= LONG_KEY or len(heard) + len(wrote) > 2
    beats_off = abs(beats - other_beats)
    if key == other:
        return key not in ("", "A") and (beats_off == 0 or (beats_off == 1 and loose))
    # A longer word heard without its last "t" or "d" ("sore through" throat) or an "r" ("sever" server).
    # A past's "-ed" is a tense, never a dropped sound ("finished" finish).
    inflected = said.endswith("ed") or written.endswith("ed")
    longer = min(len(said), len(written)) >= LONG_JOIN and beats_off == 0 and not inflected
    clipped = key + "t" == other or other + "t" == key or key + "d" == other or other + "d" == key
    unrolled = beats > 1 and key.replace("r", "") == other.replace("r", "") and abs(len(key) - len(other)) == 1
    return longer and (clipped or unrolled)


def edits(one: str, other: str) -> int:
    """Levenshtein distance, row by row."""
    row = list(range(len(other) + 1))
    for i, letter in enumerate(one, 1):
        diagonal, row[0] = row[0], i
        for j, against in enumerate(other, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (letter != against))
    return row[-1]


def unaccented(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(letter for letter in decomposed if not unicodedata.combining(letter))


def heard_term(word: str, said: list[str], wrote: set[str] | frozenset[str] = frozenset()) -> list[str] | None:
    """The said run a term word was written for, one that sounds like it, wherever it is: a correction may have
    moved it. A run whose words are written elsewhere was not heard for the term ("the engine makes" keeps
    "makes")."""
    windows = (said[i : i + size] for size in range(1, LONGEST_SPOKEN_TERM + 1) for i in range(len(said)))
    unwritten = (window for window in windows if all(w in FUNCTION_WORDS or w not in wrote for w in window))
    return next((window for window in unwritten if near("".join(window), word, whole=True)), None)


def near(said: str, term: str, whole: bool = False) -> bool:
    """A term's spoken form: its consonants, or for a longer one one off ("wakeen" wkn Joaquin jkn), a voiced
    consonant for its voiceless twin ("cooper netties" Kubernetes). `whole`: the heard sounds cover the term, so
    "razor" is never Razorpay."""
    if unaccented(said) == unaccented(term):
        return True
    # A mark inside a term is said as a word ("c plus plus" C++, "c sharp" C#).
    term = SPOKEN_TERM_MARKS.sub(lambda found: TERM_MARK_WORDS[found.group()], term)
    if said == term:
        return True
    heard, spelled = (re.sub("[aeiouyA]", "", heard_as(word)) for word in (said, term))
    if heard == spelled or max(len(heard), len(spelled)) < 3:
        return heard == spelled
    # A part of the term heard, or a plain word with a sound more ("canvas" is no Canva), is no spoken form.
    if whole and abs(len(heard) - len(spelled)) == 1 and (spelled.startswith(heard) or heard.startswith(spelled)):
        return False
    return edits(heard, spelled) <= 1 or heard.translate(VOICELESS) == spelled.translate(VOICELESS)


VOICELESS = str.maketrans("bdgvj", "ptkfc")
TERM_MARK_WORDS = {"+": "plus", "#": "sharp", "&": "and"}
SPOKEN_TERM_MARKS = re.compile(r"[+#&]")


NAME = re.compile(r"(?<![.?!:\n-])(?<!^)\s+([A-Z][a-z]+)")


def names(text: str) -> set[str]:
    """Capitalized words inside a sentence: names, which a misheard-word fix never changes."""
    found = {name.lower() for name in NAME.findall(text)}
    return found


def swappable(said: list[str], wrote: list[str], terms: set[str]) -> bool:
    """Words a sound-alike fix may change: no number, unless said words join into one word that is none ("ten your"
    tenure, "four cast" forecast), and no negation, cue, promise or term."""
    joined = len(said) > 1 and len(wrote) == 1 and not is_number(wrote[0])
    numbers = not joined and any(is_number(word) for word in (*said, *wrote))
    small = set(said) <= SMALL_WORDS and set(wrote) <= SMALL_WORDS
    kept = all(word not in UNSWAPPABLE and word not in terms for word in said if not word.isdigit())
    return not numbers and not small and kept and all(word not in NEVER_WRITTEN and word not in terms for word in wrote)


# How many said words for how many written ones a fix may take: one for one, two for one ("a head" ahead), one for
# two ("alot" a lot), and three for one only for a term ("post gres q l" PostgreSQL).
SHAPES = ((1, 1), (2, 1), (1, 2), (3, 1))
TERM_SHAPES = ((3, 1), (2, 1), (1, 1), (1, 2))


@dataclass(frozen=True)
class Speech:
    """What a fix is judged against: both texts, for names."""

    said: str
    wrote: str


def fits(heard: list[str], written: list[str], terms: set[str], speech: Speech, exact: bool) -> bool:
    """Whether `written` puts back what the recognizer heard as `heard`: in the exact pass the same letters or a
    known pair, otherwise a term's spoken form or another word with the same sounds that is no name."""
    joined_said, joined_written = "".join(heard), "".join(written)
    term = any(word in terms for word in written)
    # Three words make one only for a term or a long word ("in sure ance" insurance).
    if len(heard) == LONGEST_SPOKEN_TERM and not term and len(sound(joined_written)[0]) < LONG_JOIN:
        return False
    if exact:
        # A digit is written for its number word ("at for pm" 4 pm).
        spelled = SPELLED.get(joined_written, set()) | {joined_written}
        return joined_said == joined_written or any(frozenset((joined_said, word)) in CONFUSIONS for word in spelled)
    if term:
        return near(joined_said, joined_written, whole=True)
    alike = swappable(heard, written, terms) and (sounds_alike(heard, written) or plural_alike(heard, written))
    return alike and not set(heard + written) & (names(speech.said) | names(speech.wrote))


def plural_alike(heard: list[str], written: list[str]) -> bool:
    """A sound-alike written in its plural ("check the car break" brakes), round b4."""
    plural = len(written) == 1 and written[0].endswith("s") and not heard[-1].endswith("s")
    return plural and sounds_alike(heard, [written[0][:-1]])


def swaps(said: list[str], wrote: list[str], op: Edit, terms: set[str], speech: Speech) -> list[Swap]:
    """The misheard-word fixes in a replaced run: said words for written ones that sound the same, one for one, two
    for one ("a head" ahead) or one for two ("alot" a lot), or a term written for its spoken form. Exact
    joins and splits go first, so "mapsme" is "Maps.me", not "maps" alone."""
    _, start, end, written_start, written_end = op
    found: list[Swap] = []
    taken: set[int] = set()
    j = written_start
    while j < written_end:
        offset = start + j - written_start
        # A term may stand anywhere in the run it replaced; another word near its own place.
        near_place = range(start, end) if wrote[j] in terms else range(max(start, offset - 2), min(end, offset + 3))
        # A term takes the longest run heard for it ("a sigh ee" Açaí, never "a sigh" and a left-over "ee").
        shapes = TERM_SHAPES if wrote[j] in terms else SHAPES
        options = (
            (exact, range(i, i + said_size), range(j, j + written_size))
            for exact in (True, False)
            for said_size, written_size in shapes
            for i in near_place
            if i + said_size <= end and j + written_size <= written_end and not taken & set(range(i, i + said_size))
        )
        match = next(
            (
                (heard, written)
                for exact, heard, written in options
                if fits(said[heard.start : heard.stop], wrote[written.start : written.stop], terms, speech, exact)
                and (
                    wrote[written.start] in terms
                    or counts_alike(said[heard.start : heard.stop], wrote[written.start : written.stop], wrote, written)
                )
                and slot_fits(wrote[written.start : written.stop], wrote, written)
                and (
                    wrote[written.start] not in terms
                    or named_slot(wrote, written.start, said, said[heard.start : heard.stop])
                )
            ),
            None,
        )
        if match is None:
            j += 1
            continue
        found.append(match)
        taken |= set(match[0])
        j = match[1].stop
    return found


AT_TIMES = word_set("at by around till until from after before")
# Sound-alikes a recognizer mixes up, and where the word said is right as it stands: a word before or after it that
# only it fits ("more taste", "a piece of", "taller than", "their office", "be there at", "brake pads"). A fix that
# swaps it there writes the wrong word.
HOMOPHONES = [
    word_set(group)
    for group in (
        "taste test",
        "piece peace",
        "than then",
        "loose lose",
        "patients patience",
        "principal principle",
        "accept except",
        "their there they're",
        "brake break",
        "weather whether",
        "rain reign",
        "right write",
        "waste waist",
    )
]
HOMOPHONE_OF = {word: group - {word} for group in HOMOPHONES for word in group}
LOCKED = {
    word: (word_set(before), word_set(after))
    for word, before, after in (
        ("taste", "more less good bad bitter sweet great nice salty sour strange no", "good bad great like of buds"),
        (
            "test",
            "to unit blood driving load stress smoke a",
            "suite suites case cases results run runs build plan report",
        ),
        ("piece", "a one each every this that", "of"),
        ("peace", "at in world inner make", "talks treaty process"),
        ("than", "more less rather other better worse fewer", ""),
        ("then", "and since by until till just back now only", ""),
        ("loose", "is are was were too so very come came hang let cut set", "ends end change fit thread"),
        ("lose", "to will would might may could can don't didn't not never", "weight money track time"),
        ("patients", "our many all new", "are were have waiting need who"),
        ("patience", "have has had lost no little more some your my of", "is runs"),
        ("principal", "school", "amount balance sum loan payment investor engineer architect designer said asked"),
        ("principle", "in on guiding basic core first", ""),
        ("accept", "i we you they he she to will would can could please not don't didn't", ""),
        ("except", "everyone everything all everybody nothing anyone anything no", "for that when"),
        ("brake", "", "pads pad pedal pedals warning light fluid system line lines check"),
        ("break", "a coffee lunch tea take short quick", "room time up down even into"),
        ("weather", "the bad good nice", "is was forecast report today"),
        ("waste", "to not don't a food", "time money food of management"),
    )
}
# "their" goes before a noun; "there" before a verb, a preposition or nothing.
THERE_AFTER = word_set(
    "is are was were will has have at in on by and too yet soon now today tomorrow yesterday tonight early late again"
)


def other_person(said: list[str], wrote: list[str], opcodes: list[Edit]) -> str | None:
    """A pronoun written in place of said ones that name someone else ("we" They, "he" She, "him is" They are),
    though it was said elsewhere: only a pronoun's case may change ("him is" He is)."""
    for op, start, end, written_start, written_end in opcodes:
        if op != "replace":
            continue
        named = {person(word) for word in said[start:end]} - {None}
        other = next((word for word in wrote[written_start:written_end] if person(word) not in named | {None}), None)
        if named and other is not None:
            return other
    return None


def right_word_swapped(said: list[str], wrote: list[str], opcodes: list[Edit]) -> str | None:
    """A sound-alike written for a said word that was right where it stood, named as written."""
    for op, start, end, written_start, written_end in opcodes:
        if op != "replace" or end - start != written_end - written_start:
            continue
        for k in range(end - start):
            heard, written = said[start + k], wrote[written_start + k]
            if written in HOMOPHONE_OF.get(heard, ()) and locked(said, start + k):
                return written
    return None


def locked(said: list[str], i: int) -> bool:
    word = said[i]
    before = said[i - 1] if i else ""
    after = said[i + 1] if i + 1 < len(said) else ""
    if word == "their":
        # "their coming" is "they're coming".
        return bool(after) and after not in THERE_AFTER and after not in SMALL_WORDS and not after.endswith("ing")
    if word == "there":
        return not after or after in THERE_AFTER
    if word == "than" and len(before) > 3 and before.endswith("er"):
        return True
    befores, afters = LOCKED.get(word, (set(), set()))
    return before in befores or after in afters


# A word a count is said before: a plural, also an irregular one, or a time's "am" or "pm" ("two tickets", "two extra
# chairs", "two people", "four pm").
COUNTED = re.compile(r"^(?:[a-z]{3,}s|am|pm|people|children|men|women|feet|teeth)$")


def counts_alike(heard: list[str], written: list[str], wrote: list[str], place: range) -> bool:
    """A one-word fix between a number and a word that sounds like it counts only where a count goes: before a plural
    or a time ("to tickets" two, "for pm" 4 pm), and the other way only where none goes ("two long" too); "want to
    go" stays."""
    if len(heard) != 1 or len(written) != 1 or is_number(heard[0]) == is_number(written[0]):
        return True
    after = wrote[place.stop : place.stop + 2]
    before = wrote[place.start - 1] if place.start else ""
    # A count may have one word before its plural ("two extra chairs"), and a time its "p.m." or minutes ("at 8:30").
    meridiem = "".join(after) in MERIDIEMS
    minutes = before in AT_TIMES and bool(after) and after[0].isdigit() and len(after[0]) == 2
    # A clock time ends the sentence after "at" ("see you at for" four).
    clock = before in AT_TIMES and place.stop == len(wrote)
    counted = meridiem or minutes or clock or any(COUNTED.match(word) is not None for word in after)
    # "one" goes before a thing ("i have won question" one question); "won" before an object or alone ("we won two").
    thing = (
        bool(after) and after[0] not in SMALL_WORDS and after[0] not in DETERMINERS_BEFORE and not is_number(after[0])
    )
    single = written[0] == "one" and thing and before not in SUBJECTS_BEFORE
    return counted or single if is_number(written[0]) else not counted


# Words after which a sound-alike grammar word never goes ("the whether"), a verb's slot ("will except"), and the
# words before a term written for a plain word that fits there ("the Lyft is broken", "I Reddit").
DETERMINERS_BEFORE = word_set("the a an my your our his her their this that these those its")
GRAMMAR_ALIKES = word_set("whether there than too except")
VERB_SLOTS = word_set("will would can could should must to may might shall cannot can't won't don't didn't")
SUBJECTS_BEFORE = word_set("i we you he she they")
# "the lift", never "a Lyft" ("we booked a Lyft", "a Xiaomi"), which names one.
POINTING = word_set("the this that these those")


def slot_fits(written: list[str], wrote: list[str], place: range) -> bool:
    """Whether a one-word fix fits where it stands: a grammar word never after a determiner ("the weather" whether),
    "except" never where a verb goes ("i will accept" except)."""
    if len(written) != 1 or written[0] not in GRAMMAR_ALIKES or place.start == 0:
        return True
    before = wrote[place.start - 1]
    after_determiner = before in DETERMINERS_BEFORE
    verb_slot = written[0] == "except" and before in VERB_SLOTS
    return not after_determiner and not verb_slot


def term_slot(wrote: list[str], i: int, said: list[str]) -> bool:
    heard = heard_term(wrote[i], said, set(wrote))
    return heard is not None and named_slot(wrote, i, said, heard)


# A dictation that opens with one of these asks a question ("is the deck ready").
AUXILIARIES = word_set("is are was were do does did can could will would should has have shall may")
# Words before a term that put it where a verb goes ("can Lyft", "help me Lyft", "please Xiaomi", "have Reddit"); a
# "to" only after these ("want to lift"), not after a noun ("upload the photos to Flickr").
VERB_LEADS = (VERB_SLOTS - {"to"}) | word_set("have has had did does do please me help let i we you he she they")
TO_VERB = word_set(
    "want wants wanted need needs needed have has had going able try tried trying used ought how start started starts "
    "begin began begins continue continued seem seems seemed"
)
# Words before a number word heard for a term that name a tool ("in Xero", "our Xero invoices", "from Xero"); "start
# from zero", "was zero", "down to zero" are numbers.
TOOL_LEADS = word_set("in on into via through using our my your their the with from")
STARTING = word_set("start starts started starting begin began beginning restart scratch")
# Nouns a number word counts ("zero balance", "zero waste"), never a term's.
COUNTED_NOUNS = word_set("balance balances percent interest tolerance waste emissions fee fees cost risk sum degrees")
# "use Xero for accounting": a tool's verb with a preposition or nothing after the term.
USING_TOOL = word_set(
    "use uses used using try tried switch switched check checked open opened sync synced export import"
)
# Verbs a "the" and a term may follow ("book the Lyft").
USING = word_set("book booked booking call called order ordered take took get got open opened use used")
# A word after a term that shows it was a plain word's verb or predicate ("the lift is broken", "lights flicker at").
PLAIN_AFTER = AUXILIARIES | FUNCTION_WORDS | word_set("at when every again all")


def named_slot(wrote: list[str], i: int, said: list[str], heard: list[str]) -> bool:
    """Whether a term written for a different word said stands where the term is meant (D4): a name's slot, never a
    verb's ("He can lift", "help me lift"), never a number's ("start from zero", "the score was zero to one"), never a
    plain noun's after "the" with a verb after it ("the lift is broken"). A run of words heard for one term is no
    plain word ("the cooper netties cluster"), and a term said as itself stands anywhere."""
    if wrote[i] in said or unaccented(wrote[i]) in said:
        return True
    before = wrote[i - 1] if i else ""
    after = wrote[i + 1] if i + 1 < len(wrote) else ""
    if any(is_number(word) for word in heard):
        starting = i >= 2 and wrote[i - 2] in STARTING
        used = before in USING_TOOL and (not after or after in BESIDE or after in COORDINATORS)
        tool = (before in TOOL_LEADS and not starting) or used
        return tool and after != "to" and not is_number(after) and after not in COUNTED_NOUNS
    infinitive = before == "to" and i >= 2 and (wrote[i - 2] in TO_VERB or wrote[i - 2] in SUBJECTS_BEFORE)
    if before in VERB_LEADS or infinitive or (before == "a" and i >= 3 and wrote[i - 3] in ("give", "gave")):
        return False
    # A term opening a sentence before "the" is a verb's place ("Show me the slides").
    if i == 0 and after in DETERMINERS_BEFORE:
        return False
    if len(heard) > 1:
        return True
    plural_subject = len(before) > 3 and before.endswith("s") and not before.endswith("ss") and after in PLAIN_AFTER
    if before in POINTING:
        governed = i >= 2 and wrote[i - 2] in USING
        attributive = bool(after) and after not in PLAIN_AFTER and not past_form(after) and not after.endswith("s")
        return governed or attributive
    return not plural_subject


def no_swaps(op: Edit) -> list[Swap]:
    return []


# Marks said as words; the words of one are free where it is said.
SPOKEN_MARKS = [
    tuple(words(phrase))
    for phrase in (
        "open quote|close quote|end quote|unquote|quote|open bracket|close bracket|open paren|close paren|"
        "open parenthesis|close parenthesis|exclamation mark|exclamation point|new paragraph|new line|next line|"
        "dot dot dot"
    ).split("|")
]
MARK_STARTS = {phrase[0] for phrase in SPOKEN_MARKS}
# A mark word after one of these is a noun, never a mark ("the period of the contract", "a comma splice").
NOUN_MARKERS = word_set("the a an each every")
COORDINATORS = word_set("or and")
# A cue this close to a left-out run may be taking it back; a lone "no" there is a negation.
CUE_NEAR = 4
TAKING_CUES = {cue for cue in CUE_PHRASES if not (len(cue) == 1 and cue[0] in NEGATORS)}


def spoken_mark_spots(said: list[str], marks: list[bool]) -> set[int]:
    """Where a mark is said as words ("open quote", "new paragraph"), unless a noun marker comes before it."""
    spots: set[int] = set()
    for i, word in enumerate(said):
        if word not in MARK_STARTS or named_mark(said, marks, i):
            continue
        phrase = next((phrase for phrase in SPOKEN_MARKS if tuple(said[i : i + len(phrase)]) == phrase), ())
        spots |= set(range(i, i + len(phrase)))
    return spots


def named_mark(said: list[str], marks: list[bool], i: int) -> bool:
    return i > 0 and said[i - 1] in NOUN_MARKERS and not marks[i]


def taken_back(said: list[str], run: list[int], output: list[str], kept: set[str], marks: list[bool]) -> bool:
    """Whether a cue may take back the left-out words at `run`: one said just after them, or one later followed by
    the words written in their place ("his green journal ... actually blue notebook" his blue notebook). Words after
    a cue go only when they restate words kept, all but one written elsewhere ("i mean sourav's name" back into the
    prior)."""
    # A cue takes back words before it, never words after the replacement that follows it, and only when it reads as
    # one ("the brake warning actually stayed off" keeps "warning").
    after = run[-1] + 1
    near = any(
        tuple(said[i : i + len(cue)]) == cue and not used_plainly(said, marks, run[0], i, i + len(cue))
        for cue in TAKING_CUES
        for i in range(after, min(after + CUE_NEAR, len(said)))
    )
    if not near:
        before = said[max(0, run[0] - CUE_NEAR) : run[0]]
        cue_before = any(tuple(before[i : i + len(cue)]) == cue for cue in TAKING_CUES for i in range(len(before)))
        restating = len(run) > 1 and sum(said[i] not in kept for i in run) <= 1
        near = cue_before and restating
    if near or not output:
        return near
    later = said[run[-1] + 1 :]
    cue = last_cue(later)
    replacement = set(later[cue[1] :]) if cue else set()
    return bool(replacement) and all(word in replacement for word in output if content(word))


def content(word: str) -> bool:
    """A word that carries what was said: no grammar, spoken mark, cue, list count or number."""
    return word not in PLAIN_WORDS and not is_number(word)


def changed(
    said: list[str],
    marks: list[bool],
    wrote: list[str],
    opcodes: list[Edit],
    items: set[int],
    alike: Callable[[Edit], list[Swap]] = no_swaps,
    named: frozenset[str] = frozenset(),
    terms: frozenset[str] = frozenset(),
    as_list: bool = False,
    spoken: list[tuple[Place, str]] | None = None,
) -> tuple[int, list[str], list[list[str]], list[str]]:
    """Said words the output changed or dropped, plus grammar words it added that a fix never needs ("and", "or"),
    and the retracted words, which are free. Dropping "make that" or "change that" though it takes nothing back
    drops a verb ("make that call" "Call"), so it costs every said word; a lone "sorry" is a filler. A fix adds a
    grammar word of its groups ("the", "is") about once in four said words; each one past that is a change. A
    misheard word fixed (`alike`) is free once in ten said words; each one past that costs two. Also each run of
    content words the output left out with nothing taking them back, wherever else those words recur."""
    retracted: list[str] = []
    lost_runs: list[list[str]] = []
    dropped: list[str] = []
    kept = set(wrote) | set(digits(wrote))
    inserted = [
        word for op, _, _, start, end in opcodes if op == "insert" for word in wrote[start:end] if word in INSERTABLE
    ]
    count = max(0, len(inserted) - (len(said) // 4 + 1))
    new = [word for op, _, _, start, end in opcodes if op != "equal" for word in wrote[start:end]]
    moved = set(new) | set(digits(new))
    mark_spots = spoken_mark_spots(said, marks) if not MARK_STARTS.isdisjoint(said) else set()
    mark_spots |= dated(said, wrote) if not MONTHS.isdisjoint(said) else set()
    mark_spots |= scaled(said, wrote) if not INDIAN_SCALES.keys().isdisjoint(said) else set()
    fixes = 0
    # Numbers move with a correction ("three no four" 4), so they are read where said only without a cue.
    cued = spoken is None or not CUE_WORDS.isdisjoint(said)
    spoken = spoken or []
    for op, start, end, written_start, written_end in opcodes:
        span = said[start:end]
        output = wrote[written_start:written_end]
        if op == "equal" or (op == "delete" and tuple(span) in REPEATS):
            continue
        added = [word for word in output if word in FUNCTION_WORDS and word not in INSERTABLE and word not in span]
        if added and op == "replace":
            # A small word written for its sound-alike was heard, not added ("are" for "our").
            sounded = alike((op, start, end, written_start, written_end))
            heard_for = {wrote[j] for _, written in sounded for j in written}
            added = [word for word in added if word not in heard_for]
        # A short command has no room for an added word: "send report" can't become "I send the report". An "or" or
        # "and" put where a cue was keeps both versions ("three no four" 3 or 4).
        joining = not COORDINATORS.isdisjoint(added) and last_cue(span) is not None
        count += len(said) * len(added) if len(said) < 4 or joining else len(added)
        # A spoken mark is free unless a grammar word took its place ("and" to "or").
        free = set() if added else FORMAT_WORDS
        # A number word is fixed by a number in its place, or as a bullet's own count ("bullet one").
        numbered = any(is_number(word) for word in output)
        listed = not items.isdisjoint(range(written_start, written_end))
        marker = {i for i in range(start + 1, end) if said[i - 1] in BULLETS}
        # An ordinal that marks a step of a list the answer made ("first turn off the ac then ..." - Turn off the AC).
        marker |= {
            i
            for i in range(start, end)
            if as_list and not items and said[i] in ORDINALS and not in_prose(said, marks, i)
        }
        # A number word is fixed only by digits that say it ("step two" can't become "step 3").
        numbered = numbered and (cued or says_written(range(start, end), output, spoken))
        counted = {i for i in range(start, end) if numbered and not (listed and in_prose(said, marks, i))}
        # "number one", "step two": the word that names a list's count goes with it.
        counter = {
            i for i in range(start, end) if said[i] in COUNTERS and i + 1 in marker | counted and (items or as_list)
        }
        fixed = {
            i
            for i, word in enumerate(span, start)
            if (word in free and not named_mark(said, marks, i))
            or i in mark_spots
            or (is_number(word) and i in counted | marker)
            or i in counter
            or (word in REDUNDANT and redundant(said, i, wrote))
            # "did not" written "didn't".
            or (word == "not" and any(other.endswith("n't") for other in output))
            or form_fix(word, output)
        }
        if len(fixed) < len(span) and op == "replace":
            found = alike((op, start, end, written_start, written_end))
            heard = [said_range for said_range, _ in found]
            # A term written for its spoken form is the user's own word, never a misheard one to ration.
            fixes += sum(not any(wrote[j] in terms for j in written) for _, written in found)
            fixed |= {i for said_range in heard for i in said_range}
        if len(fixed) == len(span):
            continue
        begin = retracts(said, marks, start, end, kept, moved, named)
        written_here = {word.replace("'", "") for word in output}
        unfixed = [i for i in range(start, end if begin is None else begin) if i not in fixed]
        run = [i for i in unfixed if content(said[i]) and said[i].replace("'", "") not in written_here]
        # Words before the version a cue takes back are kept words, whatever cue follows them.
        if run and (begin is not None or not taken_back(said, run, output, kept, marks)):
            lost_runs.append([said[i] for i in run])
            # A content word left out with nothing in its place ("my teacher asked" My asked).
            # Nothing new is written there: only forms of the words said around it ("recruiter" recruiters).
            # A sound-alike put back for another said word, or a usage partner, writes nothing in its place.
            swapped = {j for _, written in alike((op, start, end, written_start, written_end)) for j in written}
            new_words = [word for j, word in enumerate(output, written_start) if j not in swapped]
            nothing_for = not any(
                content(word) and not any(related(one, word) or word in PARTNERS.get(one, ()) for one in span)
                for word in new_words
            )
            dropped += [said[i] for i in run] if nothing_for else []
        if begin is not None:
            retracted += said[begin:end]
            count += sum(i not in fixed for i in range(start, begin))
            continue
        cue = last_cue(span)
        words_of_cue = () if cue is None else tuple(span[cue[0] : cue[1]])
        cue_dropped = not set(words_of_cue) & set(output)
        # A weak cue before "for" or "about" is a verb ("wait for", "sorry about"): dropped, the sentence breaks.
        bound = cue is not None and cue[1] == len(span) and end < len(said) and said[end] in BINDERS
        misread = words_of_cue in WEAK_CUES and cue_dropped and (len(words_of_cue) > 1 or bound)
        # A mark word used as a noun ("the period of the contract") is a word, never a mark.
        misread = misread or any(
            i not in fixed and said[i] in FORMAT_WORDS and named_mark(said, marks, i) for i in range(start, end)
        )
        # A number dropped with no digits in its place loses a quantity, a time or an order, unless it moved: outside
        # a list, every saying of its value is still written ("the ninth of september" September 9).
        lost = any(
            is_number(word) and (bool(items) or not moved_number(word, said, wrote))
            for i, word in enumerate(span, start)
            if i not in fixed
        )
        count += len(said) if misread or lost else len(span) - len(fixed)
    count += OVER_FIX_COST * max(0, fixes - max(1, len(said) // FIX_EVERY))
    return count, retracted, lost_runs, dropped


COUNTERS = word_set("number step point item")
# Words a fix may drop where they say nothing: "past" or "after" inside a time ("ten after three" 3:10), and "about"
# or "regarding" after a verb that takes its object straight ("discuss about") or beside another preposition ("asked
# about for", "look into about").
REDUNDANT = word_set("past after over about regarding")
# "past" and "after" inside a time, "over" inside a reading ("one fifty over ninety" 150/90).
CLOCK_JOINS = word_set("past after over")
DIRECT_VERBS = word_set(
    "discuss discussed discusses discussing describe described explain explained explaining mention mentioned "
    "emphasize emphasized stress stressed request requested check checked checking order ordered"
)
BESIDE = word_set("for into to on of at in with")


def redundant(said: list[str], i: int, wrote: list[str]) -> bool:
    before = said[i - 1] if i else ""
    after = said[i + 1] if i + 1 < len(said) else ""
    if said[i] in CLOCK_JOINS:
        clock = (is_number(before) or before in ("half", "quarter")) and is_number(after)
        return clock and any(word.isdigit() for word in wrote)
    return before in DIRECT_VERBS or before in BESIDE or after in BESIDE


def says_written(places: range, output: list[str], spoken: list[tuple[Place, str]]) -> bool:
    """Whether the numbers written in a replaced run say a number its words said, read where they stand ("step
    two" can't become "step 3" for a "three" said later)."""
    said_values = {value for place, value in spoken if place[0] in places}
    written = set(digits(output)) | {str(VALUES[word]) for word in output if word in VALUES}
    return not said_values or not written.isdisjoint(said_values)


def dated(said: list[str], wrote: list[str]) -> set[int]:
    """The words of a day said "the third of march" or "the twenty second of november" that the answer writes as
    digits ("March 3", "November 22"): the day's words and the "the" and "of" around them."""
    spots: set[int] = set()
    written = set(digits(wrote))
    for i in range(2, len(said)):
        if said[i] not in MONTHS or said[i - 1] != "of" or not is_number(said[i - 2]):
            continue
        start = i - 2
        while start > 0 and is_number(said[start - 1]):
            start -= 1
        day = read_numbers(said[start : i - 1])
        if day and str(day[-1][1]) in written:
            spots |= set(range(start, i)) | ({start - 1} if start and said[start - 1] == "the" else set())
    return spots


def scaled(said: list[str], wrote: list[str]) -> set[int]:
    """The words of an amount of lakhs or crores the answer writes as a decimal of its scale ("one lakh fifty
    thousand rupees" ₹1.5 lakh): every number word around the scale word, and its currency."""
    decimal = any(
        a.isdigit() and b.isdigit() and scale in INDIAN_SCALES
        for a, b, scale in zip(wrote, wrote[1:], wrote[2:], strict=False)
    )
    if not decimal:
        return set()
    spots: set[int] = set()
    for i, word in enumerate(said):
        if (
            word in INDIAN_SCALES
            or (is_number(word) and spots and i - 1 in spots)
            or (word in FORMAT_WORDS and i - 1 in spots)
        ):
            spots.add(i)
    for i in range(len(said) - 1, -1, -1):
        if is_number(said[i]) and i + 1 in spots:
            spots.add(i)
    return spots


def moved_number(word: str, said: list[str], wrote: list[str]) -> bool:
    value = VALUES.get(word)
    if value is None:
        return False
    sayings = sum(VALUES.get(other) == value for other in said)
    return Counter(digits(wrote))[str(value)] >= sayings


def ordinal_nouns(said: list[str], wrote: list[str], items: set[int]) -> bool:
    """A list whose items all open with the noun an ordinal modified ("the first build failed the second build
    passed" "1. Build failed\n2. Build passed"): the ordinals told the things apart, and the list dropped them."""
    if len(items) < 2:
        return False
    heads = {wrote[item + 1] for item in items if item + 1 < len(wrote)}
    if len(heads) != 1:
        return False
    head = heads.pop()
    modified = [i for i in range(2, len(said)) if said[i] == head and said[i - 1] in ORDINALS]
    return sum(said[i - 2] in DETERMINERS for i in modified) >= 2


# A word right after itself, whatever marks follow it: the cheap test that a text may hold a stutter.
DOUBLED = re.compile(r"(?<!\S)([^\s,.;!?]+)[,.;!?]*\s+\1(?![^\s,.;!?])", re.IGNORECASE)


def unstuttered(said_tokens: list[tuple[str, bool]], wrote: list[str]) -> list[tuple[str, bool]]:
    """The said words with a stutter's second copy gone: a word right after itself ("it's it's", "the the"), unless
    the answer keeps that repeat ("that that"), it is a doubled cue ("no no"), which has its own rule, or it is said
    twice on purpose: a number ("nine one one") or a spelled letter ("w w w")."""
    repeated = {word for word, next_word in pairwise(wrote) if word == next_word}
    kept = said_tokens[:1]
    for (before, _), (word, marked) in pairwise(said_tokens):
        # A mark said twice is meant too ("c plus plus", "dot dot dot").
        meant = (word, word) in REPEATS or is_number(word) or len(word) == 1 or word in FORMAT_WORDS
        meant = meant or word in TERM_MARK_WORDS.values()
        stutter = word == before and word not in repeated and not meant
        if not stutter:
            kept.append((word, marked))
    return kept


# A numbered list's own numbers, at the start of a line.
ITEM_NUMBER = re.compile(r"^\s*(\d+)\.(?=\s)", re.MULTILINE)


def numbering(wrote_text: str, first: int) -> bool:
    """Whether the list numbers count on by one, from `first` when there is one item: they are formatting then, not
    numbers said ("I want these in a numbered list. Buy milk. Call mom." "1. Buy milk\n2. Call mom")."""
    found = [int(number) for number in ITEM_NUMBER.findall(wrote_text)]
    on = all(after == before + 1 for before, after in pairwise(found))
    return bool(found) and on and (len(found) > 1 or found[0] == first)


def by_first_word(phrases: str) -> dict[str, list[tuple[str, ...]]]:
    """The "|"-separated phrases as words, grouped by their first word for a quick lookup."""
    grouped: dict[str, list[tuple[str, ...]]] = {}
    for phrase in phrases.split("|"):
        found = tuple(words(phrase))
        grouped.setdefault(found[0], []).append(found)
    return grouped


# Every cue but a lone negation, by its first word.
CUE_STARTS = by_first_word("|".join(" ".join(cue) for cue in CUE_PHRASES if not (len(cue) == 1 and cue[0] in NEGATORS)))
# Cues a speaker never means to keep in the text when the words around them are two versions of one thing.
STRONG_CUES = by_first_word("i mean|no wait|scratch that|make that|no|sorry|actually")


def kept_correction(said: list[str], wrote: list[str], named: frozenset[str]) -> tuple[str, ...] | None:
    """Both versions kept around a cue ("Ship Friday, I mean Monday", "Monday, no, Tuesday"): the said words just
    before and after the cue are of one kind (days, numbers, names, colours) and the answer still writes the cue
    between them. Names the old version, the cue and the new one."""
    for i in range(1, len(said) - 1):
        for cue in STRONG_CUES.get(said[i], ()):
            end = i + len(cue)
            if end >= len(said) or tuple(said[i:end]) != cue:
                continue
            if len(cue) == 1 and cue[0] in NEGATORS and said[end] in NEGATION_PHRASES:
                continue
            old, new = said[i - 1], said[end]
            # "the plumber no the electrician": the same small word before each version.
            led = False
            if new in LEADS and i >= 2 and said[i - 2] == new and end + 1 < len(said) and cue not in WEAK_CUES:
                led, new = True, said[end + 1]
            names_both = {old, new} <= named
            if old == new or not (names_both or led or same_kind([old, new])):
                continue
            kept_run = [old, *cue, *([said[end]] if led else []), new]
            if any(same_run(wrote[j : j + len(kept_run)], kept_run) for j in range(len(wrote))):
                return tuple(kept_run)
    return None


# Words that set a clause's tense.
PAST_MARKERS = word_set("was were")
TENSE_WORDS = PAST_MARKERS | word_set("is are am will")


def past_dropped_by_cue(said: list[str], wrote: list[str]) -> bool:
    """A past said before a cue whose correction names only another day or thing ("the upload was due tuesday rather
    wednesday"): the answer keeps the past."""
    cue = last_cue(said)
    if cue is None or PAST_MARKERS.isdisjoint(said[: cue[0]]) or not TENSE_WORDS.isdisjoint(said[cue[1] :]):
        return False
    return PAST_MARKERS.isdisjoint(wrote)


def negations_after_cue(said: list[str]) -> int:
    """The negations the speaker says again after the last cue (no lone "no"), each before the word it negated before
    the cue ("don't send the deck i mean don't send the notes"): the corrected version keeps them."""
    cues = [
        (i, i + len(cue))
        for i in range(len(said))
        for cue in CUE_STARTS.get(said[i], ())
        if said[i : i + len(cue)] == list(cue)
    ]
    if not cues:
        return 0
    start, end = max(cues, key=lambda cue: cue[1])
    before = set(pairwise(said[:start]))
    again = [pair for pair in pairwise(said[end:]) if pair in before and negations([pair[0]])]
    return len(again)


def negations_taken_back(said: list[str]) -> int:
    """Negations a correction takes back by saying the verb again without them ("don't send it no wait send it"
    sends it after all)."""
    found: set[int] = set()
    for i in range(2, len(said) - 1):
        for cue in CUE_STARTS.get(said[i], ()):
            end = i + len(cue)
            if tuple(said[i:end]) != cue or end >= len(said):
                continue
            # The negated verb may stand a few words before the cue ("don't send the invoice yet no wait send it").
            reach = range(max(i - NEGATED_REACH, 0), i - 1)
            found |= {
                k
                for k in reach
                if negations([said[k]]) and said[k + 1] not in FUNCTION_WORDS and said[k + 1] == said[end]
            }
    return len(found)


# How far before a cue the negation it takes back by saying the verb again may stand (round b3).
NEGATED_REACH = 10


# How far back the words a cue's replacement restates may start ("include the case study actually include").
RESTATE_REACH = 4


def restated_both(said: list[str], marks: list[bool], wrote: list[str]) -> tuple[str, ...] | None:
    """Both versions kept where the words after a cue restate the ones before it, with the cue or without: the same
    opening word ("include the case study actually include the testimonial", "has no allergies actually no known
    allergies"), the same last words ("priya will lead sorry rahul will lead"), or a word of one kind and the same
    word after it ("friday works no no monday works"). A lone "no" has its own rule. Names the old version's last
    word, the cue and the new version's first."""
    for i in range(1, len(said) - 1):
        for cue in CUE_STARTS.get(said[i], ()):
            end = i + len(cue)
            if tuple(said[i:end]) != cue or end >= len(said):
                continue
            before, after = said[max(0, i - RESTATE_REACH) : i], said[end : end + RESTATE_REACH]
            # A cue that only takes back may restate as little as a clause's last word ("approve it no wait reject
            # it").
            clause_ends = end + 2 == len(said) or (end + 2 < len(said) and marks[end + 2])
            short = cue in TAKING_BACK.get(cue[0], ()) and clause_ends
            old, new = versions(before, after, 1 if short else 2)
            if not old:
                continue
            at = next((j for j in range(len(wrote)) if same_run(wrote[j : j + len(old)], old)), None)
            if at is None:
                continue
            rest = wrote[at + len(old) : at + len(old) + len(cue) + 1 + len(new)]
            if any(same_run(rest[k : k + len(new)], new) for k in range(len(cue) + 2)):
                return (old[-1], *cue, new[0])
    return None


def versions(before: list[str], after: list[str], least: int = 2) -> tuple[list[str], list[str]]:
    """The words before a cue and the words after it that say them again, or nothing: the same opening word, the
    same last words ("priya will lead sorry rahul will lead"), a word of one kind and the same word after it ("friday
    works no no monday works"), or the same small word before one of one kind ("on friday sorry on saturday")."""
    if not before or not after:
        return [], []
    restating = after[0] not in CUE_WORDS or after[0] in NEGATORS
    if after[0] in before and after[0] not in FUNCTION_WORDS and restating:
        start = len(before) - before[::-1].index(after[0]) - 1
        old = before[start:]
        return old, after[: min(len(old), len(after))]
    for size in range(3, least - 1, -1):
        echoed = len(after) > size and len(before) > size and after[1 : 1 + size] == before[-size:]
        if echoed and after[0] != before[-size - 1]:
            return before[-size - 1 :], after[: size + 1]
    if len(after) >= 2 and len(before) >= 2 and after[1] == before[-1] and same_kind([before[-2], after[0]]):
        return before[-2:], after[:2]
    if len(after) >= 2 and len(before) >= 2 and after[0] == before[-2] and same_kind([before[-1], after[1]]):
        return before[-2:], after[:2]
    return [], []


# Cues that put one number for another ("three no four", "fifty dollars wait sixty").
NUMBER_CUES = by_first_word("i mean|i meant|no wait|no|sorry|actually|wait|rather|nope|oops|make that|scratch that")
# Numbers a cue reaches across: the old one this many words before it, the new one this many after.
NUMBER_REACH = 4
# Words that join two numbers into a range or a choice, never a correction ("three or four", "three to four").
SPANS = word_set("or to and")


# Cues a speaker only says to take words back: dropped with both versions kept, the correction never happened.
TAKING_BACK = by_first_word("scratch that|no wait|i meant")


def both_kept(said: list[str], marks: list[bool], wrote: list[str]) -> tuple[str, ...] | None:
    """A cue that only takes words back, dropped while the content words just before and after it both stay
    ("approve it scratch that reject it" Approve it. Reject it.). Names them and the cue."""
    for i, word in enumerate(said):
        for cue in TAKING_BACK.get(word, ()):
            end = i + len(cue)
            # A cue that opens a sentence may take back a sentence before it, which a revision handles.
            if tuple(said[i:end]) != cue or not set(cue).isdisjoint(wrote) or (marks and marks[i]):
                continue
            # Every content word of the clause before the cue still written: nothing was taken back.
            clause = max((j for j in range(i) if marks[j]), default=0)
            window = [w for w in said[clause:i] if w not in PLAIN_WORDS]
            after = next((w for w in said[end:] if content(w)), None)
            kept_all = bool(window) and all(wrote.count(w) >= said.count(w) for w in window)
            if kept_all and after is not None and after not in window and after in wrote:
                return (window[-1], *cue, after)
    return None


def number_correction(said: list[str], wrote: list[str]) -> tuple[str, ...] | None:
    """A number put right after a cue whose answer lost the new one ("three no four" 3, "fifty dollars wait sixty
    dollars" $50), or kept the old one right beside it ("3 or 4"). Names the old number, the cue and the new one. A
    list's counts and a fraction's time are left to their own rules ("second ... no ... third", "seven i mean
    quarter to eight")."""
    written: list[int | None] = []
    for i, word in enumerate(said):
        for cue in NUMBER_CUES.get(word, ()):
            end = i + len(cue)
            if tuple(said[i:end]) != cue or (said[end : end + 1] and said[end] in NEGATION_PHRASES):
                continue
            old_run = number_before(said, i)
            # The same small word may lead both numbers ("to two i mean to one", "at five actually at five thirty").
            led = bool(old_run) and end < len(said) and said[end] in LEADS and said[i - len(old_run) - 1] == said[end]
            start = end + led
            new_run = number_after_cue(said, start)
            if not old_run or not new_run or not SPANS.isdisjoint(said[start : start + NUMBER_REACH]):
                continue
            old_numbers, new_numbers = read_numbers(old_run), read_numbers(new_run)
            # An ordinal in digits ("12th") reads as no number: nothing to compare.
            if not old_numbers or not new_numbers:
                continue
            old, new = old_numbers[-1][1], new_numbers[0][1]
            written = written or [int(w) if w.isdigit() else VALUES.get(w) for w in wrote]
            # A time put right keeps its hour and adds minutes ("five actually five thirty" 5:30, never 5).
            minutes = [value for _, value in new_numbers[1:]]
            lost = (old != new and old in written and new not in written) or (
                old == new and len(old_numbers) == 1 and bool(minutes) and not set(minutes) & set(written)
            )
            beside = old != new and any(
                written[j] == old and new in written[j + 1 : j + 2 + len(cue)] for j in range(len(written) - 1)
            )
            if lost or beside:
                return (old_run[-1], *cue, new_run[0])
    return None


def counted(word: str) -> bool:
    return is_number(word) and word not in ORDINALS and word not in FRACTION_WORDS and not word.isdigit()


def number_before(said: list[str], at: int) -> list[str]:
    """The number words said just before `at`, past a unit word or two ("fifty dollars", "three hours")."""
    end = at
    while end > max(0, at - 2) and not counted(said[end - 1]) and said[end - 1] in UNIT_WORDS:
        end -= 1
    start = end
    while start > 0 and counted(said[start - 1]):
        start -= 1
    return said[start:end]


def number_after_cue(said: list[str], at: int) -> list[str]:
    """The number words said right after a cue at `at`; none when a fraction or another word comes first."""
    end = at
    while end < len(said) and counted(said[end]):
        end += 1
    return said[at:end]


# Words a number is said before: units of money, time, weight and count.
UNIT_WORDS = FORMAT_WORDS | word_set("kilo minutes minute hours hour days day percent rupees dollars")


# A small word that may lead both versions around a cue ("the plumber no the electrician").
LEADS = word_set("the a an my your our his her their to in on at for with")


def same_run(written: list[str], said: list[str]) -> bool:
    """The said words as written, a number word maybe as its digits ("three no four" 3, no, 4)."""
    return len(written) == len(said) and all(
        one == other or (other in VALUES and one == str(VALUES[other]))
        for one, other in zip(written, said, strict=True)
    )


def asked(said: list[str], wrote: list[str]) -> bool:
    """A question turned into a statement: the auxiliary that opened it moved after its subject ("is the deck
    ready" "The deck is ready.")."""
    if not said or not wrote or said[0] not in AUXILIARIES or wrote[0] == said[0]:
        return False
    return said[0] in wrote[1:4] and said[1:2] == wrote[:1]


# A written sentence and the mark that ends it.
WRITTEN_SENTENCE = re.compile(r"[^\n]+?[.?!](?=\s|$)")
QUESTION_WORDS = word_set("what when where who whom whose why how which")
# A subject right after an opening auxiliary asks ("is the deck", "can you").
SUBJECTS = word_set("i you he she it we they this that these those there the a an my your our his her their")
OPENS_QUESTIONS = AUXILIARIES | QUESTION_WORDS
# Each sentence's first word: a sentence may ask only with one of them.
SENTENCE_FIRSTS = re.compile(r"(?:^|[.!\n])[\s\-]*([^\s,.;:!?\"']+)")


# Words a question or a grammar fix moves ("what workflows will be" what will the workflows be).
MOVABLE = AUXILIARIES | word_set("be been being not")


def order_changed(said: list[str], wrote: list[str], retracted: list[str], items: set[int]) -> tuple[str, ...] | None:
    """Two content words said once each and written once each in the other order ("from paris to lyon" from Lyon to
    Paris, a list's items moved), outside any words a cue took back. Names the pair as written."""
    taken = set(retracted)
    said_count, wrote_count = Counter(said), Counter(wrote)
    said_once = {
        w
        for w, n in said_count.items()
        if n == 1 and content(w) and not is_number(w) and w not in MOVABLE and w not in FORMS and w not in taken
    }
    order = [w for w in wrote if w in said_once and wrote_count[w] == 1]
    if len(order) < 2:
        return None
    place = {w: i for i, w in enumerate(said) if w in said_once}
    for before, after in pairwise(order):
        if place[after] < place[before]:
            return (before, after)
    return None


# Each currency's symbol and the words that say it; a lakh or a crore is rupees.
CURRENCIES = {
    "$": word_set("dollar dollars bucks usd"),
    "₹": word_set("rupee rupees rs inr paise lakh lakhs crore crores"),
    "€": word_set("euro euros"),
    "£": word_set("pound pounds quid"),
}
# Words said for the marks of an address.
ADDRESS_CHARS = {"dot": ".", "at": "@", "underscore": "_", "dash": "-", "hyphen": "-", "slash": "/"}
POINTS = word_set("point")
# Every spoken address holds one of these; "at" alone is prose.
ADDRESS_JOINS = word_set("dot underscore slash dash hyphen")
LABEL = re.compile(r"[^._/\-@]+")
DOMAINS = word_set("com in io org net co ai app dev uk edu gov me info biz xyz us tv")
DECIMAL = re.compile(r"(\d+)\.(\d+)")
RATIO = re.compile(r"(\d+):(\d+)")
# A name or a mailbox word left before "at" and a written domain ("Sam at acme.io", "support at acme.io"), never a
# verb ("we met at acme.io").
HALF_ADDRESS = re.compile(
    r"(?<![.?!]\s)(?<!^)\b(?:[A-Z][a-z]+|(?i:support|sales|info|hello|team|ops|hr|careers|admin|contact|billing|jobs))"
    rf" at [a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:{'|'.join(sorted(DOMAINS))})\b",
    re.MULTILINE,
)
# An "@" standing alone between words, never a handle ("@lena_catering").
LONE_AT = re.compile(r"(?:^|\s)@(?:\s|$)")
# Marks said as words the answer may never keep as words, and the paragraph commands.
# Each written beside its own mark ("question mark?") when the model kept the words and made the mark too.
KEPT_MARKS = [
    (tuple(words(phrase)), mark)
    for phrase, mark in (
        ("question mark", "?"),
        ("full stop", "."),
        ("exclamation mark", "!"),
        ("exclamation point", "!"),
    )
]
# A paragraph named, never said as a command ("the new paragraph in the contract").
NAMING = NOUN_MARKERS | word_set("this that is")
PARAGRAPHS = [tuple(words("new paragraph"))]
PARAGRAPH_FIRSTS = {phrase[0] for phrase in PARAGRAPHS}
MARK_FIRSTS = PARAGRAPH_FIRSTS | {phrase[0] for phrase, _ in KEPT_MARKS}


def format_changed(
    said: list[str], said_text: str, spoken: set[str], wrote_text: str, retracted: list[str]
) -> tuple[str, ...] | None:
    """A number, an amount, an address or a mark written otherwise than said: a currency symbol no word said ("twenty
    euros" $20), a decimal's zero lost or added ("three point zero five" 3.5), a ratio turned round ("three to one"
    1:3), a spoken address cut or changed ("acme dot co dot in" acme.co.), an "@" in prose, a spoken mark kept as
    words, a paragraph said but not made. Names what changed."""
    for symbol, names in CURRENCIES.items():
        if symbol in wrote_text and symbol not in said_text and names.isdisjoint(spoken):
            return (symbol,)
    if not POINTS.isdisjoint(spoken) and "." in wrote_text:
        said_decimals = decimals_said(said)
        for whole, fraction in DECIMAL.findall(wrote_text):
            # A zero lost ("3.5" for "three point zero five") or a digit cut ("3.1" for "three point one four").
            if any(f != fraction and (int(f) == int(fraction) or f.startswith(fraction)) for f in said_decimals):
                return (f"{whole}.{fraction}",)
    malformed = numbers_malformed(said, wrote_text)
    if malformed is not None:
        return malformed
    if ":" in wrote_text and "to" in spoken:
        for first, second in RATIO.findall(wrote_text):
            for i in range(1, len(said) - 1):
                turned = VALUES.get(said[i - 1]) == int(second) and VALUES.get(said[i + 1]) == int(first)
                if said[i] == "to" and turned and first != second:
                    return (f"{first}:{second}",)
    if "@" in wrote_text and LONE_AT.search(wrote_text):
        return ("@",)
    if not ADDRESS_JOINS.isdisjoint(spoken):
        squeezed = re.sub(r"[\s:]", "", wrote_text.lower())
        taken = Counter(retracted)
        for address, labels in spoken_addresses(said):
            # An address a cue took back or put right is no longer said whole; one after a prose "at" may start
            # after it ("page at studio dot io" studio.io).
            gone = any(taken[label] > 0 for label in labels)
            # Only a lone word before an "@" may be prose ("page at studio dot io"), never "priya underscore k".
            readings = [
                address,
                *(
                    address[at + 1 :]
                    for at in range(len(address))
                    if address[at] == "@" and LABEL.fullmatch(address[:at].split("@")[-1])
                ),
            ]
            if not gone and not any(reading in squeezed for reading in readings):
                return (address,)
    return spoken_marks_kept(said, spoken, wrote_text)


# A clock time with one digit of minutes ("6:6"), an ordinal with another number's ending ("4rd"), and an amount of
# lakhs or crores as a decimal ("₹3.5 lakh").
ONE_DIGIT_MINUTES = re.compile(r"\b\d{1,2}:\d(?![\d:])(?=\s*[ap]\.?m\b)", re.IGNORECASE)
ORDINAL_DIGITS = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
SCALED = re.compile(r"(\d+\.\d+)\s*(lakhs?|crores?|million|billion)", re.IGNORECASE)
SCALE_OF = {"lakh": 100_000, "crore": 10_000_000, "million": 1_000_000, "billion": 1_000_000_000}


def ordinal_ending(number: int) -> str:
    if number % 100 in (11, 12, 13):
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def numbers_malformed(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    """A number written wrong for what was said: a time's minutes as one digit, an ordinal's ending, a scaled
    decimal no amount said makes, or a number of several words cut to its first ("twenty five" 20, "three fifteen"
    3). Names what is wrong."""
    found = ONE_DIGIT_MINUTES.search(wrote_text)
    if found is not None:
        return (found.group(),)
    for number, ending in ORDINAL_DIGITS.findall(wrote_text):
        if ending.lower() != ordinal_ending(int(number)):
            return (number + ending,)
    if SCALED.search(wrote_text):
        amounts = {value for _, value in spoken_numbers(said)}
        for decimal, scale in SCALED.findall(wrote_text):
            amount = round(float(decimal) * SCALE_OF[scale.lower().removesuffix("s")])
            if str(amount) not in amounts:
                return (f"{decimal} {scale}",)
    return cut_number(said, wrote_text)


def cut_number(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    """A number said in several words written as only its first part, with the rest written nowhere: "twenty five"
    as 20, "june twenty second" June 20, "three fifteen" 3, "six oh five" 6."""
    written = Counter(digits(words(wrote_text)))
    wrote = set(words(wrote_text))
    # With a cue, a number before it may be taken back: only those said after the last cue must be whole.
    cue = last_cue(said) if not CUE_WORDS.isdisjoint(said) else None
    sayings = Counter(str(VALUES[word]) for word in said if word in VALUES)
    for offset, run in number_runs(said):
        halves = not FRACTION_WORDS.isdisjoint(run) or not TIME_HALVES.isdisjoint(run)
        if any(word.isdigit() for word in run) or halves or (cue is not None and offset < cue[1]):
            continue
        numbers = read_numbers(run)
        if not numbers:
            continue
        ends = [start for start, _ in numbers[1:]] + [len(run)]
        values = [str(value) for _, value in numbers]
        # A year or a number said in parts and written whole ("twenty twenty six" 2026).
        if len(values) > 1 and written[str(int("".join(values)))]:
            continue
        for (start, value), end in zip(numbers, ends, strict=True):
            span = [word for word in run[start:end] if word != "and"]
            # Written whole or inside a longer number ("twenty twenty six" 2026).
            inside = any(number.endswith(str(value)) for number in written)
            if len(span) < 2 or span[-1] in SCALES or inside:
                continue
            first = read_numbers(span[:-1])
            if first and written[str(first[0][1])] and span[-1] not in wrote:
                return tuple(span)
        # A clock time ("three fifteen", "six oh five") written as its hour alone, said once.
        hour, minutes = values[0], values[1:]
        spoken_minutes = len(minutes) == 1 or (len(minutes) == 2 and minutes[0] == "0")
        plain = "and" not in run and ORDINALS.isdisjoint(run) and (offset == 0 or said[offset - 1] not in MONTHS)
        clock = plain and spoken_minutes and 1 <= int(hour) <= CLOCK_HOURS and int(minutes[-1]) < MINUTES_IN_HOUR
        whole = str(int("".join(values)))
        alone = written[hour] and sayings[hour] == 1 and not any(written[minute] for minute in minutes)
        if clock and alone and not written[whole]:
            return tuple(word for word in run if word != "and")
    return None


# "half" and "quarter" are a time's minutes, which another check reads.
TIME_HALVES = word_set("half quarter")
CLOCK_HOURS = 12
MINUTES_IN_HOUR = 60


def decimals_said(said: list[str]) -> list[str]:
    """The digits said after each decimal point, zeros kept ("point zero five" 05)."""
    found: list[str] = []
    for i, word in enumerate(said):
        if word not in POINTS:
            continue
        digits_said = ""
        for after in said[i + 1 :]:
            value = 0 if after == "oh" else VALUES.get(after)
            if value is None or value > 9:
                break
            digits_said += str(value)
        if digits_said:
            found.append(digits_said)
    return found


def spoken_addresses(said: list[str]) -> list[tuple[str, list[str]]]:
    """Each address said with "dot", "underscore" or "slash" and maybe "at", as written: its words joined by their
    marks ("priya dot sharma at acme dot co dot in" priya.sharma@acme.co.in). One that opens with "slash" is a path
    ("slash users slash id" /users/id). Left out when a number is said in it, which may be written as digits."""
    found: list[tuple[str, list[str]]] = []
    # "at the rate" is how Indian English says "@".
    said = " ".join(said).replace("at the rate of ", "at ").replace("at the rate ", "at ").split()
    i = 0
    while i < len(said):
        if said[i] not in ADDRESS_CHARS or i + 1 >= len(said) or said[i + 1] in ADDRESS_CHARS:
            i += 1
            continue
        start = i if said[i] == "slash" or i == 0 else i - 1
        parts: list[str] = []
        j = start
        while j < len(said):
            if said[j] in ADDRESS_CHARS and j + 1 < len(said) and said[j + 1] not in ADDRESS_CHARS:
                parts += [ADDRESS_CHARS[said[j]], said[j + 1]]
                j += 2
            elif j == start and said[j] not in ADDRESS_CHARS:
                parts.append(said[j])
                j += 1
            else:
                break
        joined = "".join(parts)
        marks = [part for part in parts if part in ADDRESS_CHARS.values()]
        labels = [part for part in parts if part not in ADDRESS_CHARS.values()]
        # One "dot" or "slash" alone is a word ("a dot ball", "we slash costs") unless a domain ends it.
        domain = labels[-1:] and labels[-1] in DOMAINS and "." in marks
        joins = [mark for mark in marks if mark != "@"]
        address = len(joins) >= 2 or "_" in joins or bool(domain) or ("@" in marks and bool(joins))
        numbered = any(is_number(part) for part in parts)
        if address and not numbered:
            # The labels a cue may take back: those after an "@", which a prose "at" may stand before.
            after_at = (
                labels[sum(1 for part in parts[: parts.index("@")] if part not in ADDRESS_CHARS.values()) :]
                if "@" in parts
                else labels
            )
            found.append((joined, after_at))
        i = max(j, i + 1)
    return found


def spoken_marks_kept(said: list[str], spoken: set[str], wrote_text: str) -> tuple[str, ...] | None:
    """A mark said as words and kept as those words ("are you coming question mark" ... question mark?), or a
    paragraph said between words that the answer runs on with ("first line new paragraph second line")."""
    kept = marks_as_words(said, wrote_text) if not spoken.isdisjoint(AS_WORDS_FIRSTS) else None
    if kept is not None:
        return kept
    if spoken.isdisjoint(MARK_FIRSTS):
        return None
    lower = wrote_text.lower()
    for phrase, mark in KEPT_MARKS:
        if phrase[0] in spoken and f"{' '.join(phrase)}{mark}" in lower:
            return phrase
    if spoken.isdisjoint(PARAGRAPH_FIRSTS):
        return None
    starts = [i for i, word in enumerate(said) if word in PARAGRAPH_FIRSTS and i > 0]
    inside = [
        phrase
        for i in starts
        for phrase in PARAGRAPHS
        if i + len(phrase) < len(said) and tuple(said[i : i + len(phrase)]) == phrase and said[i - 1] not in NAMING
    ]
    if len(inside) > wrote_text.count("\n\n"):
        return inside[0]
    return None


# Marks said as words that an answer never keeps as words ("Note colon bring ID."), unless a word before one makes it a
# noun ("a new line of products", "the colon").
AS_WORDS = [
    tuple(words(phrase))
    for phrase in (
        "colon|semicolon|new line|next line|exclamation point|exclamation mark|open quote|close quote|end quote|"
        "unquote|"
        "open bracket|close bracket|open paren|close paren|dot dot dot|question mark|full stop"
    ).split("|")
]
AS_WORDS_FIRSTS = {phrase[0] for phrase in AS_WORDS}
NAMING_MARK = NOUN_MARKERS | word_set(
    "our my your their his her its this that of new launch product add put type insert use enter"
)
MARK_NOUN_AFTER = word_set("cancer surgery polyp polyps health cleanse key keys")


def marks_as_words(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    """A mark said as words and kept as every one of those words: named."""
    wrote = words(wrote_text)
    for phrase in AS_WORDS:
        if phrase[0] not in said:
            continue
        size = len(phrase)
        spots = [
            i
            for i in range(len(said) - size + 1)
            if tuple(said[i : i + size]) == phrase
            and (i == 0 or said[i - 1] not in NAMING_MARK)
            and (i + size == len(said) or said[i + size] not in MARK_NOUN_AFTER)
        ]
        if spots and occurrences(wrote, phrase) >= occurrences(said, phrase):
            return phrase
    return None


def unasked(wrote_text: str) -> tuple[str, ...] | None:
    """A direct question written with a full stop: one opening with an auxiliary and its subject ("can you send
    it."), with a question word and an auxiliary ("where is the invoice."), or ending with a tag ("right."). Names
    its first words. An indirect question ("I wonder if") opens otherwise and may end with a full stop."""
    firsts = {word.lower() for word in SENTENCE_FIRSTS.findall(wrote_text)}
    tagged = TAG.search(wrote_text) is not None
    opens = not OPENS_QUESTIONS.isdisjoint(firsts) or not ASKING_CONTRACTIONS.isdisjoint(firsts)
    if not opens and not tagged:
        return asked_after_comma(wrote_text)
    for found in WRITTEN_SENTENCE.finditer(wrote_text):
        sentence = found.group()
        if sentence[-1] in "?!":
            continue
        # A tag asks whatever the sentence opens with ("You sent it, didn't you.").
        if TAG.search(sentence) is not None:
            return tuple(words(sentence)[-3:])
        # Most sentences open with no question word: read only their first word before splitting them.
        edges = sentence.lower().strip(" -\t").split(maxsplit=1)
        first = edges[0].strip(".,;:!?\"'") if edges else ""
        if first not in OPENS_QUESTIONS and first not in ASKING_CONTRACTIONS:
            continue
        said = words(sentence)
        if len(said) >= 2 and asking_sentence(said, sentence):
            return tuple(said[:4])
    return asked_after_comma(wrote_text)


# A question after a greeting or a lead-in, past a comma ("Hello, how are you.").
ASKED_CLAUSE = re.compile(
    r",\s+((?:how|what|where|when|why|who)\s+(?:is|are|was|were|do|does|did|can|will|would)\s+(?:you|we|they|he|she|it|i))\b[^.?!]*\.(?=\s|$)",
    re.IGNORECASE,
)


def asked_after_comma(wrote_text: str) -> tuple[str, ...] | None:
    found = ASKED_CLAUSE.search(wrote_text) if "," in wrote_text else None
    return None if found is None else tuple(words(found.group(1)))


# "do", "have" and "may" open a command or a wish ("Do the needful.", "Have a great weekend.", "May the best team
# win.") unless a person they ask about follows ("Do you", "Have we", "May I").
COMMANDING = word_set("do have may")
ASKING_PRONOUNS = word_set("i you we they he she there")
# A question word's own contractions ("Who's covering the final over.").
ASKING_CONTRACTIONS = word_set("who's what's where's when's why's how's")
# A count or a measure asked for ("how many people are coming").
HOW_MUCH = word_set("many much long often far soon old")
# A second auxiliary makes a question word's clause the subject of a statement ("Where Maya will sit is up to you.").
FINITE = AUXILIARIES | word_set(
    "isn't aren't wasn't weren't doesn't don't didn't won't can't makes means matters sounds seems looks depends shows"
)
# A tag that asks: ", right", ", didn't you".
TAG = re.compile(r",\s*(?:right|\w+n't\s+(?:i|you|he|she|it|we|they|there))\s*\.(?=\s|$)", re.IGNORECASE)


def asking_sentence(said: list[str], sentence: str) -> bool:
    """A written sentence that asks directly: an auxiliary and its subject ("can you", "can Priya"), a question word
    and an auxiliary ("where is", "how many people are", "which vendor was"), a question word and a past ("who sent
    this")."""
    opener = said[0]
    named = len(sentence.split()) > 1 and sentence.split()[1][:1].isupper()
    asked_about = ASKING_PRONOUNS if opener in COMMANDING else SUBJECTS
    auxiliary = opener in AUXILIARIES and (said[1] in asked_about or (named and opener not in COMMANDING))
    measured = opener == "how" and said[1] in HOW_MUCH and not FINITE.isdisjoint(said[2:5])
    # "When Rahul is free, call me." and "Where Maya will sit is up to you." state; "Which vendor was cheaper?" asks.
    clause = "," in sentence or not FINITE.isdisjoint(said[3:])
    noun_first = opener != "when" and said[1] not in SUBJECTS and said[2:3] and said[2] in AUXILIARIES and not clause
    who = opener in ("who", "whose") and past_form(said[1])
    question = opener in ASKING_CONTRACTIONS or (
        opener in QUESTION_WORDS and (said[1] in AUXILIARIES or bool(noun_first) or measured or who)
    )
    return auxiliary or question


# A question mark on what asks nothing directly: an indirect question ("She asked if we can move the call?"), a wish
# or a sign-off ("Have a great weekend?", "May the best team win?").
OVERASKED = re.compile(
    r"(?:^|[.!?\n]\s*)(?:(?:i|we|she|he|they)\s+(?:wonder|wondered|asked|was wondering)\s+"
    r"(?:if|whether|why|what|when|where|how)\b"
    r"|(?:then\s+)?(?:you\s+can\s+|please\s+)?(?:ask|tell|show)\s+[^.!?\n]{1,30}?\s+(?:what|how|when|where|which)\s+to\s"
    r"|have\s+(?:a|an|fun)\s|may\s+the\s)[^.!?\n]*\?",
    re.IGNORECASE,
)


def overasked(wrote_text: str) -> tuple[str, ...] | None:
    """A sentence that asks nothing directly but ends with "?", named by its first words."""
    if "?" not in wrote_text:
        return None
    found = OVERASKED.search(wrote_text)
    if found is None:
        return None
    asked = words(found.group())[:4]
    return tuple(asked)


# A written list's lines: numbered or bulleted.
ANY_ITEM_LINE = re.compile(r"^\s*(?:\d+\.|[-•*])\s", re.MULTILINE)
# Words that announce a list (D5) or say how items are counted.
ANNOUNCES = word_set(
    "following follows below here here's these those steps things points list lists items reasons tasks ideas "
    "questions options thing notes agenda plan checklist todo todos updates summary priorities goals"
)


# Words between two counts that make each an item, not a quantity ("one add the paint advance two repair ...").
ITEM_WORDS = 3


def counts_off(said: list[str], marks: list[bool]) -> bool:
    """Whether the numbers said count items off: one written as digits ("1 consultancy fee 2 travel"), one with a
    mark beside it ("One, milk."), or one, two and on with an item of a few words between each; "one designer
    two engineers" says how many."""
    places: list[int] = []
    # "Do one thing," says how the steps go, not their count (round b6, v2-lists-en-00018).
    phrased = {i + k for phrase in DO_ONE_THING for i in phrase_starts(said, phrase) for k in range(len(phrase))}
    for i, word in enumerate(said):
        if not is_number(word) or (word in ORDINALS and prose_ordinal(said, marks, i)) or i in phrased:
            continue
        if word.isdigit() or (i > 0 and marks[i]) or (i + 1 < len(said) and marks[i + 1]):
            return True
        if VALUES.get(word) == len(places) + 1:
            places.append(i)
    gaps = [after - before - (said[after - 1] in CONNECTORS) for before, after in pairwise(places)]
    return bool(gaps) and min(gaps) >= ITEM_WORDS


def false_list(said: list[str], marks: list[bool], wrote_text: str, first_item: int) -> bool:
    """A list the speech never made: a numbered item alone where no list goes on, or items never counted, bulleted
    or announced, with no words announcing them before the first ("I woke up then made coffee", "My name is Ravi:"
    then statements). A number counts items off only where a mark stands beside it ("One, milk."), unless a
    list is announced: "one designer two engineers" says how many."""
    lines = ANY_ITEM_LINE.findall(wrote_text)
    if len(lines) == 1:
        # One item alone is a list only where one goes on, or a bullet said ("Do one thing:\n- Call him").
        numbered = lines[0].strip()[:1].isdigit()
        return first_item == 1 and (numbered or BULLETS.isdisjoint(said))
    if len(lines) < 2:
        return False
    # One ordinal and "then" tell a story ("first we went to the beach then we had lunch"); two count items off.
    ordinals = {VALUES[word] for i, word in enumerate(said) if word in ORDINALS and not prose_ordinal(said, marks, i)}
    counts = len(ordinals) >= 2 or counts_off(said, marks) or not BULLETS.isdisjoint(said)
    # Two steps after "do one thing" stay one sentence; three or more make a list (prompt rule 3).
    if not counts and len(lines) < DO_ONE_THING_ITEMS and any(occurrences(said, phrase) for phrase in DO_ONE_THING):
        return True
    counted = counts or not ANNOUNCES.isdisjoint(said)
    # Words written before the first item lead the list in when they announce it ("The plan is as follows:").
    first = ANY_ITEM_LINE.search(wrote_text)
    lead = words(wrote_text[: first.start()]) if first else []
    led_in = any(word in ANNOUNCES or is_number(word) for word in lead)
    # A plural noun that names the items announces them too ("The blockers are:"), prompt rule 3.
    plural = len(lead) >= 2 and lead[-1] in ("are", "were") and lead[-2].endswith("s") and not lead[-2].endswith("ss")
    return not counted and not led_in and not plural


def phrase_starts(said: list[str], phrase: tuple[str, ...]) -> list[int]:
    """Where `phrase` starts among the said words."""
    size = len(phrase)
    found = [i for i, word in enumerate(said) if word == phrase[0] and tuple(said[i : i + size]) == phrase]
    return found


DO_ONE_THING = [tuple(words("do one thing"))]
DO_ONE_THING_ITEMS = 3
# An ordinal before one of these is prose ("second time", "first half"), and so is "at first".
ORDINAL_NOUNS = word_set("time times half hand place round try attempt chance")


def prose_ordinal(said: list[str], marks: list[bool], i: int) -> bool:
    after = said[i + 1] if i + 1 < len(said) else ""
    before = said[i - 1] if i else ""
    return before == "at" or after in ORDINAL_NOUNS or before in DETERMINERS or in_prose(said, marks, i)


# A counting word's digit opening a sentence ("2 renewals ...").
COUNT_OPENS = re.compile(r"(?:^|[.?!]\s+)2\s")


def counted_as_numbers(said: list[str], marks: list[bool], wrote_text: str) -> bool:
    """Items counted off at sentence starts ("one ... . two ... . three ...") written as prose that opens its
    sentences with the counts as numbers ("2 renewals ..."): the list was lost and the counts read as quantities."""
    # Three counts in order, so "One more thing. Two days ago ..." stays prose.
    starts = [VALUES.get(word) for word, marked in zip(said, marks, strict=True) if marked]
    counted = {1, 2, 3} <= set(starts) and starts.index(1) < starts.index(2) < starts.index(3)
    return counted and COUNT_OPENS.search(wrote_text) is not None


# Words that ask for a numbered list ("in a numbered list").
NUMBERED_ASKS = [tuple(words(phrase)) for phrase in "numbered|numbering".split("|")]
BULLET_LINE = re.compile(r"^\s*[-•*]\s", re.MULTILINE)


def unnumbered(said: list[str], marks: list[bool], wrote_text: str) -> bool:
    """A list asked for numbered, or counted off ("first ... second ..."), that came back bulleted."""
    if BULLET_LINE.search(wrote_text) is None or ITEM_NUMBER.search(wrote_text) is not None:
        return False
    asked = any(occurrences(said, phrase) for phrase in NUMBERED_ASKS)
    ordinals = {VALUES[word] for i, word in enumerate(said) if word in ORDINALS and not prose_ordinal(said, marks, i)}
    return asked or len(ordinals) >= 2


# "bullet points", "bullet list": the word, not a spoken bullet.
NOT_A_BULLET = word_set("point points list lists form format style")


def short_list(said: list[str], wrote_text: str, retracted: list[str]) -> str | None:
    """A list missing items: its numbers skip one ("1." then "3."), or it has fewer lines than bullets said. Names
    what is missing as the correction prompt quotes it."""
    numbers = [int(number) for number in ITEM_NUMBER.findall(wrote_text)]
    skipped = [after for before, after in pairwise(numbers) if after > before + 1]
    if skipped:
        return f"item {skipped[0] - 1}"
    bullets = sum(
        word in BULLETS
        and (i + 1 == len(said) or said[i + 1] not in NOT_A_BULLET)
        and (i == 0 or said[i - 1] not in NOUN_MARKERS)
        for i, word in enumerate(said)
    )
    bullets -= sum(word in BULLETS for word in retracted)
    lines = len(ANY_ITEM_LINE.findall(wrote_text))
    return f"{lines} of {bullets} items" if bullets >= 2 and lines < bullets else None


@cache
def term_words(term: str) -> tuple[str, ...]:
    """A term's words; a user's terms come with every request, so each is split once."""
    return tuple(words(term))


def occurrences(found: list[str], term: tuple[str, ...]) -> int:
    size = len(term)
    if size == 1:
        return found.count(term[0])
    return sum(tuple(found[i : i + size]) == term for i in range(len(found) - size + 1))


def lost_term(said: list[str], said_text: str, wrote: list[str], retracted: list[str], terms: list[str]) -> str | None:
    """A term said that the answer no longer has as written ("Kubernetes" "Kubernete", "Xero" "zero"), unless a
    correction cue after it took it back whole: a word spelled nearly like it is the term misspelled, not taken
    back."""
    lower = said_text.lower()
    for term in (term for term in terms if term and term.lower() in lower):
        spelled = term_words(term)
        lost = occurrences(said, spelled) - occurrences(retracted, spelled) > occurrences(wrote, spelled)
        if not spelled or not lost:
            continue
        taken_back = CUE.search(lower, lower.find(term.lower())) is not None
        misspelled = any(word not in spelled and near(word, part) for word in wrote for part in spelled)
        if misspelled or not taken_back:
            return term
    return None


def qualifier_change(said: list[str], said_text: str, kept: Counter[str], written: Counter[str]) -> tuple[str, ...]:
    """The qualifiers dropped or added. With a cue in the speech, one said before the last cue may be taken back,
    and one written may be any said."""
    dropped, added = kept - written, written - kept
    if dropped or added:
        cue = last_cue(said) if has_cue(said_text) else None
        if cue is not None:
            dropped = Counter({word: n for word, n in dropped.items() if word not in said[: cue[0]]})
            added = Counter({word: n for word, n in added.items() if word not in said})
    return tuple(dropped + added)


MERIDIEMS = word_set("am pm")
# "9 a m", "3pm": a meridiem said apart or joined to its number.
SAID_MERIDIEM = re.compile(r"\d\s*([ap])\.?\s*m\b|\b([ap])\s*\.?\s*m\b\.?")


def meridiems(found: list[str]) -> set[str]:
    """The "am" and "pm" said or written after a time ("9 am", "nine pm"), never the verb "am"."""
    return {word for before, word in pairwise(found) if word in MERIDIEMS and is_number(before)}


def said_meridiems(said: list[str], said_text: str) -> set[str]:
    spelled = {f"{found[0] or found[1]}m" for found in SAID_MERIDIEM.findall(said_text.lower())}
    return meridiems(said) | spelled


# Typographic marks an editor may use, read as the plain marks the guardrail knows: curly quotes as straight, a dash
# between words as a comma, one between digits as a hyphen ("2-1"), an ellipsis as a full stop.
EN_DASH, EM_DASH, MINUS = "\N{EN DASH}", "\N{EM DASH}", "\N{MINUS SIGN}"
TYPOGRAPHIC = str.maketrans(
    {
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{DOUBLE LOW-9 QUOTATION MARK}": '"',
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{HORIZONTAL ELLIPSIS}": ".",
    }
)
DIGIT_DASH = re.compile(rf"(?<=\d)[{EN_DASH}{EM_DASH}](?=\d)")
WORD_DASH = re.compile(rf"\s*[{EN_DASH}{EM_DASH}]\s*")
# A minus sign before a number said "minus" or "negative" ("-3 degrees").
MINUS_SIGN = re.compile(rf"(?<![\w.])[-{MINUS}](?=\d)")
# A whole hour's ":00" ("3:00", "4:00 pm") says no number of its own.
WHOLE_HOUR = re.compile(r"\b(\d{1,2}):00\b")
# An amount padded to two decimals ("₹562.50" for "five hundred sixty two point five rupees").
PADDED = re.compile(r"([₹$€£]\s?\d[\d,]*\.)(\d)0\b")
CENTS = re.compile(r"\$0\.(\d\d)\b")
# "Rs 1,200" for rupees said.
RUPEES_WRITTEN = re.compile(r"\bRs\.?\s?(?=\d)", re.IGNORECASE)
RUPEE_WORDS = word_set("rupees rupee")
DOTTED_MERIDIEM = re.compile(r"(?<=\d)(\s?)([apAP])\.[mM]\.(?=,?\s+[a-z])")
DOTTED_MERIDIEM_END = re.compile(r"(?<=\d)(\s?)([apAP])\.[mM]\.")
TENS = dict(zip("123456789", words("ten twenty thirty forty fifty sixty seventy eighty ninety"), strict=True))
# Units an editor abbreviates after a number ("5 kg", "200 MB", "20°C"), each with the words that say it.
UNIT_SAYINGS = {
    unit: [tuple(words(saying)) for saying in sayings.split("|")]
    for unit, sayings in {
        "kg": "kilograms|kilogram|kilos|kilo",
        "g": "grams|gram",
        "mg": "milligrams|milligram",
        "km": "kilometers|kilometer|kilometres|kilometre",
        "m": "meters|meter|metres|metre",
        "cm": "centimeters|centimeter|centimetres|centimetre",
        "mm": "millimeters|millimeter|millimetres|millimetre",
        "kb": "kilobytes|kilobyte",
        "mb": "megabytes|megabyte",
        "gb": "gigabytes|gigabyte|gigs|gig",
        "tb": "terabytes|terabyte",
        "ml": "milliliters|milliliter|millilitres|millilitre",
        "l": "liters|liter|litres|litre",
        "v": "volts|volt",
        "w": "watts|watt",
        "kw": "kilowatts|kilowatt",
        "hz": "hertz",
        "ghz": "gigahertz",
        "mph": "miles per hour",
        "ft": "feet|foot",
        "lb": "pounds|pound",
        "lbs": "pounds",
        "oz": "ounces|ounce",
        "hr": "hours|hour",
        "hrs": "hours",
        "min": "minutes|minute",
        "mins": "minutes",
        "sec": "seconds|second",
        "secs": "seconds",
        "°c": "degrees celsius|degree celsius|celsius|degrees",
        "°f": "degrees fahrenheit|degree fahrenheit|fahrenheit|degrees",
        "°": "degrees|degree",
    }.items()
}
WRITTEN_UNIT = re.compile(
    r"(?<=\d)\s?(°\s?[cf]|°|(?:"
    + "|".join(sorted((u for u in UNIT_SAYINGS if u[0] != "°"), key=len)[::-1])
    + r"))(?![\w])",
    re.IGNORECASE,
)


def plain_marks(text: str) -> str:
    """Curly quotes as straight, a dash between words as a comma, one between digits as a hyphen ("2-1")."""
    straight = text.translate(TYPOGRAPHIC)
    if EM_DASH not in straight and EN_DASH not in straight:
        return straight
    hyphened = DIGIT_DASH.sub("-", straight)
    return WORD_DASH.sub(", ", hyphened)


def readable(wrote_text: str, said: list[str]) -> str:
    """The answer with an editor's typography read as the words and marks said: plain marks, a minus sign, a whole
    hour's ":00", an amount padded to two decimals, cents and a unit's abbreviation ("5 kg" 5 kilograms)."""
    text = plain_marks(wrote_text)
    if not any(mark in text for mark in MINUS + ":.$°Rr") and WRITTEN_UNIT.search(text) is None:
        return text
    spoken = set(said)
    sign = next((word for word in ("minus", "negative") if word in spoken), None)
    if sign is not None:
        text = MINUS_SIGN.sub(f"{sign} ", text)
    if "rs" in text.lower() and not RUPEE_WORDS.isdisjoint(spoken):
        text = RUPEES_WRITTEN.sub("₹", text)
    if "cents" in spoken:
        text = CENTS.sub(r"\1 cents", text)
    if ".m." in text:
        # "4 p.m." is the "pm" said; its last dot ends the sentence only where one ends.
        text = DOTTED_MERIDIEM_END.sub(r"\1\2m.", DOTTED_MERIDIEM.sub(r"\1\2m", text))
    text = WHOLE_HOUR.sub(r"\1", text)
    if not POINTS.isdisjoint(spoken):
        # "₹562.50" for "point five", never "$4.50" for "four dollars fifty".
        text = PADDED.sub(
            lambda found: found.group() if TENS[found.group(2)] in spoken else found.group(1) + found.group(2), text
        )

    def unit(found: re.Match[str]) -> str:
        written = found.group(1).lower().replace(" ", "")
        saying = next((saying for saying in UNIT_SAYINGS.get(written, ()) if occurrences(said, saying)), None)
        return found.group() if saying is None else " " + " ".join(saying)

    return WRITTEN_UNIT.sub(unit, text)


# "slash" after its subject or a modal is the verb ("we slash costs", "should slash replicas"), never a path's "/".
SLASHING = word_set("i we you they he she should will would can could must might shall lets let's")


def slash_verbed(said: list[str], wrote_text: str) -> tuple[str, ...] | None:
    """A "/" written for the verb "slash": the word said after it, written straight after a "/"."""
    for i, word in enumerate(said[1:-1], 1):
        if word != "slash" or said[i - 1] not in SLASHING:
            continue
        path = re.search(rf"(?<![\w.])/{re.escape(said[i + 1])}\b", wrote_text, re.IGNORECASE)
        if path is not None:
            return (path.group(),)
    return None


# Round b2 fix: a spoken number written as its word and its digit ("two 2-liter bottles").
SMALL_VALUES = dict(zip(words("one two three four five six seven eight nine ten"), map(str, range(1, 11)), strict=True))
SAID_TWICE = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)[\s-]+(\d+)\b", re.IGNORECASE)


def count_value(word: str) -> int:
    return VALUES.get(word, int(word) if word.isdigit() else 0)


SMALL_COUNT_WORDS = word_set("one two three four five six seven eight nine ten 1 2 3 4 5 6 7 8 9 10")


def doubled_count(said: list[str], wrote_text: str, numbers: set[int]) -> str | None:
    """A count written twice in a row where it was said once ("two liter bottles" two two-liter bottles), round b4.
    The count was said, or the answer is refused as unheard already; a list item's number is no count, and a count
    after a mark ("2.2", "two. Two") is another."""
    if SMALL_COUNT_WORDS.isdisjoint(said):
        return None
    for i, ((one, _), (other, marked)) in enumerate(pairwise(tokens(wrote_text))):
        if marked or one not in SMALL_COUNT_WORDS or i in numbers:
            continue
        head = other.partition("-")[0]
        if head not in SMALL_COUNT_WORDS:
            continue
        value = count_value(one)
        if value != count_value(head):
            continue
        said_pairs = {(count_value(first), count_value(second)) for first, second in pairwise(said)}
        if (value, value) not in said_pairs:
            return head
    return None


def signed_off(said_text: str, said: list[str], wrote_text: str) -> str | None:
    """A last line that only names someone said earlier, where the speech never ended with them nor said them on a
    line of their own: an invented sign-off ("my son aarav ... thank you" Thank you. / Aarav), round b4."""
    lines = wrote_text.rstrip().rsplit("\n", 1) if "\n" in wrote_text else []
    if len(lines) < 2:
        return None
    last_line = lines[1]
    if ANY_ITEM_LINE.match(last_line):
        return None
    last = words(last_line)
    invented = 0 < len(last) <= 2 and all(word in said and not word.isdigit() for word in last)
    if not invented or said[-len(last) :] == last:
        return None
    # A line said with the name first is a sign-off said ("Neha from Guwahati").
    said_lines = [words(line)[: len(last)] for line in said_text.splitlines()]
    return None if last in said_lines else last[-1]


def check(said_text: str, wrote_text: str, terms: list[str], first_item: int = 1) -> Reject | None:
    found = verdict(said_text, wrote_text, terms, first_item)
    return None if found is None else found.rule


def unique(found: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(found))


def verdict(
    said_text: str, wrote_text: str, terms: list[str], first_item: int = 1, revised: bool = False
) -> Reason | None:
    """The first rule the answer breaks, with the words it is about, or None for an answer to trust. `first_item`
    is the number a one-item list may start at: the next after a list in the prior. A refused answer that puts a
    usage slip right ("did you went" Did you go) is judged again against the speech with the slip fixed."""
    said_text = spoken(said_text, wrote_text)
    found = judged(said_text, wrote_text, terms, first_item, revised)
    if found is not None and any(
        judged(fixed, wrote_text, terms, first_item, revised) is None for fixed in usage_fixed(said_text)
    ):
        found = None
    announcement = unlisted(said_text, wrote_text) if found is None else None
    stepped = steps_unlisted(said_text) if announcement is None and found is None else None
    if stepped is not None and not ANY_ITEM_LINE.search(wrote_text):
        announcement = stepped
    if announcement is not None:
        return Reason(Reject.UNLISTED, (announcement,))
    return found


# A capitalised word run on inside a list item after a lowercase one ("2. Turn off the lamps Keep my laptop").
GLUED = re.compile(r"(?<=[a-z]) ([A-Z][a-z']+)\b")


def glued_item(said_text: str, wrote_text: str) -> str | None:
    """A sentence the speaker ended with a full stop, run into a list item after it: the item lost its line (founder
    -021, round b6). Names the run-on sentence's first words."""
    stopped: set[str] | None = None
    for found in GLUED.finditer(wrote_text):
        line_start = wrote_text.rfind("\n", 0, found.start()) + 1
        if not ANY_ITEM_LINE.match(wrote_text, line_start):
            continue
        if stopped is None:
            stopped = {word.lower() for word in AFTER_STOP.findall(said_text)}
        if found.group(1).lower() in stopped:
            return " ".join(wrote_text[found.start(1) :].split()[:3])
    return None


AFTER_STOP = re.compile(r"[.?!]\s+([A-Za-z']+)")
STEP_JOINS = word_set("then")
# A step that waits on what someone says ("whatever he says, then call her") is no step of its own.
DEPENDING = word_set("whatever")


def steps_unlisted(said_text: str) -> str | None:
    """Three steps or more after "do one thing", joined by "then" across full stops too, written back as prose: rule
    3 makes them a bulleted list (founder-003, round b6). Names the phrase."""
    # Letters the phrase holds, to pass over speech with none at once.
    if "thing" not in said_text:
        return None
    said = words(said_text)
    for phrase in DO_ONE_THING:
        starts = phrase_starts(said, phrase)
        if not starts:
            continue
        steps, step = 1, []
        for word in said[starts[0] + len(phrase) :]:
            if word in STEP_JOINS:
                steps += DEPENDING.isdisjoint(step)
                step = []
            else:
                step.append(word)
        return " ".join(phrase) if steps >= DO_ONE_THING_ITEMS else None
    return None


def judged(
    said_text: str, wrote_text: str, terms: list[str], first_item: int = 1, revised: bool = False
) -> Reason | None:
    """`verdict` against the speech as said."""
    said_tokens = tokens(plain_marks(said_text))
    wrote_text = readable(wrote_text, [word for word, _ in said_tokens])
    wrote = words(wrote_text)
    if DOUBLED.search(said_text):
        said_tokens = unstuttered(said_tokens, wrote)
    said = [word for word, _ in said_tokens]
    marks = [marked for _, marked in said_tokens]
    if replies(said, wrote):
        return Reason(Reject.REPLY, tuple(wrote[:3]))
    in_terms = {word for term in terms for word in term_words(term)}
    heard = set(said)
    spoken = spoken_numbers(said)
    known = heard | FUNCTION_WORDS | {value for _, value in spoken}
    if "plus" in heard:
        known.add("+")
    if DIGIT_RUN.search(said_text):
        known |= {word for digit in said if digit in SPELLED for word in SPELLED[digit]}
        # The letters said with a number in one word ("3pm" 3 pm).
        known |= {piece for word in said if DIGIT_RUN.search(word) for piece in re.findall(r"[^\d_-]+", word)}
    items = list_items(wrote_text)
    listed = items if items and numbering(wrote_text, first_item) else set()
    unexplained = [i for i, word in enumerate(wrote) if i not in listed and not explained(word, known, heard)]
    # Runs of said words written as one are many strings, so they are made only when a word needs them.
    if unexplained:
        known |= joins(said) | timed_forms(said)
        unexplained = [i for i in unexplained if not explained(wrote[i], known, heard)]
    opcodes = align(said, wrote)
    speech = Speech(said_text, wrote_text)

    found_swaps: dict[Edit, list[Swap]] = {}

    def alike(op: Edit) -> list[Swap]:
        if op not in found_swaps:
            found_swaps[op] = swaps(said, wrote, op, in_terms, speech)
        return found_swaps[op]

    if unexplained:
        fixed = {j for op in opcodes if op[0] == "replace" for _, written in alike(op) for j in written}
        unheard = [
            wrote[i] for i in unexplained if i not in fixed and not (wrote[i] in in_terms and term_slot(wrote, i, said))
        ]
        if unheard:
            return Reason(Reject.UNHEARD, unique(unheard))
    overfixed = other_person(said, wrote, opcodes) or right_word_swapped(said, wrote, opcodes)
    if overfixed is not None:
        return Reason(Reject.UNHEARD, (overfixed,))
    acknowledged = acknowledges(said, wrote) or added_sentence(wrote_text, opcodes, heard)
    if acknowledged is not None:
        return Reason(Reject.REPLY, acknowledged)
    twice = repeated(said, wrote_text)
    if twice is not None:
        return Reason(Reject.REPEATED, twice)
    lined = bare_lines(said, wrote_text) if "\n" in wrote_text and "\n" not in said_text else None
    if lined is not None:
        return Reason(Reject.FALSE_LIST, lined)
    more = doubled_count(said, wrote_text, items) or signed_off(said_text, said, wrote_text)
    if more is not None:
        return Reason(Reject.UNHEARD, (more,))
    in_numbers = ANY_LIST_ITEM.sub("", wrote_text) if listed else wrote_text
    # A digit written for its misheard sound-alike was said there ("at for pm" 4 pm).
    spoken += [
        ((heard.start, 0), wrote[j])
        for found in found_swaps.values()
        for heard, written in found
        for j in written
        if wrote[j].isdigit()
    ]
    misplaced = out_of_order(spoken, in_numbers)
    if misplaced is not None:
        return Reason(Reject.UNHEARD, (misplaced,))
    twice_said = SAID_TWICE.search(wrote_text) if not heard.isdisjoint(SMALL_VALUES) else None
    if twice_said is not None and SMALL_VALUES[twice_said.group(1).lower()] == twice_said.group(2):
        return Reason(Reject.UNHEARD, (twice_said.group(2),))
    # Names matter only around a cue, and most dictations hold none.
    named = frozenset(names(wrote_text)) if not CUE_WORDS.isdisjoint(said) else frozenset()
    count, retracted, lost_runs, dropped = changed(
        said,
        marks,
        wrote,
        opcodes,
        items,
        alike,
        named,
        frozenset(in_terms) if terms else frozenset(),
        "\n" in wrote_text and ANY_ITEM_LINE.search(wrote_text) is not None,
        spoken,
    )
    # A qualifier misheard for its sound-alike ("hand it to may" me) was never said.
    pairs = [pair for found in found_swaps.values() for pair in found]
    swapped_said = {i for heard, _ in pairs for i in heard}
    swapped_written = {j for _, written in pairs for j in written}
    heard_swapped = Counter(said[i] for i in swapped_said) if pairs else None
    written_swapped = Counter(wrote[j] for j in swapped_written) if pairs else None
    kept = set(wrote) | set(retracted) | {said[i] for i in swapped_said}
    # The said words the answer lost, which a refusal for too many changes names.
    missing = [word for word, _ in said_tokens if word not in kept]
    # An empty answer drops everything, unless a cue may have taken it back.
    if not wrote and not has_cue(said_text):
        return Reason(Reject.DROPPED_SENTENCE, tuple(said))
    # Only a list has an item, and a list has a line break.
    empty_item = "\n" in wrote_text and EMPTY_ITEM.search(wrote_text) is not None
    if count * 100 > max(MAX_CHANGED_PERCENT * len(said), 100) or empty_item or ordinal_nouns(said, wrote, items):
        # Said words written elsewhere count as changes too, and are named so a correction can put them back
        # (round b4).
        moved = [said[k] for op in opcodes if op[0] == "delete" for k in range(op[1], op[2]) if said[k] in kept]
        # Number words written as digits and spoken marks are no loss to put back (round b5).
        named_words = [word for word in missing + moved if content(word)] or missing + moved
        return Reason(Reject.TOO_MANY_CHANGES, unique(named_words))
    term = lost_term(said, said_text, wrote, retracted, terms) if terms else None
    if term is not None:
        return Reason(Reject.TERM, (term,))
    cued_speech = not CUE_WORDS.isdisjoint(said)
    said_unswapped = [word for i, word in enumerate(said) if i not in swapped_said] if pairs else said
    wrote_unswapped = [word for j, word in enumerate(wrote) if j not in swapped_written] if pairs else wrote
    unnegated = negations_taken_back(said) if cued_speech else 0
    # The negations said after the last cue are the corrected version's, which the answer keeps.
    replacing = negations_after_cue(said_unswapped) if cued_speech else 0
    kept_negations = negations(said_unswapped) - negations(retracted)
    if (
        negations(wrote_unswapped) not in {kept_negations, kept_negations - unnegated}
        or (unnegated and negations(wrote_unswapped) > negations(said_unswapped) - unnegated)
        or negations(wrote_unswapped) < replacing
    ):
        flipped = [word for word in wrote if negations([word]) and word not in said]
        flipped += [word for word in said if negations([word]) and word not in wrote]
        return Reason(Reject.NEGATION, unique(flipped))
    timing = retensed(said, wrote, opcodes, retracted, (swapped_said, swapped_written))
    timing = timing or (cued_speech and past_dropped_by_cue(said, wrote))
    if futures(said) - futures(retracted) > futures(wrote) or timing:
        timed = [word for word in said if (past_form(word) or futures([word])) and word not in wrote]
        timed = timed or [word for word in wrote if past_form(word) and word not in said]
        # A dropped "will" names itself when the words alone don't ("once i will reach" Once I reach... I will).
        timed = timed or (["will"] if futures(said) > futures(wrote) else [])
        # A tense change no word shows is another change read as one: the checks after this name it, so the
        # correction can say what to fix (14 Oct).
        if timed:
            return Reason(Reject.TENSE, unique(timed))
    kept_qualifiers = qualifiers(said) - qualifiers(retracted)
    written_qualifiers = qualifiers(wrote)
    if heard_swapped and written_swapped:
        kept_qualifiers -= heard_swapped
        written_qualifiers -= written_swapped
    differ = qualifier_change(said, said_text, kept_qualifiers, written_qualifiers)
    if differ:
        return Reason(Reject.QUALIFIER, differ)
    new_meridiems = meridiems(wrote) - said_meridiems(said, said_text) if not MERIDIEMS.isdisjoint(wrote) else None
    if new_meridiems:
        return Reason(Reject.QUALIFIER, tuple(sorted(new_meridiems)))
    verbed = slash_verbed(said, wrote_text) if "slash" in heard and "/" in wrote_text else None
    if verbed is not None:
        return Reason(Reject.FORMAT, verbed)
    # "NAME at DOMAIN dot TLD" made only half an address ("Sam at acme.io"), round b6.
    halved_address = HALF_ADDRESS.search(wrote_text) if {"at", "dot"} <= heard else None
    if halved_address is not None:
        return Reason(Reject.FORMAT, (halved_address.group(),))
    written_otherwise = format_changed(said, said_text, heard, wrote_text, retracted)
    if written_otherwise is not None:
        return Reason(Reject.FORMAT, written_otherwise)
    # A word can only have moved when the alignment both left it out and wrote it somewhere else.
    left = {said[k] for op, start, end, _, _ in opcodes if op != "equal" for k in range(start, end)}
    moved_to = {wrote[k] for op, _, _, start, end in opcodes if op != "equal" for k in range(start, end)}
    reordered = order_changed(said, wrote, retracted, items) if not pairs and not left.isdisjoint(moved_to) else None
    if reordered is not None:
        return Reason(Reject.REORDERED, reordered)
    glued = glued_item(said_text, wrote_text) if "\n" in wrote_text else None
    if glued is not None:
        return Reason(Reject.DROPPED_ITEM, (glued,))
    spoken_list = (marks, first_item, revised, heard, heard_swapped or Counter())
    return lost_content(said, wrote, wrote_text, opcodes, retracted, (lost_runs, dropped), named, spoken_list)


def lost_content(
    said: list[str],
    wrote: list[str],
    wrote_text: str,
    opcodes: list[Edit],
    retracted: list[str],
    lost: tuple[list[list[str]], list[str]],
    named: frozenset[str],
    spoken_list: tuple[list[bool], int, bool, set[str], Counter[str]] = ([], 1, False, set(), Counter()),
) -> Reason | None:
    """Content lost or changed in ways the change budget can't see: a run of content words left out, both versions
    of a correction kept, a question made a statement, a list never asked for or missing
    an item. A run of two content words, four in all, or one dropped with nothing written in its place is lost."""
    lost_runs, left_out = lost
    marks, first_item, revised, heard, misheard = spoken_list
    heard = heard or set(said)
    # A word written elsewhere moved ("what workflows will be" what will the workflows be).
    dropped = [word for word in left_out if wrote.count(word) < said.count(word)] if left_out else []
    if dropped and not CUE_WORDS.isdisjoint(heard):
        dropped = [word for word in dropped if not bridged(said, word, wrote)]
    # A "like" said right after the words that lead a list in goes with them ("the blockers are like" ...:), rule 3.
    first = ANY_ITEM_LINE.search(wrote_text) if "like" in dropped else None
    lead = words(wrote_text[: first.start()]) if first else []
    if lead and any(word == "like" and before == lead[-1] for before, word in pairwise(said)):
        dropped.remove("like")
    listed = "\n" in wrote_text or not BULLETS.isdisjoint(said) or ANY_ITEM_LINE.match(wrote_text) is not None
    short = short_list(said, wrote_text, retracted) if listed else None
    if short is not None:
        return Reason(Reject.DROPPED_ITEM, (short,))
    if any(len(run) >= LOST_RUN for run in lost_runs) or sum(map(len, lost_runs)) >= LOST_TOTAL:
        longest = max(lost_runs, key=len)
        return Reason(Reject.DROPPED_SENTENCE, tuple(longest))
    if dropped:
        return Reason(Reject.DROPPED_WORD, tuple(dropped))
    # Most answers delete no single word: one look at the edits before any search.
    single = any(op == "delete" and end - start == 1 for op, start, end, _, _ in opcodes)
    joiner = split_joiner(said, wrote_text, opcodes) if single else None
    if joiner is not None:
        return Reason(Reject.DROPPED_WORD, (joiner,))
    # A short dictation has no room to lose its subject or article ("its blocked" Blocked., "send the report" Send
    # report.).
    if len(said) < SHORT_TEXT and CUE_WORDS.isdisjoint(said):
        # "a" and "an" are one article ("we has a idea" We have an idea.).
        unapostrophed = Counter(ONE_ARTICLE.get(word, word).replace("'", "") for word in wrote)
        # A joining word ending the speech ("We left. and" We left.) is gone from `said` already (`spoken`).
        said_small = Counter(ONE_ARTICLE.get(word, word).replace("'", "") for word in said if word in SHORT_KEEP)
        # A misheard word put right is no loss ("need too people" two).
        said_small -= misheard
        lost_small = [word for word, times in said_small.items() if times > unapostrophed[word]]
        if lost_small:
            return Reason(Reject.DROPPED_WORD, tuple(sorted(lost_small)))
    cued = not CUE_WORDS.isdisjoint(heard)
    both = kept_correction(said, wrote, named) if cued else None
    if both is None and cued and not heard.isdisjoint(VALUES):
        both = number_correction(said, wrote)
    if both is None and not revised and not heard.isdisjoint(TAKING_BACK):
        both = both_kept(said, marks, wrote)
    if both is None and cued:
        both = restated_both(said, marks, wrote)
    # A cue that ends the speech kept as its words ("Approve the budget scratch that.").
    ending = next((cue for cue in ENDING_CUES if tuple(said[-len(cue) :]) == cue), None) if cued else None
    if both is None and ending is not None and tuple(wrote[-len(ending) :]) == ending:
        both = ending
    if both is not None:
        return Reason(Reject.KEPT_CORRECTION, both)
    if asked(said, wrote):
        return Reason(Reject.QUESTION, tuple(said[:4]))
    statement = unasked(wrote_text)
    if statement is not None:
        return Reason(Reject.QUESTION, statement)
    exclaimed = overasked(wrote_text)
    if exclaimed is not None:
        return Reason(Reject.NOT_A_QUESTION, exclaimed)

    if listed and false_list(said, marks, wrote_text, first_item):
        return Reason(Reject.FALSE_LIST, tuple(line.strip() for line in ANY_ITEM_LINE.split(wrote_text)[1:3]))
    if listed and unnumbered(said, marks, wrote_text):
        return Reason(Reject.NUMBERED, ())
    if not listed and counted_as_numbers(said, marks, wrote_text):
        return Reason(Reject.NUMBERED, ())
    return None


def bridged(said: list[str], word: str, wrote: list[str]) -> bool:
    """A word said on both sides of a correction cue and written once: the answer kept it where both versions meet
    ("move the delivery slot no no the courier changed the slot" the courier changed the delivery slot), round b6."""
    if not content(word) or qualifiers([word]) or word not in wrote:
        return False
    spots = [
        i for i, found in enumerate(said) for cue in CUE_STARTS.get(found, ()) if tuple(said[i : i + len(cue)]) == cue
    ]
    places = [i for i, found in enumerate(said) if found == word]
    near = [
        spot
        for spot in spots
        if any(spot - BRIDGE_BEFORE <= i < spot for i in places)
        and any(spot < i <= spot + BRIDGE_AFTER for i in places)
    ]
    return bool(near)


BRIDGE_BEFORE = 10
BRIDGE_AFTER = 5


# A sentence that announces the statements after it (D5): "Here are a few things about me.", "the following".
ANNOUNCING = re.compile(
    r"\bhere are\b.*\b(?:things|points|facts|details|notes|updates|reasons|items)\b|\bthe following\b",
    re.IGNORECASE,
)

ANNOUNCING_WORDS = 12
SAID_SENTENCE = re.compile(r"(?<=[.!?:])\s+")


def unlisted(said_text: str, wrote_text: str) -> str | None:
    """An announcement the recognizer stopped as a sentence, then two statements or more it stopped too, written
    back as prose: the list D5 asks for was lost (founder-019). Speech with no stops, or questions, is left to the
    model."""
    # Letters every announcement holds, to pass over speech with none at once.
    hinted = "ere are" in said_text or "ollowing" in said_text
    if not hinted or ANY_ITEM_LINE.search(wrote_text):
        return None
    sentences = SAID_SENTENCE.split(said_text.strip())
    for i, sentence in enumerate(sentences):
        short = len(sentence.split()) <= ANNOUNCING_WORDS
        if not short or sentence[-1:] not in ".:" or ANNOUNCING.search(sentence) is None:
            continue
        statements = [after for after in sentences[i + 1 :] if after[-1:] == "."]
        if len(statements) >= 2 and len(statements) == len(sentences) - i - 1:
            return sentence.rstrip(".:")
    return None


SHORT_TEXT = 5
# Round b2 fix: a particle that carries meaning in a short dictation ("see you then", "me too") too.
SHORT_KEEP = word_set("its it's it i we you he she they the a an this that then too also")
ONE_ARTICLE = {"an": "a"}


def spelled(term: str, found: re.Match[str], text: str) -> str:
    """The term as given where the model wrote a variant of it; a lowercase one keeps the capital the model gave it
    at a sentence start ("Zoom"). A plain lowercase word stays as written unless the term mixes cases or holds a
    mark ("ios" iOS, "node.js" Node.js): "cut me some slack" keeps "slack" with the term Slack, "let us know" keeps
    "us" with US. A word inside an email, a web address or a path stays too ("sales@northstar.com")."""
    written = found.group()
    if in_address(text, found.start(), found.end()):
        return written
    mixed = any(letter.isupper() for letter in term[1:]) and any(letter.islower() for letter in term)
    marked = not term.replace(" ", "").isalnum()
    before = text[: found.start()]
    at_start = SENTENCE_START.search(before) is not None
    # A short acronym spelled like a word ("IT", "US") is that word with a sentence's capital ("It is done").
    plain = written.islower() or (at_start and written[1:].islower() and len(term) <= SHORT_ACRONYM)
    if plain and not term.islower() and not mixed and not marked:
        return written
    starts = term.islower() and written[0].isupper() and at_start
    return term[0].upper() + term[1:] if starts else term


ADDRESS_MARKS = "@./:\\"
SHORT_ACRONYM = 3


def in_address(text: str, start: int, end: int) -> bool:
    """Whether text[start:end] is part of an email, a web address or a path: an address mark joins it to a letter."""
    joined_before = start >= 2 and text[start - 1] in ADDRESS_MARKS and text[start - 2].isalnum()
    joined_after = end + 1 < len(text) and text[end] in ADDRESS_MARKS and text[end + 1].isalnum()
    return joined_before or joined_after or (start >= 1 and text[start - 1] in "@/")


def tidy(text: str, terms: list[str]) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    tidied = "\n".join(lines)
    lower = tidied.lower()
    # Most terms aren't in the answer: skip them before building a pattern, which the re cache can't hold for long.
    for term in (term for term in terms if term.lower() in lower):
        pattern = re.compile(rf"(?<![{LETTER}]){re.escape(term)}(?![{LETTER}])", re.IGNORECASE)
        tidied = pattern.sub(lambda found, kept=term, text=tidied: spelled(kept, found, text), tidied)
    return tidied


HYPHENED = re.compile(r"\b[A-Za-z]+-[A-Za-z]+\b")


def joins_as_said(said: str, text: str, terms: list[str]) -> str:
    """A word said whole ("wifi", "oncall") loses a hyphen the model put in it, unless the hyphenated form is a term."""
    heard = {raw.lower(): raw for raw in re.findall(r"[A-Za-z]+", said)}

    def back(found: re.Match[str]) -> str:
        said_word = heard.get(found.group().replace("-", "").lower())
        if said_word is None or found.group() in terms:
            return found.group()
        starts = SENTENCE_START.search(text[: found.start()]) is not None
        return said_word[0].upper() + said_word[1:] if starts else said_word

    joined = HYPHENED.sub(back, text)
    return joined


# A determiner and a lowercase describing word before a term that is also a plain word ("the electricity Bill"); after
# "a" or "an" a product is often meant ("record a quick Loom").
DESCRIBED = re.compile(r"\b(?i:the|this|that|these|those|my|your|our|his|her|their|its)\s+([a-z]+)\s+$")


def twins_as_said(said: str, text: str, terms: list[str]) -> str:
    """A term that is also a plain word, said in lowercase and written with the term's capital after a determiner
    and a describing word, is the plain word ("the electricity bill" with Bill, "this yoga asana" with Asana), round
    b6. A term that names the thing keeps its capital ("ask Bill", "in Asana")."""
    for term in terms:
        plain = term.lower()
        if not (term.isalpha() and term[0].isupper() and term[1:] == plain[1:]) or plain not in said:
            continue
        spots = [found.start() for found in re.finditer(rf"(?<![{LETTER}]){term}(?![{LETTER}])", text)]
        # From the last, so each change leaves the text before the next spot as written.
        for spot in reversed(spots):
            described = DESCRIBED.search(text[:spot])
            if described is not None and described.group(1) not in SMALL_WORDS:
                text = text[:spot] + plain + text[spot + len(term) :]
    return text


# Each count up to ten as a digit typed alone and as its word, compiled once: `digits_as_said` runs on every answer.
SMALL_COUNTS = [
    (re.compile(rf"(?<![\w.,:$₹/-]){digit}(?![\w.,:%/-])"), re.compile(rf"\b{word}\b", re.IGNORECASE), digit)
    for word, digit in SMALL_VALUES.items()
]
ANY_DIGIT = re.compile(r"\d")


def digits_as_said(said: str, text: str) -> str:
    """A count up to ten the speaker's text already held as a digit, and never as its word, goes back to the digit
    where the model wrote the word ("5 countries" five countries), as rule 4 asks (14 Oct 2026)."""
    if ANY_DIGIT.search(said) is None:
        return text
    for typed, said_word, digit in SMALL_COUNTS:
        if typed.search(said) is not None and said_word.search(said) is None:
            text = said_word.sub(digit, text)
    return text


def as_said(said: str, text: str, terms: list[str]) -> str:
    digits_kept = digits_as_said(said, text)
    joined = joins_as_said(said, digits_kept, terms)
    tidied = tidy(joined, terms)
    twins = twins_as_said(said, tidied, terms) if terms else tidied
    hosts = hosts_lowercase(twins, terms)
    named = names_as_terms(hosts, terms) if terms else hosts
    return named


def join(*parts: str) -> str:
    present = [part for part in parts if part]
    return " ".join(present)


def mid_sentence(prior: str) -> bool:
    return bool(prior.strip()) and prior.rstrip(" \t")[-1] not in SENTENCE_ENDS


def opening(text: str) -> str:
    """The first word, lowercase and without the marks around it: cheaper than `words` for one word."""
    found = text.split(maxsplit=1)
    first = found[0].lower().strip(".,;:!?\"'()") if found else ""
    return first


def drops_join(prior: str, text: str, output: str) -> bool:
    """Whether the answer to a piece that goes on with `prior` mid-sentence lost the word joining it."""
    if not mid_sentence(prior):
        return False
    joining = opening(text)
    lost = joining in JOINS and opening(output) != joining
    return lost


# Cues that take back the sentence before them when they end the speech.
ENDING_CUES = [tuple(words(cue)) for cue in "scratch that|strike that|ignore that|cancel that|no wait".split("|")]
# Cues that only take back, which a piece may open with to correct its prior.
OPENING_CUES = [tuple(words(cue)) for cue in "no no|no wait|scratch that|strike that".split("|")]


def opening_cue(text: str) -> tuple[str, ...] | None:
    first = tuple(words(" ".join(text.split()[:2])))
    return next((cue for cue in OPENING_CUES if first[: len(cue)] == cue), None)


LAST_ITEM = re.compile(r"(?:^|\n)\s*(\d+)\.\s[^\n]*$")
LAST_BULLET = re.compile(r"(?:^|\n)\s*[-•*]\s[^\n]*$")


def next_item(prior: str) -> int:
    """The number a list in the answer goes on with: one past the prior's last line when that is a list item, and 0
    after a bulleted one, which a lone bullet may go on with."""
    found = LAST_ITEM.search(prior.rstrip())
    if found is None:
        return 0 if LAST_BULLET.search(prior.rstrip()) else 1
    return int(found.group(1)) + 1


def joined_lowercase(prior: str, text: str) -> str:
    """The piece as said, refused, with a joining word that goes on with a sentence and a capitalised word after it
    that is never a name in lowercase ("appealing and how is it")."""
    if not prior or not mid_sentence(prior) or opening(text) not in JOINS:
        return text
    pieces = text.split(" ", 2)
    if len(pieces) > 1 and pieces[1].lower().strip(".,?!") in NEVER_NAMES:
        pieces[1] = pieces[1].lower()
    pieces[0] = pieces[0].lower()
    lowered = " ".join(pieces)
    return lowered


# Words that start a mark said in a piece ("question mark", "full stop"), which a revision puts on the prior.
PIECE_MARK_WORDS = MARK_STARTS | word_set("question full comma period colon semicolon exclamation")


def retouched(prior: str, revision: str | None, text: str) -> bool:
    """A revision with the prior's words and lines, only marked or cased again: the prior is typed already, and a
    piece that takes nothing back and says no mark leaves it so (founder-016), round b4."""
    if revision is None or not PIECE_MARK_WORDS.isdisjoint(words(text)):
        return False
    same_lines = revision.count("\n") == prior.count("\n")
    same_words = words(revision) == words(prior)
    return same_lines and same_words


def past_prior(prior: str, revision: str) -> str | None:
    """What a revision adds after the prior's words, when it holds them all first: a piece that takes nothing back
    answered as the prior with the piece run into it or put on as an item (founder-017, founder-018, round b5). The
    typed prior stays as it is, and the rest is the piece's answer. None when the revision changes the prior's words."""
    said = words(prior)
    if words(revision)[: len(said)] != said:
        return None
    for gap in re.finditer(r"\s+", revision):
        if len(words(revision[: gap.start()])) == len(said):
            return revision[gap.end() :]
    return None


# Cues that open a piece and take back the end of the prior (round b6): "make that" and its like always do; the
# rest only when the piece restates words of the prior's end, and "sorry" never as an apology, nor "wait" as a verb.
ALWAYS_TAKING = [tuple(words(cue)) for cue in "make that|no wait|scratch that|i meant".split("|")]
SEAM_CUES = ALWAYS_TAKING + [tuple(words(cue)) for cue in "i mean|wait|rather|sorry".split("|")]
APOLOGY_AFTER = word_set("for about to i i'm im but guys everyone all")
WAITING_AFTER = word_set("for until till a here there and")
SEAM_REACH = 8
SEAM_PLAIN = word_set("the a an and is are was were to of it this that i we you")


def seam_cue(prior: str, text: str, wrote: str) -> tuple[str, ...] | None:
    """A piece that opens with a cue taking back the end of the prior, answered with the cue still written: both
    versions kept ("...at 3:15. I mean, at 8:45", "...the ballroom—wait, the JW Marriott..."). Names the cue."""
    said = words(text)
    cue = next((cue for cue in SEAM_CUES if tuple(said[: len(cue)]) == cue), None)
    if cue is None or len(said) == len(cue):
        return None
    after = said[len(cue) :]
    if (cue[-1] == "sorry" and after[0] in APOLOGY_AFTER) or (cue == ("wait",) and after[0] in WAITING_AFTER):
        return None
    tail = set(words(prior)[-SEAM_REACH:]) - SEAM_PLAIN
    if cue not in ALWAYS_TAKING and tail.isdisjoint(after[:SEAM_REACH]):
        return None
    heard = join(prior, text)
    written = words(WORD_DASH.sub(" ", wrote))
    return cue if occurrences(written, cue) >= occurrences(words(heard), cue) else None


def judge(prior: str, text: str, terms: list[str], revision: str | None, output: str) -> Polished:
    """A first chunk answered only as a revision is judged as that text; otherwise a revision needs a prior it
    changes."""
    if not prior and not output and revision:
        output = revision
    if not prior or revision == prior or retouched(prior, revision, text):
        revision = None
    heard = join(prior, text)
    output = undangled(text, as_heard(heard, output)) if output else output
    revision = None if revision is None else as_heard(heard, revision)
    raw = joined_lowercase(prior, text)
    if revision is None and drops_join(prior, text, output):
        joining = Reason(Reject.DROPPED_JOIN, (opening(text),))
        return Polished(text=raw, revision=None, rejected=Reject.DROPPED_JOIN, reason=joining)
    if revision is None and not mid_sentence(prior) and unpolished(text, output):
        raw = Reason(Reject.UNPOLISHED, ())
        return Polished(text=text, revision=None, rejected=Reject.UNPOLISHED, reason=raw)
    opener = opening_cue(text)
    if prior and revision is None and opener and tuple(words(output)[: len(opener)]) == opener:
        # A piece that opens by taking back the prior ("no no friday") came back with its cue and no revision.
        kept = Reason(Reject.KEPT_CORRECTION, opener)
        return Polished(text=raw, revision=None, rejected=Reject.KEPT_CORRECTION, reason=kept)
    said = text if revision is None else join(prior, text)
    wrote = output if revision is None else join(revision, output)
    seam = seam_cue(prior, text, wrote) if prior else None
    if seam is not None:
        kept = Reason(Reject.KEPT_CORRECTION, seam)
        return Polished(text=raw, revision=None, rejected=Reject.KEPT_CORRECTION, reason=kept)
    reason = verdict(said, wrote, terms, next_item(prior), revised=revision is not None)
    if reason is not None:
        return Polished(text=raw, revision=None, rejected=reason.rule, reason=reason)
    tidied_revision = None if revision is None else shaped(said, as_said(said, revision, terms))
    tidied = shaped(said, as_said(said, output, terms))
    # A joining word going on with a sentence is never capitalised, nor a word after it that is never a name
    # ("appealing and how is it", not "And How").
    if revision is None:
        tidied = joined_lowercase(prior, tidied)
    polished = Polished(text=tidied, revision=tidied_revision, rejected=None)
    return polished
