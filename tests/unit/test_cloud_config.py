import base64
import json

import pytest

from snapbak_backend.config.settings import CloudConfig, load
from snapbak_backend.services.storage import Storage, s3_client

POLISH = {"SNAPBAK_TOKEN": "dev", "SNAPBAK_BIG": "fake:edit"}
KEY = base64.b64encode(bytes(range(32))).decode()
CLOUD = {
    **POLISH,
    "DATABASE_URL": "postgresql+asyncpg://snapbak:dev@127.0.0.1:15432/snapbak",
    "REDIS_URL": "redis://127.0.0.1:16379/0",
    "S3_REGION": "ap-south-1",
    "S3_BUCKET": "snapbak-media",
    "SNAPBAK_AWS_ACCESS_KEY_ID": "AKIASNAPBAK",
    "SNAPBAK_AWS_SECRET_ACCESS_KEY": "snapbak-secret",
    "SNAPBAK_PUBLIC_URL": "http://127.0.0.1:8787/",
    "SNAPBAK_SHARE_BASE_URL": "https://snapbak.site/",
    "SNAPBAK_HOME_URL": "https://snapbak.app",
    "SNAPBAK_TRANSCRIPTION": "fake:two-speakers-en",
    "SNAPBAK_WEBHOOK_SECRET": "hook",
    "VOICEPRINT_KEYS": json.dumps({"1": KEY}),
}


def test_cld01_without_a_database_the_polish_only_server_has_no_cloud() -> None:
    assert load(POLISH).cloud is None


def test_cld01_the_cloud_settings_come_from_the_environment() -> None:
    assert load(CLOUD).cloud == CloudConfig(
        database_url="postgresql+asyncpg://snapbak:dev@127.0.0.1:15432/snapbak",
        redis_url="redis://127.0.0.1:16379/0",
        s3_region="ap-south-1",
        bucket="snapbak-media",
        aws_access_key_id="AKIASNAPBAK",
        aws_secret_access_key="snapbak-secret",
        public_url="http://127.0.0.1:8787",
        share_url="https://snapbak.site",
        home_url="https://snapbak.app",
        transcription="fake:two-speakers-en",
        assemblyai_key="",
        pyannote_key="",
        pyannote_webhook_secret="",
        webhook_secret="hook",
        voiceprint_keys={1: bytes(range(32))},
        waitlist_token="",
    )


def test_cld01_signed_urls_point_at_the_bucket_on_s3_in_its_region() -> None:
    """Never the global host: a new bucket answers it with a redirect for a while, which a signed URL can't follow."""
    config = load(CLOUD).cloud
    assert config is not None
    url = Storage(s3_client(config), config.bucket).presign_get("audio/a.m4a", 60)
    assert url.startswith("https://snapbak-media.s3.ap-south-1.amazonaws.com/audio/a.m4a?")


def test_cld01_s3_signs_with_the_snapbak_iam_user_never_the_machines_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADEFAULT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "default-secret")
    monkeypatch.setenv("AWS_PROFILE", "default")
    config = load(CLOUD).cloud
    assert config is not None
    url = Storage(s3_client(config), config.bucket).presign_get("audio/a.m4a", 60)
    assert "X-Amz-Credential=AKIASNAPBAK%2F" in url


@pytest.mark.parametrize(
    "name",
    [
        "REDIS_URL",
        "S3_BUCKET",
        "SNAPBAK_AWS_ACCESS_KEY_ID",
        "SNAPBAK_AWS_SECRET_ACCESS_KEY",
        "SNAPBAK_PUBLIC_URL",
        "SNAPBAK_SHARE_BASE_URL",
        "SNAPBAK_HOME_URL",
        "SNAPBAK_WEBHOOK_SECRET",
    ],
)
def test_cld01_a_missing_cloud_setting_is_named(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        load({**CLOUD, name: ""})


@pytest.mark.parametrize("keys", ["", "nope", '{"1": "c2hvcnQ="}', '{"x": "' + KEY + '"}'])
def test_trx08_voiceprint_keys_must_be_a_map_of_32_byte_keys(keys: str) -> None:
    with pytest.raises(ValueError, match="VOICEPRINT_KEYS"):
        load({**CLOUD, "VOICEPRINT_KEYS": keys})


def test_trx01_the_real_strategy_needs_its_vendor_keys() -> None:
    with pytest.raises(ValueError, match="ASSEMBLYAI_API_KEY"):
        load({**CLOUD, "SNAPBAK_TRANSCRIPTION": "assemblyai+pyannote"})


def test_shr01_the_home_page_must_not_be_the_share_hosts_own_root() -> None:
    with pytest.raises(ValueError, match="SNAPBAK_HOME_URL"):
        load({**CLOUD, "SNAPBAK_HOME_URL": "https://snapbak.site/"})
