# Bench

`just bench [SPEC ...]` runs the corpus sample through the server's own
`polish()`, with the same prompt, guardrail and correction, and scores each
engine against the gates. A SPEC is the polish model, `provider:model`, which
then corrects its own refused answers, or `polish+correction`, where the
correction is another `provider:model` or `none` for no retry (15 Oct 2026: one
model polishes every chunk; before, a SPEC was `small+big`, the two tiers).
`--limit N` takes the first N items of
each slice for a quick look. `--per-slice N` takes the same seeded N items from
each slice on every run, so engines and lanes compare like with like.
`--per-slice 40` is the dev sample to tune on. `--heldout N` takes N seeded
items per slice from the rows outside that dev sample (a chunked item is the
same row as its backtracking item), so a held-out run checks what tuning on dev
did without ever scoring a dev row. `--failures PATH` writes each item that
missed a gate as a JSON line, with the output, the expected outputs and what it
missed. `--split dev` or `--split heldout` keeps only that split before any
sampling: a v2 row by its own `split` field, an older row by the `--per-slice
40` dev sample (dev) or the rows outside it (held-out). `--per-slice N` then
samples within the split. It needs the providers' keys in `.env`. An engine
without a key is reported as "cannot run" and skipped.

Each item gets a `deadline_ms` of 20,000, what the app gives the last chunk from
when it is sent (accuracy over speed, founder, 11 Oct) (`LAST_MS` in the app's
`dictation.rs`), since a short hold is one last chunk. An item the providers
don't answer in time is scored as the text sent, as the app would type it, and
the report prints that share as "timed out" next to p50 and p95.

## Knobs, 15 Oct 2026

Each knob sets what the server's environment sets (`providers.md`), so a run
measures one server setup. The run prints its knobs before the results.

| Flag                               | Server setting                | Default      | Does                                                                                                                       |
| ---------------------------------- | ----------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------- |
| SPEC                               | `SNAPBAK_POLISH`              |              | the polish model                                                                                                           |
| `+correction` in the SPEC          | `SNAPBAK_CORRECTION`          | the polish model | the model that corrects a refused answer once, at medium effort; `none` turns the retry off                                |
| `--temperature T`                  | `SNAPBAK_POLISH_TEMPERATURE`  | 0            | the polish model's temperature, with seed 7, on models that take one (`providers.md`); others ignore it                    |
| `--effort E`                       | `SNAPBAK_POLISH_EFFORT`       | `none`       | the polish model's `reasoning_effort` on models that reason; others ignore it                                              |
| `--examples all\|per_module\|none` | `SNAPBAK_EXAMPLES_POLICY`     | `all`        | the worked examples before the request (`api.md`)                                                                          |
| `--features PATH`                  | the request's `features`      | every module | a JSONL file of `{"id": ..., "features": [...]}` lines; each row gets its id's features, a row not in the file gets them all |

The polish model is also the fallback that joins a slow answer or correction, as
the server's `SNAPBAK_FALLBACK` is set in `.env.example`. A chunked twin keeps
its row's id, so it gets the row's features. Every OpenAI call is billed at the
priority estimate (`billed()`), as the server sends it on the priority tier.

The report's routes are `polish` (the polish model answered), `paragraphs` (one
answer per paragraph said) and `corrected` (an answer after a refused one); the
latency line prints the corrected share and the money line `polish` and
`correction` $ per 1,000 dictations. Before 15 Oct the routes were `small`,
`big reasoned` and `big plain`.

## Spend ledger and budget

`--ledger PATH` appends one JSON line per run to PATH (created if missing, never
truncated): `{"usd", "items", "spec", "note"}`, where `usd` is what the run
spent, `items` the items it sent, `spec` its specs and `note` `ok`, `budget
reached` or `crashed: <error>`. The line is written even when the run stops
early or crashes. `--budget-usd USD` caps the ledger's total: before each item
the bench checks the ledger's total at start plus this run's spend so far, and
once that reaches the budget it sends nothing more, scores only the items that
finished, prints `budget reached: $X of $Y` and exits 1. Every run prints `$
this run: X; ledger total: Y` at the end.

Each provider call is charged as it ends, at `billed()` in `config/pricing.py`
on its real usage: input, cached input and output tokens, where OpenAI's output
count already holds the reasoning tokens. That covers the first call, the
correction and any timed-out call. A call that fails or times out reports no
usage, though the provider may have billed it, so it is charged as the dearest
call of the run so far. A 429 or 5xx asked again inside one call is not
charged: providers don't bill an answer they refused. Every run has the polish
model as its fallback, as the server does (round b5; a run with none timed a
long correction out where the server's fallback would have answered; until 15
Oct a one-model spec ran with none). With a budget set, an engine missing from
`PRICES` (other than `fake:`) is refused, since it would spend without counting.
An OpenAI call is charged at twice its list price, the priority estimate, as
from 13 Oct; for part of 15 Oct, with priority off, it was charged at list
price.

The check runs before each item, and 16 items are in flight at once, so a run
can overshoot the budget by at most the items already in flight when it is
reached: 16 items of up to four calls each (the first call, the correction, and
the fallback hedging either). Keep that margin under the money you actually
have.

The gpt-5.6-luna and gpt-6-luna prices were checked against OpenAI's Standard
short-context table on 11 Oct 2026 and match `PRICES`, so the ledger uses them
as they are. The table's long-context rows cost about twice as much; the ledger
prices short context, which a bench prompt of a few thousand tokens is. The
calibration smoke (`--per-slice 2`, both lunas, 11 Oct) sent 78 items for
$0.0256, about $0.00033 an item, so $40 buys roughly 120,000 items and the
in-flight overshoot (16 items) is about half a cent on average.

## The corpus

`bench/cleanup/*.jsonl.gz` holds one slice per file. Each line is `{kind, said,
dictionary, expected: [...]}`; `expected` lists one to three equally right
outputs. Rows carry no language: Snapbak is English only.

English only, 14 Oct 2026: the 19 files of the two other languages and
`language-kept` were deleted, and the rest filtered by
`.audit/english_corpus.py` (git ignores `.audit/`). A row tagged with another
language stays only when it is plain English (83 rows, listed for review in
`.audit/english-only-corpus-review.jsonl`); a row with another script or naming
another language goes; every row loses its `language` field; kinds and notes
lose their examples in other languages. English Indian words and names ("lakh",
"crore", "sangeet", "Priya") stay.

