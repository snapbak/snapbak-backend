"""Grammar: the small words and word forms a fix may change, and the tense, qualifiers and gender it may not."""

import re
from collections import Counter
from itertools import pairwise

# Python 3.14 calls a method of a from-imported name without its method fast path, so the shared patterns are
# reached through their module.
from snapbak_backend.ai.guardrail import tokens
from snapbak_backend.ai.guardrail.numbers import is_number
from snapbak_backend.ai.guardrail.tokens import Edit, word_set, words

LONGEST_JOIN = 6
PIECE = re.compile(r"(\d+)(?:st|nd|rd|th|वां|वीं|वें)?|[^\d_-]+")


# Small grammar words that tell a language apart.
ENGLISH_GRAMMAR = word_set(
    "a an the is are was were be been am has have had do does did to of and or for in on at with that this it its i "
    "isn't aren't wasn't weren't hasn't haven't hadn't don't doesn't didn't"
)
ROMAN_HINDI_GRAMMAR = word_set("hai hain hoon hun tha thi raha rahi rahe ka ki ke ko se ne mein")
DEVANAGARI_GRAMMAR = word_set("है हैं हूं था थी थे थीं रहा रही रहे का की के को से ने में पर")
FUNCTION_WORDS = ENGLISH_GRAMMAR | ROMAN_HINDI_GRAMMAR | DEVANAGARI_GRAMMAR


# The grammar words a fix may add unsaid, or swap for another of its group ("has" "have", "ka" "ki"). A group
# keeps its tense, so "is" "was" is a change, as is adding any other grammar word ("and", "or", "in").
FIXES = [
    word_set(group)
    for group in (
        "a an the",
        "is are am isn't aren't",
        "was were wasn't weren't",
        "be been",
        # "I am waiting since morning" is "I have been waiting since morning" in Indian English.
        "has have hasn't haven't is are am",
        "had hadn't",
        "do does don't doesn't",
        "did didn't",
        "to",
        "hai hain hoon hun",
        "tha thi the",
        "raha rahi rahe",
        "ka ki ke",
        "है हैं हूं",
        "था थी थे थीं",
        "रहा रही रहे",
        "का की के",
        # Hindi postpositions, which a grammar fix swaps ("ग्राहक को बात करके" से, "खाते पर जमा" में).
        "को से में पर",
    )
]
INSERTABLE = set().union(*FIXES)


FORMAT_WORDS = word_set(
    "at dot slash comma period full stop question mark exclamation point colon semicolon dash hyphen underscore "
    "new line paragraph bullet बुलेट next dollar dollars rupee rupees rupaye percent cents and एट डॉट स्लैश डॉलर "
    "रुपये रुपए प्रतिशत"
)


# Words whose loss changes what was meant: how much, how sure, when ("only", "maybe", "today", "sirf", "शायद").
QUALIFIERS = word_set(
    "all almost always approximately both could definitely each every just likely may maybe might mostly must nearly "
    "often only perhaps possibly probably rarely roughly seldom should sometimes today tomorrow tonight twice usually "
    "would yesterday aaj aksar bas hamesha kal lagbhag parso shayad sirf zaroor अक्सर आज कल परसों बस लगभग शायद सिर्फ "
    "हमेशा ज़रूर still already yet even again exactly abhi bhi अभी भी whether very really extremely bahut बहुत"
)


