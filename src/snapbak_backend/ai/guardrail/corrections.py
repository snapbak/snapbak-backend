"""Corrections: the cues a speaker takes words back with, what each takes back, and negations, which a doubled
"no" is not.
"""

from snapbak_backend.ai.guardrail.grammar import FORMAT_WORDS, FUNCTION_WORDS, PAST
from snapbak_backend.ai.guardrail.numbers import VALUES, is_number
from snapbak_backend.ai.guardrail.tokens import word_set
from snapbak_backend.ai.routing import CUES

NEGATORS = word_set(
    "not never no cannot dont doesnt didnt isnt arent wasnt werent hasnt havent hadnt cant couldnt wont wouldnt "
    "shouldnt"
)


CUE_PHRASES = {tuple(cue.replace(",", "").split()) for cue in CUES}
# The words of the cues, but for everyday words a cue phrase holds ("let me", "my bad"), whose loss is a loss like any
# word's.
EVERYDAY = word_set("me let meant make it my bad ignore cancel strike correction change delete don't do this")
CUE_WORDS = {word for cue in CUE_PHRASES for word in cue} - EVERYDAY
# "not friday" after a correction names the version taken back.
DESCRIBING = word_set("not")
# A "no" alone, which may itself be a cue ("latest no oldest").
LONE_NEGATORS = word_set("no")
# Cues that are also everyday words ("wait for", "sorry about"), and the ones that are cues only before a comma.
WEAK_CUES = {
    tuple(cue.split())
    for cue in (
        "wait",
        "sorry",
        "rather",
        "actually",
        "make that",
        "change that",
        "i meant",
        "make it",
        "correction",
        "strike that",
        "my bad",
        "ignore that",
        "cancel that",
        "delete that",
    )
}
# A weak cue right before one of these is an everyday phrase ("wait for it", "sorry about that").
BINDERS = word_set("for about until till than")
COMMA_CUES = {tuple(cue.removesuffix(",").split()) for cue in CUES if cue.endswith(",")}
# A doubled "no" ("no no") is a correction cue, never a negation.
REPEATS = {cue for cue in CUE_PHRASES if len(cue) == 2 and cue[0] == cue[1]}
# Words a lone "no" may put one of in another's place ("come friday no sunday").
DAYS = word_set("monday tuesday wednesday thursday friday saturday sunday today tonight tomorrow yesterday")
# Words of one kind a lone "no" may put one of in another's place ("use red no blue", "mom no dad", "him no her").
KINDS = [
    word_set(group)
    for group in (
        "red orange yellow green blue purple pink black white grey gray brown navy teal maroon violet beige cream gold "
        "silver",
        "left right center centre top bottom middle up down",
        "high low medium",
        "mom dad mum mother father mummy papa sister brother",
        "small large big medium",
        "i we you he she they",
        "me us you him her them",
        "my our your his her their",
    )
]
# After a lone "no" these make a negation phrase, never a correction ("no one objected", "no problem").
NEGATION_PHRASES = word_set("one longer idea problem way matter doubt worries worry body")
# A lone "no" before one of these starts a replacement, never a negation ("keep it with me no in your bag").
PREPOSITIONS = word_set("in on at for with from by")


def negated_again(said: list[str], span: list[str], cue_start: int, cue_end: int, end: int) -> bool:
    """A negation in the replacement after a cue: "don't" or "not" before a word not taken back ("no wait don't
    send it"), or a lone "no" right after it whose words go on to restate the taken ones ("no allergies actually no
    known allergies"), not one after an alternative ("latest no oldest")."""
    after_cue = span[cue_end:]
    said_before = set(said[: end - len(span) + cue_start])
    # "not friday" after "make it thursday" names the version taken back; it negates nothing kept.
    following = [*after_cue[1:], *said[end : end + 1]]
    negators = [
        word
        for word, next_word in zip(after_cue, following, strict=False)
        if word not in LONE_NEGATORS and not (word in DESCRIBING and next_word in said_before)
    ]
    if negations(negators):
        return True
    if not after_cue or after_cue[0] not in LONE_NEGATORS:
        return False
    # "call amy actually no call emily": a "no" before the taken words said again from their start is a cue too.
    if restarts(said, span, cue_end + 1, end):
        return False
    taken = {word for word in span[:cue_start] if word not in FUNCTION_WORDS and word not in LONE_NEGATORS}
    return not taken.isdisjoint(said[end : end + len(taken) + RESTATED_WITHIN])


