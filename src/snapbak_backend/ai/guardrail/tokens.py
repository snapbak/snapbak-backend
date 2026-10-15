"""Words: how the guardrail splits text into words and lines said words up with written ones."""

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# Kept in the split, so each gap between words can be read for a clause mark.
SPLIT = re.compile(r"([\s@/:=$₹€£%,;!?\"()\[\]]+)")
DIGIT_GROUP = re.compile(r"(?<=\d),(?=(?:\d\d,)*\d{3}(?!\d))")
CLAUSE_MARK = re.compile(r"[,.;?!\n]")


def tokens(text: str) -> list[tuple[str, bool]]:
    """Each word, and whether it starts the text or a clause mark (, . ? or a new line) comes right before it."""
    found = list(split_tokens(text))
    return found


# One check splits the same speech and answer several times; the last few splits are kept (round b4: it paid for
# the new checks' time).
@lru_cache(maxsize=64)
def split_tokens(text: str) -> tuple[tuple[str, bool], ...]:
    grouped = DIGIT_GROUP.sub("", text) if "," in text else text
    lowered = grouped.lower()
    # ASCII text has nothing to compose.
    lower = lowered if lowered.isascii() else unicodedata.normalize("NFC", lowered)
    found: list[tuple[str, bool]] = []
    marked = True
    for index, part in enumerate(SPLIT.split(lower)):
        if index % 2:
            # Most gaps are one space, which marks no clause.
            marked = marked or (part != " " and CLAUSE_MARK.search(part) is not None)
            continue
        if "." not in part:
            word = part.strip("'-")
            if word:
                found.append((word, marked))
                marked = False
            continue
        for piece_index, piece in enumerate(part.split(".")):
            marked = marked or piece_index > 0
            word = piece.strip("'-")
            if word:
                found.append((word, marked))
                marked = False
    return tuple(found)


def words(text: str) -> list[str]:
    found = [word for word, _ in tokens(text)]
    return found


def word_set(text: str) -> set[str]:
    found = words(text)
    return set(found)


type Edit = tuple[str, int, int, int, int]


def align(said: list[str], wrote: list[str]) -> list[Edit]:
    """The edits from said to written words, matched from the end, so a word said twice matches its last saying: a
    correction's final version ("book one table scratch that book two tables" book two tables)."""
    # An answer that changed only marks and capitals has the same words: one match, with no search.
    if said == wrote:
        return [("equal", 0, len(said), 0, len(wrote))] if said else []
    backwards = SequenceMatcher(a=said[::-1], b=wrote[::-1], autojunk=False).get_opcodes()
    forwards = [
        (op, len(said) - end, len(said) - start, len(wrote) - written_end, len(wrote) - written_start)
        for op, start, end, written_start, written_end in reversed(backwards)
    ]
    # A longer match can still keep a word's first saying ("no busywork rather no distractions" keeps the first
    # "no"); a removed run between matches that ends on the word kept before it moves back to keep the last.
    for i in range(1, len(forwards) - 1):
        op, start, end, written, _ = forwards[i]
        if op != "delete":
            continue
        before, after = forwards[i - 1], forwards[i + 1]
        shift = 0
        while (
            before[0] == after[0] == "equal"
            and before[2] - before[1] > shift + 1
            and said[start - shift - 1] == said[end - shift - 1]
        ):
            shift += 1
        if shift:
            forwards[i - 1] = ("equal", before[1], before[2] - shift, before[3], before[4] - shift)
            forwards[i] = ("delete", start - shift, end - shift, written - shift, written - shift)
            forwards[i + 1] = ("equal", after[1] - shift, after[2], after[3] - shift, after[4])
    return forwards


# How a word sounds, for a misheard-word fix (docs/api.md, Guardrail): English spelling rules, then the consonants,
# with an opening vowel kept as "a" ("except" and "accept" aksspt, "weather" and "whether" vtr).
HEARD_LATIN = [
    (r"^who", "ho"),
    # "two" is "to", "one" and "once" open with a "w" sound, and "g" before a last "n" and "b" after a last "m" are
    # silent ("reign" rain, "sign", "climb").
    (r"^two$", "to"),
    (r"^on(?=ce$|e$)", "wun"),
    (r"gn(?=(?:e|es|s|ed|ing)?$)", "n"),
    (r"mb(?=s?$)", "m"),
    (r"^wr", "r"),
    (r"^kn", "n"),
    (r"^wh", "w"),
    ("ph", "f"),
    ("tch", "ch"),
    (r"(?<=..)([skpfx]|sh|ch)ed$", r"\1t"),
    (r"(?<=.)gh", ""),
    ("ck", "k"),
    (r"c(?=[eiy])", "s"),
    (r"[cq]", "k"),
    ("x", "ks"),
    ("z", "s"),
    (r"(?<=[aeiou])w(?![aeiou])", ""),
    ("w", "v"),
    (r"(?<=[^aeiou])h", ""),
    (r"^[aeiou]+", "A"),
    (r"[aeiouy]", ""),
    (r"(.)\1+", r"\1"),
]
VOWEL_GROUP = re.compile(r"[aeiouy]+")
# A last "le" after a consonant is a syllable of its own ("principle"); after a vowel the "e" is silent ("sale").
SYLLABIC_LE = re.compile(r"[^aeiouy]le$")
# A last "es" after one consonant other than a hiss is silent too ("snakes", "makes"; not "boxes", "pages").
SILENT_ES = re.compile(r"[aeiouy][^aeiouyszxcgh]es$")


def heard_as(word: str) -> str:
    """A word's sound: its consonants and an opening vowel, "A" ("their" "there" tr)."""
    spelled = word.replace("'", "").replace("ç", "s")
    plain = unicodedata.normalize("NFKD", spelled).encode("ascii", "ignore").decode() or word
    for pattern, replacement in HEARD_LATIN:
        plain = re.sub(pattern, replacement, plain)
    return plain


def syllables(word: str) -> int:
    """The vowel sounds in a word, a silent last "e" or "ed" left out ("debit" 2, "debt" 1)."""
    found = len(VOWEL_GROUP.findall(word))
    silent = found > 1 and (
        (word.endswith("e") and word[-2:-1] not in "aeiouy" and SYLLABIC_LE.search(word) is None)
        or (word.endswith("ed") and word[-3:-2] not in "td")
        or SILENT_ES.search(word) is not None
    )
    return found - silent