Hindi and Hinglish were dropped on 14 Oct 2026; rounds before measured them.
Two Hinglish rows left in `grammar.jsonl.gz` (`grammar-103`, `grammar-316`) went
the same day, after a scan of every row for Devanagari and for the app's
Roman-Hindi word list (names and dishes such as "Priya" or "gulab jamun" stay).

A row with `app_style` is sent with that style, and keeps its answer in the
plain form the guardrail judges ("- ", "1. "); the bench expects it as the
server writes lists for that style (`in_style` in `ai/guardrail/shape.py`).
`platform.jsonl.gz` gives every style a list, two counted and two announced (12
Oct 2026: every row exact over two live runs).

| File                    | Slice                                                |
| ----------------------- | ---------------------------------------------------- |
| `backtracking.jsonl.gz` | corrections and trap cases, 504 items                |
| `dictionary.jsonl.gz`   | protected terms, 500 items                           |
| `formatting.jsonl.gz`   | punctuation, lists, numbers, addresses, 553          |
| `grammar.jsonl.gz`      | grammar slips and correct sentences, 466             |
| `platform.jsonl.gz`     | lists per app style, 4                               |
| `founder.jsonl.gz`      | the founder's reported dictations, 18                |
| `v2-*.jsonl.gz`         | the v2 corpus (below)                                |

`formatting.jsonl.gz` ends with hand-written list traps (kind "trap list
words"): prose that sounds like a list, such as ordinals ("first in line and
second in the race"), counts ("two people, one from sales and one from
support"), "first ... then" and a plain "bullet" ("the bullet train"). Each
keeps its words and stays prose; `gpt-5.5` checked every one. Hand-written on 11
Oct: "cue-led bullet list" rows (steps joined by "then" after words that
announce them, or three after "do one thing", become a bulleted list), "trap
list words sequence without a cue" rows (steps with no announcement, or only two
after "do one thing", stay one sentence), "digit as said or as its word" rows (a
small digit Whisper wrote, "do 1 thing", may stay or become its word; both are
right) and "trap digit stays a digit" rows.

`founder.jsonl.gz` holds every dictation the founder reported on 10 and 11 Oct
that is English, as the app sent it, with the answer the founder asked for, and
the traps the founder decided (two steps after "do one thing" stay a sentence,
"see amy no emily" becomes "See Emily."). Each row's `note` names the report or
decision, and its `prior` the text before the piece (a piece cut after a list
item, "and How is it going to show me the data?"). Rows `founder-019` to
`founder-023` are the founder's inbox reports 001 to 005 of 11 Oct, each with
its note quoting the report: an announcement and separate statements that must
be a bulleted list, with two dictionary sound-alikes ("Procall" for "Procol"); a
long dictation that must keep its requested numbered list and never skip the AI;
a run-on counted list with a dangling final "But"; a run-on that needs grammar
and punctuation everywhere; and "check if Fear is walking", which context
repairs to "check if AI is working". `founder-variants.jsonl.gz` holds generic
English variants of each report (23 rows: other words and domains, short and
long, each pattern with its traps), in the v2 row format, each note naming its
report. Founder rows and their variants must pass.

`suite-growth.jsonl.gz` holds the generated rows the corpus writer verified and
dropped in the founder inbox as `gen-r*.jsonl` or `gen-b*.jsonl`, in the v2 row
format, 2,744 English rows from rounds 1 to b6 (11 and 12 Oct; 832 dev, 1,912
held-out): word fixes in context, grammar, punctuation, formatting, corrections,
dictionary terms and their sound-alikes, traps, reply bait and lengths.
`gen_corpus_v2.py` merges a build directory named `suite-growth…` into it, with
the same duplicate drops and split rule as the other areas. Each note names its
inbox file and source row. Each intake dropped rows that shared an 8-word run or
the said text with a corpus row, and held back rows whose gold the guardrail
refused that day (mostly near-sound repairs it read as unheard words, such as
"ten your" for "tenure" or "defiantly" for "definitely", tense fixes and long
rows with many edits), so the 99.5 % gold gate holds; they join once the
guardrail accepts them. A hand read wrote numbers before a unit as digits in
every answer (rule 4: "5 years", "2 weeks") and kept terms' exact spellings. The
rows were written by hand and checked by lint, dedupe and a read-through; their
second-model check still waits for OpenAI credits. The replay leaves these rows
out of its month (`replay.md`).

