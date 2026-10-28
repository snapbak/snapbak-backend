# Providers

Every model sits behind one call:

```python
text, usage = await provider.complete(turns, max_tokens, timeout, reason)
```

`usage` is `Usage(input, output, cached, retries)`: tokens, and the times a
`429` or `5xx` answer was asked again. `reason` (default `False`) asks for a
little reasoning, as below. `turns` are `(role, content)` pairs: the system
prompt, the worked examples, then the request. `make_provider("provider:model",
keys)` builds one from the registry in
`src/snapbak_backend/ai/providers/factory.py`; each provider has its own module
beside it. Each keeps one pooled HTTP/2 client for the life of the server, and
its idle connections for 5 minutes. `/healthz` warms each tier's connection and
the fallback's at most every 30 s. A `429` (rate limit) or `500`, `502`, `503`
or `504` answer is asked again before the chain moves to the fallback, within
the call's own time: after the provider's `Retry-After` (plus up to 15 %
jitter), else after 0.25 s doubling each time, times a jitter of 0.5 to 1.5, and
never when the wait would pass the deadline (founder dictations failed with
"HTTP/2 429 Too Many Requests" on both models, 11 Oct). An account out of
credits (`insufficient_quota`, `credit_balance_exhausted`) is not asked again.
The log line counts every call's retries (`retries=N`). Any network error, other
error status or unexpected answer becomes a `ProviderError`, which carries its
retries.

| Provider    | Request                                                                  | Key                 |
| ----------- | ------------------------------------------------------------------------ | ------------------- |
| `groq`      | OpenAI-style chat at `api.groq.com/openai/v1/chat/completions`           | `GROQ_API_KEY`      |
| `openai`    | OpenAI chat at `api.openai.com/v1/chat/completions`                      | `OPENAI_API_KEY`    |
| `anthropic` | `api.anthropic.com/v1/messages`, rules in `system`, `x-api-key`, version | `ANTHROPIC_API_KEY` |
| `gemini`    | `generateContent` with `systemInstruction`, assistant turns as `model`   | `GEMINI_API_KEY`    |
| `fake`      | no network; see below                                                    | none                |

Every request asks for temperature 0, except the OpenAI models in `SAMPLING`
(`ai/providers/factory.py`). Those run at their lowest reasoning effort, and the
newest refuse temperature 0, so they get only the effort:

| OpenAI models                                                     | Sent                                       |
| ----------------------------------------------------------------- | ------------------------------------------ |
| `gpt-4.1`, `-mini`, `-nano`                                       | `temperature: 0`                           |
| `gpt-5.1`, `gpt-5.2`, `gpt-5.4`, `-mini`, `-nano`                 | `temperature: 0`, `reasoning_effort: none` |
| `gpt-5.5`, `gpt-5.6-luna`, `-terra`, `-sol`, `gpt-6-luna`, `-sol` | `reasoning_effort: none`                   |
| `gpt-5-mini`, `gpt-5-nano`                                        | `reasoning_effort: minimal`                |
| `gpt-6-astra`, `gpt-6.1-sol`                                      | `reasoning_effort: low`                    |

Each row is the lowest effort the API accepted on 10 Oct 2026, and temperature 0
only where it was accepted. Since 15 Oct the polish model is made with
`SNAPBAK_POLISH_TEMPERATURE` (default 0) in place of that 0, and every model
sent a temperature also gets `seed: 7`, so a run repeats as closely as the API
allows; a row without a temperature never gets one. Only the OpenAI-style
providers (`openai`, `groq`) take the setting; Anthropic and Gemini stay at 0.
The seed is not yet checked against the live API (no paid call was made). A
call that reasons (every polish chunk and every correction, see
`escalation.md`) sends only its effort to every model that has a
`reasoning_effort` row: the polish model's `SNAPBAK_POLISH_EFFORT` (default
`none` since R7, 15 Oct 2026; `medium` before), a correction `medium` (`low`
until round b3, which all 15 accepted on 10 Oct 2026; `minimal` is refused by
`gpt-5.6-luna` and `gpt-6-luna`, 14 Oct 2026). The other models ignore it, and
a call that sends an effort sends no temperature. OpenAI reports the reasoning tokens inside the output
(`completion_tokens_details`), and `Usage.reasoning` carries them. The
OpenAI-style and Gemini requests ask for a JSON answer. Gemini turns thinking
off (`thinkingBudget: 0`). Anthropic sends `anthropic-version: 2023-06-01`, as
the app's old `cloud.rs` did.