# Past forms: auxiliaries, and irregular pasts and participles. After a modal, "to", "do", "be" or "have" a past
# form is a slip for the base form ("will sent", "did went") or a participle ("can be informed"), and after an
# article or "most" an adjective ("the signed contract", "most qualified"): no past either way.
PAST = word_set(
    "was were wasn't weren't had hadn't did didn't tha thi था थी थे थीं done went gone saw seen "
    "got gotten made paid sent took taken gave given came wrote written bought brought thought told said found knew "
    "known left met ran began begun broke broken chose chosen spoke spoken drove driven forgot forgotten kept felt "
    "held built spent lost sold taught caught fell fallen grew grown threw thrown led meant heard understood won ate "
    "eaten drank drunk slept"
)
HAVE = word_set("has have had")
NO_TENSE_AFTER = word_set(
    "will shall can could would should must may might to do does did didn't don't doesn't be been being is are am "
    "was were has have had a an the most more very can't cannot couldn't won't wouldn't shouldn't mustn't"
)
# Words between a modal and its verb that keep the verb's slip a slip ("did not went", "can never came").
BETWEEN = word_set("not never just even really also ever still")
# Real verbs and words ending in "eed", which is no past ending ("need", "proceed"); any other is ("agreed",
# "wasteed" for wasted).
EED_WORDS = word_set(
    "need feed seed heed bleed breed speed weed deed reed steed greed creed tweed proceed succeed exceed indeed"
)


# Words that say the time is past, so a fix may put a verb in the past ("i stop it yesterday" "stopped"). Not "kal"
# or "कल", which is tomorrow as often as yesterday.
# Two words that put a story earlier today ("this morning our contractor forgets the permit" forgot).
PAST_PAIRS = {("this", "morning"), ("this", "afternoon")}
PAST_TIMES = word_set("yesterday ago last already earlier pichle pehle ne maine usne humne पिछले पहले ने मैंने उसने हमने")
# A date: a day of a month may be past or to come, so either tense fits it ("on april fifth liam save" saved).
MONTHS = word_set("january february march april may june july august september october november december")
# Words that say the time is to come, so a fix may add "will" or put a verb in the future ("next week we finished"
# will finish, "agle mahine shift kiya" karenge). Not "kal" or "कल", which is yesterday as often as tomorrow.
FUTURE_TIMES = word_set("next tomorrow tonight upcoming agle agli aane parso अगले अगली आने परसों")
# A Hindi verb's forms by tense: what a past time may make it, and what a future time may.
VERB_TENSES = [
    (words(present), words(past), words(future))
    for present, past, future in (
        ("jaa jana jaana jata jaata jati jaati jate jaate", "gaya gayi gaye gai", "jayega jayegi jayenge jaunga"),
        ("जा जाना जाता जाती जाते", "गया गई गए गयी", "जाएगा जाएगी जाएंगे जाऊंगा"),
        ("hona hota hoti hote", "hua hui hue", "hoga hogi honge"),
        ("होना होता होती होते", "हुआ हुई हुए", "होगा होगी होंगे"),
        ("karna karta karti karte", "kiya kiye ki", "karega karegi karenge karunga karungi"),
        ("करना करता करती करते", "किया किए की", "करेगा करेगी करेंगे करूंगा करूंगी"),
        ("dena deta deti dete", "diya diye di", "dega degi denge dunga"),
        ("देना देता देती देते", "दिया दिए दी", "देगा देगी देंगे दूंगा"),
        ("lena leta leti lete", "liya liye li", "lega legi lenge lunga"),
        ("लेना लेता लेती लेते", "लिया लिए ली", "लेगा लेगी लेंगे लूंगा"),
        ("aana aata aati aate", "aaya aayi aaye", "aayega aayegi aayenge aaunga"),
        ("आना आता आती आते", "आया आई आए", "आएगा आएगी आएंगे आऊंगा"),
    )
]
PAST_OF = {word: set(past) for present, past, future in VERB_TENSES for word in (*present, *future)}
FUTURE_OF = {word: {"will", *future} for present, past, future in VERB_TENSES for word in (*present, *past)}
PAST_AUXILIARIES = word_set("tha thi the था थी थे थीं")
# Words a careful editor puts for another in Indian English and common usage ("from morning" since, "less people"
# fewer, "between you and i" me): written for the other, each is said.
PARTNERS = {
    word: set(group) - {word}
    for group in map(word_set, ("from since", "since for", "less fewer", "borrow lend", "i me", "who whom"))
    for word in group
}