The bench also derives a chunked slice from every slice whose name ends in
"backtracking" (`backtracking-chunked`, `v2-backtracking-chunked`): each
correction cut before its first cue, so the part before goes in as `prior`
(after the row's own prior) and the cue and correction as `text`. That checks
`revision`. A multi-paragraph `v2-long` row also runs as `v2-long-chunked`: for
each written paragraph after the first (up to four), the written paragraphs
before it as `prior` and the said words after them as `text`.

Kinds that start with "trap" must come back with their words unchanged.

List items in every expected output start with a capital and have no full stop,
as the prompt and the app's offline list rules write them. Tests also hold every
row to these prompt rules: no expected output is a lone list item or stops at
the colon before its list, a "Can you" or "Could you" question ends with "?",
spoken times and percentages are digits, so is a number before a unit of time or
money ("five days" is "5 days"), and money takes its symbol, never a currency
word ("₹12", not "12 rupees"). `formatting.jsonl.gz` also has rows with an
`app_style` (`email`, `code`, `chat`, `docs`) that the bench sends as `style`.
Every other row is sent as `other`. `grammar.jsonl.gz` ends with speech-to-text
homophone slips ("then" for "than", "there" for "their") and traps that use
those words correctly.

## The v2 corpus

The v2 files hold one row per line with its own fields: `id`, `feature`, `kind`,
`said`, optional `prior` and `continues`, `dictionary`, `expected` (one to three
equally right answers; with a prior, the whole text after the piece), `length`,
`fixes` (`{heard, written}` for each misheard word the gold repairs), `keeps`
(words that must survive verbatim), `tags`, `domain`, `style` (`raw` or
`whisper`), optional `app_style`, `split` and `note` (why the gold is right). A
file is `v2-{area}.jsonl.gz`, and an id is `{area}-en-NNNNN`. The length bucket
counts the words said: `1-4`, `sentence` (5 to 25 words, one sentence), `2-5`
(two to five sentences, under 80 words), `paragraph` (80 to 150) and `multi`
(200 to 400, at least two paragraphs). A prior stays at 300 words or fewer.

The split is fixed per row when it is made: dev when sha256 of the id mod 10 is
under 3, else held-out, then new held-out rows move to dev until every length
cell of a file has 5 dev rows (or all of a smaller cell). Adding rows never
moves a row already in a file (14 Oct 2026: one held-out `multi` row of
`v2-stt-nearsound` moved to dev when the filter removed a dev row of that cell).
Prompt and example tuning reads dev misses only. Held-out is read only as gate
numbers, except in a gold audit, where a held-out miss may fix a wrong gold but
never inspire a prompt example. Grades are taken on held-out, three runs
averaged.

Category generators drafted the rows: a writer model per kind, two independent
checker models that must both pass the gold, static gold checks, an 8-gram
near-duplicate pass and a hand read of at least 2 % of each slice (the note says
so). `uv run scripts/gen_corpus_v2.py BUILD_DIR ...` merges a generator's
`rows.jsonl` into the files: it drops a row that repeats the said text of a
corpus row or shares an 8-word run of said words with one, applies the hand
decisions in the script (each with its reason), drops `keeps` that are not words
found verbatim in every answer (punctuation marks, spoken amounts), and renames
a "trap" kind whose gold changes more than formatting to "fixed text with a
trap: …", so the clean-text gate counts only clean text.

The files on 14 Oct 2026:

| File                | Rows  | 1-4 / sentence / 2-5 / paragraph / multi | Dev / held-out |
| ------------------- | ----- | ---------------------------------------- | -------------- |
| `v2-stt-homophones` | 527   | 142 / 352 / 33 / 0 / 0                   | 147 / 380      |
| `v2-stt-nearsound`  | 284   | 23 / 139 / 90 / 26 / 6                   | 87 / 197       |
| `v2-grammar`        | 741   | 117 / 272 / 204 / 127 / 21               | 239 / 502      |
| `v2-punctuation`    | 642   | 66 / 160 / 230 / 127 / 59                | 189 / 453      |
| `v2-lists`          | 351   | 10 / 131 / 169 / 41 / 0                  | 112 / 239      |
| `v2-numbers`        | 350   | 52 / 158 / 88 / 35 / 17                  | 117 / 233      |
| `v2-addresses`      | 259   | 46 / 142 / 61 / 10 / 0                   | 83 / 176       |
| `v2-backtracking`   | 473   | 28 / 176 / 117 / 97 / 55                 | 152 / 321      |
| `v2-dictionary`     | 524   | 88 / 290 / 100 / 46 / 0                  | 137 / 387      |
| `v2-traps`          | 391   | 70 / 168 / 113 / 40 / 0                  | 127 / 264      |
| `v2-long`           | 269   | 0 / 0 / 0 / 136 / 133                    | 84 / 185       |
| `founder-variants`  | 23    | 1 / 8 / 13 / 1 / 0                       | 12 / 11        |
| `suite-growth`      | 2,744 | 244 / 2,132 / 325 / 41 / 2               | 832 / 1,912    |

The OpenAI account ran out of credits while the generators ran, so most rows
missed the two-model check and were read by hand instead (each note says how it
was checked). A test holds each area to 90 % of its target (homophones 550,
near-sounds 300, grammar 750, punctuation 600, lists 350, numbers 350, addresses
250, backtracking 500, dictionary 500, traps 400, long 250).

On 14 Oct 2026, 2,906 English rows written by hand by Claude agents (no OpenAI
call) filled the areas that were short: homophones 232 → 527, grammar 42 → 741,
lists 59 → 351, addresses 23 → 259, backtracking 273 → 473, and the new
`v2-dictionary` (524), `v2-traps` (391) and `v2-long` (269). Every row passed
the static gold checks above, the merge's dedupe (no said text repeated and no
8-word run shared with the corpus, the prompt examples or another new row), the
guardrail on its first answer (judged as the server judges it), the
dropped-negation check, and two reads by its writer; a sample of each file was
read again by a reviewer. Each `v2-long` multi row cuts into `v2-long-chunked`
pieces at every spoken "new paragraph". Every `v2-traps` row keeps all its
words. The notes say "Hand-written and hand-reviewed by Claude"; the
second-model check still waits for OpenAI credits. Where the guardrail refused a
gold that the prompt's rules make right (for example "won" for "one", "he" for
"him", "runned" to "ran", "the blockers are" announcing a list), the writer
replaced the row instead of bending the gold; those are guardrail false refusals
worth a look.

Static gold checks run in `just test`, without a model: every v2 row has its
fields, a length bucket that matches the words said and a split fixed by its id;
dev and held-out share no said text; no two rows say the same text; a dictionary
term appears as often in every answer; each fix's heard words were said and its
written words are in an answer, and each keep is in every answer; a trap's
answers change only marks, case, apostrophes, spoken marks, number forms and the
spoken form of a term; no prompt example shares an 8-word run with any row; the
guardrail accepts at least 99.5 % of the v2 gold answers, judged as the server
judges them; and every gold obeys the prompt-rule tests above.

`just corpus` (`scripts/gen_corpus.py`) writes any missing slice. A writer model
(`gpt-5.4-mini`) drafts items per kind and a checker model (`gpt-5.5`) keeps
only items that follow the rules. It repeats until each slice has 100 checked
items, spread evenly over the kinds. Delete a file to rebuild it.

## Scoring

An output is right when its words, lowercased and with marks stripped, equal the
closest expected output's words. A hyphen inside a word splits it, so
"read-only" matches "read only", and curly quotes read as straight ones. Of
expected outputs with equally close words, the one with the closest marks is
used. A word's marks are the opening quote, apostrophe or bracket before it and
the marks after it. Punctuation and capitalization are counted only on the words
that align between the two, and only where the expected mark or case differs
from the input, so a model that changes nothing scores 0 there; a mark the input
already had counts too when the output adds, drops or swaps it, so "The deploy,
is done?" for an already right "The deploy is done." misses two (round 1, 11
Oct). A word the input never had counts as said bare and lowercase, so it counts
only for the mark or capital it needs. A missing word costs the word gate, not
the punctuation gate too. A revision of `""` retracts the whole prior. Without a
prior, a revision counts only as the whole answer, when the text is empty, as
the server judges it. A piece that starts a list item ("- ", "3. ") goes on a
new line after the prior, as the app's `join` in `dictation.rs` puts it (round
b1). A false reject is a rejected answer whose words are right once it is put
back in the request's shape, as the server unfolds an answer to a correction it
sent with the prior's last sentence. A list item is a line that starts with a
number and a full stop ("1. ") or a bullet ("-" or "•") and a space. A list
answer passes list shape when its lines hold the same number of items as an
expected list, one per line, each numbered or bulleted as expected. A prose
answer that comes back with any list line counts as a false list. `--failures`
marks either miss as `wrong_list`, and an item whose provider failed, which is
scored as its raw input, as `failed`. A full stop after a list item is optional
(founder decision D3, 11 Oct): the bench drops one from each list line of the
output and of every answer before it scores.

