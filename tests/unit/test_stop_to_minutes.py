"""`just stop-to-minutes` (scripts/stop_to_minutes.py): MOM-04's P95 against 3 min, on canned stage timings."""

import stop_to_minutes


def row(total: float, first: float) -> dict[str, float]:
    return {"first section s": first, "total s": total}


def test_mom04_runs_inside_three_minutes_pass_and_one_over_at_p95_fails() -> None:
    passing, summary = stop_to_minutes.verdict([row(120.0, 30.0), row(150.0, 35.0), row(170.0, 40.0)])
    assert passing and summary["p95 total s"] == 170.0 and summary["p95 first section s"] == 40.0
    failing, _ = stop_to_minutes.verdict([row(120.0, 30.0), row(181.0, 35.0)])
    assert not failing


def test_mom04_no_run_is_no_pass() -> None:
    passing, summary = stop_to_minutes.verdict([])
    assert not passing and summary["runs"] == 0.0


def test_mom04_each_stage_s_time_names_the_slowest() -> None:
    """One run of five took 264 s of minutes (26 Oct 2026): each section's time, and the guard's after them, show
    which call it was."""
    saves = [(1.0, 9), (3.0, 8), (4.0, 7), (64.0, 6), (65.0, 5), (66.0, 4), (67.0, 3), (68.0, 2), (69.0, 1), (70.0, 0)]
    stages = stop_to_minutes.stages(0.0, [*saves, (80.0, 0)])
    assert stages["slowest"] == "decisions" and stages["slowest s"] == 60.0
    assert stages["guard s"] == 10.0