The server refuses to start when a named provider has no key, or when a name is
unknown.

## Settings

| Variable                     | Meaning                                                                                                           |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `SNAPBAK_TOKEN`              | the bearer token (required)                                                                                       |
| `SNAPBAK_POLISH`             | the one polish model, `provider:model` (default `openai:gpt-6-luna`, the R7 pick, `bench.md`)                      |
| `SNAPBAK_POLISH_TEMPERATURE` | the polish model's temperature, 0 to 2, on models that take one (default 0)                                       |
| `SNAPBAK_POLISH_EFFORT`      | the polish model's `reasoning_effort`, on models that reason (default `none`)                                     |
| `SNAPBAK_EXAMPLES_POLICY`    | `all` (default), `per_module` or `none`: the worked examples before each request (`api.md`)                       |
| `SNAPBAK_BIG`                | the big model, which runs every command (required)                                                                |
| `SNAPBAK_CORRECTION`         | the model that corrects a refused polish answer once, at medium effort, or `none` for no retry (default the big model) |
| `SNAPBAK_FALLBACK`           | tried once when a model errors or times out (optional; `.env.example` sets the polish model)                      |
| `SNAPBAK_TRANSLATE`          | the caption translation model, `provider:model` (default the big model; TRX-07, needs a bench before launch)      |
| `SNAPBAK_EMBEDDINGS`         | the meetings' embeddings, `openai:<model>` (default `openai:text-embedding-3-small`) or `fake:hashed` |

The server refuses to start when the temperature is not a number from 0 to 2 or
the examples policy is not one of the three. Until 15 Oct the settings were
`SNAPBAK_SMALL` and `SNAPBAK_BIG`, the two tiers (`escalation.md`); the app's
E2E harness must set `SNAPBAK_POLISH` now.

## Embeddings

The meetings' search (MEETINGS.md, D38) embeds each minutes section once and
each search query, through `services/embeddings.py`. `SNAPBAK_EMBEDDINGS` names
`openai:<model>`, by default `openai:text-embedding-3-small` pinned to 1,536
dimensions on `OPENAI_API_KEY`, or `fake:hashed`: deterministic token-bag
vectors with no network, for the tests. The OpenAI client is the chat
providers' own (`Http` in `ai/providers/base.py`): pooled, with the same backoff
on a 429 or 5xx, and any failure is an `EmbedError`, which a search answers as a
502 `provider` and gives the day's search back.

`text-embedding-3-small` lists at US$0.02 per million input tokens on OpenAI's
pricing page; check it before you trust a cost report. A meeting's sections are
a few hundred tokens and a query a dozen, so embedding costs a few millionths of
a dollar a meeting; the re-rank call beside each search costs more
(`replay.md`). Embeddings are not in `PRICES`, so the bench ledger does not
count them.

## The prompt cache

Every OpenAI call sends a `prompt_cache_key` made from the hash of the system
prompt's first 1,024 characters (`OPENING` in `ai/providers/openai.py`). Since
15 Oct the system prompt is the core, the grammar module and the modules of the
chunk's features (`api.md`), always in one order. The core alone is longer than
the opening, so every feature set shares one key and lands where the shared
prefix is cached. A key per whole prompt split the traffic into up to 16 keys,
and R7 runs on the default tier came back 41 % cached; with the shared key on
priority, 70 %. Groq gets no key.