# A first-person subject with no ergative "ne": the verb's gender is the speaker's. A fix may make a verb agree
# with an auxiliary of the gender said ("बनाता थी" "बनाती थी") or put "hum" in the plural ("bhejti" "bhejte").
FIRST_PERSON = word_set("main mai hum मैं हम")
ERGATIVE = word_set("ne maine humne usne ने मैंने हमने उसने")
GENDER_SAID = {"f": word_set("thi thin rahi gayi gai थी थीं रही गई गयी"), "m": word_set("tha raha gaya था रहा गया")}


# Irregular forms a grammar fix may swap; regular ones ("send" "sends") pass as inflections.
IRREGULAR = [
    word_set(group)
    for group in (
        "can can't cannot",
        "do does did done",
        "go goes went gone",
        "see sees saw seen",
        "get gets got gotten",
        "make makes made",
        "pay pays paid",
        "send sends sent",
        "take takes took taken",
        "give gives gave given",
        # Words that sound alike, which speech to text swaps.
        "your you're",
        "there their they're",
        "then than",
        "whose who's",
        "its it's",
        "come comes came",
        "write writes wrote written",
        "buy buys bought",
        "bring brings brought",
        "think thinks thought",
        "tell tells told",
        "say says said",
        "find finds found",
        "know knows knew known",
        "leave leaves left",
        "meet meets met",
        "run runs ran",
        "begin begins began begun",
        "break breaks broke broken",
        "choose chooses chose chosen",
        "speak speaks spoke spoken",
        "drive drives drove driven",
        "forget forgets forgot forgotten",
        "keep keeps kept",
        "feel feels felt",
        "hold holds held",
        "build builds built",
        "spend spends spent",
        "lose loses lost",
        "sell sells sold",
        "teach teaches taught",
        "catch catches caught",
        "fall falls fell fallen",
        "grow grows grew grown",
        "throw throws threw thrown",
        "lead leads led",
        "mean means meant",
        "hear hears heard",
        "understand understands understood",
        "win wins won",
        "child children",
        "person people",
        "man men",
        "woman women",
        "mouse mice",
        "eat eats ate eaten",
        "drink drinks drank drunk",
        "sleep sleeps slept",
        "hua hui hue",
        # Short Hinglish verbs agree with their object or subject ("list bana diya" di, "booking kar liya" li).
        "liya li liye",
        "diya di diye",
        "gaya gayi gai gaye",
        "laga lagi lage",
        "aaya aayi aai aaye",
        "karna karta karti karte kiya kiye",
        "गया गई गईं गए गयी",
        "आया आई आए आईं आयी",
        "करना करता करती करते किया की किए",
        "देना देता देती देते दिया दी दिए",
        "लेना लेता लेती लेते लिया ली लीं लिए",
        "हुआ हुई हुए",
        "होगा होगी होंगे",
        "जाएगा जाएगी जाएंगे जाऊंगा",
        "aaunga aayega aayegi aayenge aaoge",
        "karunga karega karegi karenge karoge",
        "dunga dega degi denge doge",
        "lunga lega legi lenge loge",
        "आऊंगा आएगा आएगी आएंगे आओगे",
        "करूंगा करेगा करेगी करेंगे करोगे",
        "दूंगा देगा देगी देंगे दोगे",
        "लूंगा लेगा लेगी लेंगे लोगे",
    )
]
# Each irregular form and the other forms of its word.
FORMS = {word: set().union(*(group for group in IRREGULAR if word in group)) for forms in IRREGULAR for word in forms}
# Endings of a word's forms past their shared stem: English and Roman Hindi.
ENDINGS = set(
    "|s|'s|es|ed|d|ing|er|est|ies|ied|y|a|i|e|o|ii|ee|en|na|ni|ne|ta|ti|te|ga|gi|ge|nga|ngi|nge|yan|yaan|iyan|iyaan".split(
        "|"
    )
)
# Roman Hindi future endings, which only swap with each other ("bhejega" "bhejunga", never "bheja").
FUTURE = set("ega egi enge unga ungi oge".split())
# Devanagari endings are vowel signs and nasal marks, maybe after an independent vowel or य and around त, न or
# ग ("भेजता" "भेजा", "बनाया" "बनाई", "आई" "आईं", "पहुंचती" "पहुंचेगी").
DEVANAGARI_ENDING = re.compile(r"[इ-ऐय]?[ँ-ःा-ौ]*(?:[तनगय][ँ-ःा-ौ]*)?")


