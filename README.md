# Snapbak backend

Snapbak Cloud AI. The Snapbak macOS app sends each chunk of a dictation here
while the user still holds the key. The server polishes it: punctuation, casing,
lists, numbers, small grammar fixes, and corrections such as "no wait, make it
Thursday". It also rewrites a selection when the user speaks a command. It
stores nothing and logs no text.

Snapbak is English only (14 Oct 2026); text is never translated.

## Run it

```sh
mise install                 # Python 3.14, uv and just, as pinned
uv sync
cp .env.example .env         # then fill in the keys you have
just serve                   # http://127.0.0.1:8787
```

For the app's end-to-end tests, run it with the fake model:

```sh
SNAPBAK_SMALL=fake:edit SNAPBAK_BIG=fake:edit SNAPBAK_TOKEN=dev just serve
```

```sh
curl -s localhost:8787/healthz
curl -s localhost:8787/v1/polish -H 'Authorization: Bearer dev' \
  -H 'Content-Type: application/json' -d @tests/contract/polish.request.json
```

## How it works

1. `POST /v1/polish` checks the token and the request limits.
2. The escalation rules pick a tier: the small model, or the big one for
   corrections and long chunks.
3. The model gets a fixed system prompt, worked examples and the request as
   JSON, and answers `{"revision", "text"}`.
4. The guardrail checks the answer (a stutter like "it's it's" may lose a copy,
   and a word the recognizer misheard may become the word that sounds like it):
   no unheard words or numbers, no lost or invented dictionary term, no rewrite
   of over 30 %, no dropped sentence, no flipped negation or tense, no dropped
   qualifier, no reply, no question left out of order. An
   answer refused only for a mark or a list's shape is mended without a model
   when it can be. A refused answer, small or big, is corrected once by the big
   model with reasoning if at least 1.5 s is left: it gets its answer back and
   the request again, with the rule and the words the guardrail named in a
   `checker` field. If the correction is refused too, the request text comes
   back unchanged with `rejected: true`.
5. A provider's `429` or `5xx` answer is asked again with backoff while time is
   left. If a provider fails, the fallback engine is tried once. If it hasn't
   answered after 10 s (a correction: after half the time it has left), the
   fallback is asked too, without reasoning, and the first answer wins. If none
   answers in time, the answer is `502 {"error": "provider"}`. The app gives
   each chunk up to 20 s.

## Code

`src/snapbak_backend/`, top to bottom; each part imports only those above it.

| Path            | Holds                                                                                                                                  |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `config/`       | `settings.py` reads the environment; `pricing.py` holds the price table                                                                |
| `ai/`           | the model layer: `prompts/`, `providers/`, `routing.py` (which tier), `chain.py` (a tier, then the fallback, in time) and `guardrail/` |
| `schemas/`      | the request and response models                                                                                                        |
| `transformers/` | a request as model turns, and the model's JSON back as an answer                                                                       |
| `services/`     | the polish and command flows, and the counts-only log line                                                                             |
| `api/`          | the token check, the error bodies and one module per route                                                                             |
| `main.py`       | the app: startup, error handlers and routes                                                                                            |

## Docs

- [API, limits, errors and the guardrail](docs/api.md)
- [Providers, the fake model and prices](docs/providers.md)
- [Escalation](docs/escalation.md)
- [Bench: corpus, gates, latency and cost](docs/bench.md)
- [Replay: a heavy user's month](docs/replay.md)

## Development

`just test` runs every test level, and `just lint` runs ruff and pyright. Rules
for working here are in [CLAUDE.md](CLAUDE.md).
