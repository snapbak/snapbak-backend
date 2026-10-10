# Snapbak backend

Snapbak Cloud AI. The Snapbak macOS app sends each chunk of a dictation here
while the user still holds the key. The server polishes it: punctuation, casing,
lists, numbers, small grammar fixes, and corrections such as "no wait, make it
Thursday". It also rewrites a selection when the user speaks a command. It
stores nothing and logs no text.

English, Hinglish (Hindi in Roman script) and Hindi (Devanagari) are polished in
the language and script they were spoken in, never translated.

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
   corrections, Hinglish, Hindi and long chunks.
3. The model gets a fixed system prompt, worked examples and the request as
   JSON, and answers `{"revision", "text"}`.
4. The guardrail checks the answer: no unheard words or numbers, no rewrite of
   over 30 %, no dropped sentence, no flipped negation, tense or speaker gender,
   no dropped qualifier, no reply. A rejected small answer is retried once on the
   big model if the deadline allows. If it is still rejected, the request text
   comes back unchanged with `rejected: true`.
5. If a provider fails or times out, the fallback engine is tried once. If that
   fails too, the answer is `502 {"error": "provider"}`.

## Docs

- [API, limits, errors and the guardrail](docs/api.md)
- [Providers, the fake model and prices](docs/providers.md)
- [Escalation](docs/escalation.md)
- [Bench: corpus, gates, latency and cost](docs/bench.md)
- [Replay: a heavy user's month](docs/replay.md)

## Development

`just test` runs every test level, and `just lint` runs ruff and pyright. Rules
for working here are in [CLAUDE.md](CLAUDE.md).