Every OpenAI call asks for priority processing (`service_tier: "priority"`;
gpt-5.6-luna answers `"priority"` and gpt-6-luna `"fast"`), and `billed` in
`config/pricing.py` counts it at twice the list price (`PRIORITY`), an estimate
on the safe side. Priority went from 13 Oct, came off on 15 Oct (it cost twice
as much and its runs then returned no cached tokens) and came back the same day
after the R7 round (`bench.md`): the default tier was 25 % slower at p50 for the
same model (`gpt-6-luna` without reasoning, 1,961 against 1,579 ms; p95 3,387
against 2,480 ms), and with the shared key the priority runs were cached too.
The bench ledger and the replay bill `billed()`.

An Anthropic call marks two cache breakpoints, each an ephemeral
`cache_control`: the system prompt, and the last worked example, the last
assistant turn before the request. So the rules and the examples are read from
the cache, and a retry after a refusal still reads them. `Usage.input` counts
the tokens Anthropic reports apart (`cache_read_input_tokens` and
`cache_creation_input_tokens`), and `Usage.cached` the ones read. Gemini caches
a repeated prefix on its own, with nothing to send.

## The fake provider

For the app's end-to-end tests, `fake:<mode>` answers without a model. Like
OpenAI, every mode refuses a `reason` that is no reasoning effort, a JSON-mode
call with no message saying "json", and a completion budget outside 1 to
128,000 tokens, so a test catches a call the real API would answer with a 400. A
correction reaches the model with the prior's last sentence in front of it (see
`api.md`), so `edit` swaps that sentence's last word and the server hands the
rest of the prior back as the revision. Words that end with a cue or a "no" are
no sentence to put right, so "no wait make it friday" gets the bang:

| Mode    | Polish                                                                  | Command                   |
| ------- | ----------------------------------------------------------------------- | ------------------------- |
| `echo`  | `text` unchanged                                                        | the selection             |
| `edit`  | `text` with its last `.`, `?` or `!` turned into `!`, or `!` added      | the selection in capitals |
| `edit`  | for `<sentence> <cue> <word>`, any cue of `routing.CUES` ("no wait", "scratch that", "I mean", "delete that", "no, don't do this"), a "no" before or after it ("wait, no") and a comma, full stop or "!" after each of its words: the sentence, last word swapped | the same as above         |
| `fail`  | a provider error, so a 502                                              | the same                  |
| `slow`  | waits 5 s, then echoes, so a short deadline gives a 502                 | the same                  |
| `reply` | "Sure, " and the text, which the guardrail rejects                      | the selection             |
| `translate-drop` | `text` unchanged                                               | the selection             |
| `junk`  | prose, never JSON: the meetings' parse and repair must end it (`bad_answer`) | the same              |

In `meetings` mode the fake answers each minutes section from the request's
first line (a contradiction from the first two), with the first action-items
answer fabricating a word so the section's repair runs; the detail guard's call
gets every missing detail appended to the MoM.

In every mode but `fail` and `slow`, a caption translation answers each line as
`[<target>] <text>`; `translate-drop` leaves the last line out, for the retry
path.

## Prices

`PRICES` in `src/snapbak_backend/config/pricing.py` holds US$ per million input
and output tokens per engine. `CACHED` holds the price of an input token the
provider served from its prompt cache; an engine not in it bills those as input.
The bench and the replay price calls with them. OpenAI's are the Standard tier
from <https://developers.openai.com/api/docs/pricing>, read on 10 Oct 2026.
These are list prices and change, so check them before you trust a cost report.
An engine not in the table costs 0.

OpenAI reports the cached part of each prompt (`prompt_tokens_details`), and
`Usage.cached` carries it. The fixed prompt and examples (about 13,800 tokens on
13 Oct 2026; 77 % of prompt tokens came from the cache on the bench) are the
same on every call, so they are what gets cached. The bench measured:

- 90 % of prompt tokens cached for `gpt-4.1` models at 16 requests in flight;
- 25 % for `gpt-5.6-*` and `gpt-6-*`, and 0 % for `gpt-5.2`, `gpt-5.4-nano` and
  `gpt-5.5`, at 16 in flight;
- one request at a time, 90 to 100 % for `gpt-4.1-nano`, `gpt-4.1-mini`,
  `gpt-6-luna`, `gpt-5.6-luna` and `gpt-6-sol`, and 0 % for `gpt-5.2`.

## Choosing the models, 10 Oct 2026

Every OpenAI model the key lists was screened on `--per-slice 10`, and the
leaders were run on `--per-slice 40` (440 items, each model as both tiers). Mean
is the mean of the seven accuracy gates (false rejects left out); a column that
scored other languages left with them. "Small items" is the 112 items the
escalation leaves on the small tier (English, no cue): words right, then
punctuation and capitals. Latency is server time at 16 in flight. $/1k is per
1,000 dictations at the measured cache share.

| Model          | Mean | Back | Small items        | p50 ms | p95 ms | $/1k  |
| -------------- | ---- | ---- | ------------------ | ------ | ------ | ----- |
| `gpt-4.1-nano` | 65.1 | 13.1 | 72.3 / 86.3 / 91.4 | 952    | 1503   | 0.065 |
| `gpt-4.1-mini` | 78.1 | 59.9 | 92.9 / 96.0 / 97.2 | 886    | 1224   | 0.247 |
| `gpt-4.1`      | 81.7 | 78.1 | 98.2 / 95.1 / 99.3 | 907    | 1245   | 1.324 |
| `gpt-5.4-nano` | 71.7 | 37.2 | 91.1 / 91.1 / 97.2 | 1074   | 1469   | 0.361 |
| `gpt-5.6-luna` | 83.7 | 77.4 | 94.6 / 95.0 / 95.0 | 1551   | 2134   | 0.282 |
| `gpt-6-luna`   | 83.1 | 73.7 | 94.6 / 96.1 / 98.6 | 1670   | 2380   | 0.139 |
| `gpt-5.2`      | 83.6 | 77.4 | 92.9 / 94.2 / 95.7 | 1498   | 2252   | 3.221 |
| `gpt-5.5`      | 87.9 | 87.6 | 96.4 / 96.1 / 98.6 | 1663   | 2616   | 8.880 |
| `gpt-5.6-sol`  | 89.6 | 88.3 | 96.4 / 98.1 / 99.3 | 1442   | 2267   | 5.520 |
| `gpt-6-sol`    | 87.8 | 84.7 | 99.1 / 98.1 / 98.6 | 1407   | 1969   | 2.762 |

On the 10-per-slice screen `gpt-5-nano` (mean 61.9) and `gpt-5-mini` (68.2)
trailed. `gpt-5.1` (78.2), `gpt-5.4-mini` (79.6) and `gpt-5.4` (82.1) scored
about as well as `gpt-6-luna` (81.5) at 8 to 25 times its price, and
`gpt-5.6-terra` (85.4) did worse than `gpt-6-sol` (87.0) at the same price.
`gpt-6-astra` (mean 90.4 on the screen, p95 2716 ms, $17.57/1k without cache)
and `gpt-6.1-sol` (88.7, p95 2520 ms) have no row: the account ran out of
credits during their 40 sample (440 and 123 provider errors).

