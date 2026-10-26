"""The test stack's guard (grading 3): without the AWS key or Redis, a plain `pytest` skips the cloud tests, but
`just test` sets SNAPBAK_REQUIRE_CLOUD, so a run that skipped them can never read as green."""

import pytest
from conftest import absent


def test_a_missing_service_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNAPBAK_REQUIRE_CLOUD", raising=False)
    with pytest.raises(pytest.skip.Exception):
        absent("no Snapbak AWS key")


def test_a_missing_service_fails_when_the_cloud_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNAPBAK_REQUIRE_CLOUD", "1")
    with pytest.raises(pytest.fail.Exception, match="no Snapbak AWS key"):
        absent("no Snapbak AWS key")
