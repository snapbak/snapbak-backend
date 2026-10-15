# Escalation

A chunk goes to the small model unless it is hard. `tier(text)` in
`src/snapbak_backend/ai/routing.py` is a pure function:

| Chunk                     | Tier  |
| ------------------------- | ----- |
| holds a correction cue    | big   |
| more than 150 words       | big   |
| anything else             | small |
| a command (`/v1/command`) | big   |

Hindi and Hinglish were dropped on 14 Oct 2026 (Snapbak is English only);
earlier rounds below measured them too, and only their English findings stay.

Round b4 (11 Oct, D8) tried sending English chunks of more than 40 words to the
big tier. On 60 dev rows of 41 to 150 words with no cue (one run each,
`acc2/fix-b4/ab` RC and R40), the small tier passed 34 with 50 right in words
and one AI skip; the big tier passed 35 with 51 and none. The big tier fixed
more misheard words (misheard words fixed 60.0 → 93.3) and grammar slips, but
made a false list, invented two words and changed more clean text (clean text
unchanged 87.5 → 81.2). That is within one run's noise, so the 150-word rule
stays until a larger run shows the big tier more accurate there.

## Correction cues

"no," (with the comma), "no no", "no wait", "no actually", "no sorry", "wait",
"scratch that", "strike that", "I mean", "I meant", "actually", "make that",
"make it", "rather", "sorry", "change that", "oops", "nope", "let me rephrase",
"correction", "my bad", "ignore that", "cancel that", "delete that", "no,
don't do this" and "no, don't do that" (`routing.CUES`; the last three since 14
Oct 2026). "Don't do that" without the "no" is an everyday instruction, not a
cue.

Cues match whole words in any case, so "nobody waited" and "the actuality" don't
count, nor do "await" or "piano,". A bare "no" counts only with a comma after
it, so "no problem" and "there is no data" stay on the small tier. So does an
unpunctuated correction such as "send the invoice to northwind no send it to
Tidewell": the guardrail accepts the small model's right answer, so it costs no
correction unless the answer is wrong. Some ordinary uses ("it actually works")
still go to the big tier. That costs a little and never hurts quality.

### What each trigger buys, 10 Oct 2026

On the bench's `--per-slice 40` sample, with `gpt-6-luna` small and a "sol"
model big, sending a trigger's chunks to the big tier fixed this many of them:

| Trigger                                             | Chunks | Fixed by the big tier |
| --------------------------------------------------- | ------ | --------------------- |
| "actually"                                          | 14     | 4 to 5                |
| "i mean", "rather", "change that"                   | 23     | 3                     |
| "wait", "sorry", "no,", "make that", "scratch that" | 41     | -1 to 2               |

Ordinary "actually" is 31 of the corpus's 161 chunks with that word (19 %), and
the small model gets 5 of the sample's 14 "actually" chunks wrong, so "actually"
stays a cue. No cue was dropped: the ones that bought little have under 10
chunks each, too few to tell. With `gpt-5.6-luna` as the big tier the gain is
about a point overall.

## Reasoning

Every polish chunk is answered with reasoning at its route's effort,
`routing.EFFORTS = {"cue": "medium", "plain": "medium", "correction":
"medium"}`: `effort(text)` is the cue route's effort when the chunk holds a
correction cue, else the plain route's, on either tier (founder, 14 Oct: English
always thinks at medium). The correction of a refused answer asks for the
correction route's effort. A command sends no effort (not measured: there is no
command corpus). The call asks a reasoning model for `reasoning_effort` instead
of its lowest effort (`providers.md`), and its output budget gets 4,000 more
tokens (`REASONING_TOKENS`) so the reasoning can't use up the answer's room.
Without room, 14 to 16 of the 440 dev chunks came back empty at low effort, and
1,000 tokens still cut some medium answers off empty. A model that doesn't
reason (the `gpt-4.1` fallback) ignores it.

### Effort per route, 14 Oct 2026

