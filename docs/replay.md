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
   Requests are measured as the server sends them, without empty fields. It
   overestimates Devanagari.
5. Bill `CACHE_HIT` (50 %) of the fixed prompt at the provider's cached price,
   the share the founder set on 10 Oct 2026. The bench measured 25 % for
   `gpt-6-luna` and `gpt-5.6-luna` on a cold start at 16 requests in flight, and
   99.6 to 99.9 % warm (the `gpt-4.1` models got 90 % cold).

It prints two months: the corpus language mix, and the same month all in
Hinglish, where every chunk goes to the big tier.

## Round b6, 12 Oct 2026

Round b6's rules, the `checker` field and five examples grew the fixed prompt
from 12,356 to 13,070 tokens for accuracy (`api.md`). With the founder's pair
the month is 19.61 M input and 0.077 M output tokens over 1,491 requests, 51.7 %
on the big tier: $1.627 → $1.716 for the corpus mix and $1.136 → $1.198 all in
Hinglish, a report against the old $0.50 reference. Routing stays as it was
(`escalation.md`).

## Round b5, 12 Oct 2026

Round b5's rules and two examples grew the fixed prompt from 11,444 to 12,356
tokens for accuracy (`api.md`). With the founder's pair the month is 18.55 M
input and 0.077 M output tokens over 1,491 requests, 51.7 % on the big tier:
$1.512 → $1.627 for the corpus mix and $1.058 → $1.136 all in Hinglish, a report
against the old $0.50 reference.

## Round b4, 11 Oct 2026

Round b4's rules and examples grew the fixed prompt from 10,081 to 11,444
tokens for accuracy (`api.md`). With the founder's pair the month is 17.19 M
input and 0.077 M output tokens over 1,491 requests, 51.7 % on the big tier:
$1.341 → $1.512 for the corpus mix and $0.940 → $1.058 all in Hinglish, a report
against the old $0.50 reference.

## Round b3, 11 Oct 2026

Round b3's rules and examples grew the fixed prompt from 6,807 to 10,081 tokens
for accuracy (`api.md`). With the founder's pair the month is 15.16 M input and
0.077 M output tokens over 1,491 requests, 51.7 % on the big tier: $0.928 →
$1.341 for the corpus mix and $0.658 → $0.940 all in Hinglish, a report against
the old $0.50 reference. The big tier now reasons at medium effort, which uses
more reasoning tokens than the low-effort figure the month still bills per
call; the replay keeps that figure until a run measures medium's.

## Round b2, 11 Oct 2026

Round b2's rules and examples grew the fixed prompt from 4,667 to 6,807 tokens
for accuracy (`api.md`). With the founder's pair the month is 10.27 M input and
0.077 M output tokens over 1,491 requests, 51.7 % on the big tier: $0.659 →
$0.928 for the corpus mix and $0.474 → $0.658 all in Hinglish, over the old
$0.50, which is now only a reference.

## Round b1, 11 Oct 2026

Round b1's rules and examples grew the fixed prompt from 3,369 to 4,667 tokens.
The month is 7.08 M input and 0.077 M output tokens over 1,491 requests, 51.7 %
on the big tier: $0.659 with `gpt-5.6-luna` small (over the target), $0.457
with `gpt-6-luna` on both tiers, and $0.474 all in Hinglish. Round b1 made
`gpt-6-luna` the small default for that reason; the founder reverted it (D8):
`gpt-5.6-luna` is the small tier again.

## Results, 10 Oct 2026

1,696 requests a month, 3.53 M input and 0.07 M output tokens, 39.8 % of them on
the big tier. List prices are from `PRICES` and `CACHED`, with 50 % of the fixed
prompt cached.