def grammar_words(said: list[str]) -> set[str]:
    """The small grammar words of each language spoken: Devanagari by its script, Roman Hindi by one of its own
    grammar words, and English by one of its own or by Latin words with no Roman Hindi grammar word."""
    spoken = set(said)
    hindi = bool(spoken & ROMAN_HINDI_GRAMMAR)
    latin = any(not tokens.DEVANAGARI.search(word) for word in said)
    grammar = set()
    if spoken & ENGLISH_GRAMMAR or (latin and not hindi):
        grammar |= ENGLISH_GRAMMAR
    if hindi:
        grammar |= ROMAN_HINDI_GRAMMAR | {"the"}
    if tokens.DEVANAGARI.search(" ".join(said)):
        grammar |= DEVANAGARI_GRAMMAR
    return grammar


def joins(said: list[str]) -> set[str]:
    """Runs of said words written as one ("note id" noteid, "c o r p" corp)."""
    return {
        "".join(said[i:end]) for i in range(len(said)) for end in range(i + 2, min(i + LONGEST_JOIN, len(said)) + 1)
    }


def ending(tail: str, stem: str) -> bool:
    """An inflection ending, maybe after a doubled last letter ("stopped")."""
    doubled = tail[:1] == stem[-1:] and tail[1:] in ENDINGS
    return tail in ENDINGS or doubled or DEVANAGARI_ENDING.fullmatch(tail) is not None


def inflects(said: str, word: str) -> bool:
    shared = 0
    for a, b in zip(said, word, strict=False):
        if a != b:
            break
        shared += 1
    stem = said[:shared]
    tails = said[shared:], word[shared:]
    future = all(tail in FUTURE for tail in tails)
    shortest = 2 if tokens.DEVANAGARI.search(stem) else 3
    return shared >= shortest and (future or all(ending(tail, stem) for tail in tails))


def related(said: str, word: str) -> bool:
    """Another form of the same word: an inflection or an irregular form, also of a regularized slip ("buyed")."""
    stems = {said, said.removesuffix("ed"), said.removesuffix("d"), said.removesuffix("s")}
    irregular = bool(stems & FORMS.get(word, set()))
    return irregular or inflects(said, word)


def explained(word: str, known: set[str], heard: set[str]) -> bool:
    """A written word the user said: as is, without its apostrophe or hyphen, or piece by piece ("v1", "7B", "4th")."""
    if word in known or word.replace("'", "") in known or word.replace("-", "") in known:
        return True
    for found in PIECE.finditer(word):
        digits = found.group(1)
        piece = str(int(digits)) if digits else found.group()
        if piece not in known and not any(related(said, piece) for said in heard):
            return False
    return True


# Each word's usage partners and other tenses, together.
RETIMED = {
    word: PARTNERS.get(word, set()) | PAST_OF.get(word, set()) | FUTURE_OF.get(word, set())
    for word in {*PARTNERS, *PAST_OF, *FUTURE_OF}
}


def timed_forms(said: list[str]) -> set[str]:
    """Words a fix may write that the user never said: a usage partner of a said word ("since" for "from"), and a
    past or future form of a said Hindi verb when a time word says when ("पिछले साल ... जाते" गए, "agle mahine ...
    kiya" karenge, "next week" will)."""
    found = {partner for word in said for partner in PARTNERS.get(word, ())}
    spoken = set(said)
    if spoken & PAST_TIMES:
        found |= {form for word in said for form in PAST_OF.get(word, ())}
    if spoken & FUTURE_TIMES:
        found |= {form for word in said for form in FUTURE_OF.get(word, ())} | {"will"}
    return found