Since 15 Oct 2026 one model polishes every chunk (`escalation.md`), and the
defaults keep the old pair's models until the bench picks the cheap one: polish
`gpt-5.6-luna`, big (commands and corrections) `gpt-6-luna`, fallback
`gpt-5.6-luna`. Everything below this paragraph measured the two tiers before
15 Oct. The defaults in `.env.example` were the founder's pair: small
`gpt-5.6-luna`, big `gpt-6-luna`, fallback `gpt-5.6-luna` (founder, 11 Oct: a
model other than the
big tier's, from the same family, needing no second key). Round b1 briefly made
`gpt-6-luna` the small tier to keep the heavy month under $0.50; the founder
reverted it (D8, 11 Oct: keep the same models, accuracy over cost and time), so
the month is a report (`replay.md`) and the $0.50 target only a reference. On
round b1's A/B (364 rows: every founder row, 150 dev rows the round missed and
150 random dev rows, scratchpad `acc2/fix-b1`), `gpt-6-luna` without reasoning
as the small tier got 12 English rows right that `gpt-5.6-luna` missed and
missed 4 it got (103 of 135 English rows against 95), at p50 2036 ms and p95
5814 ms against 2114 and 8724 ms. Since 14 Oct both tiers reason at medium
(`escalation.md`). Both tiers are near the best accuracy within budget; the
trade is about twice the p95 of the `gpt-4.1` models. Every model below is in
`SAMPLING`, `PRICES` and `CACHED`, so switching the pair took only
`SNAPBAK_SMALL` and `SNAPBAK_BIG` in `.env`, no code (now `SNAPBAK_POLISH`). A
heavy month (`just replay`) on the 11 Oct prompt, at a 50 % and a 90 % cache
share (corpus mix / the same month with every chunk on the big tier, measured
before English only; Hindi and Hinglish were dropped on 14 Oct 2026, and
`replay.md` has today's):

| Small, big                     | Small items | p95 ms       | 50 % cached     | 90 % cached     |
| ------------------------------ | ----------- | ------------ | --------------- | --------------- |
| `gpt-5.6-luna`, `gpt-6-luna`   | 94.6        | 2530 to 3096 | $0.414 / $0.283 | $0.184 / $0.141 |
| `gpt-4.1-nano`, `gpt-4.1-mini` | 72.3        | 1273 to 1523 | $0.607 / $1.084 | $0.366 / $0.667 |
| `gpt-4.1-mini`, `gpt-4.1-mini` | 92.9        | 1224 to 1251 | $1.037 / $1.084 | $0.607 / $0.667 |
| `gpt-4.1-mini`, `gpt-6-luna`   | 92.9        | 1224 to 2380 | $0.683 / $0.257 | $0.378 / $0.132 |
| `gpt-6-luna`, `gpt-5.6-luna`   | 94.6        | 2134 to 2380 | $0.360 / $0.538 | $0.177 / $0.287 |
| `gpt-6-luna`, `gpt-6-luna`     | 94.6        | 2380         | $0.240 / $0.257 | $0.111 / $0.132 |

The default pair's p95 is its own, live with reasoning on the big tier: 2530 to
2535 ms on the whole corpus and 2773 to 3096 ms on the dev and held-out samples,
where more chunks go big (`bench.md`). No other pair with a `gpt-6-luna` or
`gpt-5.6-luna` tier has run live as a pair: its p95 range is each model's own.
Small items are the small model's.

The `gpt-4.1` models measured 90 % cached at 16 in flight, the `gpt-6` and
`gpt-5.6` models 25 % cold and 99.6 to 99.9 % warm; the month is priced at the
50 % the founder set. The `gpt-6-luna` pair is the most accurate and, at equal
cache, the cheapest, but about twice as slow at p95. Every "sol" big tier goes
over $0.50: `gpt-6-sol` costs $2.33 a month at 50 % cached and $1.18 at 90 %.

No model meets the 500 ms p95 goal for the last chunk; the fastest p95 is about
1.2 s. The connection is not the cause: TCP connect to the API takes 6 to 15 ms,
so the time is the model's own.

The `priority` service tier (shown as "fast" for the `gpt-6` models) cut p50 by
150 to 400 ms on the newer models and not at all on `gpt-4.1`. It costs twice
the Standard price; every OpenAI call uses it (see The prompt cache).
