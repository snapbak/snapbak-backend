"""Share routes and pages log the route, the status and the time: never a title, a slug, a URL or the viewer's
address (CLAUDE.md rule 3, 12 LLD Telemetry)."""

import logging
import re
from typing import Any

import pytest
from conftest import RECORDING, register, share_live

TITLE = "Recording 2026-10-17 at 10.02.11"


def test_shr01_share_logs_hold_no_title_slug_url_or_address(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        auth = register(api)
        slug = share_live(api, auth)["slug"]
        for path in ("", "/video.mp4", "/poster.jpg", "/download", "/src"):
            api.get(f"/v/{slug}{path}", follow_redirects=False)
        api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
        api.get(f"/v/{slug}")
        api.patch(f"/v1/shares/{slug}x", json={"state": "live"}, headers=auth)
        api.patch(f"/v1/shares/{slug}", json={"state": "nope"}, headers=auth)
        api.delete(f"/v1/shares/{slug}", headers=auth)
    lines = [record.getMessage() for record in caplog.records if record.name == "snapbak_backend"]
    refused = [line.split(" ")[0] + " " + line.split(" ")[-2] for line in lines if not re.search(r"verdict=[23]", line)]
    assert refused == [
        "route=/v/{slug} verdict=410",
        "route=/v1/shares/{slug} verdict=not_found",
        "route=/v1/shares/{slug} verdict=invalid",
    ]
    for line in lines:
        for secret in (slug, TITLE, "testserver", "testclient", "X-Amz", RECORDING):
            assert secret not in line, (secret, line)
