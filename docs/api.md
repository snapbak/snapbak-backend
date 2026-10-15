# API

JSON in and out, UTF-8. Every route but `/healthz` needs `Authorization: Bearer
<SNAPBAK_TOKEN>`. The token is compared in constant time. The shapes are pinned
by `tests/contract/*.json`, which are byte-identical copies of the app's
fixtures; `tests/integration/test_contract.py` compares them with the app's when
`../snapbak` is checked out next door, and skips that check when it isn't.

## GET /healthz

No token. Answers `200 {"ok": true}`. The app calls it on key-down to open a
warm connection. After answering, the server opens each tier's provider
connection and the fallback's too, with a `HEAD` at most every 30 s whose answer
is ignored, so the first chunk, and a backup call, skip their TLS handshakes.

Run the server with `--timeout-keep-alive 75`, as `just serve` does. The app's
first chunk comes at least 5.7 s after key-down, so uvicorn's default of 5 s
would close the warmed connection first. Provider connections stay open 5
minutes.

## POST /v1/polish

One chunk of a dictation, with the text polished before it.

| Field         | Type     | Limit                                                 |
| ------------- | -------- | ----------------------------------------------------- |
| `text`        | string   | 0 to 8,000 characters; empty only when `prior` is not |
| `prior`       | string   | at most 300 words; `""` for the first chunk           |
| `terms`       | string[] | at most 200, each 1 to 100 characters                 |
| `app`         | string   | at most 100 characters; `""` when unknown             |
| `style`       | string   | `chat`, `email`, `code`, `docs` or `other`            |
| `continues`   | bool     | optional, default `false`; see below                  |
| `deadline_ms` | int      | 300 to 20,000                                         |

The answer:

| Field      | Meaning                                                           |
| ---------- | ----------------------------------------------------------------- |
| `text`     | the polished chunk; `""` when a correction took all of it back    |
| `revision` | the corrected `prior` when a correction reaches back, else `null` |
| `rejected` | `true` when the guardrail refused the answer and its correction   |
| `engine`   | `provider:model` that gave the answer                             |
| `tier`     | `small` or `big`                                                  |
| `ms`       | time spent on the server                                          |
| `tokens`   | input plus output tokens over every model call                    |

When `rejected` is `true`, `text` is the request text unchanged and `revision`
is `null`.

Lists come back the way the app renders them (founder, 12 Oct 2026). The
guardrail judges the plain form ("- " bullets, "1. " numbers); once the answer
is accepted, `style` writes its lists:

| `style`                   | Lists                                                |
| ------------------------- | ---------------------------------------------------- |
| `chat`                    | "• " bullets; numbered lists stay "1. "              |
| `email`, `docs`, `other`  | "- " bullets and "1. " numbers                       |
| `code`                    | no list: each item is a sentence after its lead line |

A `prior` the app typed with "• " bullets is read as "- " before judging, so a
chunk can go on with a chat list. `text` and `revision` both come back styled.

Snapbak is English only (14 Oct 2026): the request has no `language` field. An
older app that still sends one gets a normal answer; the field is ignored and
never reaches the model.

`continues` is `true` for a first chunk (`prior` empty) when the text before the
cursor ends mid-sentence, as in "hey" before "sam ship it": the model then
starts lowercase unless the first word is a name ("hey Sam, ship it."). Only the
flag leaves the Mac, never the field's text.

## POST /v1/command

Rewrites a selection as a spoken instruction asks. It always runs on the big
tier.

Request: `selection` (1 to 20,000 characters), `instruction` (1 to 2,000), `app`
(at most 100) and `deadline_ms` (300 to 10,000). Answer: `text`, `engine`, `ms`
and `tokens`.

## Errors

| Status | Body                        | When                                     |
| ------ | --------------------------- | ---------------------------------------- |
| 401    | `{"error": "unauthorized"}` | no token or a wrong one                  |
| 422    | `{"error": "invalid"}`      | not JSON, or outside the limits above    |
| 502    | `{"error": "provider"}`     | every provider failed or ran out of time |
| 500    | `{"error": "internal"}`     | a bug                                    |

The token is checked before the body, so a bad token with a bad body is a 401.

A 502's log line names the first model asked, not the fallback, and its verdict
says what happened to each model called: `error`, `timeout`, or for a command
`malformed`. For example `model=gpt-6-luna tier=- verdict=timeout` means the big
tier ran out of time and none was left for the fallback.

## What the model sees

The system prompt and the worked examples never change between requests, so a
provider can cache them. The last user turn is the request as JSON without
`deadline_ms`, and without `prior`, `terms`, `app` or a `false` `continues`. The
model answers `{"revision": string|null, "text": string}` for polish and
`{"text": string}` for a command.

