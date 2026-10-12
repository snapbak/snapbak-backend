"""Two edits made to an answer before it is judged: a word respelled to another accepted spelling of itself goes back
to the speaker's spelling (rule 6), and a joining word left dangling at the very end of the speech drops (founder,
11 Oct).
"""

import re
import unicodedata

from snapbak_backend.ai.guardrail.tokens import DEVANAGARI

# A nukta written or not, and a chandrabindu for an anusvara, spell one Devanagari word ("कागज़" "कागज", "जाँच"
# "जांच"); so do "ये" and "ए", "यी" and "ई" inside a word ("जायेगा" "जाएगा", "गयी" "गई"). The nukta of "ड़" and "ढ़"
# makes another letter, and "ॉ" another word ("कॉल" "काल"), so they stay.
NUKTA = re.compile("(?<![डढ])़")
Y_VOWELS = re.compile("(?<=[ऀ-ॿ])य([ेी])")
Y_FREE = {"े": "ए", "ी": "ई"}
# Roman Hindi has no one spelling: each group spells one word ("rha" "raha"), as "ok" and "okay" do in English. A
# group never mixes a gender or a number ("gaya" and "gayi" are two words).
SPELLINGS = [
    "ok okay",
    "h hai",
    "rha raha",
    "rhi rahi",
    "rhe rahe",
    "kr kar",
    "kro karo",
    "krna karna",
    "krke karke",
    "nhi nahi nahin",
    "gya gaya",
    "gyi gayi gai",
    "gye gaye",
    "me mein",
    "hu hoon hun",
    "ham hum",
    "fir phir",
    "acha achha accha achchha",
    "thik theek",
    "bahut bohot bahot bohat",
    "hisab hisaab",
    "sham shaam",
    "wo woh vo voh",
    "ye yeh",
    "kyunki kyuki kyonki kyoki",
    "zaruri jaruri zaroori",
    "zarurat jarurat zaroorat",
    "yaha yahan yahaan",
    "waha wahan wahaan",
    # Round b3: a "j" for a "z" sound is how many people type Roman Hindi ("bujurg" buzurg).
    "zyada jyada jiyada",
    "zaroor jaroor zarur jarur",
    "buzurg bujurg",
    "zindagi jindagi",
    "zameen jameen",
    "zabardast jabardast",
    "khazana khajana",
    "intezaar intejaar intezar intejar",
    "intezaam intejaam intezam intejam",
    # Round b6: long vowels typed doubled or not ("pucha" poocha, "dupahar" dopahar, "lani" laani).
    "pucha poocha puchha",
    "puchi poochi",
    "puche pooche",
    "pucho poocho",
    "dupahar dopahar",
    "lani laani",
    "lana laana",
]
SPELLING_KEY = {spelling: group.split()[0] for group in SPELLINGS for spelling in group.split()}
WORD = re.compile(r"[A-Za-z]+|[ऀ-ॣ०-ॿ]+")


def devanagari_key(word: str) -> str:
    composed = unicodedata.normalize("NFC", word)
    bare = NUKTA.sub("", composed).replace("ँ", "ं")
    keyed = Y_VOWELS.sub(lambda found: Y_FREE[found.group(1)], bare)
    return keyed


def key(word: str) -> str | None:
    if DEVANAGARI.search(word):
        return devanagari_key(word)
    found = SPELLING_KEY.get(word.lower())
    return found


def as_heard(said: str, text: str) -> str:
    """The answer with each word that only respells a word said, and was never said itself, spelled as said; a
    capital the answer gave it stays."""
    heard = WORD.findall(said)
    spelled = set(heard)
    by_key: dict[str, str] = {}
    for word in heard:
        found = key(word)
        if found is not None:
            by_key.setdefault(found, word.lower() if word.isascii() else word)

    def back(found: re.Match[str]) -> str:
        written = found.group()
        if written in spelled or written.lower() in spelled:
            return written
        keyed = key(written)
        said_word = by_key.get(keyed) if keyed is not None else None
        if said_word is None or said_word == written.lower():
            return written
        return said_word[0].upper() + said_word[1:] if written[0].isupper() else said_word

    respelled = WORD.sub(back, text)
    return respelled


# Words that join on to what comes next: said last with nothing after them, they join nothing. "so", "then", "phir"
# and "तो" also end a sentence ("I told him so", "kal milte hain phir"), so they dangle only after a full stop.
DANGLING = {"and", "but", "or", "aur", "lekin", "और", "लेकिन"}
DANGLING_AFTER_STOP = {"so", "then", "toh", "to", "phir", "तो", "फिर"}
LAST_WORD = re.compile(r"[\s,]*([^\s,.?!।]+)[.?!।]?\s*$")
STOPPED = re.compile(r"[.?!।]\s*$")
ITEM_LINE = re.compile(r"^\s*(?:\d+\.|[-•*])\s")


def undangled(said: str, text: str) -> str:
    """The answer without the joining word the speech ended on, and the sentence it hung on ended."""
    said_last = LAST_WORD.search(said)
    if said_last is None:
        return text
    word = said_last.group(1).lower()
    stopped = STOPPED.search(said[: said_last.start()]) is not None
    if word not in DANGLING and not (word in DANGLING_AFTER_STOP and stopped):
        return text
    found = LAST_WORD.search(text)
    if found is None or found.group(1).lower() != said_last.group(1).lower() or found.start() == 0:
        return text
    kept = text[: found.start()].rstrip(" ,\n")
    last_line = kept.rsplit("\n", 1)[-1]
    if not kept or kept[-1] in ".?!।:" or ITEM_LINE.match(last_line):
        return kept
    stop = "।" if DEVANAGARI.search(last_line) else "."
    return kept + stop
