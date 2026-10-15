"""An accepted answer's lists and marks put in shape without a model (docs/api.md, Shaping)."""

import re

from snapbak_backend.ai.guardrail.tokens import align, word_set, words

ITEM = re.compile(r"^(\d+)\.\s+")
BULLET = re.compile(r"^[-•*]\s+")
LIST_LINE = re.compile(r"^(?:\d+\.|[-•*])\s+")
# A first item glued to the line before it ("I am running. 1. It needs...", "Anya, 1. Keep"), told by a "2. " on the
# next line.
GLUED_FIRST = re.compile(r"^(.*[.:!?,])\s+1\.\s+(\S.*)$")
COUNTERS = word_set("first second third fourth fifth sixth seventh eighth ninth tenth firstly secondly thirdly")
# A counting word the model kept at the start of a numbered item, set off by its comma ("1. First, can you").
COUNTER_LEAD = re.compile(r"^(\d+\.\s+)(?:and\s+)?([^\s,]+),\s+(\S)", re.IGNORECASE)
ENDS_BARE = re.compile(r"\w$", re.MULTILINE)
# Words the recognizer capitalised after a joining word that are never a name ("and How is it").
NEVER_NAMES = word_set(
    "how what when where why who which whose is are was were can could will would should do does did the a an it "
    "this that these those there we you they he she my our your their"
)
ITEM_MARK = re.compile(r"^\s*(?:\d+\.|[-•*])\s+")


def unglued(lines: list[str]) -> list[str]:
    """A first item glued to the words before it goes on its own line, and those words end with a colon."""
    found: list[str] = []
    for index, line in enumerate(lines):
        glued = GLUED_FIRST.match(line)
        follows = index + 1 < len(lines) and lines[index + 1].startswith("2. ")
        if glued is None or not follows:
            found.append(line)
            continue
        words_before = glued.group(1)
        introducing = words_before[:-1] + ":" if words_before.endswith(".") else words_before
        found += [introducing, "1. " + glued.group(2)]
    return found


def numbered(lines: list[str]) -> list[str]:
    """A bullet straight after numbered items takes the next number, and a numbered item loses a counting word
    left at its start ("1. First, can you" 1. Can you)."""
    found: list[str] = []
    last = 0
    for line in lines:
        item = ITEM.match(line)
        if item is None and last and BULLET.match(line):
            line = BULLET.sub(f"{last + 1}. ", line)
            item = ITEM.match(line)
        last = int(item.group(1)) if item else 0
        counted = COUNTER_LEAD.match(line) if item else None
        if counted is not None and counted.group(2).lower() in COUNTERS:
            line = counted.group(1) + counted.group(3).upper() + line[counted.end() :]
        found.append(line)
    return found


# An email's greeting keeps its comma, also before a list ("Hi team,").
GREETING = re.compile(r"^(?:hi|hello|hey|dear)\b", re.IGNORECASE)


def introduced(lines: list[str]) -> list[str]:
    """The words before a list end with a colon, not a full stop or a comma ("Mom," Mom:), but for a greeting's (a
    list going on from the prior has none)."""
    found = list(lines)
    for index in range(1, len(found)):
        before = found[index - 1]
        starts = BULLET.match(found[index]) or found[index].startswith("1. ")
        stopped = before.endswith(".") and not before.endswith("..")
        commaed = before.endswith(",") and GREETING.match(before) is None
        if starts and (stopped or commaed) and not LIST_LINE.match(before):
            found[index - 1] = before[:-1] + ":"
    return found


SAID_BULLET = re.compile(r"\bbullet\b", re.IGNORECASE)


def items_as_said(said: str, lines: list[str]) -> list[str]:
    """Bulleted items as the speaker said them after each "bullet": an item the model split mid-way joins the one
    before ("bullet confirm pricing lock" - Confirm pricing, - Lock), and one said after "and" keeps it."""
    said_items = [f" {' '.join(words(piece))} " for piece in SAID_BULLET.split(said)[1:]]
    if len(said_items) < 2:
        return lines
    found: list[str] = []
    for line in lines:
        item = BULLET.match(line)
        before = BULLET.match(found[-1]) if found else None
        if item is None:
            found.append(line)
            continue
        rest = line[item.end() :]
        item_words = words(rest)
        lowered = rest[:1].lower() + rest[1:]
        if before is not None:
            run = " ".join(words(found[-1][before.end() :]) + item_words)
            if any(f" {run} " in said_item for said_item in said_items):
                found[-1] = f"{found[-1]} {lowered}"
                continue
        opening = " ".join(item_words[:2])
        if item_words and any(said_item.startswith(f" and {opening} ") for said_item in said_items):
            line = f"{line[: item.end()]}And {lowered}"
        found.append(line)
    return found


