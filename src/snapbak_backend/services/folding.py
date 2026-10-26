"""How a quote is checked against the line it cites (MEETINGS.md 7.2): both texts folded the same way, so a quote
matches whatever case, punctuation or Devanagari spelling variant it was written in. Shared by the minutes'
validator, the detail guard (services/details.py), Ask and the share page."""

import unicodedata

# The Devanagari block, and the marks that are spelling variants of one word rather than another word.
DEVANAGARI = ("\u0900", "\u097f")
NUKTA = "\u093c"
CHANDRABINDUS = ("\u0900", "\u0901")
ANUSVARA = "\u0902"


def folded(char: str) -> str:
    """One character of a decomposed, casefolded text as the quote check reads it: a letter or digit as it is, a
    Devanagari vowel sign or virama as it is (कम is not काम), the nukta dropped and a chandrabindu read as an
    anusvara (a spelling choice, not another word), any other script's combining mark dropped, the rest a space."""
    if char == NUKTA:
        return ""
    if char in CHANDRABINDUS:
        return ANUSVARA
    if unicodedata.category(char).startswith("M"):
        return char if DEVANAGARI[0] <= char <= DEVANAGARI[1] else ""
    return char if char.isalnum() else " "


def skeleton(text: str) -> str:
    """The text's words in any script as the quote check compares them (MEETINGS.md 7.2): NFKC (which splits the
    precomposed nukta letters), casefolded, every character `folded`, the spaces collapsed."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text).casefold())
    spaced = "".join(folded(char) for char in decomposed)
    return " ".join(spaced.split())


def quoted(quote: str, line_text: str) -> bool:
    """The quote is whole words of the line, as their skeletons; a quote with no words is never a quote."""
    frame = skeleton(quote)
    if not frame:
        return False
    return f" {frame} " in f" {skeleton(line_text)} "
