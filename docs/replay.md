# Replay

`just replay [SMALL BIG]` prices a heavy user's month: two hours of dictation
through the escalation rules. It runs offline and is a report, not a gate: it
always exits 0 and prints the PRD's old US$0.50 target only for reference
(founder decision D8, 11 Oct: the same models, accuracy over cost and time). The
defaults are the founder's pair, `openai:gpt-5.6-luna openai:gpt-6-luna`.

## How it works

1. Draw corpus utterances with a fixed seed until they add up to two hours of
   speech at 2.5 words a second (18,000 words). The generated `suite-growth`
   rows are left out: they are eval rows from the founder inbox, and their share
   would move the month with every intake.
2. Cut each utterance into 15-word chunks, about what the app's ChunkGate sends:
   at least 5 s of audio, then a pause. Each chunk carries the words before it
   as `prior`, capped at 300 words, as the app sends it.
3. Pick each chunk's tier with `routing.tier`. Assume 5 % of answers on either
   tier are refused and corrected on the big tier with reasoning, sending the
   request, the refused answer and the reasons (about 50 tokens), until the
   bench measures the real share.
4. Estimate tokens as UTF-8 bytes over 4. A chunk on the big tier adds 46 output
   tokens of reasoning, the larger of two bench means (`escalation.md`); every
   pair is billed for them, though only OpenAI reasoning models spend them. That
   counts the fixed prompt and examples (about 2,050 tokens) on every call.
   Requests are measured as the server sends them, without empty fields.
5. Bill `CACHE_HIT` (50 %) of the fixed prompt at the provider's cached price,
   the share the founder set on 10 Oct 2026. The bench measured 25 % for
   `gpt-6-luna` and `gpt-5.6-luna` on a cold start at 16 requests in flight, and
   99.6 to 99.9 % warm (the `gpt-4.1` models got 90 % cold).

It prints one month: the English corpus as the app would send it. Reasoning
tokens are billed only on the big tier, so the month understates the small
tier's medium reasoning (`escalation.md`).

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