def lone_cue(said: list[str], marks: list[bool], span: list[str], at: int, end: int) -> bool:
    """A lone "no" right after a cue that goes on with it: a mark after it ("actually no, Nora") or the taken words
    said again from their start after it."""
    marked = at + 1 < len(said) and marks[at + 1]
    return marked or restarts(said, span, at - (end - len(span)) + 1, end)


def restarts(said: list[str], span: list[str], at: int, end: int) -> bool:
    """Whether the word at `span[at]`, or the one said after the span when it ends there, says the span's first word
    again."""
    following = span[at : at + 1] or said[end : end + 1]
    return bool(following) and following == span[:1]


def taking_cue(span: list[str]) -> tuple[int, int] | None:
    """The cue that takes words back in a removed run: the last, unless that is a lone "no" right after another
    cue, which then opens the replacement ("no allergies actually no known allergies")."""
    cue = last_cue(span)
    if cue is not None and tuple(span[cue[0] : cue[1]]) in WEAK_CUES:
        # Two cues in a row are one ("no wait make it thursday").
        earlier = last_cue(span[: cue[0]])
        return (earlier[0], cue[1]) if earlier is not None and earlier[1] == cue[0] else cue
    if cue is None or tuple(span[cue[0] : cue[1]]) not in COMMA_CUES:
        return cue
    earlier = last_cue(span[: cue[0]])
    right_after = (
        earlier is not None and earlier[1] == cue[0] and tuple(span[earlier[0] : earlier[1]]) not in COMMA_CUES
    )
    return earlier if right_after else cue


def last_cue(span: list[str]) -> tuple[int, int] | None:
    """Where the last correction cue in `span` starts and ends, if it holds one: the longest of those ending last
    ("no no", not its second "no")."""
    found = [
        (i, i + len(cue))
        for i, word in enumerate(span)
        for cue in CUES_BY_FIRST.get(word, ())
        if tuple(span[i : i + len(cue)]) == cue
    ]
    return max(found, key=lambda cue: (cue[1], -cue[0]), default=None)


# The cues by their first word: `last_cue` looks up only the cues a word can start (round b2, 11 Oct).
CUES_BY_FIRST: dict[str, list[tuple[str, ...]]] = {}
for _cue in CUE_PHRASES:
    CUES_BY_FIRST.setdefault(_cue[0], []).append(_cue)


RESTATED_WITHIN = 3
# The letters a name put right keeps ("maria" mariam, "jose" joseph).
RESPELLED = 4
# A cue that puts one word right takes back a few words at most ("send it to finance no to legal").
SHORT_TAKEN = 3
# Prefixes that make a word's opposite ("happy no unhappy").
OPPOSITES = ("un", "in", "im", "ir", "il", "dis", "non")