def form_fix(word: str, written: list[str]) -> bool:
    """A fix of `word` in place: another form of it, a grammar word for another of its group ("has" "had"), the
    word joined into a written one ("no fault" no-fault), its usage partner or its other tense ("हुई थी" होगी)."""
    swapped = any(word in group and any(other in group for other in written) for group in FIXES)
    joined = any(word in other and other not in FUNCTION_WORDS for other in written)
    retimed = word in RETIMED and not RETIMED[word].isdisjoint(written)
    retimed = retimed or (word in PAST_AUXILIARIES and futures(written) > 0)
    return swapped or joined or retimed or any(related(word, other) for other in written)


def regular_past(word: str) -> bool:
    return word.endswith("ed") and word not in EED_WORDS and len(word) > 3 and not is_number(word)


def past_form(word: str) -> bool:
    return word in PAST or regular_past(word)


def pasts(found: list[str], places: range | list[int], hinglish: bool) -> list[str]:
    """The words at `places` that put the sentence in the past; Hinglish "the" is the plural of "tha"."""
    past: list[str] = []
    for index in places:
        word = found[index]
        before_at = index - 1
        while before_at > 0 and found[before_at] in BETWEEN:
            before_at -= 1
        before = found[before_at] if before_at >= 0 else ""
        slip = before in NO_TENSE_AFTER or before.endswith("'ll")
        if not slip and (word in PAST or regular_past(word) or (hinglish and word == "the")):
            past.append(word)
    return past


def perfects(found: list[str], places: range | list[int]) -> list[str]:
    """The participles at `places` right after "has", "have" or "had": an action done ("have sent")."""
    done = [found[index] for index in places if index and found[index - 1] in HAVE and past_form(found[index])]
    return done


def retensed(
    said: list[str],
    wrote: list[str],
    opcodes: list[Edit],
    retracted: list[str],
    misheard: tuple[set[int], set[int]] = (set(), set()),
) -> bool:
    """The changed words moved out of the past or a done action ("we shipped it" "We ship it", "i have sent it" "I
    send it"), or into the past though nothing said was past ("he is coming" "He was coming"). A misheard word and
    its sound-alike ("threw" through, "new" knew) carry no tense."""
    heard_wrong, written_for = misheard
    hinglish = bool(set(said) & ROMAN_HINDI_GRAMMAR)
    removed: list[str] = []
    added: list[str] = []
    removed_done: list[str] = []
    added_done: list[str] = []
    done = not HAVE.isdisjoint(said) or not HAVE.isdisjoint(wrote)
    for op, start, end, written_start, written_end in opcodes:
        if op != "equal":
            # A past said apart and written joined stays ("under paid" underpaid, "wait listed" waitlisted).
            joined = wrote[written_start:written_end]
            kept = {i for i in range(start, end) if any(word != said[i] and word.endswith(said[i]) for word in joined)}
            removed += pasts(said, [i for i in range(start, end) if i not in heard_wrong | kept], hinglish)
            added += pasts(wrote, [j for j in range(written_start, written_end) if j not in written_for], hinglish)
        if op != "equal" and done:
            removed_done += perfects(said, [i for i in range(start, end) if i not in heard_wrong])
            added_done += perfects(wrote, [j for j in range(written_start, written_end) if j not in written_for])
    # A verb put in the future to match a time word said ("next week we finished" will finish).
    if not FUTURE_TIMES.isdisjoint(said) and futures(wrote) > futures(said):
        return False
    if len(added) > len(removed):
        earlier_today = any(pair in PAST_PAIRS for pair in pairwise(said))
        dated = not MONTHS.isdisjoint(said) and any(is_number(word) for word in said)
        said_past = pasts(said, range(len(said)), hinglish) or not PAST_TIMES.isdisjoint(said) or earlier_today
        return not (said_past or dated or perfective(said))
    kept_removed = Counter(removed + removed_done) - Counter(retracted)
    return kept_removed.total() > len(added) + len(added_done)


