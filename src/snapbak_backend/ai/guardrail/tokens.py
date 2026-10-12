"""Words: how the guardrail splits text into words, lines said words up with written ones, and reads an address
said in Devanagari in the Latin letters it was written in.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# Kept in the split, so each gap between words can be read for a clause mark.
SPLIT = re.compile(r"([\s@/:=$₹€£%,;!?\"()\[\]।॥]+)")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
DIGIT_GROUP = re.compile(r"(?<=\d),(?=(?:\d\d,)*\d{3}(?!\d))")
CLAUSE_MARK = re.compile(r"[,.;?!।॥\n]")
# One Devanagari word spelled with a nasal consonant before another consonant or with a nasal mark ("फाइनेन्स"
# "फाइनेंस", "यहाँ" "यहां"), or with a nukta or without ("ज़्यादा" "ज्यादा").
NASAL = re.compile(r"[ङञणनम]्(?=[क-ह])")


def tokens(text: str) -> list[tuple[str, bool]]:
    """Each word, and whether it starts the text or a clause mark (, . ? । or a new line) comes right before it."""
    found = list(split_tokens(text))
    return found


# One check splits the same speech and answer several times; the last few splits are kept (round b4: it paid for
# the new checks' time).
@lru_cache(maxsize=64)
def split_tokens(text: str) -> tuple[tuple[str, bool], ...]:
    grouped = DIGIT_GROUP.sub("", text) if "," in text else text
    lowered = grouped.lower()
    # ASCII text has nothing to compose and no Devanagari spelling to read alike.
    composed = lowered if lowered.isascii() else unicodedata.normalize("NFC", lowered)
    devanagari = not composed.isascii() and DEVANAGARI.search(composed) is not None
    lower = NASAL.sub("ं", composed.replace("\u093c", "").replace("ँ", "ं")) if devanagari else composed
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


# An email or web address, and the spoken marks that join its pieces in Hindi.
ADDRESS = re.compile(r"[a-z0-9:/.@_-]*[a-z0-9][@.][a-z][a-z0-9:/.@_-]*")
ADDRESS_MARKS = word_set("एट डॉट स्लैश कोलन")
LETTER_NAMES = dict(
    zip(
        words("ए बी सी डी ई एफ जी एच आई जे के एल एम एन ओ पी क्यू आर एस टी यू वी डब्ल्यू एक्स वाई ज़ेड"),
        "abcdefghijklmnopqrstuvwxyz",
        strict=True,
    )
)
# Each sound as one Latin letter: Devanagari consonants (vowels and signs drop) and Latin spellings.
DEVANAGARI_SOUNDS = str.maketrans("कखगघचछजझटठडढणतथदधनपफबभमरलवशषसहं", "kkggccjjttddnttddnpfbbnrlvssshn")
LATIN_SOUNDS = [
    (r"([ptsbdkg])h", r"\1"),
    (r"c(?=[eiy])", "s"),
    (r"[cq]", "k"),
    ("x", "ks"),
    ("w", "v"),
    ("z", "j"),
    ("m", "n"),
    (r"[aeiouy]", ""),
]


type Edit = tuple[str, int, int, int, int]


def align(said: list[str], wrote: list[str]) -> list[Edit]:
    """The edits from said to written words, matched from the end, so a word said twice matches its last saying: a
    correction's final version ("ek tab dena scratch that do tab dena" 2 tab dena)."""
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


def sound(word: str) -> str:
    """A word's consonants, which a Devanagari word and its Latin spelling share ("बिल्स" "bills" bls)."""
    translated = word.translate(DEVANAGARI_SOUNDS)
    latin = DEVANAGARI.sub("", translated)
    for pattern, replacement in LATIN_SOUNDS:
        latin = re.sub(pattern, replacement, latin)
    consonants = re.sub(r"(.)\1+", r"\1", latin)
    return consonants


def in_latin(said: list[str], wrote_text: str) -> list[str]:
    """The said words with each Devanagari one beside a spoken address mark ("एट", "डॉट") that the written address
    spells in Latin letters swapped for that spelling: "बिल्स एट प्रोकोल" bills@procol, a spelled letter run "एच टी टी
    पी एस कोलन" https:. Only an address may change script."""
    if not any(DEVANAGARI.search(word) for word in said):
        return said
    address = {word for found in ADDRESS.findall(wrote_text.lower()) for word in words(found)}
    by_sound = {heard: word for word in address if (heard := sound(word))}
    if not by_sound:
        return said
    groups: list[list[str]] = []
    for word in said:
        letters = bool(groups) and groups[-1][0] in LETTER_NAMES and word in LETTER_NAMES
        if letters:
            groups[-1].append(word)
        else:
            groups.append([word])
    latin: list[str] = []
    for index, group in enumerate(groups):
        beside = groups[index - 1 : index] + groups[index + 1 : index + 2]
        if not any(near[0] in ADDRESS_MARKS for near in beside):
            latin += group
        elif group[0] in LETTER_NAMES:
            latin += [LETTER_NAMES[word] for word in group]
        else:
            latin += [as_latin(word, by_sound, address) if DEVANAGARI.search(word) else word for word in group]
    return latin