def leads_kept(said: str, lines: list[str]) -> list[str]:
    """A line the speaker ended with a colon ("Budget note:") keeps it where the model wrote a full stop."""
    leads = {tuple(words(line)) for line in said.split("\n") if line.rstrip().endswith(":")}
    if not leads:
        return lines
    found = list(lines)
    for index, line in enumerate(found):
        if line.endswith(".") and not line.endswith("..") and tuple(words(line)) in leads:
            found[index] = line[:-1] + ":"
    return found


def leads_run_on(said: str, lines: list[str]) -> list[str]:
    """A list's lead the model put on a line of its own runs on after the sentence before it, when the speaker said
    no line break ("joiners.", new line, "Here are a few things:" joiners. Here are a few things:)."""
    if "\n" in said:
        return lines
    found: list[str] = []
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        before = found[-1] if found else ""
        leads = line.endswith(":") and LIST_LINE.match(following) is not None and LIST_LINE.match(line) is None
        if leads and before.endswith((".", "!", "?")) and LIST_LINE.match(before) is None:
            found[-1] = f"{before} {line}"
            continue
        found.append(line)
    return found


def asked_back(said: str, text: str) -> str:
    """A line left with no end mark gets back the "?" the speaker said after its last words."""
    raw = said.split()
    heard = [words(token) for token in raw]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not ENDS_BARE.search(line):
            continue
        tail = words(line)[-2:]
        size = len(tail)
        for start in range(len(raw) - size + 1):
            run = [word for found in heard[start : start + size] for word in found]
            if run == tail and raw[start + size - 1].endswith("?"):
                lines[index] = line + "?"
                break
    asked = "\n".join(lines)
    return asked


def rejoined(text: str, output: str) -> str:
    """An answer to a piece going on with a sentence, made a new list item or without the piece's joining word, with
    the word back in lowercase going on with the line ("3. how is it" and how is it)."""
    unmarked = ITEM_MARK.sub("", output, count=1)
    joining = text.split(maxsplit=1)[0]
    first = unmarked.split(" ", 1)
    if first[0].lower().strip(".,?!") in NEVER_NAMES:
        first[0] = first[0].lower()
    if words(first[0])[:1] == words(joining)[:1]:
        first[0] = first[0].lower()
        return " ".join(first)
    rest = " ".join(first)
    return f"{joining.lower()} {rest}"


PARAGRAPH_SAID = tuple(words("new paragraph"))
CONTEXT_WORDS = 3
TOKEN = re.compile(r"\S+")
SENTENCE_GAP = re.compile(r"(?<=[.?!])[ \t]*\n?[ \t]*(?=\S)")


def paragraphed(said: str, text: str) -> str | None:
    """The answer with a blank line after the sentence each spoken "new paragraph" followed, where the model ran the
    two paragraphs on; None when a place can't be found."""
    raw = said.split()
    heard = [words(token) for token in raw]
    spans = [(found.start(), found.end(), words(found.group())) for found in TOKEN.finditer(text)]
    written = [word for _, _, found in spans for word in found]
    owner = [index for index, (_, _, found) in enumerate(spans) for _ in found]
    places: list[int] = []
    for start in range(1, len(raw)):
        said_here = tuple(word for found in heard[start : start + 2] for word in found)
        if said_here[: len(PARAGRAPH_SAID)] != PARAGRAPH_SAID:
            continue
        before = [word for found in heard[max(0, start - CONTEXT_WORDS) : start] for word in found][-CONTEXT_WORDS:]
        size = len(before)
        at = next(
            (end for end in range(size, len(written) + 1) if written[end - size : end] == before),
            None,
        )
        if at is None:
            return None
        gap = SENTENCE_GAP.search(text, spans[owner[at - 1]][0])
        if gap is None:
            return None
        places.append(gap.start())
    if not places:
        return None
    fixed = text
    for place in sorted(set(places), reverse=True):
        gap = SENTENCE_GAP.match(fixed, place)
        if gap is not None:
            fixed = fixed[: gap.start()] + "\n\n" + fixed[gap.end() :]
    return fixed


