"""Two edits made to an answer before it is judged: a word respelled to another accepted spelling of itself goes back
to the speaker's spelling (rule 6), and a joining word left dangling at the very end of the speech drops (founder,
11 Oct), and so does a lone subject the speech ended on (14 Oct).
"""

import re

from snapbak_backend.ai.guardrail.grammar import SUBJECT_CASE
from snapbak_backend.ai.guardrail.shape import WRITTEN_SENTENCES

# A word with more than one accepted spelling, each read as its first ("ok" and "okay").
SPELLING_KEY = {"ok": "ok", "okay": "ok"}
WORD = re.compile(r"[A-Za-z]+")


def as_heard(said: str, text: str) -> str:
    """The answer with each word that only respells a word said, and was never said itself, spelled as said; a
    capital the answer gave it stays."""
    heard = WORD.findall(said)
    spelled = set(heard)
    by_key: dict[str, str] = {}
    for word in heard:
        found = SPELLING_KEY.get(word.lower())
        if found is not None:
            by_key.setdefault(found, word.lower())

    def back(found: re.Match[str]) -> str:
        written = found.group()
        if written in spelled or written.lower() in spelled:
            return written
        keyed = SPELLING_KEY.get(written.lower())
        said_word = by_key.get(keyed) if keyed is not None else None
        if said_word is None or said_word == written.lower():
            return written
        return said_word[0].upper() + said_word[1:] if written[0].isupper() else said_word

    respelled = WORD.sub(back, text)
    return respelled


# Words that join on to what comes next: said last with nothing after them, they join nothing. "so" and "then" also
# end a sentence ("I told him so"), so they dangle only after a full stop.
DANGLING = {"and", "but", "or"}
DANGLING_AFTER_STOP = {"so", "then"}
# A subject said alone after a finished sentence starts nothing: the speaker stopped, or the recognizer heard it in
# the silence at the end ("...on weekends? You"). It stays where it answers "who" or is asked ("You?").
LONE_SUBJECTS = set("i you we they he she it".split())
# A subject with no object form (SUBJECT_CASE) ends the speech mid-clause only when the speaker stopped ("...a personal
# project I"), unless the word before it ends a sentence on one ("than I", "you and I", "so do I", "aren't we").
ENDS_ON_SUBJECT = set(
    "than as and or nor not so neither but am is are was were do does did can could will would should shall may "
    "might must have has had aren't isn't wasn't weren't don't doesn't didn't can't couldn't won't wouldn't "
    "shouldn't haven't hasn't hadn't".split()
)
ASKS_WHO = re.compile(r"\b(?:who|whom)\b", re.IGNORECASE)
LAST_WORD = re.compile(r"[\s,]*([^\s,.?!]+)[.?!]?\s*$")
STOPPED = re.compile(r"[.?!]\s*$")
ITEM_LINE = re.compile(r"^\s*(?:\d+\.|[-•*])\s")


def dangling(said: str) -> re.Match[str] | None:
    """The joining word or lone subject the speech ended on, which joins on to nothing."""
    said_last = LAST_WORD.search(said)
    if said_last is None:
        return None
    word = said_last.group(1).lower()
    before = said[: said_last.start()]
    stopped = STOPPED.search(before) is not None
    if word in DANGLING or (word in DANGLING_AFTER_STOP and stopped):
        return said_last
    if word not in LONE_SUBJECTS or said.rstrip()[-1:] in "?!":
        return None
    said_before = before.split()
    previous = said_before[-1].lower() if said_before else ""
    if not stopped and (word not in SUBJECT_CASE or previous in ENDS_ON_SUBJECT or not previous):
        return None
    last_sentence = WRITTEN_SENTENCES.split(before.rstrip())[-1]
    if ASKS_WHO.search(last_sentence) is not None:
        return None
    return said_last


def echoed(said_last: re.Match[str], text: str) -> re.Match[str] | None:
    """The answer's last word, when it is the dangling word the speech ended on."""
    found = LAST_WORD.search(text)
    if found is None or found.group(1).lower() != said_last.group(1).lower():
        return None
    return found


def spoken(said: str, text: str) -> str:
    """What was said, less the word it ended on that joins nothing, when the answer `text` left that word out."""
    said_last = dangling(said)
    if said_last is None or said_last.start() == 0 or echoed(said_last, text) is not None:
        return said
    kept = said[: said_last.start()]
    return kept


def undangled(said: str, text: str) -> str:
    """The answer without the joining word or lone subject the speech ended on, and the sentence before it ended."""
    said_last = dangling(said)
    found = None if said_last is None else echoed(said_last, text)
    if found is None or found.start() == 0:
        return text
    kept = text[: found.start()].rstrip(" ,\n")
    last_line = kept.rsplit("\n", 1)[-1]
    if not kept or kept[-1] in ".?!:" or ITEM_LINE.match(last_line):
        return kept
    return kept + "."
