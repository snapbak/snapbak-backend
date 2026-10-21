# Snapbak backend

The Snapbak Cloud AI server: it polishes dictated text and runs spoken commands
for the Snapbak macOS app, and turns recordings into transcripts with speakers.
Dictation stores nothing. Cloud transcripts keep their data in Postgres and S3,
with an arq worker on Redis (`docs/cloud.md`). The app repo lives next door in
`../snapbak`.

## Rules

1. **Never commit.** Git is read-only for you (status, diff, log, show). No
   init, add, commit, push, branch, stash, reset or worktree. Leave every change
   uncommitted and end with a suggested Conventional Commits message.
2. **Test first.** Write the failing test, watch it fail for the right reason,
   then write the smallest code that passes. Never skip, delete or weaken a test
   to get green.
3. **No text in logs.** Logs carry the route, provider, model, tier, time,
   tokens and verdict only. Never log dictated text, prior text, terms, a
   selection, an instruction or a model's output. Cloud transcripts log IDs,
   times and verdicts, never a word, a name or a title. The tests in
   `tests/integration/test_app.py`, `tests/functional/` and `tests/e2e/` check
   this.
4. **Docs move with the code.** Change `README.md` and `docs/` in the same
   change as the code they describe. Markdown is wrapped at 80 columns.
5. **Ask when in doubt.** If a requirement, the API contract or a product choice
   is unclear, ask before you build.
6. **The contract is shared.** `tests/contract/*.json` are byte-identical copies
   of the app's fixtures. Change them only together with the app.
7. **Small and readable.** The smallest change that fully works. No recursion;
   no long call chains in a `return`. No new dependency without an OK.

## Commands

| Command       | Does                                                     |
| ------------- | -------------------------------------------------------- |
| `just serve`  | the API on 127.0.0.1:8787                                |
| `just worker` | the arq worker for cloud transcripts                     |
| `just db-up`  | Postgres and Redis in Docker, migrated                   |
| `just bucket-rules` | the S3 buckets' lifecycle rules, once per environment |
| `just test`   | unit, integration, functional and end-to-end tests       |
| `just test-cloud` | real transcription vendors, on demand (costs money)  |
| `just lint`   | ruff check, ruff format --check, pyright                 |
| `just bench`  | quality gates, latency and cost per engine (needs keys)  |
| `just replay` | a heavy user's month of cost against $0.50 (offline)     |
| `just corpus` | generate missing corpus slices with OpenAI (needs a key) |
| `just eval-transcript` | transcript merge, lines, fillers, pauses, captions on AMI (free) |
| `just eval-transcript-live` | the same on real vendors (keys, `--budget-usd`; costs money) |

Tools are pinned in `.mise.toml`: run `mise install`, then `uv sync`. Settings
and keys go in `.env` (copy `.env.example`); `SETUP.md` walks through each one,
AWS included. Files live on real S3, local dev and tests included.

## Map

| Need                           | Read                 |
| ------------------------------ | -------------------- |
| Where the code lives           | `README.md`, Code    |
| The HTTP API and the guardrail | `docs/api.md`        |
| Cloud transcripts, the worker  | `docs/cloud.md`      |
| Providers, the fake, prices    | `docs/providers.md`  |
| When the big model runs        | `docs/escalation.md` |
| The corpus, gates and bench    | `docs/bench.md`      |
| The monthly cost replay        | `docs/replay.md`     |
