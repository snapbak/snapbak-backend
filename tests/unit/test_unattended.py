"""`just unattended` (scripts/unattended.py): D39's unattended success, read from the worker's verdict lines."""

import unattended

LOG = """2026-10-26 09:00:01,000 task=generate_minutes job=m1 vendor=openai:gpt-6-luna ms=8000 verdict=ready:unattended
2026-10-26 09:05:00,000 task=generate_minutes job=m2 vendor=- ms=10 verdict=failed:provider
2026-10-26 09:20:00,000 task=generate_minutes job=m2 vendor=openai:gpt-6-luna ms=9000 verdict=ready:clicked
2026-10-26 10:00:00,000 task=merge_and_store job=j1 vendor=- ms=10 verdict=ready
2026-10-27 08:00:00,000 task=generate_minutes job=m3 vendor=openai:gpt-6-luna ms=7000 verdict=ready:unattended
2026-10-27 08:00:05,000 task=generate_minutes job=m3 vendor=openai:gpt-6-luna ms=7000 verdict=ready:clicked
"""


def test_mom04_a_meeting_counts_as_unattended_only_when_no_click_was_needed() -> None:
    """m1 made it alone; m2 failed and needed Try again; m3 made it alone, and its later regenerate doesn't undo
    that. Other tasks' lines don't count."""
    days = unattended.rates(LOG.splitlines())
    assert days == {"2026-10-26": (1, 2), "2026-10-27": (1, 1)}


def test_mom04_the_rate_below_99_percent_fails() -> None:
    assert unattended.main([], LOG.splitlines()) == 1
    clean = [line for line in LOG.splitlines() if "job=m2" not in line]
    assert unattended.main([], clean) == 0


def test_mom04_an_empty_or_unparsed_log_is_no_pass() -> None:
    """Nothing counted is no evidence of 99 %: a changed log format must fail the gate, not pass it (grading 3)."""
    assert unattended.main([], []) == 1
    assert unattended.main([], ["2026-10-26 nothing here"]) == 1


def test_mom04_a_real_worker_verdict_line_is_counted() -> None:
    """The line as the worker's own formatter writes it (worker.startup's basicConfig), through task_log."""
    import logging

    from snapbak_backend.services import metrics

    caught: list[str] = []
    formatter = logging.Formatter("%(asctime)s %(message)s")

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caught.append(formatter.format(record))

    keep = Keep()
    metrics.logger.addHandler(keep)
    metrics.logger.setLevel(logging.INFO)
    try:
        metrics.task_log("generate_minutes", "m9", "openai:gpt-6-luna", 9000, "ready:unattended")
    finally:
        metrics.logger.removeHandler(keep)
    assert len(caught) == 1
    assert unattended.rates(caught) == {caught[0][:10]: (1, 1)}


def test_mom04_minutes_ready_with_a_section_missing_are_no_unattended_success() -> None:
    """A section left incomplete needed a retry or a click: it is a miss toward the 99 % (grading 4)."""
    line = "2026-10-26 09:00:01,000 task=generate_minutes job=m7 vendor=x ms=1 verdict=ready:unattended:incomplete:1"
    assert unattended.rates([line]) == {"2026-10-26": (0, 1)}
