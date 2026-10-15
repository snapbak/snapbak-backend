"""Numbers and lists: every number the words say, in order, and list items counted off ("first", "second")."""

import re

# Python 3.14 calls a method of a from-imported name without its method fast path, so the shared patterns are
# reached through their module.
from snapbak_backend.ai.guardrail import tokens
from snapbak_backend.ai.guardrail.tokens import word_set, words

LONGEST_NUMBER = 12


BULLETS = word_set("bullet")
# A count after "and" ("one red and two green") or an ordinal before an unmarked "in" ("first in line") is prose,
# not a list marker.
CONNECTORS = word_set("and")
# A count next to one of these is a choice ("one or two fixes"), and one between two counts a range ("one to two
# days"): never a list's numbering.
CHOICES = word_set("or")
RANGES = word_set("to")
ORDINALS = word_set("first firstly second secondly third thirdly fourth fourthly fifth fifthly")
PREPOSITIONS = word_set("in of on at")
# An ordinal after one of these modifies a noun ("the first build", "my first draft"), never counts items off.
DETERMINERS = word_set("the my our your his her their this that its")
LIST_ITEM = re.compile(r"\s*\d+\.")
ANY_LIST_ITEM = re.compile(r"^\s*\d+\.", re.MULTILINE)
DIGIT_RUN = re.compile(r"\d+")


# Spoken number word -> value, counting and ordinal. "half" and "quarter" are the minutes of a time ("half past
# three" 3:30).
NUMBER_NAMES = {
    0: "zero oh",
    1: "one first firstly",
    2: "two second secondly",
    3: "three third thirdly",
    4: "four fourth fourthly",
    5: "five fifth fifthly",
    6: "six sixth",
    7: "seven seventh",
    8: "eight eighth",
    9: "nine ninth",
    10: "ten tenth",
    11: "eleven eleventh",
    12: "twelve twelfth",
    13: "thirteen thirteenth",
    14: "fourteen fourteenth",
    15: "fifteen fifteenth quarter",
    16: "sixteen sixteenth",
    17: "seventeen seventeenth",
    18: "eighteen eighteenth",
    19: "nineteen nineteenth",
    20: "twenty twentieth",
    30: "thirty thirtieth half",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}
VALUES = {word: value for value, names in NUMBER_NAMES.items() for word in words(names)}
# The counting words for a digit up to ten, never an ordinal: the prompt writes these numbers as words, so a digit
# Whisper wrote ("do 1 thing") may come back as one ("do one thing").
SPELLED = {
    str(value): {word for word in words(NUMBER_NAMES[value]) if word not in ORDINALS and not word.endswith("th")}
    for value in range(1, 11)
}
# Lakh and crore are English words in India too ("fifty two lakh rupees" ₹52 lakh).
SCALE_NAMES = {
    100: "hundred",
    1_000: "thousand",
    100_000: "lakh",
    1_000_000: "million",
    10_000_000: "crore",
    1_000_000_000: "billion",
}
SCALES = {word: value for value, names in SCALE_NAMES.items() for word in words(names)}


def is_number(word: str) -> bool:
    # Most words are letters only, which hold no digit: skip the search for them.
    digit = not word.isalpha() and DIGIT_RUN.search(word) is not None
    return word in VALUES or word in SCALES or word in FRACTION_WORDS or digit


def number_runs(said: list[str]) -> list[tuple[int, list[str]]]:
    """Runs of number words and digits and where each starts, with an "and" inside a run ("two hundred and
    fifty")."""
    runs: list[tuple[int, list[str]]] = []
    run: list[str] = []
    for position, word in enumerate(said):
        if is_number(word) or (word == "and" and run):
            run.append(word)
        elif run:
            runs.append((position - len(run), run))
            run = []
    if run:
        runs.append((len(said) - len(run), run))
    return runs


def fits(current: int, value: int) -> bool:
    """Whether a number word continues the number so far ("twenty" takes "five"; "two hundred" takes "fifty")."""
    last = current % 100
    return value < 100 if last == 0 else last >= 20 and last % 10 == 0 and value < 10