Marks a careful editor writes either way are read one way before scoring and in
the exact match (round 3, 11 Oct, and later rounds): a comma before the last
"and" of a series or between clauses, a comma after a short opening phrase of
time or place ("On Monday, I met"), a semicolon or a full stop between two
clauses ("; then" and ". Then"), "a.m." or "AM" for "am", a comma before
"because", after a sentence's opening "so", "okay", "ok", "well", "also",
"finally", "actually", "anyway", "hi", "hello", "hey" or "sorry", before "but"
and a sequence's "then", after a reporting verb ("said", "asked", "told me",
"wrote"), a colon or a comma after "Do one thing", a full stop or semicolon
before "But", "And", "So" or "Then", a title's full stop ("Ms." Ms), a hyphen
read away inside a compound ("sign-up" signup), a Latin accent ("entrée"
entree), quote marks around a few words no answer quotes, and an email's
greeting and sign-off on their own lines or run on. A "?" or "!" still counts,
and sentences stay judged where they end ("I called Sam. He said yes." is not "I
called Sam, he said yes."). A comma that is needed ("Hey Sam, ship it") still
counts. A dictionary term inside an email, a web address or a path counts in
either case. A miss in `--failures` lists the answers the guardrail refused,
each with its verdict (`refused`), so a grader can read what the model wrote. A
real word change still fails ("check list" is not "checklist").

| Gate                  | Bar     | Over                                                                               |
| --------------------- | ------- | ---------------------------------------------------------------------------------- |
| backtracking          | ≥95 %   | non-trap backtracking items, chunked included                                      |
| punctuation           | ≥95 %   | aligned words whose marks had to change                                            |
| grammar               | ≥95 %   | non-trap grammar items                                                             |
| grammar kept          | ≥99 %   | trap grammar items: correct text left correct, word for word                       |
| capitalization        | ≥98 %   | aligned words whose case had to change                                             |
| clean text unchanged  | ≥99 %   | trap items                                                                         |
| dictionary terms kept | ≥99 %   | items whose expected output holds a term                                           |
| false rejects         | ≤3 %    | items where the guardrail refused a right answer                                   |
| list shape            | ≥95 %   | items whose expected output is a list                                              |
| false lists           | ≤1 %    | items whose expected output is prose                                               |
| misheard words fixed  | ≥95 %   | each fix: its written words where the answer has them                              |
| words not over-fixed  | ≥99 %   | each keep where the answer has it, and trap items with no word outside the answers |
| sentence correctness  | ≥95 %   | items equal to an answer in every word, mark and capital                           |
| dictionary strict     | ≥99.5 % | items whose answer holds a term: each term as often, spelled exactly               |
| term twins kept       | ≥99 %   | items with a term no answer holds: the output leaves it out                        |
| spoken forms to terms | ≥97 %   | items whose answer holds a term never said as spelled                              |
| no unheard content    | ≤0.5 %  | items whose output holds a word neither said nor in an answer                      |
| no reply              | ≥99.5 % | reply-bait items (tag `assistant-bait`) with no unheard word                       |
| numbers right         | ≥97 %   | words right on `v2-numbers` items                                                  |
| length parity         | ≥95 %   | words right in the weaker of the paragraph and multi buckets                       |
| paragraphs kept       | ≥99 %   | items whose answer has a paragraph break: as many breaks as an answer              |

Sentence correctness holds 93 % in every length bucket of 30 items or more, and
length parity fails when it is more than 2 points under the sentence bucket. A
fix counts where the answer changed what was said, so "test" elsewhere in the
sentence does not count. A digit in the output counts as said when the speaker
said a number.

The report prints each gate overall and per length bucket, lists each bar a run
misses, then each slice's gates on a line. It ends with a grade per area. A run
whose provider errors (not timeouts) pass 5 % of its items prints "NOT RUN" and
grades nothing, so a run out of credits is never read as the model typing the
text as said. It first names the items whose first answer the guardrail refuses
("guardrail refuses N gold answers"): such a row stays in the bench, flagged, so
the bench still measures the class of fix the guardrail can't yet accept (round
3, 11 Oct).

## Grades

Each gate grades 0 to 10: at or over its bar, 9.5 + 0.5 x (value - bar) / (100 -
bar), or for a lower-is-better gate 9.5 + 0.5 x (bar - value) / bar; under its
bar, 9.5 - 0.5 x the points short (5 points short is 7.0), never below 0. An
area grades as its weakest gate: Misheard words (misheard words fixed, words not
over-fixed), Grammar (grammar, grammar kept), Punctuation and casing, Sentence
correctness (sentence correctness, length parity), Formatting (list shape, false
lists, numbers right, paragraphs kept), Corrections (backtracking), Dictionary
(dictionary terms kept, dictionary strict, term twins kept, spoken forms to
terms) and Safety (clean text unchanged, no unheard content, no reply, false
rejects). The overall grade is the weakest area. A reviewer who is not the tuner
then reads every held-out miss of an area and 30 seeded passes; any blunder (an
invented word, a changed meaning, a flipped negation or number, a translation, a
lost or changed term, a reply, an ungrammatical sentence) caps the area at 9.0
until it is fixed and run again. A miss whose gold is wrong is fixed in the
corpus, with a second model's check and a note, never by moving a bar. Done is
every area at 9.5 or more on held-out.

The report also prints p50 and p95 server time, then the same for each route
with its mean output and reasoning tokens per chunk: `small`, `big reasoned`,
`big plain`, `paragraphs` (a chunk split where a paragraph was said) and
`corrected` (an answer after a refused one). It also prints the share of chunks
that ended on the big tier, US$ per 1,000 dictations for each tier, priced from
`PRICES` and `CACHED`, and the share of prompt tokens the provider served from
its cache.

## R7 one-model round, 15 Oct 2026

The round that picked the one polish model (`escalation.md`). The full R7 set,
687 English rows. The baseline is today's setup, the `fast-e5` and `fast-e4`
recorded outputs replayed through today's server path at $0. Every candidate ran
with the rules gate's per-row features (`--features`) and its corrections on
`gpt-6-luna` at medium (the default big model). The gates are the overall
column.

