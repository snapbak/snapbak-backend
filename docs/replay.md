# Replay

`just replay [POLISH CORRECTION]` prices a heavy user's month: two hours of
dictation on the one polish model, with a share corrected on the correction
model (15 Oct 2026; until then `[SMALL BIG]` through the two-tier escalation).
Every OpenAI call is billed at the priority estimate, twice the list price
(`providers.md`). It runs
offline and is a report, not a gate: it
always exits 0 and prints the PRD's old US$0.50 target only for reference
(founder decision D8, 11 Oct: the same models, accuracy over cost and time). The
defaults are the server's, `openai:gpt-6-luna openai:gpt-6-luna`.

## How it works

1. Draw corpus utterances with a fixed seed until they add up to two hours of
   speech at 2.5 words a second (18,000 words). The generated `suite-growth`
   rows are left out: they are eval rows from the founder inbox, and their share
   would move the month with every intake.
2. Cut each utterance into 15-word chunks, about what the app's ChunkGate sends:
   at least 5 s of audio, then a pause. Each chunk carries the words before it
   as `prior`, capped at 300 words, as the app sends it.
3. Polish every chunk on the polish model. Assume 5 % of answers are refused
   and corrected on the correction model with reasoning, sending the request,
   the refused answer and the reasons (about 50 tokens), until the bench
   measures the real share.
4. Estimate tokens as UTF-8 bytes over 4. A correction adds 46 output tokens of
   reasoning, the larger of two bench means (`escalation.md`); every model is
   billed for them, though only OpenAI reasoning models spend them. That counts
   the fixed prompt with every module and all examples (10,213 tokens) on every
   call, at the priority estimate. Requests are measured as the server sends
   them, without empty fields.
5. Bill `CACHE_HIT` (50 %) of the fixed prompt at the provider's cached price,
   the share the founder set on 10 Oct 2026. The bench measured 25 % for
   `gpt-6-luna` and `gpt-5.6-luna` on a cold start at 16 requests in flight, and
   99.6 to 99.9 % warm (the `gpt-4.1` models got 90 % cold).

It prints one month: the English corpus as the app would send it. Reasoning
tokens are billed only on corrections; the polish model reasons at none.

## Meeting minutes v2, 26 Oct 2026

The minutes are now ten calls, one per section, each sending the whole
transcript (MEETINGS.md 7, minutes v2), so the replay prices ten prompts per
meeting, a 1,000-token answer each, a repair on one section in five, and the
detail guard's completion call on half the runs (the transcript, the summary
and MoM in, 2,000 tokens out). On `gpt-6-luna` at the priority estimate: 30 min
is 105,646 in + 10,000 out tokens, US$0.040; 2 h is 412,622 in + 10,000 out,
US$0.117. A heavy month of 20 two-hour meetings is about US$2.34, ten times
the single call's: the price of a section each with its own room and repair.

## Meeting minutes, 25 Oct 2026 (one call, before v2)

The replay also prices one meeting's minutes (MEETINGS.md 11.5): 30 min and 2 h
of two-speaker talk drawn from the corpus (same seed), cut into 20-word lines as
the transcript stores them, sent whole in the server's minutes prompt in one
call (D25), with a 1,500-token answer. One run in five is assumed to need the
repair turn, which resends the transcript and the first answer. On
`gpt-6-luna` at the priority estimate: 30 min is 10,511 in + 1,500 out tokens,
US$0.0044; 2 h is 41,209 in + 1,500 out, US$0.0118. A heavy month of 20
two-hour meetings is about US$0.24.

## R7 one-model round, 15 Oct 2026

With `gpt-6-luna` polishing every chunk at effort none and correcting, at the
priority estimate, the month is 14.91 M input and 0.036 M output tokens over
1,448 requests: $1.686 polish + $0.090 correction = $1.776, against $1.738 for
`gpt-5.6-luna` at list price on the default tier (below). `gpt-6-luna` lists at
half `gpt-5.6-luna`'s input price and priority doubles it, so the polish costs
the same; the correction doubles. At the 70 % cached that the R7 priority runs
measured (`bench.md`), the month is $1.217.

