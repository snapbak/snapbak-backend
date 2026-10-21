"""Server settings from the environment (docs/providers.md)."""

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

KEYS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
# How many worked examples go before a polish request: every one (as before 15 Oct 2026), a few per flagged prompt
# module, or none (docs/api.md).
type ExamplesPolicy = Literal["all", "per_module", "none"]
# The one polish model and its effort, the R7 round's winner (15 Oct 2026, docs/bench.md).
POLISH = "openai:gpt-6-luna"
EFFORT = "none"
POLICIES: tuple[ExamplesPolicy, ...] = get_args(ExamplesPolicy.__value__)
# The cloud transcript's limits (CLD-03, Q98): 2 h of audio a job, 120 minutes a device a UTC day.
MAX_JOB_SECONDS = 7200
DAILY_SECONDS = 7200
# An install ID alone mints a device token, so each address may register this many devices an hour (HLD risk table).
REGISTRATIONS_PER_HOUR = 10
VOICEPRINT_KEY_BYTES = 32
# The share caps (Q98, HLD open question 1): a video's bytes, and the shares a device may have that are not deleted.
SHARE_MAX_BYTES = 1_073_741_824
SHARES_PER_DEVICE = 100
# Caption lines a device may translate a UTC day (CLD-03, 16-caption-translation LLD): about 130 ten-minute talks.
DAILY_TRANSLATED_LINES = 20_000
# Default early-access spot pool when the waitlist status is first read and no row exists yet.
WAITLIST_SPOTS_DEFAULT = 100


@dataclass(frozen=True)
class CloudConfig:
    """Postgres, Redis, S3 and the transcription vendors (docs/cloud.md)."""

    database_url: str
    redis_url: str
    s3_region: str
    # One bucket for every environment, by folder: `audio/`, `raw/`, `clips/`, `shares/` (SETUP.md).
    bucket: str
    # The `snapbak` IAM user's key. S3 uses only this, never the machine's AWS profile or `AWS_*` variables.
    aws_access_key_id: str
    aws_secret_access_key: str
    public_url: str
    # The share page's base, `https://snapbak.site`, and the product home page its call to action opens (12 LLD).
    share_url: str
    home_url: str
    transcription: str
    assemblyai_key: str
    pyannote_key: str
    pyannote_webhook_secret: str
    webhook_secret: str
    voiceprint_keys: dict[int, bytes]
    waitlist_token: str


@dataclass(frozen=True)
class Config:
    token: str
    polish: str
    big: str
    correction: str | None
    fallback: str | None
    temperature: float
    effort: str
    examples: ExamplesPolicy
    keys: dict[str, str]
    cloud: CloudConfig | None = None
    # The caption translation model; None asks the big model (16-caption-translation LLD).
    translate: str | None = None


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"Set {name} in the environment (see .env.example).")
    return value


def temperature(env: Mapping[str, str]) -> float:
    said = env.get("SNAPBAK_POLISH_TEMPERATURE") or "0"
    try:
        value = float(said)
    except ValueError:
        value = -1.0
    if not 0 <= value <= 2:
        raise ValueError(f"Set SNAPBAK_POLISH_TEMPERATURE to a number from 0 to 2, not '{said}'.")
    return value


def examples(env: Mapping[str, str]) -> ExamplesPolicy:
    said = env.get("SNAPBAK_EXAMPLES_POLICY") or "all"
    policy: ExamplesPolicy | None = next((known for known in POLICIES if known == said), None)
    if policy is None:
        raise ValueError(f"Set SNAPBAK_EXAMPLES_POLICY to one of {', '.join(POLICIES)}, not '{said}'.")
    return policy


def voiceprint_keys(env: Mapping[str, str]) -> dict[int, bytes]:
    """`VOICEPRINT_KEYS`, a JSON map of key ID (1 to 255) to a base64 AES-256 key."""
    try:
        said = json.loads(env.get("VOICEPRINT_KEYS") or "")
        keys = {int(key_id): base64.b64decode(key, validate=True) for key_id, key in said.items()}
    except ValueError, AttributeError, TypeError, binascii.Error:
        keys = {}
    if not keys or any(not 1 <= key_id <= 255 or len(key) != VOICEPRINT_KEY_BYTES for key_id, key in keys.items()):
        raise ValueError("Set VOICEPRINT_KEYS to a JSON map of key ID (1 to 255) to a base64 32-byte key.")
    return keys


def cloud(env: Mapping[str, str]) -> CloudConfig | None:
    """The cloud settings, or None without DATABASE_URL, so the polish-only server starts without a database."""
    database_url = env.get("DATABASE_URL", "")
    if not database_url:
        return None
    transcription = env.get("SNAPBAK_TRANSCRIPTION") or "fake:two-speakers-en"
    real = transcription == "assemblyai+pyannote"
    share_url = required(env, "SNAPBAK_SHARE_BASE_URL").rstrip("/")
    home_url = required(env, "SNAPBAK_HOME_URL")
    if home_url.rstrip("/") == share_url:
        # `/` on the share host redirects to the home page, which would then be itself.
        raise ValueError("Set SNAPBAK_HOME_URL to the product home page, not the share host's own root.")
    vendor = required if real else lambda env, name: env.get(name, "")
    return CloudConfig(
        database_url=database_url,
        redis_url=required(env, "REDIS_URL"),
        s3_region=env.get("S3_REGION") or "ap-south-1",
        bucket=required(env, "S3_BUCKET"),
        aws_access_key_id=required(env, "SNAPBAK_AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=required(env, "SNAPBAK_AWS_SECRET_ACCESS_KEY"),
        public_url=required(env, "SNAPBAK_PUBLIC_URL").rstrip("/"),
        share_url=share_url,
        home_url=home_url,
        transcription=transcription,
        assemblyai_key=vendor(env, "ASSEMBLYAI_API_KEY"),
        pyannote_key=vendor(env, "PYANNOTE_API_KEY"),
        pyannote_webhook_secret=vendor(env, "PYANNOTE_WEBHOOK_SECRET"),
        webhook_secret=required(env, "SNAPBAK_WEBHOOK_SECRET"),
        voiceprint_keys=voiceprint_keys(env),
        waitlist_token=env.get("SNAPBAK_WAITLIST_TOKEN") or "",
    )


def load(env: Mapping[str, str]) -> Config:
    keys = {provider: env.get(name, "") for provider, name in KEYS.items()}
    big = required(env, "SNAPBAK_BIG")
    # A refused answer is corrected once, on the big model unless another is named; "none" turns the retry off.
    correction = env.get("SNAPBAK_CORRECTION") or big
    return Config(
        token=required(env, "SNAPBAK_TOKEN"),
        polish=env.get("SNAPBAK_POLISH") or POLISH,
        big=big,
        correction=None if correction == "none" else correction,
        fallback=env.get("SNAPBAK_FALLBACK") or None,
        temperature=temperature(env),
        effort=env.get("SNAPBAK_POLISH_EFFORT") or EFFORT,
        examples=examples(env),
        keys=keys,
        cloud=cloud(env),
        translate=env.get("SNAPBAK_TRANSLATE") or None,
    )
