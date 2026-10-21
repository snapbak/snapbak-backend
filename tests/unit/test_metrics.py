import logging

import pytest

from snapbak_backend.ai.chain import Call, retries
from snapbak_backend.ai.providers.base import Usage
from snapbak_backend.services.metrics import log, task_log


def test_the_log_line_counts_the_retries_of_every_call(caplog: pytest.LogCaptureFixture) -> None:
    # A provider's 429 and 5xx answers asked again are logged as a count, never with text (D6).
    calls = [Call("openai:small", Usage(0, 0, 0, 2), "error"), Call("openai:big", Usage(100, 10, 0, 1), "ok")]
    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        log("/v1/polish", "openai:big", "big", 900, 110, "error>ok", retries(calls))
    assert caplog.messages == [
        "route=/v1/polish provider=openai model=big tier=big ms=900 tokens=110 verdict=error>ok retries=3"
    ]


def test_trx01_a_task_logs_its_job_vendor_time_and_verdict_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        task_log("merge_and_store", "5d0a", "-", 840, "ready")
    assert caplog.messages == ["task=merge_and_store job=5d0a vendor=- ms=840 verdict=ready"]