The replay can't see the bench's −64 %: it sends every module on every call
(the gate's cut is not in it) and bills no reasoning on the polish call, which
the old setup's medium effort spent.

## One model, 15 Oct 2026

With `gpt-5.6-luna` polishing every chunk and `gpt-6-luna` correcting, at list
price on the default tier, the month is 14.91 M input and 0.036 M output tokens
over 1,448 requests: $1.693 polish + $0.045 correction = $1.738, against $3.391
for the same models as the two-tier pair at the priority estimate the same day.
Halving the price for the default tier accounts for $3.391 → $1.696; the rest
($0.042) is the 5 % of chunks that held a cue or ran long, now on the polish
model, which lists at twice `gpt-6-luna`'s price, and 12 more prompt tokens. The
month still sends every module and all 92 examples: it can't tell a chunk's
features offline, so the prompt the app's gate cuts is not in it yet.

## Filled v2 corpus, 14 Oct 2026

The hand-written v2 rows that filled the short areas (`bench.md`) joined the
utterance pool; the prompt did not change. With the founder's pair the month is
14.84 M input and 0.040 M output tokens over 1,443 requests, 5.6 % on the big
tier: $3.388 → $3.371.

## English only, 14 Oct 2026

Hindi and Hinglish were dropped on 14 Oct 2026; earlier months priced them too.
The other languages' rules, 40 worked examples and corpus rows went, and 26
English examples came in (`api.md`). The fixed prompt went from 13,619 to 10,201
tokens. With the founder's pair the month is 15.27 M input and 0.044 M output
tokens over 1,486 requests, 10.4 % on the big tier: $3.603 → $3.388. The old
month was 49.2 % big because every chunk the app tagged in another language went
to the big tier.

## Lone subjects and named lists, 14 Oct 2026

Rules and four examples for a lone subject the speech ends on and a noun that
names a list's items grew the fixed prompt from 13,070 to 13,619 tokens for
accuracy (`api.md`). With the founder's pair the month is 20.29 M input and
0.075 M output tokens over 1,480 requests, 49.2 % on the big tier: $3.464 →
$3.603 for the corpus mix, a report against the old $0.50 reference.

## Round b6, 12 Oct 2026

Round b6's rules, the `checker` field and five examples grew the fixed prompt
from 12,356 to 13,070 tokens for accuracy (`api.md`). With the founder's pair
the month is 19.61 M input and 0.077 M output tokens over 1,491 requests, 51.7 %
on the big tier: $1.627 → $1.716 for the corpus mix, a report against the old
$0.50 reference. Routing stays as it was (`escalation.md`).

## Round b5, 12 Oct 2026

Round b5's rules and two examples grew the fixed prompt from 11,444 to 12,356
tokens for accuracy (`api.md`). With the founder's pair the month is 18.55 M
input and 0.077 M output tokens over 1,491 requests, 51.7 % on the big tier:
$1.512 → $1.627 for the corpus mix, a report against the old $0.50 reference.

## Round b4, 11 Oct 2026

Round b4's rules and examples grew the fixed prompt from 10,081 to 11,444 tokens
for accuracy (`api.md`). With the founder's pair the month is 17.19 M input and
0.077 M output tokens over 1,491 requests, 51.7 % on the big tier: $1.341 →
$1.512 for the corpus mix, a report against the old $0.50 reference.

## Round b3, 11 Oct 2026

Round b3's rules and examples grew the fixed prompt from 6,807 to 10,081 tokens
for accuracy (`api.md`). With the founder's pair the month is 15.16 M input and
0.077 M output tokens over 1,491 requests, 51.7 % on the big tier: $0.928 →
$1.341 for the corpus mix, a report against the old $0.50 reference. The big
tier now reasons at medium effort, which uses more reasoning tokens than the
low-effort figure the month still bills per call; the replay keeps that figure
until a run measures medium's.

## Round b2, 11 Oct 2026

Round b2's rules and examples grew the fixed prompt from 4,667 to 6,807 tokens
for accuracy (`api.md`). With the founder's pair the month is 10.27 M input and
0.077 M output tokens over 1,491 requests, 51.7 % on the big tier: $0.659 →
$0.928 for the corpus mix, over the old $0.50, which is now only a reference.

## Round b1, 11 Oct 2026

Round b1's rules and examples grew the fixed prompt from 3,369 to 4,667 tokens.
The month is 7.08 M input and 0.077 M output tokens over 1,491 requests, 51.7 %
on the big tier: $0.659 with `gpt-5.6-luna` small (over the target), $0.457 with
`gpt-6-luna` on both tiers. Round b1 made `gpt-6-luna` the small default for
that reason; the founder reverted it (D8): `gpt-5.6-luna` is the small tier
again.

## Results, 10 Oct 2026

1,696 requests a month, 3.53 M input and 0.07 M output tokens, 39.8 % of them on
the big tier. List prices are from `PRICES` and `CACHED`, with 50 % of the fixed
prompt cached.

| Small, big                                   | Corpus mix |
| -------------------------------------------- | ---------- |
| `openai:gpt-5.6-luna`, `openai:gpt-6-luna`   | $0.414     |
| `openai:gpt-4.1-nano`, `openai:gpt-4.1-mini` | $0.607     |
| `openai:gpt-4.1-mini`, `openai:gpt-4.1-mini` | $1.037     |
| `openai:gpt-4.1-mini`, `openai:gpt-6-luna`   | $0.683     |
| `openai:gpt-6-luna`, `openai:gpt-5.6-luna`   | $0.360     |
| `openai:gpt-6-luna`, `openai:gpt-6-luna`     | $0.240     |
| `openai:gpt-6-luna`, `openai:gpt-6-sol`      | $2.329     |
| `openai:gpt-6-luna`, `openai:gpt-5.6-sol`    | $4.528     |
| `openai:gpt-4.1-nano`, `openai:gpt-4.1`      | $2.463     |
| `groq:llama-3.1-8b-instant`, `llama-3.3-70b` | $1.042     |
| `groq:openai/gpt-oss-20b`, `gpt-oss-120b`    | $0.424     |
| `gemini:gemini-2.5-flash-lite`, `2.5-flash`  | $0.803     |

The fixed prompt is most of the cost, so the cache share moves the month a lot.
The default pair costs $0.629 with no cache, $0.502 at 25 % and $0.170 at 90 %.
The `gpt-4.1-mini` and `gpt-6-luna` pair costs $1.063 with no cache and $0.378
at 90 %.

From 10 Oct to round 3 (11 Oct) the prompt grew from about 1,450 tokens to 3,369
as rules and examples came in: correction cues, stricter correction, list and
number rules, bulleted lists, misheard words, term hints, announced lists,
questions, run-ons, quantities and number sound-alikes. Each step was cut to
stay under the then $0.50 target (the default pair's month ran from $0.374 to
$0.500 at 50 % cached). On 10 Oct the founder set the cache share to 50 % and
turned reasoning on for every big-tier chunk (`escalation.md`).