def read_numbers(run: list[str]) -> list[tuple[int, int]]:
    """The numbers a run says one after another, each with where it starts: "one thirty five" is 1, 35; "nine one
    one" is 9, 1, 1."""
    numbers: list[tuple[int, int]] = []
    total = current = 0
    start: int | None = None
    for index, word in enumerate(run):
        if word in SCALES:
            start = index if start is None else start
            current = max(current, 1) * SCALES[word]
            if SCALES[word] >= 1_000:
                total, current = total + current, 0
            continue
        if word.isdigit():
            if start is not None:
                numbers.append((start, total + current))
            # A scale after the digits multiplies them ("2 lakh" 200000).
            total, current, start = 0, int(word), index
            continue
        value = VALUES.get(word)
        if value is None:
            continue
        if start is not None and (total + current == 0 or not fits(current, value)):
            numbers.append((start, total + current))
            total = current = 0
            start = None
        start = index if start is None else start
        current += value
    if start is not None:
        numbers.append((start, total + current))
    return numbers


def digits(found: list[str]) -> list[str]:
    """The digit runs inside words, in order, without leading zeros ("a17" 17, "0071" 71)."""
    runs = [str(int(run)) for run in DIGIT_RUN.findall(" ".join(found))]
    return runs


type Place = tuple[int, int]


def spoken_numbers(said: list[str]) -> list[tuple[Place, str]]:
    """Every number the words say, as digits, and where: the word, then the digit run inside it. That is each
    word's value, each number, and numbers said digit by digit ("seven seven one" 771, "one thirty five" 135,
    "twenty twenty" 2020), never across an "and" ("five and six")."""
    if not DOUBLES.isdisjoint(said):
        said = doubled(said)
    found = [((position, 0), str(VALUES[word])) for position, word in enumerate(said) if word in VALUES]
    if DIGIT_RUN.search(" ".join(said)):
        found += [
            ((position, run), value)
            for position, word in enumerate(said)
            if DIGIT_RUN.search(word)
            for run, value in enumerate(digits([word]))
        ]
    for offset, run in number_runs(said):
        numbers = read_numbers(run)
        ends = [after - 1 for after, _ in numbers[1:]] + [len(run) - 1]
        for first, (start, _) in enumerate(numbers):
            for last in range(first, min(first + LONGEST_NUMBER, len(numbers))):
                if "and" in run[start : numbers[last][0]]:
                    break
                joined = "".join(str(value) for _, value in numbers[first : last + 1])
                # A number of several words takes them all: written once, its words are no longer said alone
                # ("twenty ninth at two" is never 29 and 9).
                span = ends[last] - start + 1
                found.append(((offset + start, -span if span > 1 else 0), str(int(joined))))
        found += counts_before_scales(offset, run)
    found += shared_scales(said)
    if "point" in said or "to" in said:
        found = points_and_minutes(said, found)
    if not INDIAN_SCALES.keys().isdisjoint(said):
        found += decimal_scales(said, found)
    if not POINT_WORDS.isdisjoint(said) and not SCALES.keys().isdisjoint(said):
        found += point_scales(said)
    return fractions(said, found) if not FRACTION_WORDS.isdisjoint(said) else found


# Minutes said before an hour ("ten to nine" 8:50), unless a range leads them.
RANGE_LEADS = word_set("from past between point half quarter")
MINUTES_TO = {5, 10, 20, 25}
INDIAN_SCALES = {word: value for word, value in SCALES.items() if value in (100_000, 10_000_000)}


def points_and_minutes(said: list[str], found: list[tuple[Place, str]]) -> list[tuple[Place, str]]:
    """`found` with a decimal said without its whole ("point eight" 0.8), and minutes said "to" an hour ("ten to
    nine" 8:50). Minutes over the hour can't be a range, so they are no longer said as themselves ("ten to nine"
    is never 9:10)."""
    for i in [i for i, word in enumerate(said) if word in ("point", "to")]:
        word = said[i]
        if word == "point" and (i == 0 or not is_number(said[i - 1])):
            found.append(((i, 0), "0"))
        before = VALUES.get(said[i - 1]) if i else None
        hour = VALUES.get(said[i + 1]) if i + 1 < len(said) else None
        # "from ten to nine", "half past ten to nine" and "two point five to three" are ranges.
        ranged = i >= 2 and (said[i - 2] in RANGE_LEADS or is_number(said[i - 2]))
        if word == "to" and before in MINUTES_TO and hour is not None and 1 <= hour <= 12 and not ranged:
            if before > hour:
                found = [entry for entry in found if entry not in {((i - 1, 0), str(before)), ((i + 1, 0), str(hour))}]
            found += [((i - 1, 0), str(hour - 1 or 12)), ((i - 1, LATE), str(60 - before))]
    return found