def reads_as_cue(
    said: list[str], marks: list[bool], start: int, cue_start: int, cue_end: int, named: frozenset[str] = frozenset()
) -> bool:
    """Whether the cue at `said[cue_start:cue_end]` takes back `said[start:cue_start]`, though nothing kept is left
    in its place: a strong cue does, and a weak one after a clause mark unless an everyday phrase goes on ("Wait for
    it."), inside a sentence, or when the words after it restate a word it takes back or open the same way ("mark
    said sorry matt said", "the daughter actually the son"). Inside a sentence a lone "no" is a negation ("there is
    no data") unless what follows it opens like the words it takes back ("ship it to staging no to
    production") or with a preposition, or the word after it is of a kind it takes back: a day or a number ("book 3
    tickets no 4")."""
    after = said[cue_end:]
    negator = tuple(said[cue_start:cue_end]) in COMMA_CUES
    if negator and after[:1] and after[0] in NEGATION_PHRASES:
        return False
    bound = bool(after) and after[0] in BINDERS and not marks[cue_end]
    taken = said[start:cue_start]
    # A restatement follows the cue closely; a word said again much later is just said again. So does a word put
    # right ("maria sorry mariam"), and a word with the same small word after it as one taken ("finance on monday no
    # legal on monday").
    soon = after[: len(taken) + RESTATED_WITHIN]
    restated = {word for word in taken if word not in FUNCTION_WORDS} & set(soon) or after[:1] == taken[:1]
    short = len(taken) <= SHORT_TAKEN
    respelled = (
        short
        and bool(after)
        and any(len(word) >= RESPELLED and after[0].startswith(word[:RESPELLED]) for word in taken)
    )
    mirrored = (
        short and len(after) > 1 and after[1] in FUNCTION_WORDS and after[1] in taken[1:] and after[0] not in taken
    )
    opens_clause = marks[cue_start] and not bound
    cue = strong(said, marks, cue_start, cue_end) or opens_clause
    opens_alike = start > 0 and after[:1] == said[start - 1 : start]
    replaces = opens_alike or (bool(after) and after[0] in PREPOSITIONS)
    # A name for another follows only a lone "no" ("amy no emily").
    names_for = named if said[cue_start:cue_end] == ["no"] else frozenset()
    alike = any(same_kind([word, *after[:1]], names_for) for word in taken)
    # A number or a day for another is a correction also at the start ("six no seven chairs"), when it takes back only
    # that.
    lone = sum(word not in FUNCTION_WORDS for word in taken) == 1
    plain = used_plainly(said, marks, start, cue_start, cue_end)
    inside = not plain and ((not marks[start] and (not negator or replaces or alike)) or (lone and alike))
    # One word, then "no" and another, opening the speech names who or what instead ("priya no rahul will lead").
    opener = (
        said[cue_start:cue_end] == ["no"]
        and start == 0
        and len(taken) == 1
        and taken[0] not in FUNCTION_WORDS
        and bool(after)
        and after[0] not in FUNCTION_WORDS
    )
    return cue or inside or bool(restated) or respelled or mirrored or opener


def used_plainly(said: list[str], marks: list[bool], start: int, cue_start: int, cue_end: int) -> bool:
    """A weak cue inside a sentence used as an everyday word: a verb, a joining word or "to" goes on after it, a
    reporting verb comes before it, it binds the word after, or, with nothing else taken, its "that" points at the
    word after ("the brake warning actually stayed off", "is fine sorry but", "sorry to hear", "amit said sorry", "we
    cannot wait for", "delete that file"), unless another version of the words before follows it ("risks sorry for
    blockers", "tuesday actually wednesday")."""
    cue = tuple(said[cue_start:cue_end])
    if cue not in WEAK_CUES:
        return False
    after = said[cue_end:]
    taken = said[start:cue_start]
    if any(same_kind([word, *after[:1]]) for word in taken):
        return False
    goes_on = bool(after) and (after[0] in EVERYDAY_AFTER or (past_form(after[0]) and not any(map(past_form, taken))))
    reported = bool(taken) and taken[-1] in REPORTING
    bound = bool(after) and after[0] in BINDERS and not marks[cue_end]
    # A "that" pointing at the word after takes back nothing when nothing was said before it in the run.
    pointing = (
        not taken and cue[-1] in POINTING and bool(after) and not marks[cue_end] and after[0] not in FUNCTION_WORDS
    )
    soon = after[: len(taken) + RESTATED_WITHIN]
    restated = {word for word in taken if word not in FUNCTION_WORDS} & set(soon) or after[:1] == taken[:1]
    opens_alike = start > 0 and after[:1] == said[start - 1 : start]
    return goes_on or reported or ((bound or pointing) and not restated and not opens_alike)


# Words that go on with a sentence after a weak cue used plainly, and verbs that report what was said before one.
EVERYDAY_AFTER = word_set("but and so because or yet to that if when though although")
REPORTING = word_set("said told asked")
# The last word of a cue that may point at the word after it ("delete that file", "cancel that order").
POINTING = word_set("that this")


def past_form(word: str) -> bool:
    return word in PAST or (word.endswith("ed") and len(word) > 3 and not is_number(word))