# A Hindi perfective participle said with "hoon" or "hai" ("soya hoon", "गया हूं"): an action done, which a fix may
# put in the past ("soya tha").
PERFECTIVE = re.compile(r"^(?:[a-z]{2,}(?:ya|yi|ye)|[ऀ-ॿ]+(?:या|यी|ये|ई|ए))$")
PRESENT_AUXILIARIES = word_set("hoon hun hai hain हूं है हैं")


def perfective(said: list[str]) -> bool:
    return any(PERFECTIVE.match(word) and after in PRESENT_AUXILIARIES for word, after in pairwise(said))


# Qualifiers a recognizer splits in two, and each qualifier's forms owning a thing ("today's", "todays").
SPLIT_QUALIFIERS = word_set("maybe tonight today tomorrow again")
# The second words of those split ("may be", "to night", "a gain").
SPLIT_ENDS = word_set("be night day morrow gain")
QUALIFIER_FORMS = {form: word for word in QUALIFIERS for form in (word, f"{word}s", f"{word}'s")}


def qualifiers(found: list[str]) -> Counter[str]:
    """The qualifiers, two words said for one read as it ("may be" maybe, "to night" tonight), and one owning a thing
    as itself ("todays" today's today)."""
    joined = found
    if not SPLIT_ENDS.isdisjoint(found):
        joined = []
        index = 0
        while index < len(found):
            pair = "".join(found[index : index + 2])
            whole = index + 1 < len(found) and pair in SPLIT_QUALIFIERS
            joined.append(pair if whole else found[index])
            index += 2 if whole else 1
    # A qualifier inside a hyphenated word is still said ("all hands" all-hands).
    parts = [part for word in joined for part in word.split("-")] if "-" in "".join(joined) else joined
    counted = Counter(QUALIFIER_FORMS[word] for word in parts if word in QUALIFIER_FORMS)
    return counted


def gender(word: str) -> str | None:
    """A Hindi verb form's gender by its last vowel ("raha" "rahi", "रहा" "रही"), "e" and "े" being plural."""
    last = word.removesuffix("ं")[-1:]
    if last in ("a", "e", "ा", "े", "ए"):
        return "m"
    return "f" if last in ("i", "ी", "ई") else None


# Words that start a new clause, whose verb has its own subject ("gaadi kharab ho gayi isliye main late hua").
CLAUSE_BREAKS = word_set("toh to isliye kyunki lekin magar aur तो इसलिए क्योंकि लेकिन मगर और")
# A possessive tells its noun's gender ("meri gaadi"), which the verb of its clause agrees with.
POSSESSIVE_GENDER = {
    **dict.fromkeys(
        words("meri teri uski hamari tumhari apni aapki unki iski मेरी तेरी उसकी हमारी तुम्हारी अपनी आपकी उनकी इसकी"), "f"
    ),
    **dict.fromkeys(
        words("mera tera uska hamara tumhara apna aapka unka iska मेरा तेरा उसका हमारा तुम्हारा अपना आपका उनका इसका"), "m"
    ),
}


POSSESSIVES = frozenset(POSSESSIVE_GENDER)


def clauses(said: list[str]) -> list[int]:
    """Each said word's clause, counted from 0: a clause word ("toh", "isliye", "और") starts the next."""
    found: list[int] = []
    clause = 0
    for word in said:
        clause += word in CLAUSE_BREAKS
        found.append(clause)
    return found


