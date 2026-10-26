set dotenv-load

# Serve the API on 127.0.0.1 (PORT, default 8787). Connections stay open 75 s, longer than any hold.
serve:
    uv run uvicorn snapbak_backend.main:app --host 127.0.0.1 --port ${PORT:-8787} --timeout-keep-alive 75

# The arq worker for the cloud transcript (needs `just db-up` and DATABASE_URL, REDIS_URL in .env).
worker:
    uv run arq snapbak_backend.worker.WorkerSettings

# Postgres and Redis in Docker, with the schema migrated (docs/cloud.md).
db-up:
    docker compose up -d --wait postgres redis
    DATABASE_URL=${DATABASE_URL:-postgresql+asyncpg://snapbak:dev@127.0.0.1:15432/snapbak} uv run alembic upgrade head

# The lifecycle rules on S3_BUCKET; once (SETUP.md).
bucket-rules:
    uv run scripts/bucket_rules.py

# Stop the compose stack; its data stays in Docker volumes.
db-down:
    docker compose down

# Apply the migrations to DATABASE_URL.
migrate:
    uv run alembic upgrade head

# Unit, integration, functional and end-to-end tests. A test that needs the compose stack or the AWS key fails
# here when either is missing (a plain `uv run pytest` skips it, with the reason); start the stack with `just db-up`.
test:
    SNAPBAK_REQUIRE_CLOUD=1 uv run pytest

# Lint, format check and type check.
lint:
    uv run ruff check
    uv run ruff format --check
    uv run pyright

# Real AssemblyAI and pyannoteAI on bench/transcription/ audio: costs money, on demand only, never in `just test`
# (docs/cloud.md). Needs the vendor keys, a real bucket and a tunnel for the webhooks.
test-cloud *args:
    uv run pytest -m cloud
    uv run scripts/test_cloud.py {{ args }}

# Polish quality, latency and cost per engine on the corpus sample (needs keys).
bench *specs="openai:gpt-5.6-luna+openai:gpt-6-luna openai:gpt-4.1-nano+openai:gpt-4.1-mini":
    uv run scripts/bench.py {{ specs }}

# A heavy user's month through the escalation, priced (offline).
replay *engines:
    uv run scripts/replay.py {{ engines }}

# Generate the missing corpus slices with OpenAI (needs OPENAI_API_KEY).
corpus *args:
    uv run scripts/gen_corpus.py {{ args }}

# Our transcript code scored on AMI meetings with seeded vendor errors (free); fails when worse than the baseline.
eval-transcript *args:
    uv run scripts/eval_transcript.py {{ args }}

# The same AMI excerpts through the real vendors (costs money): needs both keys, a real bucket and --budget-usd.
eval-transcript-live *args:
    uv run scripts/eval_transcript.py --live {{ args }}

# Minutes (per section, details kept, probes, contradictions, Hinglish) and Ask scored on AMI, QMSum and hand-made
# meetings, recorded answers replayed (free); fails below the baseline or a gate.
eval-minutes *args:
    uv run scripts/eval_minutes.py {{ args }}

# The same through the server's polish model (costs money): needs OPENAI_API_KEY and --budget-usd, the ledger cap.
# `just eval-minutes --private DIR --budget-usd USD` runs exported meetings from outside the repo instead.
eval-minutes-live *args:
    uv run scripts/eval_minutes.py --live {{ args }}

# MOM-04 end to end on real vendors and the real model: P95 stop to minutes for a 30 min meeting (costs money).
stop-to-minutes *args:
    uv run scripts/stop_to_minutes.py {{ args }}

# D39's unattended success from the worker's verdict lines (`just unattended < worker.log`); exits 1 under 99 %.
unattended *args:
    uv run scripts/unattended.py {{ args }}