def decimal_scales(said: list[str], found: list[tuple[Place, str]]) -> list[tuple[Place, str]]:
    """An amount of lakhs or crores written as a decimal of its scale ("one lakh fifty thousand" ₹1.5 lakh, "one
    crore twenty lakh" ₹1.2 crore): the whole and the decimals of each amount the words say."""
    decimals: list[tuple[Place, str]] = []
    for place, value in found:
        amount = int(value)
        for scale in INDIAN_SCALES.values():
            if amount > scale and amount % scale:
                whole, rest = divmod(amount, scale)
                share = f"{rest / scale:.6f}"[2:].rstrip("0")
                decimals += [((place[0], 0), str(whole)), ((place[0], LATE), str(int(share)) if share else "0")]
    return decimals


POINT_WORDS = word_set("point")


def point_scales(said: list[str]) -> list[tuple[Place, str]]:
    """A decimal of a scale as the whole amount ("two point five crore" 25000000, "1.5 million" 1500000)."""
    found: list[tuple[Place, str]] = []
    for i in range(1, len(said) - 2):
        whole = VALUES.get(said[i - 1])
        if said[i] not in POINT_WORDS or whole is None:
            continue
        end = i + 1
        decimals = ""
        while end < len(said) and said[end] in VALUES and VALUES[said[end]] < 10:
            decimals += str(VALUES[said[end]])
            end += 1
        if decimals and end < len(said) and said[end] in SCALES:
            amount = round(float(f"{whole}.{decimals}") * SCALES[said[end]])
            found.append(((i - 1, 0), str(amount)))
    return found


DOUBLES = word_set("double")


def doubled(said: list[str]) -> list[str]:
    """A number said "double five" as the "five five" it is ("seven four double two" 7422)."""
    out = list(said)
    for index, word in enumerate(said[:-1]):
        if word in DOUBLES and said[index + 1] in VALUES:
            out[index] = said[index + 1]
    return out


def counts_before_scales(offset: int, run: list[str]) -> list[tuple[Place, str]]:
    """The count before a lakh, a crore, a million or a thousand, which is often written before the word ("fifty
    two lakh" ₹52 lakh), and the two amounts either side of an "and" after a whole thousand ("twenty five thousand
    and thirty thousand" 25000, 30000; "two thousand and five" is also 2005)."""
    found: list[tuple[Place, str]] = []
    begin = 0
    for index, word in enumerate(run):
        if word in SCALES and SCALES[word] >= 1_000:
            counted = read_numbers(run[begin:index])
            found += [((offset + begin + start, 0), str(value)) for start, value in counted[-1:]]
            begin = index + 1
        # "four hundred and five hundred" is two amounts too: the second has its own scale.
        after_scale = index > 0 and run[index - 1] in SCALES
        own_scale = after_scale and (SCALES[run[index - 1]] >= 1_000 or not SCALES.keys().isdisjoint(run[index + 1 :]))
        if word == "and" and own_scale:
            before, after = read_numbers(run[:index])[-1:], read_numbers(run[index + 1 :])[:1]
            found += [((offset + start, 0), str(value)) for start, value in before]
            found += [((offset + index + 1 + start, 0), str(value)) for start, value in after]
    return found


def shared_scales(said: list[str]) -> list[tuple[Place, str]]:
    """A range or choice of amounts said with one scale ("twenty to twenty five thousand" 20,000 to 25,000, "nine
    or ten thousand"): the first takes the scale too."""
    found: list[tuple[Place, str]] = []
    for index in range(1, len(said) - 2):
        if said[index] not in RANGES | CHOICES | CONNECTORS or said[index - 1] not in VALUES:
            continue
        after = number_after(said, index + 1)
        if after is not None and after[1] >= 100:
            found.append(((index - 1, 0), str(VALUES[said[index - 1]] * after[1])))
    return found


# "quarter to six" 5:45 takes the number after it back by a quarter.
FRACTION_WORDS = word_set("quarter")
# The decimals and minutes go after every other number said at the same word ("quarter to four" 3, then 45).
LATE = 9