def same_kind(pair: list[str], named: frozenset[str] = frozenset()) -> bool:
    """Two words of one kind: days, numbers, colours, sides, levels or family, or a name written for a word that
    is no grammar word ("amy no emily" Emily)."""
    if len(pair) != 2:
        return False
    days = set(pair) <= DAYS
    numbers = all(is_number(word) for word in pair)
    kind = any(set(pair) <= group for group in KINDS)
    name = pair[1] in named and pair[0] not in FUNCTION_WORDS and pair[0] not in NEGATORS
    one, other = pair
    opposite = any(other == prefix + one or one == prefix + other for prefix in OPPOSITES)
    return days or numbers or kind or name or opposite


def strong(said: list[str], marks: list[bool], cue_start: int, cue_end: int) -> bool:
    """A cue that is never an everyday word: not a weak one, and "no" only with a comma or a negation after it ("no
    don't send it")."""
    cue = tuple(said[cue_start:cue_end])
    after = cue_end < len(said) and (marks[cue_end] or negations(said[cue_end : cue_end + 1]) > 0)
    return cue not in WEAK_CUES and (cue not in COMMA_CUES or after)


def restated(taken: list[str], after: list[str]) -> int:
    """Where the words after a cue start saying two or more of the taken words again ("the deck to sam and the
    notes to ravi scratch that the notes to priya" from "the notes"), or 0 when they don't or an earlier cue took
    back the words before."""
    for i in range(len(taken)):
        k = 0
        while k < len(after) and i + k < len(taken) and taken[i + k] == after[k]:
            k += 1
        if k >= 2 and any(word not in FUNCTION_WORDS for word in after[:k]):
            earlier_cue = last_cue(taken[:i])
            return 0 if earlier_cue else i
    return 0


def retracts(
    said: list[str],
    marks: list[bool],
    start: int,
    end: int,
    kept: set[str],
    moved: set[str],
    named: frozenset[str] = frozenset(),
) -> int | None:
    """Where the removed run `said[start:end]` starts taking words back, if it does: words, a cue, then the
    replacement. The said words up to the cue hold more than grammar: "i am sorry" takes nothing back. When the run
    ends at the cue, the said words after it are the replacement, if the cue reads as one ("ravi said sorry about"
    does not), and it takes back only from where the replacement restates the run. Otherwise one of its words went
    into the retracted words' place (`moved`), or the run is followed by kept words and the output kept all of
    it."""
    span = said[start:end]
    cue = taking_cue(span)
    if cue is None or len(span) < 2:
        return None
    cue_start, cue_end = cue
    # A negation said after the cue is the replacement's ("no wait don't send it"), and a weak cue before "for" or
    # "about" is a verb ("wait for"): neither takes words back.
    if negated_again(said, span, cue_start, cue_end, end) or (
        tuple(span[cue_start:cue_end]) in WEAK_CUES and span[cue_end:][:1] and span[cue_end] in BINDERS
    ):
        return None
    takes_back = any(word not in FUNCTION_WORDS and word not in CUE_WORDS for word in said[: start + cue_start])
    if not takes_back:
        return None
    followed = end < len(said)
    # "not tuesday" after the cue names the version taken back, said before it, and is no replacement.
    taken = set(said[: start + cue_start])
    named_back = {
        i + 1
        for i, word in enumerate(span[cue_end:], cue_end)
        if word in DESCRIBING and span[i + 1 : i + 2] and span[i + 1] in taken
    }
    replacement = [
        word
        for i, word in enumerate(span[cue_end:], cue_end)
        if (word not in FUNCTION_WORDS or is_number(word))
        and word not in FORMAT_WORDS
        and not (word in DESCRIBING and i + 1 in named_back)
        and i not in named_back
        # "call amy actually no call emily": a lone "no" right after a cue, before the taken words said again, goes
        # on with the cue.
        and not (i == cue_end and word in LONE_NEGATORS and lone_cue(said, marks, span, start + i, end))
    ]
    if not replacement:
        if not followed:
            return start if scratches_a_sentence(said, marks, start, cue_start + start, cue_end + start) else None
        if not reads_as_cue(said, marks, start, cue_start + start, cue_end + start, named):
            return None
        skipped = restated(span[:cue_start], said[end:])
        before = span[:skipped]
        # A negation said just before the restated words goes back with them ("don't send it no wait send it").
        negated = negations(before) > 0 and all(word in FUNCTION_WORDS or negations([word]) for word in before)
        begin = start if negated else start + skipped
        return begin
    # The replacement is written: one word of it in the retracted words' place, and every other word of it but one
    # somewhere, so a run that goes on past the replacement ("no march fourth we meet in room two eleven") is no
    # retraction.
    in_place = any(written(word, moved) for word in replacement)
    # Digits said one by one are written as one number ("three four five six seven" 34567).
    spelled = "".join(str(VALUES.get(word, "")) for word in replacement if is_number(word))
    numbers_written = bool(spelled) and spelled in kept
    lost = [word for word in replacement if not written(word, kept) and not (numbers_written and is_number(word))]
    # A sentence said again may name with a pronoun what the answer keeps as its noun ("give the medicine to Grandma
    # scratch that give it to her before lunch" Give the medicine to Grandma before lunch), round b4.
    if any(word not in STAND_INS for word in replacement):
        lost = [word for word in lost if word not in STAND_INS]
    # A negation said again in the replacement must be written ("didn't note it i mean ... didn't note it").
    if negations(lost):
        return None
    takes = (in_place and len(lost) <= 1) or (followed and not lost)
    return start + version_start(span[:cue_start], replacement, span[cue_end:]) if takes else None