# Letter names, longest first, to read a word said as letters ("आईओ" io, "एबीसी" abc).
NAMES_LONGEST = sorted(LETTER_NAMES, key=len, reverse=True)


def letters(word: str) -> str | None:
    """A Devanagari word said as letter names, in Latin letters ("आईओ" io), or None."""
    out = ""
    rest = word
    while rest:
        name = next((name for name in NAMES_LONGEST if rest.startswith(name)), None)
        if name is None:
            return None
        out += LETTER_NAMES[name]
        rest = rest[len(name) :]
    return out


def as_latin(word: str, by_sound: dict[str, str], address: set[str]) -> str:
    """The address word written in Latin for a Devanagari one said beside an address mark: the one with its sound,
    its letters said one by one ("आईओ" io), or a letter then a sound ("जीमेल" gmail), a last "ज" being an "s"
    ("सर्विसेज" services)."""
    heard = sound(word)
    hissed = heard.removesuffix("j")
    found = by_sound.get(heard) or by_sound.get(hissed) or by_sound.get(hissed + "s")
    if found is not None:
        return found
    spelled = letters(word)
    if spelled is not None and spelled in address:
        return spelled
    name = next((name for name in NAMES_LONGEST if word.startswith(name) and len(word) > len(name)), None)
    if name is not None:
        rest = sound(word[len(name) :])
        match = (latin for latin in address if latin[:1] == LETTER_NAMES[name] and sound(latin[1:]) == rest)
        return next(match, word)
    return word


# How a word sounds, for a misheard-word fix (docs/api.md, Guardrail): English spelling rules, then the consonants,
# with an opening vowel kept as "a" ("except" and "accept" aksspt, "weather" and "whether" vtr). Devanagari keeps
# its consonants, aspirated or not, and drops vowel signs and nasal marks ("में" "मैं", "चिता" "चिंता").
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
DEVANAGARI_CONSONANTS = dict(zip("कखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह", "kkggccjjttddnttddnpfbbmyrlvsssh", strict=True))
# Vowels by kind, short and long together ("ि" "ी" i), "ऐ" with "ए" and "औ" with "ओ" ("मैं" "में" me).
DEVANAGARI_VOWELS = {
    **dict.fromkeys("ाअआ", "a"),
    **dict.fromkeys("िीृइई", "i"),
    **dict.fromkeys("ुूउऊ", "u"),
    **dict.fromkeys("ेैॅएऐ", "e"),
    **dict.fromkeys("ोौॉओऔ", "o"),
}
VOWEL_GROUP = re.compile(r"[aeiouy]+")
# A last "le" after a consonant is a syllable of its own ("principle"); after a vowel the "e" is silent ("sale").
SYLLABIC_LE = re.compile(r"[^aeiouy]le$")
# A last "es" after one consonant other than a hiss is silent too ("snakes", "makes"; not "boxes", "pages").
SILENT_ES = re.compile(r"[aeiouy][^aeiouyszxcgh]es$")


def heard_as(word: str) -> str:
    """A word's sound: its consonants and an opening vowel, "A" ("their" "there" tr, "बिल" "bill" bl)."""
    if DEVANAGARI.search(word):
        return heard_in_devanagari(word)
    spelled = word.replace("'", "").replace("ç", "s")
    plain = unicodedata.normalize("NFKD", spelled).encode("ascii", "ignore").decode() or word
    for pattern, replacement in HEARD_LATIN:
        plain = re.sub(pattern, replacement, plain)
    return plain


def heard_in_devanagari(word: str) -> str:
    """Each consonant and each vowel sign, without a consonant's own "a" ("कल" kl, "केला" kela, "मैं" me); nasal
    marks and the nukta drop ("चिंता" "चिता" cita)."""
    sounds = [DEVANAGARI_CONSONANTS.get(letter) or DEVANAGARI_VOWELS.get(letter, "") for letter in word]
    joined = "".join(sounds)
    return re.sub(r"(.)\1+", r"\1", joined)


def syllables(word: str) -> int:
    """The vowel sounds in an English word, a silent last "e" or "ed" left out ("debit" 2, "debt" 1); 0 for
    Devanagari."""
    found = len(VOWEL_GROUP.findall(word))
    silent = found > 1 and (
        (word.endswith("e") and word[-2:-1] not in "aeiouy" and SYLLABIC_LE.search(word) is None)
        or (word.endswith("ed") and word[-3:-2] not in "td")
        or SILENT_ES.search(word) is not None
    )
    return found - silent