| Small, big                                   | Corpus mix | All Hinglish |
| -------------------------------------------- | ---------- | ------------ |
| `openai:gpt-5.6-luna`, `openai:gpt-6-luna`   | $0.414     | $0.283       |
| `openai:gpt-4.1-nano`, `openai:gpt-4.1-mini` | $0.607     | $1.084       |
| `openai:gpt-4.1-mini`, `openai:gpt-4.1-mini` | $1.037     | $1.084       |
| `openai:gpt-4.1-mini`, `openai:gpt-6-luna`   | $0.683     | $0.257       |
| `openai:gpt-6-luna`, `openai:gpt-5.6-luna`   | $0.360     | $0.538       |
| `openai:gpt-6-luna`, `openai:gpt-6-luna`     | $0.240     | $0.257       |
| `openai:gpt-6-luna`, `openai:gpt-6-sol`      | $2.329     | $5.138       |
| `openai:gpt-6-luna`, `openai:gpt-5.6-sol`    | $4.528     | $10.276      |
| `openai:gpt-4.1-nano`, `openai:gpt-4.1`      | $2.463     | $5.420       |
| `groq:llama-3.1-8b-instant`, `llama-3.3-70b` | $1.042     | $2.180       |
| `groq:openai/gpt-oss-20b`, `gpt-oss-120b`    | $0.424     | $0.602       |
| `gemini:gemini-2.5-flash-lite`, `2.5-flash`  | $0.803     | $1.360       |

The fixed prompt is most of the cost, so the cache share moves the month a lot.
The default pair costs $0.629 with no cache ($0.413 all in Hinglish), $0.502
($0.335) at 25 % and $0.170 ($0.132) at 90 %. The `gpt-4.1-mini` and
`gpt-6-luna` pair costs $1.063 ($0.413) with no cache and $0.378 ($0.132) at 90
%. The corpus over-represents corrections and Hindi, so real English use sends
fewer chunks to the big tier than 39.8 %.

The prompt grew from about 1,450 tokens to about 1,850 on 10 Oct: the Hindi and
Hinglish cues and the end-of-speech cue rule, then the stricter correction,
list, number and Hindi grammar rules and a Hindi list example. Later on 10 Oct
it grew to about 2,110: rules for small numbers, spelling, corrections in
mid-sentence and counted lists, and three correction examples. At 25 % cached,
the default pair's month went from $0.443 to $0.498. Reasoning on correction
chunks (`escalation.md`) adds $0.008, so the round-1 merge dropped one Hindi
correction example (about 2,050 tokens): $0.494, $0.006 under the target. The
next rule needs room made elsewhere in the prompt or a measured cache share.
Warm bench runs measure 99.6 to 99.9 % cached; at 90 % a prompt of about 3,050
tokens costs $0.207.

Later on 10 Oct the founder set the cache share to 50 % and turned reasoning on
for every big-tier chunk (`escalation.md`): $0.374 a month ($0.257 all
Hinglish). At the old 25 % it would be $0.502, over the target. The round-1
prompt with the stronger rules (about 3,050 tokens) would cost $0.527 at 50 %,
so the prompt stayed at about 2,050 tokens. On 11 Oct the bulleted-list rules
and their two worked examples added about 270 tokens: $0.375 to $0.414 a month
($0.258 to $0.283 all Hinglish).

Later on 11 Oct the misheard-word, term-hint and announced-list rules, ten
generic worked examples and the correction of every refused answer (`api.md`)
took the fixed prompt from about 2,325 to about 3,326 tokens. On the v2 corpus
mix (1,491 requests, 51.7 % big) the default pair's month went from $0.355 to
$0.491 ($0.257 to $0.358 all Hinglish), $0.009 under the target. The run-on and
list examples were shortened to stay under it; the next rule needs room made
elsewhere or a measured cache share.

The round-1 rules (11 Oct: spoken marks in Hindi and in quote pairs, a mark word
used as a word, "then" between two subjects, the colon after "do one thing",
clock phrases, and a lone "no" as a cue only before another version of a word)
took the fixed prompt from 3,326 to 3,396 tokens, paid for in part by dropping
the shorter of two grammar-slip examples: $0.491 to $0.500 a month ($0.358 to
$0.364 all Hinglish), on the target with nothing to spare. The Hindi and
Hinglish worked examples the graders asked for cost about 60 to 120 tokens each
and did not fit; adding any needs room made elsewhere or a measured cache share.

Round 3 (11 Oct) rewrote the rules shorter, so they could take questions,
run-ons, quantities, number and negation sound-alikes, addresses in Hindi and
more cues, shortened the grammar and Hindi list examples, dropped a second
announced list and a second English misheard word, and added five short
examples (a Hinglish quantity kept as prose, a Hinglish "kya" question, "then"
starting a sentence, "to" fixed to "two", and a request to the assistant typed
as said). The fixed prompt went from 3,396 to 3,369 tokens: $0.500 to $0.496 a
month ($0.364 to $0.362 all Hinglish), $0.004 under the target.
