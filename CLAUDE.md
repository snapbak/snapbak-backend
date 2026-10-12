# Snapbak backend

The Snapbak Cloud AI server: it polishes dictated text and runs spoken commands
for the Snapbak macOS app. One FastAPI process, no storage. The app repo lives
next door in `../snapbak`.

## Rules

1. **Never commit.** Git is read-only for you (status, diff, log, show). No
   init, add, commit, push, branch, stash, reset or worktree. Leave every change
   uncommitted and end with a suggested Conventional Commits message.
2. **Test first.** Write the failing test, watch it fail for the right reason,
   then write the smallest code that passes. Never skip, delete or weaken a test
   to get green.
3. **No text in logs.** Logs carry the route, provider, model, tier, time,
   tokens and verdict only. Never log dictated text, prior text, terms, a
   selection, an instruction or a model's output. The tests in
   `tests/integration/test_app.py` and `tests/e2e/` check this.
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
| `just test`   | unit, integration, functional and end-to-end tests       |
| `just lint`   | ruff check, ruff format --check, pyright                 |
| `just bench`  | quality gates, latency and cost per engine (needs keys)  |
| `just replay` | a heavy user's month of cost against $0.50 (offline)     |
| `just corpus` | generate missing corpus slices with OpenAI (needs a key) |

Tools are pinned in `.mise.toml`: run `mise install`, then `uv sync`. Settings
and keys go in `.env` (copy `.env.example`).

## Map

| Need                           | Read                 |
| ------------------------------ | -------------------- |
| Where the code lives           | `README.md`, Code    |
| The HTTP API and the guardrail | `docs/api.md`        |
| Providers, the fake, prices    | `docs/providers.md`  |
| When the big model runs        | `docs/escalation.md` |
| The corpus, gates and bench    | `docs/bench.md`      |
| The monthly cost replay        | `docs/replay.md`     |