def breaks_kept(said: str, text: str) -> str:
    """The answer with a line break the text came with put back where the model ran the two lines on: after the
    written word the line's last said word is, or before the one the next line's first is."""
    if "\n" not in said:
        return text
    said_lines = said.split("\n")
    said_words = words(said)
    spans = list(TOKEN.finditer(text))
    owner = [index for index, span in enumerate(spans) for _ in words(span.group())]
    written = [word for span in spans for word in words(span.group())]
    same = {
        start + k: written_start + k
        for op, start, end, written_start, _ in align(said_words, written)
        if op == "equal"
        for k in range(end - start)
    }
    gaps: set[int] = set()
    boundary = 0
    for line in said_lines[:-1]:
        boundary += len(words(line))
        if boundary - 1 in same:
            at = same[boundary - 1] + 1
        elif boundary in same:
            at = same[boundary]
        else:
            continue
        if 0 < at < len(written) and owner[at - 1] != owner[at]:
            gaps.add(owner[at - 1])
    fixed = text
    for index in sorted(gaps, reverse=True):
        gap_start, gap_end = spans[index].end(), spans[index + 1].start()
        if "\n" not in fixed[gap_start:gap_end]:
            fixed = fixed[:gap_start] + "\n" + fixed[gap_end:]
    return fixed


BLANK = re.compile(r"\n\n+")
SPOKEN_BREAK = re.compile(r"\n|new paragraph", re.IGNORECASE)
SIGN_OFF_WORDS = 3


def unparagraphed(said: str, text: str) -> str:
    """The answer without a blank line the speaker never asked for: one sets off an email's greeting (its line ends
    with a comma) or its sign-off (a short line), and before a list item it is one line break."""
    if "\n\n" not in text or SPOKEN_BREAK.search(said):
        return text

    def gap(found: re.Match[str]) -> str:
        before = text[: found.start()].rsplit("\n", 1)[-1]
        after = text[found.end() :].split("\n", 1)[0]
        if LIST_LINE.match(after):
            return "\n"
        if before.endswith(",") or len(after.split()) <= SIGN_OFF_WORDS:
            return found.group()
        return " "

    run_on = BLANK.sub(gap, text)
    return run_on


# Two end marks stacked on one sentence ("Friday?."): the question or exclamation mark stays (round b3).
STACKED = re.compile(r"(?<=[^\s.?!])(?:\.+[?!]|[?!]\.+)")
# A question or exclamation closed by a quote takes no stop after the quote ("?”." ?”), round b4.
QUOTE_STOPPED = re.compile("(?<=[?!][\u201d\u2019\"'])\\.")
# The name under a sign-off ("Cheers,\nTom.") ends the letter with no full stop (round b3).
SIGNED_NAME = re.compile(
    r"(\n(?:thanks|thank you|best|cheers|regards|best regards|warm regards),"
    r"\n[^\n.]{1,30})\.$",
    re.IGNORECASE,
)


def unstacked(text: str) -> str:
    collapsed = STACKED.sub(
        lambda found: next((mark for mark in found.group() if mark in "?!"), found.group()[0]), text
    )
    unquoted = QUOTE_STOPPED.sub("", collapsed)
    signed = SIGNED_NAME.sub(r"\1", unquoted)
    return signed


# A count word before a unit of time, money or measure is written as digits (prompt rule 4: "four days" 4 days),
# which the model sometimes leaves as a word. "one" ("one day we will") is left alone, and so are rough pairs ("two
# three days") and a sentence's first word.
COUNT_DIGITS = {
    word: str(value) for value, word in enumerate("two three four five six seven eight nine ten".split(), start=2)
}
MEASURES = word_set(
    "seconds minutes hours days weeks months years minute hour day week month year dollars rupees euros percent "
    "kilos kilograms grams meters metres kilometers km kg litres liters kilo"
)
NOT_AFTER = {"one", *COUNT_DIGITS}
COUNT_BEFORE_MEASURE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(COUNT_DIGITS, key=len, reverse=True)) + r")(\s+)(\S+)", re.IGNORECASE
)
LAST_WORD = re.compile(r"(\w+)\W*$")


def measured_in_digits(text: str) -> str:
    """Each count word before a unit written as digits ("in two hours" in 2 hours)."""

    def digits(found: re.Match[str]) -> str:
        before = text[: found.start()].rstrip()
        previous = LAST_WORD.search(before)
        unit = found.group(3).rstrip(".,?!:;").lower()
        opens = not before or before[-1] in ".?!:\n"
        if unit not in MEASURES or opens or (previous and previous.group(1).lower() in NOT_AFTER):
            return found.group(0)
        return f"{COUNT_DIGITS[found.group(1).lower()]}{found.group(2)}{found.group(3)}"

    written = COUNT_BEFORE_MEASURE.sub(digits, text)
    return written