| gate                   | today e5               | today e4 | winner run 1 (default tier) | winner run 2 (default) | winner priority |
| ---------------------- | ---------------------- | -------- | --------------------------- | ---------------------- | --------------- |
| backtracking           | 91.2                   | 91.2     | 91.2                        | 91.2                   | 93.4            |
| punctuation            | 96.2                   | 95.2     | 96.6                        | 95.8                   | 95.9            |
| grammar                | 92.3                   | 92.3     | 92.3                        | 92.3                   | 92.3            |
| capitalization         | 99.6                   | 98.6     | 99.7                        | 99.5                   | 99.6            |
| clean text unchanged   | 96.7                   | 97.8     | 97.8                        | 98.3                   | 97.8            |
| dictionary terms kept  | 99.1                   | 100.0    | 98.2                        | 99.1                   | 97.4            |
| list shape             | 100.0                  | 100.0    | 95.8                        | 100.0                  | 100.0           |
| misheard words fixed   | 97.0                   | 93.9     | 97.0                        | 97.0                   | 97.0            |
| sentence correctness   | 92.7                   | 92.4     | 92.4                        | 91.4                   | 91.8            |
| dictionary strict      | 99.1                   | 100.0    | 97.4                        | 98.2                   | 97.4            |
| spoken forms to terms  | 97.9                   | 100.0    | 95.7                        | 97.9                   | 93.6            |
| no unheard content     | 2.3                    | 1.3      | 1.7                         | 0.6                    | 1.0             |
| numbers right          | 82.2                   | 91.1     | 84.4                        | 88.9                   | 88.9            |
| length parity          | 63.2                   | 57.9     | 73.7                        | 73.7                   | 68.4            |
| paragraphs kept        | 93.5                   | 100.0    | 96.8                        | 100.0                  | 96.8            |
| polish p50 / p95 ms    | 1382 / 2262 (priority) | –        | 1961 / 3387                 | 1986 / 4176            | 1579 / 2480     |
| $ per 1,000 dictations | 1.99 (priority ×2)     | –        | 0.59                        | 0.61                   | 0.72            |
| prompt cached          | 66 %                   | –        | 41 %                        | 41 %                   | 70 %            |

The winner is `openai:gpt-6-luna` at reasoning effort none, the request's
features, examples policy `all`, on OpenAI's priority tier. Exact rows (the
answer equals a gold answer): today 600, the winner on priority 593, on the
default tier 597 and 590.

The other candidates ran the same setup, on the default tier unless noted
(sentence correctness, p50 / p95 ms, $ per 1,000 dictations):

- `gpt-4.1-nano` at temperature 0, 0.2 and 0.4: 72.8, 71.5 and 72.5;
  backtracking 55 to 58; p95 5.1 to 5.8 s; $0.29 to $0.38. Fails.
- `gpt-4.1-mini` at temperature 0: 82.4, backtracking 71.4; 1187 / 2484; $1.18.
  Fails.
- `gpt-5.4-nano` at effort none (it refuses minimal): 66.8; $0.50. Fails.
- `gpt-5.4-mini` at effort none: 85.4, backtracking 84.6; 1407 / 2580; $1.37.
  Fails.
- `gpt-5.6-luna` at effort none, every module, priority: 90.2; 1448 / 2098;
  $2.45. Fails, and costs more.
- `gpt-6-luna` at none with `per_module` examples: default tier 91.1, 1771 /
  3297, $0.12; priority 90.0, 1391 / 2252, $0.26. Exact rows 580 to 590 against
  today's 600: about 1.5 points lower, outside the noise. Flagged modules
  showing all their tagged examples: 90.0, 1363 / 2214, $0.31.
- `gpt-6-luna` at none with every module and every example (one fixed prompt),
  priority: 92.1, 1593 / 2408, $1.22.
- Examples `none`: 86.5, $0.07. Fails. Examples after the shared rules, with the
  modules in a second system turn: 90.0, $1.31. Worse.

What the round showed:

- No small model holds backtracking or the dictionary gates.
- `gpt-6-luna` without reasoning keeps accuracy within today's run-to-run noise:
  today's own two runs differ by up to 9 points on the 45-row gates.
- The latency is the model's on its tier, not the prompt's size: with no
  examples, 98.5 % cached, the default tier still took 1.76 s at p50.
- Priority restores the speed, and with the shared cache key
  (`providers.md`) its calls now hit the cache: 70 %.
- Against the old runs from 14 Oct, the cost fell 64 % and the latency rose
  14 % at p50 and 10 % at p95. Measured at the same time (below), the new
  setup is the faster one.
- For −87 % cost and about 1.5 points less accuracy, set
  `SNAPBAK_EXAMPLES_POLICY=per_module`.

### Same-time A/B, 15 Oct

OpenAI's latency moves with the hour, so the old and new setups ran back to
back on the same 150 R7 rows (seed 15), both on priority. "Old" is
`gpt-5.6-luna` at medium reasoning with every module and example, the old small
tier that answered most chunks.

| Setup                                | p50 ms | p90 ms | p95 ms | Exact rows | $ / 1,000 |
| ------------------------------------ | ------ | ------ | ------ | ---------- | --------- |
| old: `gpt-5.6-luna`, medium          | 1920   | 2935   | 3464   | 136 / 150  | 2.65      |
| new: `gpt-6-luna`, none              | 1550   | 2107   | 2292   | 130 / 150  | 0.28      |
| new, every call sent twice at once   | 1467   | 1890   | 2160   | -          | 0.56      |

The new setup is 19 % faster at p50 and 34 % at p95 at the same moment. Sending
every call twice was not kept: 5 to 6 % for twice the price, where the app's
polish-ahead in a pause saves more (`DICTATION-AI.md` in the app). Sending the
second copy later gains nothing, since every call takes about 1.1 s at least.

## English only, 14 Oct 2026

The English bench for this change: backtracking, its chunked twins, dictionary,
formatting and grammar at `--per-slice 100`, platform 4, founder 18 and
founder-variants 23 (545 rows; English rows of the filtered corpus, the same
rows before and after). Before: three runs on the 14 Oct work-in-progress tree;
after: two runs on the final tree, their outputs re-scored offline with
`digits_as_said`. One value per run, or the range of the runs:

| Gate                  | Before       | After       |
| --------------------- | ------------ | ----------- |
| sentence correctness  | 93.9 to 94.5 | 94.7 / 93.9 |
| backtracking          | 98.3 to 98.9 | 99.4 / 98.3 |
| grammar               | 88.9 to 92.6 | 92.6 / 94.4 |
| punctuation           | 96.8 to 97.7 | 97.3 / 97.6 |
| capitalization        | 99.8 to 100  | 99.8 / 99.7 |
| list shape            | 100          | 100 / 100   |
| clean text unchanged  | 97.4 to 98.2 | 97.4 / 98.2 |
| words not over-fixed  | 99.1         | 98.3 / 99.1 |
| no unheard content    | 1.3 to 1.5   | 0.9 / 1.5   |
| dictionary terms kept | 100          | 100 / 100   |
| false rejects         | 0            | 0 / 0       |
| p50 ms                | 1561 to 1705 | 1499 to 1602 |
| p95 ms                | 2573 to 2964 | 2447 to 2656 |

By route, sentence correctness: chunks with a cue 93.7 to 94.7 → 96.3 / 95.2,
plain chunks 93.5 to 94.7 → 93.5 / 93.0. Dropping the 40 worked examples in the
other languages first cost the plain route about 1.7 points (92.1 to 92.4 on 316
shared rows in three runs, against 93.4 to 94.3 for the old prompt run the same
day, so not drift in the API); 26 generic English examples teaching the same
skills brought it back to 94.0 and 93.7 (`api.md`). The per-route effort sweep
is in `escalation.md`. The runs went through the ledger: $41.95 → $51.21 of the
$51.17 cap (the last sweep run stopped at the cap, $0.04 in flight).

## Polish features, offline, 14 Oct 2026

With no OpenAI credit left, the day's stored English runs (545 rows each:
three before the English-only change, three after it, the two final runs and
three of the effort sweep) were re-scored with the current scorer and gold, and
their outputs replayed through the new shaping rules (`api.md`, English bench,
14 Oct), with no model call. Rows exactly right per feature, the final runs
before → after the rules:

| Feature                               | Rows | Final run 1 | Final run 2 |
| ------------------------------------- | ---- | ----------- | ----------- |
| Corrections (backtracking, chunked)   | 200  | 192 → 192   | 190 → 190   |
| Formatting                            | 100  | 88 → 93     | 89 → 91     |
| Grammar                               | 99   | 95 → 95     | 96 → 96     |
| Dictionary                            | 100  | 97 → 97     | 96 → 96     |

Over all 11 runs, 20 rows became exact and none got worse. The cue set grew by
"delete that" and "no, don't do this" (or "that"); no corpus row says either,
so no stored row changes route, and the corpus has no rows yet that measure
them.

## Polish features, offline, 15 Oct 2026

The same 11 stored runs, replayed through the 15 Oct shaping rules (`api.md`,
English bench, 15 Oct) against the 14 Oct shaping, with no model call
(`~/.snapbak-bench/spend.jsonl` unchanged at 29 lines). Mean rows exactly right
per feature over the 11 runs, and the final runs:

| Feature                             | Rows | Mean of 11      | Final run 1 | Final run 2 |
| ----------------------------------- | ---- | --------------- | ----------- | ----------- |
| Corrections (backtracking, chunked) | 200  | 189.18 → 189.18 | 192 → 192   | 190 → 190   |
| Formatting                          | 100  | 91.36 → 95.27   | 93 → 96     | 91 → 96     |
| Grammar                             | 99   | 91.55 → 91.55   | 95 → 95     | 96 → 96     |
| Dictionary                          | 100  | 95.45 → 95.45   | 97 → 97     | 96 → 96     |

47 rows became exact (formatting-038 in 10 runs, formatting-226 and
formatting-145 in 7 each, formatting-241 and formatting-231 in 3 each) and none
got worse. Grammar's misses left are word choices (`were` was, `needs change`
needs to be changed), which no rule can settle from the speech alone. The mean
of 11 counts the low-effort sweep run, which stopped at the cap with 507 of 545
rows (66 grammar rows).

### R7 sample, offline

R7 is the frozen dev sample of `fast_bench.py` (seed 7, 1,501 rows); its last
measured run is fast-e5 (14 Oct). Of its rows, 687 are English and still in
today's corpus (803 Hindi or Hinglish rows are gone, 4 English rows left the
corpus, and 7 rows tagged Hinglish are left out). Every column is scored with
today's scorer and gold, with no model call:

- (a) fast-e5's stored outputs.
- (e) fast-e5's recorded model outputs replayed through today's server path:
  `bench.run` with a provider that answers each call with the recorded output,
  in order (685 rows make the same calls, 2 make fewer, none needs an unrecorded
  call).

| Gate                  | (a) R7      | (e) today | Rows     |
| --------------------- | ----------- | --------- | -------- |
| misheard words fixed  | 97.0        | 97.0      | 99 fixes |
| dictionary terms kept | 99.1        | 99.1      |          |
| dictionary strict     | 99.1        | 99.1      |          |
| punctuation           | 96.3        | 96.2      |          |
| capitalization        | 99.4        | 99.6      |          |
| grammar               | 92.3        | 92.3      |          |
| list shape            | 95.8        | 100.0     |          |
| false lists           | 0.5         | 0.5       |          |
| corrections           | 91.2        | 91.2      | 91       |
| numbers right         | 80.0        | 82.2      | 45       |
| no reply              | 100.0       | 100.0     |          |
| no unheard content    | 2.3         | 2.3       |          |
| sentence correctness  | 92.4        | 92.7      | 687      |
| length parity         | 63.2        | 63.2      | 19 multi |
| polish p50 / p95 ms   | 1382 / 2262 | -         |          |

The `out-*` runs overlap R7 in 65 rows only (none in numbers, length or misheard
words): sentence correctness there is 98.5 for fast-e5, 96.4 for the three runs
before English-only (mean), 97.7 for the two final runs and 98.5 for the final
runs' text through today's shaping; p50 / p95 1385 / 1923, 1637 / 2860 and 1562
/ 2294 ms. The founder set's 23 English rows (fast-e5 ran them twice): sentence
correctness 93.5 → 97.8 replayed through today's path, misheard words 92.9 →
92.9.

Of the rows still missing a weak gate after the replay, each was checked against
the model's own answers: a miss is deterministic when some answer the model gave
was right and the server's text is not. One was (`suite-growth-en-02231`, the
question tag in `api.md`), now fixed. Left: corrections 8 misses (7 model, 1
gold-inconsistent), numbers right 8 (model), length parity 16 (model), misheard
words 3 (model), sentence correctness 50 (46 model, 4 gold-inconsistent: a count
written as digits in one gold and as words in others, `app store`, and a gold
that puts serial commas in one list and not the next). Digits for counts of 10
and over, and for "nights" and "feet", were tried and rejected: each changes
gold answers that keep the word.