def number_after(said: list[str], at: int) -> tuple[int, int] | None:
    """The number said from `at` on and the largest scale in it ("fifteen thousand" 15000, 1000), if one starts
    there."""
    run: list[str] = []
    while at + len(run) < len(said) and is_number(said[at + len(run)]) and len(run) < LONGEST_NUMBER:
        run.append(said[at + len(run)])
    numbers = read_numbers(run)
    if not numbers or numbers[0][0] != 0:
        return None
    scale = max((SCALES[word] for word in run if word in SCALES), default=0)
    return numbers[0][1], scale


def fractions(said: list[str], found: list[tuple[Place, str]]) -> list[tuple[Place, str]]:
    """`found` with what each "quarter to" says: the number it takes back, its decimals and minutes after that
    number, and the whole amount before a scale. A number taken back is no longer said as itself ("quarter to six"
    is never 6:45)."""
    for position, word in enumerate(said):
        if word != "quarter" or said[position + 1 : position + 2] != ["to"]:
            continue
        at = position + 2
        after = None if said[at : at + 1] and said[at] in SCALES else number_after(said, at)
        if after is None:
            continue
        value, scale = after
        whole = value // scale if scale else value
        total = value - scale * 0.25
        taken = {str(whole), str(value)}
        found = [entry for entry in found if not (entry[0][0] == at and entry[1] in taken)]
        found = [entry for entry in found if entry != ((position, 0), "15")]
        found += [((at, 0), str(whole - 1)), ((at, LATE), "75"), ((at, LATE), "45")]
        if scale:
            found.append(((at, 0), str(int(total))))
    return found


TIME = re.compile(r"\d+(?::\d+)*")


def out_of_order(spoken: list[tuple[Place, str]], wrote_text: str) -> str | None:
    """The first written number that was not said, or not in the order said (swapped, moved or written twice for one
    saying), or None; the parts of a time go in any order ("ten after three" 3:10). A refusal names only that number
    (round b3), not every digit."""
    last: Place = (-1, 0)
    for time in TIME.findall(tokens.DIGIT_GROUP.sub("", wrote_text)):
        places: list[Place] = []
        for part, value in enumerate(time.split(":")):
            # A time's hour is a number said, never the decimals or minutes a fraction adds ("quarter to four" 3:45).
            found = [
                place
                for place, said in spoken
                if said == str(int(value)) and place > last and not (part == 0 and ":" in time and place[1] == LATE)
            ]
            if not found:
                return time
            place = min(found)
            # A number of several words ends at its last word.
            places.append((place[0] - place[1] - 1, LATE + 1) if place[1] < 0 else place)
        last = max(places)
    return None


def list_items(wrote_text: str) -> set[int]:
    """Where each numbered list item's number stands among the written words."""
    found: set[int] = set()
    # Most answers hold no list, so their lines are never split into words here.
    if ANY_LIST_ITEM.search(wrote_text) is None:
        return found
    offset = 0
    for line in wrote_text.splitlines():
        if LIST_ITEM.match(line):
            found.add(offset)
        offset += len(words(line))
    return found


def counts_on(said: list[str], marks: list[bool], i: int) -> bool:
    """A count after "and" that numbers the next item of a list counted off before it: "One, milk. Two, eggs. And
    three, bread." after a clause mark, or the third count of a run ("one ... two ... and three")."""
    value = VALUES.get(said[i])
    if value is None or value < 2:
        return False
    earlier = [VALUES[word] for word in said[: i - 1] if word in VALUES]
    follows = earlier[-1:] == [value - 1]
    return follows and (marks[i - 1] or earlier[-2:-1] == [value - 2])


def in_prose(said: list[str], marks: list[bool], i: int) -> bool:
    """The number word `said[i]` is prose, not one counting items off."""
    after_and = i > 0 and said[i - 1] in CONNECTORS and said[i] not in ORDINALS and not counts_on(said, marks, i)
    before_preposition = i + 1 < len(said) and not marks[i + 1] and said[i + 1] in PREPOSITIONS
    choice = not CHOICES.isdisjoint(said[max(i - 1, 0) : i] + said[i + 1 : i + 2])
    starts_range = i + 2 < len(said) and said[i + 1] in RANGES and is_number(said[i + 2])
    ends_range = i >= 2 and said[i - 1] in RANGES and is_number(said[i - 2])
    return after_and or before_preposition or choice or starts_range or ends_range