# "NAME at DOMAIN dot TLD" sent to someone is one address (prompt rule 4), which the model sometimes writes with "at"
# as a word ("to UX at lanternworks.io"); a web address after "at" ("the docs are at docs.example.org") is none.
SENT_TO_AT = re.compile(r"(?<=\bto )([A-Za-z][\w.+-]*) at ((?:[\w-]+\.)+[a-z]{2,})(?![\w/])", re.IGNORECASE)
# The name kept before its own address ("Noor at noor@silverline.school"), and an address's name capitalised where the
# speaker said it in lowercase ("UX@lanternworks.io").
NAMED_ADDRESS = re.compile(r"\b([A-Za-z][\w.+-]*) at (?=\1@)", re.IGNORECASE)
WRITTEN_NAME = re.compile(r"(?<![\w.+-])[A-Za-z][\w.+-]*(?=@(?:[\w-]+\.)+[a-z]{2,})")


def addressed(said: str, text: str) -> str:
    """Each "to NAME at DOMAIN" the speaker said as an address written as one, name@domain, and each address written
    with its name as the speaker said it."""
    if "@" not in text and SENT_TO_AT.search(text) is None:
        return text
    heard = f" {' '.join(words(said))} "

    def address(found: re.Match[str]) -> str:
        name, domain = found.group(1).lower(), found.group(2)
        spoken = f" {name} at {domain.split('.')[0].lower()} dot "
        return f"{name}@{domain}" if spoken in heard else found.group(0)

    joined = SENT_TO_AT.sub(address, text)

    def unnamed_once(found: re.Match[str]) -> str:
        name = found.group(1).lower()
        return found.group() if f" {name} at {name} at " in heard else ""

    unnamed = NAMED_ADDRESS.sub(unnamed_once, joined)
    spoken = f" {' '.join(said.split())} "
    lowered = WRITTEN_NAME.sub(
        lambda found: found.group().lower() if f" {found.group().lower()} at " in spoken else found.group(), unnamed
    )
    return lowered


# A size after a number is written in capitals ("2 mb" 2 MB).
SIZE_UNIT = re.compile(r"(?<=\d )(?:kb|mb|gb|tb)\b")
SPOKEN_COLON = re.compile(r"\bcolon\b", re.IGNORECASE)
SPOKEN_COMMA = re.compile(r"\bcomma\b", re.IGNORECASE)
MID_COLON = re.compile(r"(\w+): (\w[\w']*)([^\n]*)")
# The sentence after the colon is a question.
ASKS = re.compile(r"[^.?!]*\?")


def colons_as_said(said: str, text: str) -> str:
    """A colon inside a line as the speaker said it. After a spoken "colon" the next word keeps the lowercase it was
    said in ("said colon kick off" said: kick off), but for a question. When the speaker says their marks but no colon,
    the model's colon becomes the comma they said ("payments comma 1042" payments, 1042) or goes ("says show" says
    show). A capital stays on "I" and in a speech said all in lowercase, where the word could be a name."""
    spoken = SPOKEN_COLON.search(said) is not None
    if not spoken and (":" in said or SPOKEN_COMMA.search(said) is None):
        return text
    heard = f" {' '.join(words(said))} "
    said_words = {word.strip(".,?!:;") for word in said.split()}
    cased = said != said.lower()

    def as_said(found: re.Match[str]) -> str:
        before, after, rest = found.groups()
        word = after.lower() if cased and after != "I" and after not in said_words else after
        if spoken:
            asked = ASKS.match(rest) is not None
            return f"{before}: {word}{rest}" if not asked else found.group()
        if f" {before.lower()} comma " in heard:
            return f"{before}, {after}{rest}"
        if f" {before.lower()} {after.lower()} " in heard:
            return f"{before} {word}{rest}"
        return found.group()

    mended = MID_COLON.sub(as_said, text)
    return mended


def shaped(said: str, text: str) -> str:
    text = breaks_kept(said, addressed(said, text))
    text = SIZE_UNIT.sub(lambda found: found.group().upper(), measured_in_digits(text))
    paragraphs = unparagraphed(said, text)
    text = unstacked(paragraphs)
    lines = text.split("\n")
    if "\n" in text:
        lines = unglued(lines)
        lines = numbered(lines)
        lines = items_as_said(said, lines)
        lines = introduced(leads_kept(said, lines))
        lines = leads_run_on(said, lines)
    joined = "\n".join(lines)
    # A "?" said goes back on a line left bare.
    asked = unstacked(asked_back(said, joined)) if "?" in said and ENDS_BARE.search(joined) else joined
    colons = colons_as_said(said, asked)
    return colons