### R7 replay, shaping rules, 15 Oct

The shipped config's four stored R7 runs (`gpt-6-luna`, effort none, all
examples, the rules features: `wave2/luna-all-priority`, `wave1/luna-none`,
`wave2/luna-all`, `wave2/luna-full-priority`, 687 rows each, 672 replayed) and
the two old baselines (`fast-e5`, `fast-e4-merged`), replayed through the server
path before and after the R7 shaping rules (`api.md`), at $0. Gates that moved
(before → after); every other gate is equal:

| gate                  | all-priority | none        | all         | full-priority | mean          | e5          | e4          |
| --------------------- | ------------ | ----------- | ----------- | ------------- | ------------- | ----------- | ----------- |
| sentence correctness  | 91.7 → 92.4  | 92.4 → 93.3 | 91.2 → 92.0 | 91.8 → 92.3   | 91.8 → 92.5   | 92.7 → 93.0 | 92.4 → 92.7 |
| backtracking          | 92.1 → 93.4  | 90.8 → 92.1 | 89.5 → 90.8 | 89.5 → 90.8   | 90.5 → 91.8   | 91.2        | 91.2        |
| dictionary terms kept | 97.3 → 99.1  | 98.2 → 100  | 99.1 → 100  | 96.4 → 98.2   | 97.8 → 99.3   | 99.1        | 100         |
| dictionary strict     | 97.3 → 99.1  | 97.3 → 99.1 | 98.2 → 99.1 | 96.4 → 98.2   | 97.3 → 98.9   | 99.1        | 100         |
| spoken forms to terms | 93.3 → 97.8  | 95.6 → 100  | 97.8 → 100  | 91.1 → 95.6   | 94.5 → 98.4   | 97.9        | 100         |
| paragraphs kept       | 96.7 → 100   | 96.7 → 100  | 100         | 100           | 98.4 → 100    | 93.5        | 100         |
| punctuation           | 95.1         | 96.6        | 95.0        | 94.9 → 95.0   | 95.40 → 95.43 | 96.2        | 95.2        |

Misses fall 57 → 52, 52 → 46, 59 → 54, 55 → 52, 52 → 50 and 53 → 51; across
all 39 stored runs 67 rows are newly exact and none got worse. Of the 82 rows
the four runs still miss or missed, the rest need the model: a word choice or
mark the speech doesn't settle (most), both versions kept around "let's change
that" or a lone "no" (the guardrail accepts a full restatement by design), and
three long rows whose paragraphs a replay answers out of order. Left as
gold-inconsistent: "N people" (50 golds in words, 45 in digits:
`suite-growth-en-01246`), a short fragment's last full stop
(`v2-numbers-en-00029` has one, `dictionary-180` and `grammar-344` don't), and
"p.m." (22 golds) against "pm", which the scorer reads as one but `invented()`
counts as unheard.

## Results, 10 Oct 2026

Each OpenAI model on `--per-slice 40` is in the model table of the providers
doc. No configuration passes the gates yet. The best single model,
`gpt-5.6-sol`, reaches backtracking 88.3, punctuation 89.9, grammar 80.6,
capitalization 96.1, clean text unchanged 91.1, dictionary terms kept 100 and
false rejects 5.0.

The pair `openai:gpt-5.6-luna+openai:gpt-6-luna` with the list gates, dev
(`--per-slice 40`) then held-out (`--heldout 40`): backtracking 80.3 and 80.9,
punctuation 92.8 and 90.5, grammar 80.6 and 75.9, capitalization 95.5 and 92.4,
clean text unchanged 90.8 and 93.5, dictionary terms kept 100 and 100, false
rejects 0.5 and 0, list shape 84.6 (13 rows) and 80.0 (20 rows), false lists 0
and 0. p50 1513 and 1438 ms, p95 2731 and 2203 ms.

With low reasoning effort on correction chunks (`escalation.md`), two runs each,
dev then held-out: backtracking 86.1 / 85.4 and 86.3 / 85.5, punctuation 95.5 /
92.8 and 91.1 / 90.4, grammar 79.2 / 79.2 and 70.9 / 75.9, capitalization 96.1 /
95.2 and 94.9 / 93.7, clean text unchanged 89.9 / 89.9 and 93.5 / 94.4,
dictionary terms kept 100 everywhere, false rejects 0.5 / 0.5 and 0 / 0, list
shape 100 / 92.3 and 85.0 / 85.0, false lists 0 everywhere. p95 2472 / 2476 and
2153 / 2279 ms. Putting back a word said whole that the model hyphenated
("wifi", "oncall"), on the same outputs, moves backtracking 82.5 to 83.2 on two
dev runs and 77.9 to 78.6 held-out; no other gate moves.

A gold audit of that pair's misses on both splits fixed 31 rows, each checked
against the prompt rules by `gpt-5.5`: sixteen broke rule 4 (a count under ten
written as a digit, a number before a unit of time written as a word, or money
without its symbol), and fifteen admit a second right answer, such as "make that
a taxi" keeping "home". Open: rule 4 doesn't say whether a number that names a
thing ("section two", "room 9") or a dose ("one tablet") is a word or a digit,
and the corpus does both.

The prompt of 10 Oct (small numbers stay words, spelling kept, corrections in
mid-sentence, counted lists, correction examples) moved the pair, averaged over
three dev and two or three held-out runs, from backtracking 82.0 to 89.6
(held-out 80.2 to 85.7), punctuation 93.1 to 96.0 (91.1 to 93.7), grammar 80.6
to 81.5 (76.0 to 78.9), capitalization 95.2 to 97.4 (91.5 to 94.9), clean text
unchanged 90.2 to 94.4 (90.8 to 92.6) and list shape 87.2 to 100 (90.0 to 90.0).
The round-1 merge (that prompt, reasoning on corrections, the hyphen rule and
the gold audit), three runs per split, dev then held-out: backtracking 83.2 to
89.8 and 80.9 to 87.5, punctuation 94.1 to 95.4 and 91.8 to 94.9, grammar 81.5
to 85.6 and 77.2 to 79.8, capitalization 95.8 to 97.1 and 91.7 to 96.7, clean
text unchanged 90.2 to 96.3 and 90.7 to 95.7; p95 rose from about 2.4 and 2.1 s
to 2.6 to 2.8 s. Single runs differ by up to 5 points a gate, so compare
averages. The founder's round-2 decisions (low reasoning effort on every
big-tier chunk, a 50 % cache share in the month) are in `escalation.md`.

