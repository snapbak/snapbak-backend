import asyncio
import json
import math
import sys
from pathlib import Path

import pytest

import bench
from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.base import ProviderError, Turn, Usage
from snapbak_backend.ai.providers.fake import Fake

# gpt-6-luna lists $0.10 per million input tokens, so each call below costs $0.10.
MILLION = Usage(1_000_000, 0)


class Priced:
    """Echoes the text as gpt-6-luna would bill it; `crash_after` calls in, it raises an unexpected error and
    `fail_after` calls in a provider error."""

    def __init__(self, engine: str = "openai:gpt-6-luna", crash_after: int = 0, fail_after: int = 0) -> None:
        self.engine = engine
        self.echo = Fake("echo")
        self.sent = 0
        self.crash_after = crash_after
        self.fail_after = fail_after

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        self.sent += 1
        if self.crash_after and self.sent > self.crash_after:
            raise RuntimeError("crash")
        if self.fail_after and self.sent > self.fail_after:
            raise ProviderError("down")
        text, _ = await self.echo.complete(turns, max_tokens, timeout, reason)
        return text, MILLION

    async def aclose(self) -> None:
        pass


def rows(count: int) -> list[bench.Item]:
    made = [
        bench.Item("grammar", "tense", "She goes home.", "", [], "en", "other", ["She goes home."])
        for _ in range(count)
    ]
    return made


def entries(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found = [json.loads(line) for line in lines]
    return found


def run_main(monkeypatch: pytest.MonkeyPatch, provider: Priced, count: int, ledger: Path, budget: float) -> int:
    monkeypatch.setattr(bench, "load", lambda: rows(count))
    monkeypatch.setattr(bench, "make_provider", lambda spec, keys: provider)
    monkeypatch.setattr(bench, "CONCURRENCY", 1)
    argv = ["bench.py", provider.engine, "--ledger", str(ledger), "--budget-usd", str(budget)]
    monkeypatch.setattr(sys, "argv", argv)
    code = bench.main()
    return code


def test_a_run_appends_its_cost_to_the_ledger_and_prints_this_run_and_the_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "spend.jsonl"
    earlier = {"usd": 1.0, "items": 10, "spec": "openai:gpt-5.6-luna", "note": "ok"}
    ledger.write_text(json.dumps(earlier) + "\n", encoding="utf-8")
    run_main(monkeypatch, Priced(), 3, ledger, 40)
    written = entries(ledger)
    assert written[0] == earlier
    assert written[1]["usd"] == pytest.approx(0.3)
    assert (written[1]["items"], written[1]["spec"], written[1]["note"]) == (3, "openai:gpt-6-luna", "ok")
    assert "$ this run: 0.3000; ledger total: 1.3000" in capsys.readouterr().out


def test_a_missing_ledger_is_created_with_the_runs_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    run_main(monkeypatch, Priced(), 1, ledger, 40)
    assert [entry["items"] for entry in entries(ledger)] == [1]


def test_a_spent_budget_sends_nothing_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(json.dumps({"usd": 40.0, "items": 9, "spec": "x", "note": "ok"}) + "\n", encoding="utf-8")
    provider = Priced()
    code = run_main(monkeypatch, provider, 3, ledger, 40)
    assert (code, provider.sent) == (1, 0)
    assert "budget reached: $40.00 of $40.00" in capsys.readouterr().out
    assert entries(ledger)[1]["usd"] == 0
    assert entries(ledger)[1]["note"] == "budget reached"


def test_the_run_stops_at_the_budget_and_scores_only_what_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "spend.jsonl"
    provider = Priced()
    code = run_main(monkeypatch, provider, 5, ledger, 0.25)
    out = capsys.readouterr().out
    assert (code, provider.sent) == (1, 3)
    assert "openai:gpt-6-luna: 3 items" in out
    assert "budget reached: $0.30 of $0.25" in out
    assert entries(ledger)[0]["usd"] == pytest.approx(0.3)


def test_the_ledger_line_is_written_when_the_run_crashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    with pytest.raises(RuntimeError):
        run_main(monkeypatch, Priced(crash_after=1), 2, ledger, 40)
    [entry] = entries(ledger)
    # The answered call, and the crashed one charged as the dearest so far.
    assert entry["usd"] == pytest.approx(0.2)
    assert entry["note"] == "crashed: RuntimeError"


def test_a_budgeted_run_refuses_an_engine_with_no_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = Priced(engine="openai:gpt-unpriced")
    code = run_main(monkeypatch, provider, 2, tmp_path / "spend.jsonl", 40)
    assert (code, provider.sent) == (1, 0)
    assert "no price for openai:gpt-unpriced" in capsys.readouterr().out


def test_a_call_that_failed_without_usage_is_charged_as_the_dearest_call_so_far() -> None:
    budget = bench.Budget(40, 0)
    budget.charge("openai:gpt-6-luna", MILLION)
    budget.charge("openai:gpt-6-luna", None)
    assert budget.spent == pytest.approx(0.2)


def test_an_item_whose_provider_failed_is_charged_as_one_call() -> None:
    budget = bench.Budget(40, 0)
    provider = bench.Metered(Priced(fail_after=1), budget)
    tiers = Tiers(small=provider, big=provider, fallback=None)
    gate = asyncio.Semaphore(1)
    first, second = rows(2)
    asyncio.run(bench.run(first, tiers, gate, budget))
    failed = asyncio.run(bench.run(second, tiers, gate, budget))
    assert failed is not None and failed.failed
    assert (budget.spent, budget.items) == (pytest.approx(0.2), 2)


def test_the_real_escalation_runs_with_the_small_model_as_its_fallback_as_the_server_does() -> None:
    # Round b5: the server's fallback is the small model (gpt-5.6-luna), which joins a slow big answer or retry; a
    # bench with no fallback timed long retries out where the server would have answered.
    budget = bench.Budget(math.inf, 0.0)
    tiers = bench.tiers_of("fake:echo+fake:slow", budget)
    assert tiers is not None
    assert tiers.fallback is not None
    assert tiers.fallback.engine == tiers.small.engine == "fake:echo"
    alone = bench.tiers_of("fake:echo", budget)
    assert alone is not None
    assert alone.fallback is None