def misgenders(said: list[str], wrote: list[str], opcodes: list[Edit]) -> str | None:
    """A verb whose gender the output flipped against its subject, as written: a first-person verb ("main aaunga"
    "main aaungi") in a clause whose subject is "main" or "hum", or one that agreed with its noun's possessive
    ("meri gaadi kharab ho gayi" gaya). A verb in a clause of its own may agree with its own noun ("meri gaadi ...
    gaya isliye main" gayi)."""
    if FIRST_PERSON.isdisjoint(said) and POSSESSIVES.isdisjoint(said):
        return None
    clause_of = clauses(said)
    ergative = not ERGATIVE.isdisjoint(said)
    kept = set(wrote)
    pairs = [
        (i, other)
        for op, start, end, written_start, written_end in opcodes
        if op == "replace"
        for i in range(start, end)
        for other in wrote[written_start:written_end]
    ]
    for i, other in pairs:
        one = said[i]
        to = gender(other)
        plural = other.removesuffix("ं")[-1:] in ("e", "े", "ए")
        if plural or one in POSSESSIVE_GENDER or not related(one, other) or {gender(one), to} != {"m", "f"}:
            continue
        before = [word for word, at in zip(said[:i], clause_of, strict=False) if at == clause_of[i]]
        first_person = not ergative and not FIRST_PERSON.isdisjoint(before)
        if first_person and not set(said) & GENDER_SAID[str(to)]:
            return other
        owner = [POSSESSIVE_GENDER[word] for word in before[-OWNER_NEAR:] if word in POSSESSIVE_GENDER and word in kept]
        if not first_person and owner and owner[-1] == gender(one):
            return other
    return None


# A possessive this close before a verb may be its subject's ("meri gaadi kharab ho gayi").
OWNER_NEAR = 4


# A Hindi future ends in a vowel, maybe nasal, then ga, gi or ge ("भेजेगा", "भेजूंगा", "होंगे", "आएगा"); never "लगा"
# or "मांगा".
HINDI_FUTURES = tuple(vowel + nasal + "ग" + end for vowel in "ूेोऊएओ" for nasal in ("", "ं") for end in "ाीे")


# A Roman Hindi future ("karenge", "aaunga", "jaoge"); never "omega" or "doge".
ROMAN_FUTURES = ("ega", "egi", "enge", "engi", "unga", "ungi", "oge")
ROMAN_FUTURE = re.compile(r"^[a-z]{2,}(?:ega|egi|enge|engi|unga|ungi|oge)$")


# Every future ends in one of these; words with none are counted with one search.
FUTURE_HINT = re.compile(r"(?:will|shall|'ll|ग[ाीे]|ega|egi|enge|engi|unga|ungi|oge)(?:\s|$)")


def futures(found: list[str]) -> int:
    """How often the words promise something: "will", "shall", "'ll" or a Hindi future in either script."""
    if FUTURE_HINT.search(" ".join(found)) is None:
        return 0
    promised = [
        word
        for word in found
        if word in ("will", "shall")
        or word.endswith("'ll")
        or (word[-1:] in "ाीे" and word.endswith(HINDI_FUTURES))
        or (word.endswith(ROMAN_FUTURES) and len(word) > 4 and ROMAN_FUTURE.match(word) is not None)
    ]
    return len(promised)


# Usage slips a careful editor fixes, Indian English most of all, each with the said words put right: an answer that
# makes the fix is judged against the speech as if said right ("did you went" did you go, "me and priya will" Priya and
# I will, "once i will reach" once I reach). Only the words of the slip change; the rest is judged as said.
PARTICIPLES = {
    past: participle
    for past, participle in (
        pair.split("/")
        for pair in (
            "came/come went/gone ran/run became/become did/done saw/seen ate/eaten wrote/written took/taken gave/given "
            "spoke/spoken drove/driven broke/broken chose/chosen forgot/forgotten began/begun grew/grown threw/thrown "
            "fell/fallen knew/known drank/drunk"
        ).split()
    )
}
VERB_GROUPS = [
    group for group in IRREGULAR if not group & word_set("your there then whose its child person man woman mouse")
]
BASES = {
    word: min(
        (form for form in group if form not in PAST and not form.endswith("s")), key=lambda form: (len(form), form)
    )
    for group in VERB_GROUPS
    for word in group
    if word in PAST
}
OBJECT_CASE = {"me": "i", "him": "he", "her": "she", "us": "we", "them": "they"}
SUBJECT_CASE = {"i", "he", "she", "we", "they"}
CLAUSE_OPEN = r"(?:^|(?<=[.!?,;]\s)|(?<=[.!?,;]))"


def base_form(word: str) -> str | None:
    """A past's base form ("went" go, "finished" finish), or None for a word that is no past."""
    if word in BASES:
        return BASES[word]
    if regular_past(word):
        return word[:-2]
    return None