The whole corpus, 10 Oct 2026, the default pair, `just bench` (3,101 items, 25
provider failures scored as said): backtracking 90.9, punctuation 97.0, grammar
84.9, capitalization 98.2, clean text unchanged 96.1, dictionary terms kept
99.4, false rejects 0.2, list shape 96.0, false lists 0; p50 1521 and p95 2530
ms; $0.114 small and $0.042 big per 1,000 dictations at 71.8 % cached. The whole
corpus holds the dev rows the prompt was tuned on, so it reads a little high.

## Gold audits, rounds 1 to b2 (11 Oct 2026)

The rounds that ran no model (the OpenAI account was out of credits) audited the
gold by hand, applied by logged scripts (each row's before, after and reason;
scratchpad `acc2/fix*/goldfix.log.jsonl`), each row still with at most three
answers and a trap's answers changing only formatting:

- **Round 1 (82 rows):** trap rows whose only answer was the raw input now carry
  the capital, names' capitals and end mark a careful editor adds; founder-011
  "See Amy no Emily." became "See Emily." (a lone "no" between two names takes
  the first back); backtracking-144 answers "no compete" as said; wrong
  punctuation or casing fixed (comma splices, end marks, a tag "right." that is
  a question, mixed date styles); and second right answers added ("check-in", ",
  so please", "Dr Meera", "Right-hand rule.", "24 hours' notice", the colon
  after "Do one thing" on two-step traps). v2-stt-homophones-en-00062 ("river
  sight") and -00112 ("oncall") stay as said (rules 5 and 6).
- **Round 3 (87 rows):** clearly wrong answers fixed ("then" before its own
  subject starts a sentence, a line after a greeting takes a capital, a comma
  splice, a "₹" never said); rule 4 digits first for section, page, night, room
  and chapter numbers; second right answers and better first answers (the colon
  after "do one thing", a contrast in commas); acronyms ("api", "qa", "pdf",
  "pr") also uppercase.
- **Round b1 (28 rows):** a direct "can you" question takes "?", Northwind is a
  name, "; however," for the comma after "however"; second answers for the
  retracted clause's noun in place of "it", a colon after "do one thing",
  "chapter 4", a simple past and a phone grouped with hyphens.
- **Round b2 (28 rows):** founder-022 and founder-023 carry their `fixes`
  ("ruling engine" rule engine, "Fear is walking" AI is working), and second
  answers for comma splices, "HDFC ERGO", one person named "Maya Chen", the
  nearest name a cue replaces, ordinal suffixes on dates, "The IT team" and "It
  is over here". Rescored offline on round b2's outputs, the scorer, gold and
  shaping fixes moved dev rows passing every gate from 1,113 to 1,160 of 1,501
  and founder items from 106 to 113 of 132, with no row newly missed.

## Round b2 fix: live A/B (11 Oct 2026)

Arm A is the code and prompt before the round; arm B the round's (prompt,
shaping, repairs and guardrail), on every founder row twice, the 345 dev rows
round b2 missed and 150 random dev rows (seed 11), scored with the same scorer
and gold (scratchpad `acc2/fix-b2`):

| Arm | Rows right | Words right | Founder | Random | Backtracking | Grammar | Numbers | Paragraphs |
| --- | ---------- | ----------- | ------- | ------ | ------------ | ------- | ------- | ---------- |
| A   | 53.9 %     | 77.5 %      | 84.8 %  | 140    | 72.1         | 60.0    | 62.7    | 77.3       |
| B   | 58.1 %     | 79.5 %      | 87.9 %  | 140    | 72.1         | 80.0    | 65.7    | 81.8       |

Punctuation went 80.2 → 82.9, capitalization 91.9 → 93.3, clean text unchanged
87.1 → 87.7, misheard words fixed 72.6 → 74.2, words not over-fixed 94.4 → 95.4
and sentence correctness 54.1 → 58.3. A run on the founder set alone (66 items
twice) passed 119 of 132 with no AI skipped.

## Rounds b3 to b6 (11 and 12 Oct 2026)

Each round fixed the scorer (each with a test in
`tests/unit/test_bench_scoring.py`), audited the gold from its graders' notes
(logs in `acc2/fix-b*/`) and ran a live A/B against the round's code before:

- **Round b3:** a false reject is a refused answer exact in every word and mark;
  "dictionary terms kept" counts a term as the strict gate does; an ellipsis and
  a dash are marks; "; then" scores like ", then"; a comma before "but" is
  optional; a revision keeps the break the prior had after the part it kept.
  Live A/B on 376 rows: 223 → 246 passed; the founder rows passed 62 of 66 on
  the final code.
- **Round b4:** a prose answer has the right shape when one expected answer is
  prose; a comma left by a joined "And" is optional. On 299 rows (founder rows,
  83 named rows and 150 random dev rows) arm C, the round's second rules,
  examples and `unlisted`, passed 240 against 221 and shipped; two founder
  layout rules that cost 6 rows overall were reverted.
- **Round b5:** an email's greeting on a line of its own or run on scores as one
  form; a word written more often than said and than any answer holds it is
  unheard content; the real escalation has the small model as its fallback. On
  313 rows: 232 → 248 passed, words not over-fixed 97.0 → 98.2, clean text
  unchanged 94.9 → 97.4, sentence correctness 74.1 → 79.2, p95 11,395 → 9,862
  ms. The guardrail caught 4.35 % → 86.25 % of joining words dropped at a split.
- **Round b6:** a comma after an opening "also", "finally", "actually",
  "anyway", "hi", "hello" or "hey" is optional; a sign-off after one line break
  or a blank line is one layout; quote marks around five words or fewer are
  optional when no answer quotes. No GMMCO in the corpus (its distractor rows
  name other companies). On 338 rows: 227 → 248 passed, punctuation 84.5 → 90.2,
  sentence correctness 67.5 → 73.7, backtracking 71.2 → 78.8, term twins kept
  81.8 → 100; a founder-only run passed 65 of 66. Guardrail on the corpus: gold
  refused 2 → 2, bad answers caught 99.54 % → 99.54 %, 167.1 → 165.7 µs a call.

Gold the graders called wrong stays where a corpus rule the founder set says
otherwise: a number before a unit of time is a digit (rule 4), a keep is never
changed, and a trap row keeps the speaker's words.
