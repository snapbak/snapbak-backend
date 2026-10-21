"""What a share's page shows, from its stored state, its expiry and its soft delete (12 HLD, Share states)."""

from datetime import UTC, datetime, timedelta

import pytest

from snapbak_backend.db.models import Share, page_state

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def share(state: str = "live", expires_at: datetime | None = None, deleted_at: datetime | None = None) -> Share:
    return Share(state=state, expires_at=expires_at, deleted_at=deleted_at)


@pytest.mark.parametrize("state", ["live", "uploading", "disabled"])
def test_shr01_a_share_without_expiry_shows_its_stored_state(state: str) -> None:
    assert page_state(share(state), NOW) == state


def test_shr01_a_live_share_expires_at_exactly_its_expiry() -> None:
    assert page_state(share(expires_at=NOW + timedelta(microseconds=1)), NOW) == "live"
    assert page_state(share(expires_at=NOW), NOW) == "expired"


def test_shr01_turned_off_wins_over_expired() -> None:
    assert page_state(share("disabled", expires_at=NOW - timedelta(days=1)), NOW) == "disabled"


def test_cld04_a_deleted_share_is_gone_whatever_its_state() -> None:
    assert page_state(share("live", deleted_at=NOW), NOW) == "gone"