def inverted_past(found: re.Match[str]) -> str:
    base = base_form(found.group(4))
    return found.group() if base is None else f"{found.group(1)}{found.group(2) or ''} {found.group(3)} {base}"


def coordinated(found: re.Match[str]) -> list[str]:
    """A subject of two joined by "and" with a pronoun in the object case, in the subject case: "me and him" he and
    I, "me and priya" Priya and I (and I and Priya)."""
    first, second = found.group(1), found.group(2)
    one, other = OBJECT_CASE.get(first, first), OBJECT_CASE.get(second, second)
    if one == first and other == second:
        return []
    variants = [f"{one} and {other}"]
    if one == "i":
        variants.append(f"{other} and i")
    return variants


USAGE = [
    (re.compile(r"\b(did|does|do)( not)? (i|you|he|she|it|we|they|\w+) (\w+)\b"), inverted_past),
    (
        re.compile(r"\b(has|have|had)( \w+)? (" + "|".join(PARTICIPLES) + r")\b"),
        lambda found: f"{found.group(1)}{found.group(2) or ''} {PARTICIPLES[found.group(3)]}",
    ),
    (re.compile(r"\b(?:was|were) (" + "|".join(PARTICIPLES) + r")\b"), r"\1"),
    (re.compile(r"\b(when|once|after|before|until|till|if|unless|as soon as) ((?:\w+ )?\w+) will "), r"\1 \2 "),
    (re.compile(r"\btold that\b"), "said that"),
    (re.compile(r"\b(senior|junior|superior|inferior|prior) than\b"), r"\1 to"),
    (re.compile(r"\belder than\b"), "older than"),
    (
        re.compile(r"\b(am|is|are) knowing\b"),
        lambda found: {"am": "have", "are": "have"}.get(found.group(1), "has") + " known",
    ),
    (re.compile(r"\bsince long\b"), "for a long time"),
    (
        re.compile(r"\b(don't|doesn't|didn't|do not|does not|did not) (have|has|had|want|need|know|see|get) no\b"),
        r"\1 \2 any",
    ),
    (re.compile(r"\b(return|revert|repeat) back\b"), r"\1"),
]
# Slips with more than one right fix.
USAGE_CHOICES = [
    (re.compile(r"\b(can|could) (you|i|we|they|he|she) able to\b"), [r"\1 \2", r"will \2 be able to"]),
    # A question word with the subject before its auxiliary ("why you did not tell me" why didn't you tell me).
    (
        re.compile(
            r"\b(why|what|where|when|how) (you|we|they|he|she|i) (did|do|does|will|can|could|would|should)( not)? "
        ),
        [r"\1 \3\4 \2 ", r"\1 \3n't \2 "],
    ),
    # A thing that has no plural takes no "a" ("a advice" advice, some advice).
    (
        re.compile(r"\ban? (advice|information|furniture|equipment|feedback|luggage|news|homework)\b"),
        [r"\1", r"some \1"],
    ),
    (re.compile(CLAUSE_OPEN + r"myself (\w+)"), [r"i am \1", r"this is \1"]),
]
COORDINATED = re.compile(CLAUSE_OPEN + r"(me|him|her|us|them|i|he|she|we|they) and (me|him|her|us|them|i|\w+)\b")


def usage_fixed(said_text: str) -> list[str]:
    """The speech with its usage slips put right, one text for each choice of fix; none when it has no slip."""
    lower = said_text.lower()
    fixed = lower
    for pattern, replacement in USAGE:
        fixed = pattern.sub(replacement, fixed)
    texts = [fixed]
    for pattern, choices in USAGE_CHOICES:
        if pattern.search(fixed):
            texts = [pattern.sub(choice, text) for text in texts for choice in choices]
    found = COORDINATED.search(fixed)
    if found is not None:
        cased = coordinated(found)
        texts = [
            text[: found.start(1)] + variant + text[found.end(2) :] for text in texts for variant in cased
        ] or texts
    return [text for text in texts if text != lower]