On the English bench (backtracking, its chunked twins, dictionary, formatting
and grammar at `--per-slice 100`, platform 4, founder 18, founder-variants 23:
545 rows), each run set every route to one effort. "minimal" is refused by
`gpt-5.6-luna` and `gpt-6-luna` (8 of 8 provider errors, $0). Sentence
correctness %, p50 and p95 ms, and $ per 1,000 dictations with a warm prompt
cache, one value per run:

| Route (rows)        | Effort | Sentence correctness | p50 ms      | p95 ms      | $/1k  |
| ------------------- | ------ | -------------------- | ----------- | ----------- | ----- |
| plain (356)         | medium | 93.5 / 93.0          | 1457 / 1412 | 2636 / 2433 | 0.623 |
| plain (319)         | low    | 92.8                 | 1415        | 2097        | -     |
| plain (356)         | none   | 92.7 / 92.7          | 1258 / 1349 | 1707 / 1984 | 0.514 |
| cue (189)           | medium | 96.3 / 95.2          | 1786 / 1610 | 2667 / 2511 | 0.327 |
| cue (188)           | low    | 93.1                 | 1334        | 2072        | -     |
| cue (189)           | none   | 94.7 / 93.7          | 1249 / 1183 | 1787 / 1847 | 0.246 |
| correction (4 to 9) | medium | 80.0 / 75.0          | 4423 / 5301 | -           | -     |
| correction (9)      | low    | 44.4                 | 3727        | -           | -     |
| correction (8, 9)   | none   | 75.0 / 77.8          | 2627 / 2468 | -           | -     |

Medium's band on the same rows before the change was 93.5 to 94.7 (plain) and
93.7 to 94.7 (cue), three runs. At no effort the plain route falls outside it,
with list shape 100 → 92.3 and punctuation 97.9 → 95.6, and so does the cue
route in one run of two; low falls outside on both (one run: the bench budget
ran out at its cap, so it has no warm-cache $). The correction route has too few
rows a run to judge. Every route stays at medium, so `HEDGE_S` and
`REASONING_TOKENS` stay as they are, and the early backup for an answer that
does not reason (`FAST_HEDGE_S`) went: no route answers without reasoning. A
unit test pins `routing.EFFORTS` to these values.

### Reasoning on the big tier, 10 Oct 2026

The founder chose reasoning on every big-tier chunk on 10 Oct 2026, after it
first ran on correction chunks only. Against that (the round-1 merge), two runs
each, means dev then held-out: backtracking 90.6 to 91.6 and 88.6 to 90.1,
punctuation 96.7 to 95.9 and 95.1 to 96.3, grammar 84.7 to 84.0 and 80.4 to
84.8, capitalization 97.5 to 98.1 and 97.1 to 97.6, clean text unchanged 96.6 to
95.4 and 95.4 to 92.6, list shape 92.3 to 100 and 95 to 95; dictionary terms
kept, false rejects and false lists did not move. p95 went from 2633 to 2716 ms
to 2773 to 3096 on dev, and from 2803 to 2818 to 2852 to 2992 held-out. A
reasoned call spent 20.5 reasoning tokens on average (25 on corrections; p95
102), and a heavy month (`replay.md`) cost $0.414 at the 50 % cache share (11
Oct prompt).

