# Snapbak backend

Snapbak Cloud AI. The Snapbak macOS app sends each chunk of a dictation here
while the user still holds the key. The server polishes it: punctuation, casing,
lists, numbers, small grammar fixes, and corrections such as "no wait, make it
Thursday". It also rewrites a selection when the user speaks a command.
Dictation stores nothing and logs no text.

It also makes cloud transcripts: the app uploads a recording's audio, and the
server and its worker send it to AssemblyAI and pyannoteAI and store words,
times, speakers and names in Postgres ([docs/cloud.md](docs/cloud.md)). And it
serves share links: the app uploads a recording's web video, and anyone with the
link watches it at `snapbak.site/v/<slug>`
([docs/cloud.md](docs/cloud.md#share-links)).

Meetings ride on cloud transcripts (MEETINGS.md): a recording promoted to a
meeting gets minutes from one model call (summary, minutes, decisions, action
items, open questions, pins), every claim checked against the transcript by
rules, then per-meeting questions and a search over all minutes. Its share page
shows the player, the minutes and the full transcript
([docs/cloud.md](docs/cloud.md#meetings)).

Snapbak is English only (14 Oct 2026); text is never translated.

## Run it

```sh
mise install                 # Python 3.14, uv and just, as pinned
uv sync
cp .env.example .env         # then fill it in: SETUP.md
just serve                   # http://127.0.0.1:8787
```

For the app's end-to-end tests, run it with the fake model:

```sh
SNAPBAK_POLISH=fake:edit SNAPBAK_BIG=fake:edit SNAPBAK_TOKEN=dev just serve
```

```sh
curl -s localhost:8787/healthz
curl -s localhost:8787/v1/polish -H 'Authorization: Bearer dev' \
  -H 'Content-Type: application/json' -d @tests/contract/polish.request.json
```

## How it works

1. `POST /v1/polish` checks the token and the request limits.
2. One polish model answers every chunk: `gpt-6-luna` without reasoning, on
   OpenAI's priority tier, the R7 round's pick (15 Oct 2026, `docs/bench.md`).
   Commands go to the big model.
3. The model gets a system prompt of a core and the rule modules of the
   request's `features` (every module when it sends none), the worked examples
   the examples policy picks, and the request as JSON, and answers
   `{"revision", "text"}`.
4. The guardrail checks the answer (a stutter like "it's it's" may lose a copy,
   and a word the recognizer misheard may become the word that sounds like it):
   no unheard words or numbers, no lost or invented dictionary term, no rewrite
   of over 30 %, no dropped sentence, no flipped negation or tense, no dropped
   qualifier, no reply, no question left out of order. An answer refused only
   for a mark or a list's shape is mended without a model when it can be. A
   refused answer is corrected once by the correction model (the big one by
   default, or none) with reasoning if at least 1.5 s is left: it gets its
   answer back and the request again, with the rule and the words the guardrail
   named in a `checker` field. If the correction is refused too, the request
   text comes back unchanged with `rejected: true`.
5. A provider's `429` or `5xx` answer is asked again with backoff while time is
   left. If a provider fails, the fallback engine is tried once. If it hasn't
   answered after 10 s (a correction: after half the time it has left), the
   fallback is asked too, without reasoning, and the first answer wins. If none
   answers in time, the answer is `502 {"error": "provider"}`. The app gives
   each chunk up to 20 s.

## Code

`src/snapbak_backend/`, top to bottom; each part imports only those above it.

| Path             | Holds                                                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config/`        | `settings.py` reads the environment; `pricing.py` holds the price table                                                                                                   |
| `ai/`            | the model layer: `prompts/`, `providers/`, `routing.py` (correction cues from `cues.json`, paragraphs), `chain.py` (a model, then the fallback, in time) and `guardrail/` |
| `schemas/`       | the request and response models                                                                                                                                           |
| `transformers/`  | a request as model turns, and the model's JSON back as an answer                                                                                                          |
| `db/`            | the cloud tables (SQLAlchemy) and the engine; migrations live in `migrations/`                                                                                            |
| `transcription/` | the `TranscriptionStrategy` facade, AssemblyAI + pyannoteAI, the fake, the merge, languages and voiceprint spans                                                          |
| `services/`      | the polish and command flows, the counts-only log line, and the cloud services: storage, events, jobs, usage, sealing, shares, meetings, minutes, details, folding, qa, purge |
| `api/`           | the token check, the error bodies, one module per route, and `share_page.py`, the public share page                                                                       |
| `templates/`     | the share page and its error page, with the app's design tokens (`tokens.css`, a copy checked by a test)                                                                  |
| `main.py`        | the app: startup, error handlers and routes                                                                                                                               |
| `worker.py`      | the arq worker's tasks for cloud transcripts                                                                                                                              |

## Docs

- [API, limits, errors and the guardrail](docs/api.md)
- [Cloud transcripts, meetings and share links: routes, worker,
  runbook](docs/cloud.md)
- [Providers, the fake model and prices](docs/providers.md)
- [Escalation](docs/escalation.md)
- [Bench: corpus, gates, latency and cost](docs/bench.md)
- [Replay: a heavy user's month](docs/replay.md)

## Development

`just test` runs every test level (start the compose stack with `just db-up`
and set the AWS credentials first, or the cloud tests fail; a plain
`uv run pytest` skips them instead; see [SETUP.md](SETUP.md)), and `just lint`
runs ruff and pyright. Rules
for working here are in [CLAUDE.md](CLAUDE.md).