# Round b6: answers refused only for a mark or a list's shape, after every word passed, mended without a model.
SENTENCE_STOP = re.compile(r"\.(?=\s|$)")
ASKED_MARK = re.compile(r"\?(?=\s|$)")
WRITTEN_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def end_marks_swapped(text: str, marks: re.Pattern[str], mark: str) -> list[str]:
    """The text with one end mark swapped for `mark`, each in turn from the last."""
    spots = [found.start() for found in marks.finditer(text)]
    swapped = [text[:spot] + mark + text[spot + 1 :] for spot in reversed(spots)]
    return swapped


def asked_marked(text: str) -> list[str]:
    """A direct question left with a full stop: the text with one of them made "?", each in turn."""
    return end_marks_swapped(text, SENTENCE_STOP, "?")


def unasked_marked(text: str) -> list[str]:
    """A "?" on a sentence that asks nothing directly: the text with one "?" made a full stop, each in turn."""
    return end_marks_swapped(text, ASKED_MARK, ".")


def announced_list(text: str, announcement: str) -> str | None:
    """Announced statements written as prose (D5): the announcing sentence ends with a colon and each sentence after
    it is a bulleted item without its full stop. None when the announcement or a statement can't be found."""
    if "\n" in text:
        return None
    sentences = WRITTEN_SENTENCES.split(text.strip())
    wanted = words(announcement)
    at = next((i for i, sentence in enumerate(sentences) if words(sentence) == wanted), None)
    if at is None:
        return None
    items = sentences[at + 1 :]
    if len(items) < 2 or any(item[-1:] != "." for item in items):
        return None
    lead = " ".join(sentences[: at + 1])
    bulleted = "\n".join(f"- {item[:-1]}" for item in items)
    return f"{lead[:-1]}:\n{bulleted}"


def bullets_numbered(text: str, first: int) -> str:
    """A bulleted list counted off, numbered from `first`."""
    lines = text.split("\n")
    number = max(first, 1)
    for index, line in enumerate(lines):
        if BULLET.match(line):
            lines[index] = BULLET.sub(f"{number}. ", line)
            number += 1
    numbered_text = "\n".join(lines)
    return numbered_text


# Lists as the app the user types in renders them (founder, 12 Oct): round bullets in chat apps, a list's items as
# sentences in code editors and terminals. The guardrail judges the plain form ("- ", "1. "); styling comes after.
DASH_ITEM = re.compile(r"^- ", re.MULTILINE)
ROUND_ITEM = re.compile(r"^• ", re.MULTILINE)


def plain_bullets(text: str) -> str:
    """Round bullets the app typed before, read as the plain "- " the guardrail knows."""
    plain = ROUND_ITEM.sub("- ", text)
    return plain


def in_style(text: str, style: str) -> str:
    """An accepted answer's lists written for the app's style."""
    if style == "chat":
        rounded = DASH_ITEM.sub("• ", text)
        return rounded
    if style == "code":
        return unlisted(text)
    return text


def unlisted(text: str) -> str:
    """Each list item as a sentence, run on after its lead ("Notes:") or the item before it."""
    lines: list[str] = []
    listing = False
    for line in text.split("\n"):
        item = LIST_LINE.match(line)
        if item is None:
            lines.append(line)
            listing = line.endswith(":")
            continue
        sentence = ended(line[item.end() :].strip())
        if listing:
            lines[-1] = f"{lines[-1]} {sentence}"
        else:
            lines.append(sentence)
        listing = True
    unlisted_text = "\n".join(lines)
    return unlisted_text


def ended(sentence: str) -> str:
    """A sentence with its end mark: its own, or a full stop."""
    if sentence[-1:] in ".?!":
        return sentence
    return sentence + "."


# Both versions kept around a cue ("Friday, I mean, Monday"): the guardrail names the old one, the cue and the new one
# (founder, 13 Oct: mended without a correction call). Marks and spaces between them go with the old version.
BETWEEN = r"[\s,;:\u2013\u2014-]*"
EDGE_BEFORE = r"(?<!\w)"
EDGE_AFTER = r"(?!\w)"


def one_version(text: str, kept: tuple[str, ...]) -> str | None:
    """The answer with the old version and the cue taken out, the new version kept; None when they are not written
    as named. The new version takes the old one's capital at a sentence's start."""
    old, *cue, new = kept
    run = BETWEEN.join(re.escape(word) for word in [old, *cue])
    found = re.search(rf"{EDGE_BEFORE}{run}{BETWEEN}(?={re.escape(new)}{EDGE_AFTER})", text, re.IGNORECASE)
    if found is None:
        return None
    rest = text[found.end() :]
    if found.group()[:1].isupper():
        rest = rest[:1].upper() + rest[1:]
    mended = text[: found.start()] + rest
    return mended