# Third-person words that stand for a noun said before them; never "me" or "you", who are other people.
STAND_INS = word_set("he she it they him her them his its their")

# Small words a replacement may open with like the version it takes back ("on prod no on staging").
LEAD_WORDS = word_set("on in at to for with from by the a an")


def version_start(taken: list[str], replacement: list[str], after: list[str]) -> int:
    """Where the version a replacement puts right starts among the taken words: at the last word of its kind ("eight
    feet rather twelve feet", "upgrade it tomorrow i mean friday") or its opening small word ("happen on prod no on
    staging"); else all of them."""
    kinds = [i for i, word in enumerate(taken) if replacement and same_kind([word, replacement[0]])]
    if kinds:
        # Of several, the one whose words after it the replacement says again most ("one forty over ninety wait one
        # thirty over eighty" from "one"), the earliest on a tie.
        said_again = set(replacement)
        return max(kinds, key=lambda i: (len(set(taken[i:]) & said_again), -i))
    leads = [i for i, word in enumerate(taken) if after[:1] == [word] and word in LEAD_WORDS]
    return leads[-1] if leads else 0


def scratches_a_sentence(said: list[str], marks: list[bool], start: int, cue_start: int, cue_end: int) -> bool:
    """A strong cue that ends the text takes back the one sentence before it, never more: "Ship it today. Loop in
    Priya. Scratch that." loses "Loop in Priya", and "Approve the budget. Scratch that." all of it."""
    # The first sentence goes only when a mark ends it before the cue: unmarked speech may hold more than one.
    first = start > 0 or marks[cue_start]
    one_sentence = first and 0 <= start < cue_start and marks[start] and not any(marks[start + 1 : cue_start])
    return one_sentence and cue_end == len(said) and strong(said, marks, cue_start, cue_end)


def written(word: str, output: set[str]) -> bool:
    """`word` is in `output`, maybe inside a joined word ("lease" in leasedesk, "grandmas" in grandma's) or as
    digits."""
    if word.isdigit():
        return word in output
    digits = is_number(word) and any(other.isdigit() and as_digits(word, other) for other in output)
    return digits or any(word in other.replace("'", "") for other in output)


def as_digits(word: str, written_digits: str) -> bool:
    """Whether digits may write a number word: its value leads them ("four" 4, "twenty" 25, "two" 200); "four"
    is never 3. A scale or a fraction word may lead any."""
    value = VALUES.get(word)
    if value is None:
        return True
    leading = str(value).rstrip("0") or "0"
    return written_digits.startswith(leading)


def negations(said: list[str]) -> int:
    joined = " ".join(said)
    if NEGATORS.isdisjoint(said) and "'" not in joined and "-" not in joined:
        return 0
    pieces = [piece for word in said for piece in word.replace("'", "").split("-")]
    found = sum(piece in NEGATORS for piece in pieces)
    if found < 2:
        return found
    count = index = 0
    while index < len(pieces):
        if tuple(pieces[index : index + 2]) in REPEATS:
            index += 2
            continue
        count += pieces[index] in NEGATORS
        index += 1
    return count
