set dotenv-load

# Serve the API on 127.0.0.1 (PORT, default 8787). Connections stay open 75 s, longer than any hold.
serve:
    uv run uvicorn snapbak_backend.main:app --host 127.0.0.1 --port ${PORT:-8787} --timeout-keep-alive 75

# Unit, integration, functional and end-to-end tests.
test:
    uv run pytest

# Lint, format check and type check.
lint:
    uv run ruff check
    uv run ruff format --check
    uv run pyright

# Polish quality, latency and cost per engine on the corpus sample (needs keys).
bench *specs="openai:gpt-5.6-luna+openai:gpt-6-luna openai:gpt-4.1-nano+openai:gpt-4.1-mini":
    uv run scripts/bench.py {{ specs }}

# A heavy user's month through the escalation, priced (offline).
replay *engines:
    uv run scripts/replay.py {{ engines }}

# Generate the missing corpus slices with OpenAI (needs OPENAI_API_KEY).
corpus *args:
    uv run scripts/gen_corpus.py {{ args }}