When the chunk holds a correction cue and `prior` is not empty, the prior's last
sentence moves to the front of `text` and only the rest goes as `prior` (a full
stop after a digit, as in "1. ", ends no sentence). So a correction that reaches
back is fixed as one piece of text, and a long prior isn't written out again.
The answer goes back in the usual shape before the guardrail judges it against
the request: when the model's text starts with that last sentence unchanged,
`text` is the rest and `revision` is `null`; otherwise `text` is the model's
text and `revision` is the rest of the prior (or the model's revision of it),
which the app joins in front.

The prompt asks the model to:

- write what a careful editor would type: correct words, grammar, sentences,
  punctuation and formatting
- add punctuation and casing, give names a capital, and turn spoken punctuation
  into that mark, with "open quote ... close quote" around its words; keep a
  mark word used as a word ("the period of the contract"); split run-on speech
  into sentences and never join two with only a comma; start a new sentence at
  "then" before its own subject; end a direct question with "?" (also one with a
  tag such as "right") and an indirect one ("I wonder if", "ask him what to do")
  with a full stop; start lowercase when `continues` is set; drop a full stop
  the recognizer put inside one sentence, and a "?" it put on a sentence that
  asks nothing directly; end every sentence with a mark, a short phrase said
  alone too; set off a name or title the speaker talks to with commas ("Okay,
  Ravi, invoice sent."), never turning it into a possessive or a subject or
  adding "is" to a note; write acronyms said as letters in capitals; add quote
  marks only for "open quote" or a literal label; put a paragraph break only
  where "new paragraph" is said or around an email's greeting and sign-off, and
  a line break only for "new line", a list or an email's greeting and sign-off;
  keep a full stop typed after "do one thing"
- turn items counted off in order ("first", "one"), even when each is a
  sentence, into numbered lists (`1.`), without the counting words and even with
  no words before them, and into bulleted lists (`-`) items said after "bullet"
  or steps joined by "then" after words that announce them ("the following"), or
  three steps or more after "do one thing" (two stay one sentence, with a colon
  after "do one thing", and so does a step that depends on the one before it),
  with no full stop after an item; steps with no announcement stay one sentence,
  and a number that says how many of a thing is a quantity, never a list count;
  continue the numbering on a new line when `prior` ends in a list item, where a
  chunk that starts with "and" goes on with that item in lowercase; one list
  never mixes "1." and "-", and words before the first item end with a colon
- turn several separate statements or items after words that announce them
  ("here are a few things about me", "I want the following in a numbered list")
  or a plural noun that names them ("the priorities are") into a list even when
  each is a full sentence: numbered when counted or asked for, else bulleted;
  drop "and" before the last count ("and three, bread" `3. Bread`), number an
  item said after the counted ones, and keep closing words ("Thanks.") on their
  own line
- put back a word the recognizer misheard only when the sentence is wrong or
  meaningless with it and one word that sounds the same or nearly the same,
  maybe split or joined otherwise, makes it right ("weather it works" whether,
  "a head of" ahead); a number word or a negation may be put back for its
  sound-alike (no/know, to/two, for/four, then/than) but never change what a
  name, a number or a negation means, or how much, how sure or when; never a
  word that is right in its sentence or when both readings fit, and never add,
  drop, reorder or translate words to make a sentence better; a phrase said
  earlier in the same text tells what a later sound-alike is ("the fan is
  running ... if the fan is ruining"), and so does the topic; a two- or
  three-word phrase is fixed too ("steal frame bikes"), and a noun the
  recognizer split is joined when the joined form is the one word for it ("bed
  room" bedroom; a verb stays apart, "log in")
- write money (with `$` or `₹`), dates, times, percentages, numbers over ten, a
  page, section, step, chapter or room number, and numbers before a unit as
  digits; keep any other number up to ten a word ("two ways"), also beside
  digits ("three calls in 4 days"), and idioms as words ("cloud nine"); keep a
  digit already written a digit; write a clock phrase as its time ("quarter to
  five" 4:45, "ten past three" 3:10) and a lakh or crore amount as "₹2.5 lakh"
  or Indian-grouped digits; let a currency symbol replace the currency word
  ("₹50", never "₹50 rupees"); spell out spoken email addresses and paths only
  when "at", "dot" or "slash" is said, and never for the verb "slash" ("we slash
  costs"); a number and a unit before a noun may take a hyphen ("a 30-day
  trial")
- fix every clear grammar slip; never change word choice, tone or meaning, and
  never add a word the sentence doesn't need; change only the word that is
  wrong; write a word said twice in a row once ("and And"); give a clause
  missing only its subject its subject back ("Is in the drive." It's in the
  drive.); put a question's misplaced auxiliary in order ("where the files will
  be" where will the files be); agree a verb with the head noun, the last noun
  of a compound; let a time word set the tense; keep an opening "so"
- keep every other word spelled as said: no respelling, standardizing, joining
  or hyphenating inside a word ("oncall", "wifi" stay)
- drop retracted words and their cue ("no wait", "scratch that", "I mean", "I
  meant", "oops", "nope", "let's change that"), even with no comma or in
  mid-sentence, so only the correction is left, never "X, no, Y"; a lone "no" is
  a cue only before another version of the word before it ("priya no rahul"
  Rahul); a new instruction after the cue replaces the clause before it, and a
  whole sentence after it replaces the sentence before it; a cue that ends the
  speech drops the sentence before it; ordinary uses such as "no problem",
  "actually" before a verb, "sorry" as an apology, a cue word in reported speech
  ("he said sorry") or an "I mean" that only explains stay; both values are
  never written as a range ("4 pm I mean 5 pm" is 5 pm); a cue may start at the
  end of `prior` and go on in `text`, and a chunk that opens with a cue takes
  back the end of the prior, even one word
- return `revision`, polished like the text, when the correction reaches into
  `prior`, keeping every line break of the prior, and always `null` when there
  is no `prior`
- spell every entry in `terms` exactly, even one that looks like a misspelling
  or a common word; an all-lowercase term takes a capital at a sentence start; a
  term the app sent because a heard word sounds like it ("Procall" for Procol,
  "lift" for Lyft) is written in that word's place only where the sentence means
  the term ("the lift is broken" stays), and a term that is also a plain word
  takes the term's spelling only where it names the thing ("zoom in", "we hope
  so" stay with Zoom or Hope)
- follow the app's style for formatting only, never for words
- keep every word in the language it was said in, never translate, and never
  answer the text
- drop a joining word that ends the speech with nothing after it ("... and",
  "... but"), and a subject the speaker stopped on or the recognizer heard in
  the silence at the end ("... on weekends? You", "... a personal project I"),
  unless it answers "who", is asked or ends its sentence ("than I", "so do I")

The worked examples are generic and share no run of 8 words with a corpus row
(`test_no_prompt_example_shares_an_8_word_ngram_with_any_row`): misheard words
put back ("i no the vendor will bring to extra chairs", "the fan is ruining",
"steal frame bikes", "sum of us" some, "flower for the cake" flour), a right
word kept ("the soup needs more taste"), a term that looks like a misspelling
kept beside its plain twin ("nimble" and Nimbl), a term written for its sound
("flow stack" Flowstak), announced and counted lists, corrections in
mid-sentence and across a chunk, numbers, questions, exclamations, a vocative
set off with commas, a request to the assistant typed as said ("tell me a joke
about cricket"), a digit kept, and a right sentence returned unchanged.

The prompt grew for accuracy from 3,369 tokens (round 3, 11 Oct) to 13,619 (14
Oct) through rounds b1 to b6, each adding the rules and generic examples the
graders found missing (`bench.md`); since the founder's decision D8 (11 Oct: the
same models, accuracy over cost and time) the heavy month is a report, not a
gate (`replay.md`), so the prompt may grow as far as accuracy needs.

English only, 14 Oct 2026: Hindi and Hinglish were dropped; earlier rounds
measured them too. Their rules and 40 worked examples left the prompt, and the
system prompt went from 30,011 to 23,119 characters. Dropping the examples alone
cost the plain (small-tier) route about 1.7 points of sentence correctness (92.1
to 92.4 on 316 shared rows, three runs, against 93.4 to 94.3 for the old prompt
run the same day, so not drift in the API). 26 generic English examples that
teach the same skills brought it back to 94.0 and 93.7. The fixed prompt (system
and 92 examples) is 37,815 characters against 48,550, about 10,200 tokens
against 14,368. The "translated" and "gender" refusals went with the other
languages (the gender check judged only their verbs).

## Correction

When the guardrail refuses an answer, small or big tier, the big tier gets one
chance to correct it, with reasoning, when at least 1,500 ms are left: the same
turns, then the refused answer as the assistant's turn, then the request asked
again as JSON with a `checker` field naming the rule and the words the guardrail
found ("That answer was refused: you wrote 'okay', which the speaker never said;
... Answer the same request again by the same rules, changing only what this
names: keep every other fix you made, and reply with the JSON only."). The
system prompt says `checker` is never dictation. Until round b6 (12 Oct) the
note was a user turn of its own, and the model twice polished the note and sent
it back as the dictation (founder-019 run 2, formatting-214), so the text went
in as said. The guardrail returns that reason as data (`Reason(rule, words)` in
`ai/guardrail/judge.py`; `correction()` in `ai/prompts/polish.py` words it). A
corrected answer refused again, or no time left, gives `rejected: true` and the
request text, which the app types as said ("AI skipped"); a piece going on with
a sentence keeps its joining word in lowercase there, and a capital the
recognizer put on a word after it that is never a name ("and How is it" and how
is it, round b2). The reason's words never reach a log: the log line has only
each call's rule name. A tense refusal never pushes the model to undo a grammar
fix: it adds "keep the time the speaker meant (a verb may change only to agree
with a time word said or its auxiliary)" (round 3, 11 Oct).

Round b4 (11 Oct) added `unlisted`: speech that announces statements ("Here are
a few things about my team.") and then says two or more, each stopped by the
recognizer, answered as prose. The correction asks for the announcement with a
colon and each statement as a "- " item, numbered only when counted (D5,
founder-019, which stayed prose in both round b4 runs). Speech with no stops,
one statement, or questions after the announcement is left to the model. No
corpus gold answer and no other round b4 output trips it.

Before asking again, two refusals are put right without a model when the rest of
the answer is right (`repair()` in `transformers/polish.py`, round b2): a piece
going on with a sentence that came back as a new list item or without its
joining word ("3. how is it going" for "and How is it going") gets the word back
in lowercase, and a "new paragraph" said but run on gets its blank line after
the sentence the speaker said it after. The mended answer is judged again, and
used only when it passes.

Round b5 (12 Oct) adds a third repair: a run-on split into sentences by dropping
its joining word ("...a simple queue. It will pick" for "a simple queue and it
will pick") gets the word back opening that sentence ("...a simple queue. And it
will pick"), which rule 2 allows; a word that only opened the sentence ("It",
"The") loses its capital. The correction for that refusal names the joining word
and asks to keep it ("write a comma and the word, or open the next sentence with
it"). A refusal for too many changes now names only the content words the answer
lost or moved, never the number words it wrote as digits or the spoken marks, so
the correction can tell what to put back.

Round b6 (12 Oct) mends four more refusals without a model, each only after
every word of the answer passed: a direct question left with a full stop gets a
"?" (each sentence's end mark tried in turn, from the last), a "?" on a sentence
that asks nothing directly gets a full stop, a list counted off but bulleted is
numbered (from the prior's next number), and announced statements written as
prose (`unlisted`) become the announcement with a colon and each statement as a
"- " item without its full stop. So founder-019's answer, right in every word
but prose, is listed without asking again.

A piece that takes nothing back leaves the typed prior as it is (round b5,
founder-017 and founder-018): a revision that holds every word of the prior
first and then goes on ("...\n2. how good it looks\n3. and how does it show the
data?") is read as the piece's answer after the prior, so the joining-word check
and its repair apply to it, and the prior is never re-marked.

## Deadline

`deadline_ms` bounds all the work. The server stops 250 ms early, so the answer
travels back before the client stops waiting. The app sends 20,000 ms for every
chunk, the last one counted from when it is sent (founder, 11 Oct: accuracy over
speed, so a correction and a provider's retries fit), and the server gets 19,750
ms: the default pair answers in about 1.5 s and up to about 2.9 s at the p95.
Each model call gets only the time left. A provider's `429` or `5xx` answer is
asked again within that time, before the fallback (`providers.md`). The fallback
starts only while time is left: at once when the tier fails, or after 10,000 ms
when the tier is still asking (`HEDGE_S`; then without reasoning, the first
answer wins and the other is cancelled, because gpt-6-luna at medium reasoning
answers in 3.4 s at the median and 10.6 s at the p95, round b3). The correction
starts only with at least 1,500 ms left (about the big model's median answer
time, so one that would mostly time out doesn't hold up the rules text), and
both are cut off when time runs out. The correction is hedged within the time it
has: the fallback joins it once half the time left has gone, or after 10,000 ms
if that comes first (`RETRY_HEDGE_SHARE` in `services/polish.py`, round b5). A
long dictation's correction starts about 10 s in, and the big tier reasoning
over it used to run past the deadline before the fallback was asked: 4 of the 8
long dictations AI-skipped in round b5 ended that way.

Every chunk, on either tier, and every correction asks for medium reasoning (14
Oct 2026; before, only big-tier chunks), with 4,000 more output tokens of room
(`escalation.md`; low effort and 1,000 tokens until round b3). On the whole
corpus (3,101 items, 10 Oct 2026) the default pair answered in p50 1459 to 1521
ms and p95 2530 to 2535 ms; on the dev and held-out samples, where three in four
chunks go big, p95 was 2773 to 3096 ms.

## Guardrail

Ported from the app's old `polish.rs` and widened. The answer is checked as
`revision + text` against `prior + text` when a revision comes back, and as
`text` against the request `text` otherwise. A first chunk answered only as a
revision, with an empty `text`, is judged as that text.

Before it judges (round b1, 11 Oct), two edits go into the answer. A word the
model respelled to another accepted spelling of a word said ("okay" for "ok"),
and that was never said itself, gets the speaker's spelling back (rule 6), so a
respelling is never refused as a dropped or unheard word. And a joining word
that ends the whole speech with nothing after it drops, with a full stop for the
sentence it hung on unless that line is a list item: "and", "but" and "or"
always; "so" and "then" only after a full stop, since they also end a sentence
("I told him so").

A subject said alone at the very end starts nothing and drops the same way, in
any chunk (founder, 14 Oct 2026): "you", "I", "we", "they", "he", "she" or "it"
after a full stop ("... on weekends? You"), and "I", "we", "they", "he" or "she"
with no verb after any word but one that ends a sentence on a subject ("... a
personal project I"; "than I", "you and I", "so do I" stay). It stays where the
sentence before asks "who" ("Who goes first? You.") or the speaker asked it
("You?"). The checker judges an answer that left the word out against the speech
without it, so the drop is never refused as a dropped word. "Priorities" and
"goals" announce a list as "things" and "points" do, so a bulleted list after
them is not refused as one never asked for.

Round b3 (11 Oct) added to those edits: after an answer is accepted, two end
marks stacked on one sentence ("Friday?.") become the "?" or "!", and the name
under a sign-off ("Cheers,\nTom.") loses its full stop.

Round b3 also made three refusals name the right thing. A fraction word left
before an hour's digit ("quarter to 8") is refused as `format` naming it, before
any number is called unheard; one such slip used to name all 22 numbers of a
note, so the correction could not fix it. A number written out of the order said
names only the first one out of order, not every digit. And a negation is taken
back when the correction says its verb again a few words on ("don't send the
invoice yet no wait send it today" Send the invoice to the client today); a
correction that restates only an object ("don't send it to Sam no wait to Ravi")
still keeps it. On the corpus golds and the generated wrong answers the
guardrail refuses 2 of 10,458 golds and catches 99.53 % of bad answers, as
before, at 180.2 µs a call (180.2 to 182.0 before).

Round b4 (11 Oct) accepts four right answers it refused, each a round b4 miss: a
sound-alike written in its plural ("the bike's break" brakes) or as a hyphenated
compound ("brake glass" Break-glass); a sentence said again after a cue that
names with a pronoun what the answer keeps as its noun ("give the medicine to
Grandma after lunch, scratch that, give it to her before lunch" Give the
medicine to Grandma before lunch.); and a piece that takes nothing back but came
with the prior only re-marked or re-cased ("?" added to a typed item,
founder-016): that revision is dropped and the prior stays as typed, unless the
piece says a mark ("question mark") that the revision puts on the prior. It
refuses two more wrong ones as `unheard`: a count written twice in a row where
it was said once ("two liter bottles" two two-liter bottles; not a decimal's
digits or a count on each side of a full stop), and a last line that only names
someone said earlier, as an invented sign-off ("my son aarav ... thank you for
your help" ... / Aarav). A refusal for too many changes also names the said
words the answer moved, so the correction can put them back. After an answer is
accepted, a full stop after a quote closed by "?" or "!" goes ("?”." ?”). On the
corpus's 11,143 first expected answers the guardrail refuses 2, as before, and
catches 99.52 % of the generated bad answers, as before (65,299 of 65,615). A
check now splits the same speech and answer into words once (`split_tokens`
keeps the last 64 splits), which paid for the new checks: 177.2 to 180.0 µs a
call before, 169.5 to 173.3 µs after (best of 5, four runs each, alternating).

Round b6 (12 Oct) adds five refusals and three edits, each with a test in
`tests/unit/ai/test_guardrail.py`. Refused: three steps or more after "do one
thing", joined by "then" across full stops too, written as prose (`unlisted`; a
step that waits on "whatever he says" is no step of its own; this split matches
all 82 such corpus rows); a piece that opens with a cue taking back the prior's
last words ("make that", "no wait", "scratch that", "i meant" always; "i mean",
"wait", "rather", "sorry" only when the piece restates a word of the prior's
last eight, never "sorry for" or "wait for") answered with the cue still written
(`kept_correction`, "...at 3:15. I mean, at 8:45"); a sentence the speaker ended
with a full stop run into a list item ("2. Turn off the lamps Keep my laptop",
`dropped_item`); "NAME at DOMAIN dot TLD" written as half an address ("Sam at
acme.io", `format`; a name or a mailbox word before "at", never a verb); a line
break in a dictation of fewer than eight words that no greeting or sign-off line
explains ("Signed by\nPatel", `false_list`); and "ask X what to do?" said as a
statement (`not_a_question`, then mended). "Do one thing," no longer counts the
"one" as a list count, so two steps after it bulleted are a false list again.
Accepted: a word said on both sides of a correction cue and written once where
the versions meet ("the delivery slot no no the courier's slot" written once).
Edited: a term that is also a plain word, said in lowercase and written with the
term's capital after "the", "this" or a possessive and a describing word, is the
plain word ("the electricity bill" with Bill, "this yoga asana" with Asana; "a
quick Loom" stays). On the round b6 outputs (2,900 rows without a prior) the new
checks refuse 6 answers, all misses, and no passing one; the guardrail refuses 2
of the corpus's 11,734 first expected answers and catches 99.54 % of the
generated bad answers, both as before, at 165.7 µs a call against 167.1 µs (best
of 5, five runs each, alternating; a single sentence is no longer split into
words again to look for a repeated one).

Said and written words are matched from the end, so a phrase said twice matches
its last saying, the correction's final version, even where a longer match would
keep its first ("no busywork rather no distractions" keeps the second "no"). A
stutter, a word said twice in a row ("it's it's"), may lose its second copy for
free (founder, 11 Oct), unless the answer keeps the repeat, it is a doubled cue
("no no"), a number ("nine one one") or a spelled letter. It is rejected when:

- **Split joiner (round b5):** a joining word said between two words the answer
  kept on either side of a new sentence ("and", "but", "so", "because", "then")
  is left out there: `dropped_word` naming it, and the repair above puts it
  back. A list item's start is no split, a joining word said twice in a row is a
  stutter, and a run of words a correction takes back is left to the other
  checks. On the corpus golds made wrong this way (", and X" written ". X",
  2,575 answers), the guardrail refuses 86.25 % against 4.35 % before.
- **Bare lines (round b5):** three or more short lines (two words or fewer, no
  list marker, not a greeting or sign-off) where the speech had no line break
  and no spoken "new line" or "new paragraph" ("tom cooks lisa cleans" Tom /
  Cooks / Lisa / Cleans): `false_list`.
- **Repeated (round b5):** a sentence of two words or more written more often
  than it was said: `repeated`.
- **Dropped join:** the chunk goes on with a `prior` that ends mid-sentence and
  starts with "and" or "then", and the answer lost that word: it would read as a
  new sentence glued to the last ("visually appealing How is it…", 11 Oct).
  Kept, the word comes back lowercase.
- **Reply:** it opens like an answer ("Sure", "Here is") and the user didn't
  open that way; a spoken "new paragraph" or "new line" before the user's own
  "here is" opens nothing.
- **Unheard:** it holds a word the user never said. Allowed: every word of
  `terms`; a digit up to ten written as its counting word ("1" one; never an
  ordinal), since the prompt writes those numbers as words; small grammar words;
  other forms of a said word ("sent" for "send", "saw" for "seen", "bought" for
  "buyed", "ran" for "runned", and the homophones speech to text mixes up:
  "your" "you're", "there" "their" "they're", "then" "than", "whose" "who's",
  "its" "it's"), where a regular form differs only by an ending ("-s", "-ed",
  "-ing"), so "access" is no form of "accept"; said words joined ("note id"
  noteId, "c o r p" corp) or without an apostrophe or hyphen; and digits only
  for a number the user said ("twenty five" 25, "eight forty five" 8:45, "nine
  one one" 911), never across an "and" ("five and six" is not 56). "half" and
  "quarter" are minutes ("half past three" 3:30), and "quarter to" takes the
  hour back (5:45 for "quarter to six", never 6:45). Indian digit groups count
  as one number (₹1,20,000), and the count before a lakh, crore or thousand may
  stand alone ("fifty two lakh" ₹52 lakh); "double five" is 55. Numbers come in
  the order said, each once, so "five not six" can't become "6, not 5"; only the
  parts of a time may turn round ("ten after three" 3:10). A word like "v1",
  "7B" or "4th" is checked piece by piece, and "3pm" may be written "3 pm". A
  numbered list's own numbers, at line starts and counting on by one, are
  formatting, not numbers said, so an item said after counted ones may take the
  next number and "I want these in a numbered list" may be numbered.
- **Misheard word:** a written word the user never said is allowed when it puts
  back a word the recognizer misheard: in a replaced run, one said word for one
  written (taste test, weather whether), two for one ("a head" ahead, "get way"
  gateway) or one for two, with the same sound key (`heard_as()` in
  `ai/guardrail/tokens.py`: the consonants after English spelling rules, an
  opening vowel as "A") and as many vowel sounds, or one more or less in a word
  of four consonants, or a word-boundary repair. A few pairs a letter apart are
  listed (cash cache, quite quiet, aloud allowed, should of should have, no
  know). Never a number, a negation other than "no" for "know", a correction
  cue, "will" or "shall", a term's word, a capitalized name, or two small
  grammar words for each other ("in" "on"); debit stays debit. One such fix in
  ten said words is free; each one past that costs two changes, so "taste the
  waist of the peace" can't become "Test the waste of the piece." The other
  checks still run, with the swapped pair out of the negation, tense and
  qualifier counts.
- **Term:** a word of `terms` the user didn't say is allowed only in place of
  said words that sound like it, one consonant off at most from three on
  ("Procall" Procol, "zero" Xero, "acai" Açaí), so a term can't be invented
  ("send it" can't become "Send it to Procol"). A term the user said must come
  back as written unless a correction cue after it took it back whole: "deploy
  it on Kubernetes" can't become "Kubernete", nor "we use Xero" "zero", and a
  written word one consonant off the term is the term misspelled even with a cue
  after it ("ship the android build on thursday actually on friday" can't become
  "Ship the Androi build on Friday").
- **Dropped sentence:** the answer is empty with no correction cue said, or
  content words went with nothing taking them back: two in a row or four in all,
  wherever else those words recur (round 1, 11 Oct; it replaced a check that
  needed every content word of a sentence gone from the whole answer, which a
  word said twice hid, and which any cue anywhere switched off). Content words
  are all but grammar words, spoken marks, cues, list counts and numbers, and
  are compared without apostrophes ("dont" "don't"). A cue takes the run back
  when it is said within four words of it (a lone "no" there is a negation),
  when the run is a retracted span ("send the report to ravi no don't do this"
  can become "Don't do this."), or when a later cue is followed by the words
  written in its place ("his green journal" then "actually blue notebook" in the
  next piece). Number and unit words written as digits are kept ("fifty five
  dollars" $55).
- **Too many changes:** more than 30 % of the said words changed or went (always
  at least one word may go). Free: spoken punctuation (also "open quote", "close
  quote", "unquote", "open bracket", "exclamation mark"; but a mark word after
  "the", "a", "an", "each" or "every" is a noun, and dropping it costs every
  said word: "the period of the contract" can't become "The. Of the contract"),
  number words that became digits, a fix in place (another form of the word, a
  grammar word for another of its closed group, which keeps its tense: "a an
  the", "is are", "was were", "has have", "do does", or words joined), and a
  retracted span. A tense swap ("are" to "were") costs one change. A fix may add
  a grammar word of those groups about once in four said words; each one past
  that is a change, so "ship it" can't become "The ship is to the it a." Each
  other grammar word the answer adds or swaps in ("and" to "or", "in" to "on",
  an unsaid "it") counts as a change, so "call priya" can't become "Call Priya
  and it"; in a text of under four words each one costs every said word, so
  "send report" can't become "I send the report" nor "pay it" "Pay for it". A
  number word dropped with no number in its place (other than a bullet's own
  count, "bullet one") loses a quantity, a time or an order, so the answer is
  rejected: "call me at three" can't become "Call me." A said number counts as a
  numbered list item's number only when it counts items off: not a count after
  "and" ("two apples one red and two green"), nor an ordinal straight before
  "in", "of", "on" or "at" ("first in line", "first of the month"), nor one
  beside "or" ("one or two fixes"), nor one either side of "to" between two
  counts ("one to two days"). A list item with nothing after its number counts
  as too many changes, and so does a list whose items all open with the noun an
  ordinal after "the" or "my" modified ("the first build failed the second build
  passed" can't become "1. Build failed"). A span is retracted when it holds a
  cue, something more than small grammar words was said before the cue, and its
  replacement stands: when the span ends at the cue, words must follow it, and a
  cue that is also an everyday word ("wait", "sorry", "rather", "actually",
  "make that", "change that", or "no" with neither a comma nor a negation after
  it) must read as one: after a clause mark unless "for", "about", "until",
  "till" or "than" goes on with no mark ("Wait for it.", "Sorry for the delay"
  take nothing back), inside a sentence, or followed by words that restate what
  it takes back ("mark said sorry matt said", "the daughter actually the son").
  A doubled "no" ("no no") is a cue that may go even with nothing before it. So
  "ravi said sorry about the delay" can't lose "ravi said". When the words after
  the cue restate two or more of the span's words, only the words from there are
  taken back, unless an earlier cue in the span took back the rest: "send the
  deck to sam and the notes to ravi scratch that the notes to priya" can't lose
  "send the deck to sam". A negation before the restated words goes with them
  ("don't send it, no wait, send it" can become "Send it."). Otherwise a word
  after the cue went into the retracted words' place, or the span is followed by
  kept words and the output kept every word after the cue. So "No problem, I
  will take it" can't lose "No problem", "I am sorry the build broke" can't lose
  "I am sorry", and a lone "actually" can't erase the prior. A strong cue that
  ends the text ("Scratch that.") takes back the one sentence before it, never
  the first sentence or more than one. Dropping "make that" or "change that"
  when it takes nothing back drops a verb and is rejected ("make that call"
  can't become "Call"), and so is dropping a cue whose "that" points at the word
  after it with nothing else taken ("please delete that file" can't become
  "Please file", 14 Oct), and a weak cue before "for" or "about"
  ("sorry about the delay" can't become "About the delay.", nor "i will wait for
  the report" "I will the report."). "no wait" and "no actually" are strong cues
  ("approve it no wait reject it" can become "Reject it."). A negation after the
  cue belongs to the replacement and can't be taken back ("send it no wait don't
  send it" can't become "Send it."), and so does a lone "no" right after another
  cue when the words after it restate the taken ones ("the patient has no
  allergies actually no known allergies" can't become "The patient has known
  allergies."); "not friday" after "make it thursday" names the version taken
  back. A said number in digits counts as kept only when those digits are
  written ("2022 no for 2021" can't keep 2022).
- **Negation:** the output has a different number of negations ("not", "never",
  "no", "n't", "cannot") than the user said, outside retracted spans and doubled
  cues. "I know" can't become "I don't know". Inside a sentence a lone "no" is a
  negation, never a cue, unless what follows it replaces a word it takes back
  with one of its kind (a day, a number, a colour, a side, a level, a family
  word, a pronoun of the same case or a name the answer writes with a capital:
  "book 3 tickets no 4" can become "Book 4 tickets.", "use red no blue" "Use
  blue.", "send it to him no her" "Send it to her.", "see amy no emily" "See
  Emily."), opens like the words it takes back ("to staging no to
  production"), or opens with a preposition ("in", "on", "at", "for", "with",
  "from", "by"): "tell the vendor no pickup" can't become "Tell the pickup". A
  "no" before "one", "longer", "idea", "problem" or "way" is a negation phrase,
  never a cue ("june fifth no one objected" keeps its "No").
- **Tense:** the output drops a "will" or "shall" the user said outside
  retracted spans ("'ll" keeps it), so "I will send it" can't become "I sent
  it." The changed words may not leave the past ("we shipped it" can't become
  "We ship it", nor "i have sent the file" "I send the file"), nor enter it
  ("was", "had", an irregular past or an "-ed" word) unless the user said a past
  form or a past time word ("yesterday", "already", "ago", "last", "earlier"):
  "he is coming" can't become "He was coming", while "i stop it yesterday" may
  become "I stopped it yesterday". A past form after a modal, "to", "do", "be"
  or "have" ("will sent", "can be informed") or after an article or "most" ("the
  signed contract") is a slip, a participle or an adjective, not a past.
- **Qualifier:** the output drops or repeats a word that sets how much, how
  sure, how often or when ("only", "all", "twice", "almost", "rarely", "maybe",
  "must", "probably", "definitely", "today"), outside retracted spans. "call
  priya today" can't become "Call Priya.", nor "we almost lost the client" "We
  lost the client." With a correction cue said, a qualifier said before the last
  cue may go and one written may be any said; one said after it must stay. An
  "am" or "pm" after a time that the user never said is a changed qualifier
  ("two to three pm" can't become "2 to 3 am"); "9 a m" and "3pm" count as said.
- **Kept correction:** both versions kept around a cue: the words just before
  and after a strong cue ("I mean", "no wait", "scratch that", "no", "sorry",
  "actually") are of one kind (days, numbers, colours, sides, levels, family
  words, or two names the answer capitalizes) and the answer still has the cue
  between them: "ship friday i mean monday" can't become "Ship Friday, I mean
  Monday."
- **Unpolished:** an answer of four words or more with no mark and no capital,
  as raw speech comes (round b1).
- **Question:** a dictation that opens with an auxiliary ("is", "can", "do",
  "will") came back with it after its subject: "is the deck ready" can't become
  "The deck is ready." Also a written sentence ending in "." that opens with an
  auxiliary and a subject ("Can you send the deck.") or a question word and an
  auxiliary ("Where is the invoice."), unless a verb before it makes it an
  indirect question ("Ask him where the invoice is.").
- **False list:** the answer holds a list of two items or more, and the user
  counted nothing off, said no "bullet" and no announcing word ("the following",
  "here's", "steps", "things", "points", "notes", "plan"), and the words before
  the first item announce nothing, nor end on a plural noun and "are" or "were"
  ("The blockers are:"): "I woke up then made coffee then left" can't become
  three bullets, nor "my name is ravi i live in pune" "My name is Ravi:" and
  bullets. A number counts items off only as digits, with a mark beside it
  ("One, milk."), or as one, two and on with three words or more between them;
  "we hired one designer two engineers" says how many and can't become a list. A
  numbered list of one item where no list goes on is false too ("first we need
  to hire" can't become "1. We need to hire").
- **Dropped item:** a numbered list skips a number ("1." then "3."), or a
  bulleted dictation came back with fewer list lines than "bullet"s said (not
  "bullet points").
- **Malformed:** the answer is not the JSON shape above.

Each refusal names its rule and the words it is about, which the correction
quotes: the unheard words, the dropped sentence, the changed negation, tense or
qualifier word, the lost term, the dropped joining word, the kept correction,
the question, the false list's first items or the missing list item. A negation
refusal about a lone "no" is worded so it never pushes the model back to "X, no,
Y": "you dropped 'no'; if the speaker used it to take words back, keep only the
corrected version and drop the cue, and if it is a real negation, keep it". The
"translated" and "gender" refusals went with the other languages (14 Oct 2026).

An accepted answer has line-end spaces trimmed, and every term's spelling and
casing put back ("Gmmco" becomes "GMMCO", "NGINX" Nginx, "ios" iOS). A plain
lowercase word the model kept plain stays plain when the term's case is only
capitals ("cut me some slack" with Slack, "let us know" with US, "It is done"
with IT), and so does a word inside an email, a web address or a path
("sales@northstar.com" with Northstar). An all-lowercase term keeps the capital
the model gave it at a sentence start. A word the user said whole loses a hyphen
the model put in it ("wifi", not "Wi-Fi"; "oncall", not "on-call"), with a
capital at a sentence start, unless the hyphenated form is a term. Words said
apart keep the hyphen ("follow-up"). A count up to ten the user's text already
held as a digit, and never as its word, goes back to the digit where the model
wrote the word ("5 countries" five countries; `digits_as_said`, 14 Oct 2026: it
changed in two English bench runs of two, as rule 4 forbids).

English only (14 Oct 2026): the guardrail kept every English check, and the
other languages' spellings, number words, negators, cues, questions, verb
genders and the translation check went; the rounds below measured them too.
"here's" now announces a list (one English row had passed only because a word of
another language read as a count). "lakh" and "crore" stay for English Indian
amounts.

False-rejection audit (14 Oct 2026): every corpus gold answer, all of them and
not only the first, judged as the server judges it, plus answers prompt rules 3,
5 and 6 call right. These now pass (each refusal typed the raw speech instead):

- a regularized past with its consonant doubled ("runned" ran, "forgetted"
  forgot), more irregular verbs ("swimmed" swam, "singed" sang; their pasts
  count as past for the tense check), and a past written as its base ("readed"
  read, "cutted" cut: a usage slip);
- a pronoun in the object case opening a clause before its verb ("him is" He is,
  "us need" We need), and a subject pronoun after a preposition with no verb
  after it ("give it to he today" him): usage slips, so "him is" still can't
  become "She is";
- "their" before "yesterday", "tonight", "early", "late" or "again" ("went their
  yesterday" there); a number word for its sound-alike before an irregular
  plural ("too people" two) or as a clock time ending the text after "at" ("see
  you at for" four), also in a text under five words; "ours" hours;
- a plural noun and "are" or "were" leading a list in ("The blockers are:"), and
  a "like" said right after the lead ("the blockers are like the api is down
  then...");
- "new paragraph here is ..." answered "Here is ...".

On the corpus (11,918 gold answers with the chunked twins) refusals went from 48
(0.40 %) to 44 (0.37 %), 40 to 36 after the rule mendings; on 62 prompt-rule
probes from 9 to 1 and on 66 more from 23 to 4. Generated bad answers are caught
as before or more: an unheard sentence 99.99 %, a reply 100 %, half the text
dropped 99.84 %, a real "no" dropped 100 %, a number word put for "two" where it
was right 82.4 % → 84.9 %, and gender, person, tense and digit swaps unchanged.
Speed is unchanged (0.63 to 0.72 ms a chunk before, 0.66 to 0.71 ms after, best
of 5). Still refused on purpose: an agreement slip fixed into the past ("the
landlord ask us" asked; rule 6 keeps the tense the sentence shows), "would" for
a reported "will", a contraction or an added word ("I've been", "so we should"),
a date turned round, "Patio works, right." with a full stop, a vowel sound
dropped from a short word ("taxi" tax, as "debit" debt), and a dialect past no
table holds ("brung").

Pronoun swap audit (14 Oct 2026): a pronoun written in place of a said one that
names someone else is refused as `unheard`, naming the new pronoun
(`other_person` in `ai/guardrail/judge.py`, `PERSONS` in `grammar.py`). A fix
may change only its case ("him is" He is, "me and him" He and I, "there going"
They're going); "we" They, "you" they, "my" our, "he" She, "his" her and
"himself" herself all change who did it, also where the new pronoun was said
elsewhere, in a list item, after a correction cue or in a later chunk. "it" and
"they" count as one, since a fix agrees them with a plural noun ("the worksheets
was ... it is" were ... they are). On every corpus row's first gold with one
said pronoun swapped (scratch harness), answers accepted went from 3,739 of
7,580 (49.3 %; "we" they 89 %, "you" they 87 %) to 0 for person and number,
and from 137 of 1,511 (9.1 %) to 0 for gender. Gold refusals stay 44 of 11,918
(43 of 11,916 after two Hinglish rows left the corpus), the 62 and 66
prompt-rule probes stay at 1 and 4, and speed is unchanged (0.25 to 0.28 ms a
chunk before, 0.26 ms after, mean of the best of 5, ABBA). A pronoun the
speaker puts right with a lone "no" ("send it to him no her") is written as
corrected since 14 Oct (`KINDS` in `corrections.py`); the swap harness still
accepts 0 of 9,091 swaps, and gold refusals stay 43 of 11,916.

Before round 3, on the corpus's own expected answers the guardrail refused 15 of
6,951 (0.22 % after the round-1 gold audit; 19 before round 1 and 673 before the
misheard-word, number and list allowances of 11 Oct), at most 1 in 100 per file:
among them Indian English tense rephrasings, a money range ("four hundred and
five hundred dollars"), and near-sounds it can't tell from a rewrite ("bones"
bonus, "negate" negotiate, "ten your" tenure). A word said again much later no
longer reads as a restatement after a cue (only the three words after it plus
the span's length count), so dropping a lone "no" with the word before it from
the expected answers that hold one is rejected.

Strictness against generated bad answers on every corpus row (scratch harness,
11 Oct; before the change → after): unheard word inserted 99.93 % → 99.93 %
caught, an unrelated word for a said one 99.93 % → 99.91 %, an invented clause
99.81 % → 99.81 %, a number changed 99.83 % → 99.79 %, a reply 100 % → 100 %, a
term misspelled 59.9 % → 96.6 %, the last sentence dropped 79.1 % → 77.2 %,
three words dropped 56.0 % → 54.6 %; 90.89 % → 91.79 % overall. The dips come
from rows whose gold the old guardrail refused (any bad answer to them was
"caught"): on the rows both accept, misses fall from 2,561 to 2,413 for three
words dropped and from 529 to 44 for a misspelled term. Speed on the whole
corpus's gold: 143.7 to 144.4 µs a call before, 141.2 to 141.9 µs after (ABBA,
best of 5, Python 3.14, M-series): the new checks run only on replaced runs.

Round 3 (11 Oct) widened what may change and narrowed what may go:

- **Allowed now:** a number word fixed for its sound-alike where a count goes or
  doesn't ("to tickets" two, "two long" too, "for pm" 4 pm; "want to go" stays,
  as no plural or time follows), a cue word written for its sound-alike ("weight
  in the lobby" wait), silent letters ("reign" rain, "climb"), a split word that
  is a number in part ("ten your" tenure, "four cast" forecast), three words for
  one long word ("in sure ance" insurance), known pairs a syllable apart (bones
  bonus, chorus course, formally formerly, negate negotiate); a past after a
  negated modal ("did not went" go, "can't came" come), a future with a future
  time word ("next week we finished" will finish), a date making either tense
  right, Indian English and usage partners ("from morning" since, "am waiting"
  have been waiting, "since two years" for, "between you and i" me, "whom is"
  who, "less people" fewer); "the third of march" or "the twenty second of
  november" as "March 3", "four hundred and five hundred dollars" as "$400 and
  $500", "one lakh fifty thousand" as "₹1.5 lakh", "plus nine one" as "+91",
  "ten to nine" as 8:50, "point eight" as 0.8; the cues "I meant", "oops" and
  "nope" (never "correction", a noun far more often), a lone "no" between two
  words opening the speech ("priya no rahul will lead"), "no not tuesday
  wednesday", an opposite ("happy no unhappy"), a word put right ("maria sorry
  mariam"); a term written for its sound-alike costs no change ("call shivon in
  lester" Siobhan, Leicester).
- **Refused now:** one content word left out with nothing new written in its
  place (**Dropped word**: "my teacher asked" can't become "My asked"); two
  names, places or list items written in the other order (**Reordered**: "from
  paris to lyon" can't become "from Lyon to Paris"); "still", "already", "yet",
  "even", "again" or "exactly" dropped (**Qualifier**); an acknowledgement
  written more often than said ("Noted:", "Thanks!", "Got it") or an opening
  "Done." (**Reply**); a number correction that kept the old number ("three no
  four" 3, "fifty dollars wait sixty" $50, "3 or 4"), both versions kept with
  the same small word before each ("the plumber, no, the electrician") or a
  strong cue dropped with nothing taken back ("approve it scratch that reject
  it" Approve it. Reject it.), and an "or" or "and" put where a cue was (**Kept
  correction**); a currency symbol no word said ("twenty euros" $20), a
  decimal's zero lost ("three point zero five" 3.5), a ratio turned round
  ("three to one" 1:3), a spoken address cut or changed ("acme dot co dot in"
  acme.co., "priya underscore k" priya-k), an "@" in prose, a mark said and kept
  as words ("question mark?"), a paragraph said between words but not made, and
  a "/" written for the verb "slash" after its subject or a modal ("we slash
  cloud spend" We /cloud spend.) (**Format**); a compound number's words reused
  ("twenty ninth at two" 29 and 9 pm) and "quarter to five" as 4:15; a
  sound-alike grammar word after a determiner ("the whether") and "except" where
  a verb goes ("will except"), a term written for a plain word after "the",
  "this" or a subject ("the Lyft is broken", "I Reddit yesterday").

Round b2 (11 Oct) refuses more wrong answers: a short dictation (under five
words) losing "then", "too" or "also", unless it is a joining word dangling
after a full stop; a spoken number written as its word and its digit ("two liter
bottles" two 2-liter bottles, **Unheard**). An accepted answer is then put in
shape without a model (`ai/guardrail/shape.py`): a count word before a unit of
time, money or measure becomes digits as rule 4 asks ("in two hours" in 2
hours), except "one", a rough pair and a sentence's first word (12 Oct: round
b7's dev outputs 1,329 → 1,332 exact of 1,503, none worse, no expected answer in
the corpus changed; 164.5 to 165.2 → 163.7 to 165.0 µs a call, ABBA); a bullet
straight after numbered items takes the next number, a first item glued to the
line before it ("running. 1. It needs") goes on its own line, the words before a
list end with a colon, a counting word left at a numbered item's start with its
comma ("1. First, can you") goes, a line left with no end mark gets back the "?"
the speaker said after its last words, and a blank line the speaker never asked
for runs on (one that sets off an email's greeting or sign-off stays, and before
a list item it is one line break). On the corpus's 9,883 first expected answers
(scratch harness, before → after, same corpus): 2 → 2 refused, generated bad
answers caught 99.69 % → 99.69 %, and 253.1 to 253.7 → 183.8 to 184.3 µs a call
(ABBA, best of 5, Python 3.14, M-series): the speed comes from looking up
correction cues by their first word (`CUES_BY_FIRST` in `corrections.py`), which
was a third of the time.

English bench, 14 Oct: three more shaping rules, from the formatting misses of
the day's stored English runs. The words before a list end with a colon, not a
comma (`Mom,` Mom:), but for an email's greeting (`Hi team,`); a line break the
text came with goes back where the model ran the two lines on (`June. Show`
June., new line, Show), placed by aligning the said words with the written ones;
and "to NAME at DOMAIN" said as "NAME at DOMAIN dot TLD" becomes one address
("to UX at lanternworks.io" ux@lanternworks.io), which rule 4 asks for and the
model sometimes misses; a web address after "at" ("the docs are at
docs.example.org") stays. Replayed without a model on the 11 stored runs of the
day (`bench.md`): 20 rows newly exact, none worse; no gold answer of the corpus
(11,916) changes, and the guardrail still refuses 43 golds (35 after repair) and
catches the same generated bad answers, in 0.57 to 0.59 → 0.54 to 0.57 ms a gold
(interleaved A/B, three runs each; one more run at 0.86 under machine load).

English bench, 15 Oct: more shaping rules from the misses left in the same 11
stored runs, each a mark, capital or line the speaker set and the model lost.
After a spoken "colon" the next word stays lowercase as said ("Priya said colon
kick off" Priya said: kick off), but for a question; when the speaker says their
marks ("comma") but no colon, a colon the model added becomes the comma they
said there or goes (`payments: 1042` payments, 1042; `says: show` says show). A
capital stays on "I", on a word said with one, and on any word of a speech said
all in lowercase, where it could be a name. A name kept before its own address
goes (`Noor at noor@silverline.school`), unless the speaker said it twice, and
an address's name the speaker said in lowercase is written so (`UX@` ux@). A
line the speaker ended with a colon keeps it (`Budget note.` Budget note:).
Items said after "bullet" keep their words: an item split mid-way joins back (`-
Confirm pricing`, `- Lock`) and a leading "and" stays. A first item glued after
a comma (`Anya, 1. Keep`) goes on its own line, a size after a number is in
capitals (`2 mb` 2 MB), and a list's lead the model put on its own line runs on
after the sentence before it when the speaker said no line break. Replayed
without a model on the 11 runs (`bench.md`): 47 rows newly exact, none worse; no
gold answer of the corpus (11,916) changes; the guardrail refuses the same 43
golds (35 after repair) and catches the same generated bad answers; `shaped()`
21.8 to 23.1 → 18.7 to 20.5 µs a call (interleaved A/B, best of 7, two runs),
since the address rules now skip a text with no address. Rejected for changing a
gold answer: digits for "two cups" (`suite-growth-en-00033` keeps the word),
digits where the speech had them (`formatting-597` to `-599`), a said single
line break kept over the model's blank line (`formatting-172` keeps it), the
said comma kept (75 golds drop it in a correction) and every said mark made
exact (`formatting-285` keeps "; however,").

Also 15 Oct, from the R7 sample (`bench.md`): a speech that ends on a question
tag ("we are still on for lunch aren't we") keeps its subject. The lone-subject
rule (14 Oct) read the "we" after "aren't" as a word the speaker trailed off on
and dropped it with the "?" (`aren't.`); a negative contraction before the
subject now ends a sentence on it, as "is" and "do" already did
(`ENDS_ON_SUBJECT` in `ai/guardrail/respell.py`). Every stored fast run replayed
through today's server path changes in 20 rows across the 22 runs, all newly
exact (with the shaping rules above); judged as a model's answer, no gold answer
changes except that row's own gold, which the guardrail had cut and now keeps;
refusals and catches are the same, in 0.58 to 0.60 → 0.52 ms a gold (interleaved
A/B on a quiet machine; 1.06 → 1.00 to 1.06 under load).

Round b1 (11 Oct), on the corpus's 9,186 first expected answers (scratch
harness, before → after): 2 → 2 refused, generated bad answers caught 99.50 % →
99.50 %, and 264 → 265 µs a call, within the run-to-run noise of about 3 µs.

Round 2 (11 Oct): of 136 hand probes from the round-2 grades, right answers
accepted rose from 3 of 80 to 74 of 80 and wrong answers refused from 0 of 56 to
53 of 56; against the scratch harness's generated bad answers 98.66 % → 98.99 %
were caught overall, with 58 more three-word drops, 44 more qualifier drops, 23
more last-sentence and 20 more middle-sentence drops caught and none newly
missed. Speed on the whole corpus's gold: 133.7 to 135.4 µs a call before, 133.1
to 133.2 µs after (best of 5): an answer whose words match the speech skips the
alignment search.

Round 1 (11 Oct) added the content-run, kept-correction, question, false-list,
dropped-item and meridiem checks, paired spoken marks and the wider correction
kinds above. On every corpus row, with the scratch harness's generated bad
answers (before → after, same corpus): the last sentence dropped 77.2 % → 97.5 %
caught, a middle sentence dropped 67.7 % → 94.1 %, three content words dropped
54.6 % → 94.9 %, a list item dropped 39.7 % → 99.5 %, a qualifier dropped 70.3 %
→ 80.6 %, "am" and "pm" swapped 49.5 % → 88.1 %, a term misspelled 96.6 % → 99.6
%, and unheard words, unrelated words, invented clauses, changed numbers and
replies unchanged (99.8 % to 100 %); 88.83 % → 98.58 % overall. Speed on the
whole corpus's gold: 140.6 to 142.3 µs a call before, 139.9 to 140.2 µs after
(ABBA, best of 5): the check that re-read every sentence for a dropped one is
gone, the new checks run only on removed runs or when a cue, a list or a mark
word is there.