When reasoning first ran, on correction chunks only, with the default pair, 10
Oct 2026 (two runs each; the pair's before-runs were made the same day):

| Split    | Backtracking before | After        | p95 ms before | After        |
| -------- | ------------------- | ------------ | ------------- | ------------ |
| dev      | 81.0 to 82.5        | 85.4 to 86.1 | 2032 to 2093  | 2472 to 2476 |
| held-out | 81.7 to 84.0        | 85.5 to 86.3 | 1826 to 1852  | 2153 to 2279 |

No other gate moved more than run-to-run noise (about 2 points). A correction
chunk answers with about 46 more output tokens, so a heavy month (`replay.md`)
goes from $0.443 to $0.451. Correction chunks alone have a p95 of 3295 to 3349
ms on dev (2546 to 2992 held-out), up from 1981 to 2219 (1861 to 1916). The
guardrail refuses fewer answers: 3.4 to 3.6 % of dev chunks, down from 3.9 to
4.3 %.

With the 10 Oct prompt merged in (round 1), reasoning still pays on a sample
that is in neither dev nor held-out (three runs each): backtracking 92.8 to
93.5, other gates within noise. Held-out, two runs each: backtracking 87.0 to
88.5, clean text unchanged 93.5 to 95.4, grammar 81.6 to 80.4, p95 2385 to 2430
ms to 2803 to 2818 ms.

### What else was tried, 10 Oct 2026

Each option was scored on the dev sample, and the leaders on held-out. A month
is `just replay` at its 25 % cache share, corpus mix and every chunk big.

| Option                                                          | Gates, dev                                     | p95 ms       | Month                                |
| --------------------------------------------------------------- | ---------------------------------------------- | ------------ | ------------------------------------ |
| Low effort on every big-tier chunk                              | backtracking +7, grammar +5                    | 3029 to 3043 | $0.456 / $0.299                      |
| Low effort on both tiers, every chunk                           | as above, punctuation +2                       | 3079 to 3098 | $0.470 / $0.299                      |
| Chunks with a cue and a prior to `gpt-6-sol`                    | chunked 35 → 37 of 40 (low effort: 36 to 38)   | 2032         | $0.461 / $0.288                      |
| Every cue to `gpt-5.6-sol`                                      | backtracking +11                               | 2074         | $2.494 / $2.322                      |
| A big-tier answer the guardrail refuses, retried on `gpt-6-sol` | backtracking +2, punctuation +1                | 2436         | $0.675 / $0.502 at the 4.3 % refused |
| `priority` service tier on the last chunk                       | none; p95 1425 when every chunk used it        | 1425         | $1.006 / $0.613                      |

`gpt-6.1-sol`, `gpt-5.5` and `gpt-6-astra` scored within noise of `gpt-6-sol`
and `gpt-5.6-sol` on each route and cost the same or more (`gpt-6.1-sol` and
`gpt-6-astra` with a p95 over 3 s). Every "sol" route but one sends 2 % or more
of a month's chunks to a model about 20 times the price, which costs more than
the $0.06 left under $0.50. The one that fits, a cue with a prior (0.4 % of
chunks), gains no more than low effort does. The best single model on dev,
`gpt-5.6-sol` on both tiers, still misses every accuracy gate but punctuation,
dictionary terms and list shape, so no routing reaches the bars. The priority
tier costs twice the Standard price and its calls get no cached tokens. Low
effort on every big-tier chunk, the first row, is what runs now (above).

Round b3 (11 Oct, D8) raised the effort from low to medium. On 376 bench rows
(66 founder, 160 earlier failures, 150 random dev, one run each, ledger
`acc2/spend.jsonl`), with the round b3 prompt: the low-effort pair passed 246;
routing every chunk to the big tier at medium passed 273, and medium on both
tiers 265. Every chunk on the big tier would undo the founder's small tier, so
the small tier stayed, then without reasoning: on its 115 plain English rows it
passed 91 without and 94 with medium reasoning, within the run-to-run noise. On
14 Oct the founder turned medium on for the small tier too (above). The fallback
now joins a still-asking tier after 10 s (`HEDGE_S`), since medium reasoning
takes 3.4 s at the median and 10.6 s at the p95, and the earlier 3.5 s hedge let
the unreasoned fallback win long answers.

Round b6 (12 Oct, D8) tried sending English chunks of 41 to 150 words with no
cue to the big tier as well (`MAX_SMALL_WORDS` 150 → 40), on the 40 bench rows
that route changes (10 the round b6 graders named or drew, 30 random dev rows;
one run each, ledger `acc2/spend.jsonl`): 31 → 32 passed, but words right 36 →
35, clean text unchanged 100 → 92.3 and false lists 0 → 2.6, so it showed no
accuracy gain and the small tier keeps those chunks.

## Speed at the same accuracy, 13 Oct 2026

The founder asked for lower latency at round 7's accuracy. Built, measured only
where noted:

- **Reasoning where it pays.** A big-tier chunk reasoned (medium effort) when it
  held a correction cue, and an English chunk that was big only for its length
  answered without reasoning. Since 14 Oct every chunk reasons at its route's
  effort (Reasoning, above).
- **An earlier backup.** An answer that does not reason was joined by the
  fallback after `FAST_HEDGE_S` (3 s); a reasoning answer keeps `HEDGE_S` (10
  s), since the fallback does not reason. Removed 14 Oct, once every route
  reasoned.
- **Paragraphs in parallel.** When "new paragraph" is said with at least 30
  words on each side, each paragraph is polished at once (`routing.paragraphs`);
  the chunk takes its slowest paragraph, and a refused paragraph alone comes
  back as said.
- **One mend more.** Both versions kept around a cue ("Friday, I mean, Monday")
  are mended without a correction call: the old version and the cue go, and the
  answer is judged again (`shape.one_version`).
- **Priority processing and a cache key** on every OpenAI call
  (`docs/providers.md`). On the founder rows, two runs: p50 2.58 and 2.59 → 2.38
  and 2.03 s, p95 6.27 and 6.56 → 5.89 and 5.86 s, 129 of 132 exact as before.

The app cuts shorter chunks too, so the last chunk is about a sentence
(`DICTATION-AI.md` in the app repo). The rest is not yet measured against round
7 on the frozen sample (`~/.snapbak-bench/frozen-r7-sample.json`).

## Where the time goes, 13 Oct 2026

The founder asked for every answer to be much faster at the same models, with no
gate allowed to drop. On 500 dev rows (seed 7) and the 66 founder rows, two runs
each with the default pair, the time is all the model's: the guardrail takes
about 0.6 ms a chunk.

| Route        | Dev rows | p50 ms | p95 ms | Output tokens | Reasoning |
| ------------ | -------- | ------ | ------ | ------------- | --------- |
| small        | 289      | 1429   | 1935   | 38            | 0         |
| big reasoned | 657      | 2247   | 4986   | 218           | 168       |
| paragraphs   | 24       | 4408   | 7965   | -             | -         |
| corrected    | 30       | 8187   | 12016  | about 1,100   | about 830 |

A reasoned answer is about 50 tokens; the rest is the model thinking. A
correction (2.8 % of rows, most of them `unheard`) is where answers take 10 s.

Each route's effort is in `routing.EFFORTS`, so one can be benched alone. Low
effort, two runs each on the rows it changes, against the same rows' baseline:

| Route       | p50 ms      | p95 ms      | Gates that fell                               |
| ----------- | ----------- | ----------- | --------------------------------------------- |
| English cue | 1533 → 1424 | 2915 → 2292 | numbers 90.0 → 80.0, backtracking 94.4 → 91.7 |
| Correction  | 2020 → 1909 | 5636 → 5618 | within noise; no speed gained                 |

Every route stayed at medium. Also not taken:

- **Mending `unheard` by rule.** Most of those refusals are the model rightly
  fixing a misheard word ("in the entire" → "in the interim"); putting the said
  word back would type the mishearing. The correction call stays.
- **Predicted outputs.** OpenAI answers `'prediction' is not supported with this
  model` for `gpt-5.6-luna` and `gpt-6-luna`.
- **Two copies of a call, first answer wins.** On four runs of each dev row, the
  faster of two would take p95 from 5500 to 4694 ms only.

The wait after key-up is mostly the app's: it polishes one chunk at a time, so
chunks cut during a slow answer queue up (`DICTATION-AI.md` in the app repo).

## Retry

When the guardrail refuses an answer, small or big tier, the big tier corrects
it once if at least 1,500 ms of the deadline are left (gpt-6-luna, p50 1.49 s on
30 corpus items without reasoning, 11 Oct 2026): it gets the refused answer and
the rule and words the guardrail named, with reasoning (`api.md`, Correction;
founder, 11 Oct: a blind retry often repeated the same mistake). A failed or
refused correction keeps the request text with `rejected: true`. The fallback
joins a still-asking correction once half the time left has gone (round b5), so
a long dictation's correction answers before the deadline instead of timing out.

## Fallback

When a tier errors or times out, `SNAPBAK_FALLBACK` is tried once with the time
that is left. The answer then names the fallback engine with the tier that was
asked for.
