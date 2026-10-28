"""`just eval-minutes` end to end on the tiny AMI and QMSum excerpts in `tests/fixtures/meetings-eval/` (AMI CC BY
4.0, QMSum MIT; docs/bench.md): the checksummed caches, the parsers, the truth table, the fake provider, recorded
answers replayed through the real `generate` and Ask's one `qa.answer` call, the gates, the baseline and the results
file. Nothing is downloaded and no model is called."""

import hashlib
import json
import zipfile
from pathlib import Path

import eval_minutes as ev
import eval_transcript
import pytest

from snapbak_backend.services import minutes
from snapbak_backend.services.minutes import CALLS

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "meetings-eval"
GOOD = {"i": 2, "speaker": "Project Manager", "atMs": 96_000, "quote": "do some stuff"}
MADE_UP = {"i": 2, "speaker": "Project Manager", "atMs": 96_000, "quote": "do other stuff"}


def entry(path: Path) -> dict[str, str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"url": "https://example.invalid/" + path.name, "file": path.name, "sha256": digest}


def answer(derivation: dict[str, object]) -> str:
    made = {
        "summary": "{{p:1}} presented the agenda.",
        "mom": "Minutes.",
        "actionItems": [{"id": "act_1", "text": "Do some stuff", "derivedFrom": [derivation]}],
        "openQuestions": [],
        "decisions": [],
    }
    return json.dumps(made)


def sections(derivation: dict[str, object]) -> list[list[str]]:
    """A minutes v2 recording: each section's answer with the section it answered, the action item the only item."""
    doc = json.loads(answer(derivation))
    made = [
        ["summary", json.dumps({"title": "Agenda", "summary": doc["summary"]})],
        ["mom", json.dumps({"mom": doc["mom"]})],
    ]
    # The gold problem ("Nobody knew the agenda."), raised as an open question on the agenda line.
    agenda = {"i": 1, "speaker": "Project Manager", "atMs": 92_000, "quote": "this is our agenda"}
    asked = [{"id": "q_1", "text": "Nobody knew the agenda", "derivedFrom": [agenda]}]
    items = {"actionItems": doc["actionItems"], "openQuestions": asked}
    made += [[name, json.dumps({"items": items.get(name, [])})] for name in CALLS[2:]]
    return made


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ami, qmsum = tmp_path / "ami", tmp_path / "qmsum"
    ami.mkdir()
    qmsum.mkdir()
    with zipfile.ZipFile(ami / "tiny.zip", "w") as made:
        for file in sorted((FIXTURES / "ami").rglob("*.xml")):
            made.write(file, file.relative_to(FIXTURES / "ami").as_posix())
    (qmsum / "test.jsonl").write_bytes((FIXTURES / "qmsum.jsonl").read_bytes())
    (tmp_path / "ami.json").write_text(json.dumps({"annotations": entry(ami / "tiny.zip")}))
    manifest = {"meetings": ["ES2004a"], "qmsum": entry(qmsum / "test.jsonl")}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    said = json.dumps({"answer": "It is red.", "citations": [{"i": 1, "quote": "it is red"}]})
    (recorded / "minutes.json").write_text(json.dumps({"ES2004a": sections(GOOD)}))
    (recorded / "qa.json").write_text(json.dumps({"qmsum-0:0": [said]}))
    monkeypatch.setattr(eval_transcript, "CACHE", ami)
    monkeypatch.setattr(ev, "AMI_MANIFEST", tmp_path / "ami.json")
    monkeypatch.setattr(ev, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(ev, "QMSUM_CACHE", qmsum)
    monkeypatch.setattr(ev, "RECORDED", recorded)
    monkeypatch.setattr(ev, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ev, "OUT", tmp_path / "results.json")
    return tmp_path


def test_mom05_the_first_run_records_the_baseline_and_an_equal_run_passes(paths: Path) -> None:
    assert ev.main([]) == 0
    baseline = json.loads((paths / "baseline.json").read_text())
    assert set(baseline) == {"validator", "fake minutes", "minutes", "qa"}
    assert baseline["minutes"]["citation precision %"] == 100.0
    assert baseline["minutes"]["repaired %"] == 0.0
    assert baseline["minutes"]["served fabricated"] == 0.0
    assert baseline["minutes"]["gold items cited %"] == 100.0
    assert baseline["qa"]["citations in span %"] == 100.0
    assert baseline["qa"]["answer ROUGE-L %"] == 100.0
    results = json.loads((paths / "results.json").read_text())
    assert "minutes ES2004a" in results
    assert "qa qmsum-0" in results
    assert ev.main([]) == 0


def test_mom05_a_run_worse_than_the_baseline_fails_and_is_not_saved(paths: Path) -> None:
    assert ev.main([]) == 0
    baseline = json.loads((paths / "baseline.json").read_text())
    baseline["qa"]["answer ROUGE-L %"] = 100.5
    (paths / "baseline.json").write_text(json.dumps(baseline))
    assert ev.main([]) == 1
    with pytest.raises(SystemExit, match="answer ROUGE-L"):
        ev.main(["--save-baseline"])


def test_mom05_a_validator_that_lets_a_fabrication_through_misses_the_gate(
    paths: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(minutes, "derivation_ok", lambda *_: True)
    made_up = json.dumps({"ES2004a": sections(MADE_UP)})
    (paths / "recorded" / "minutes.json").write_text(made_up)
    assert ev.main([]) == 1
    printed = capsys.readouterr().out
    assert "minutes: served fabricated 1.00 (gate <= 0.00)" in printed
    assert "validator: wrong speaker refused %" in printed
    assert not (paths / "baseline.json").exists()


def test_mom05_the_report_prints_metrics_and_ids_never_the_meetings_words(
    paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ev.main([]) == 0
    printed = capsys.readouterr().out
    for said in ("agenda", "stuff", "remote", "Okay", "Yeah", "Project Manager"):
        assert said not in printed


def test_mom04_the_replay_leaves_the_live_speed_row_to_the_live_run(paths: Path) -> None:
    """No replay times anything: a baseline's speed row is neither compared nor dropped when the replay saves."""
    assert ev.main([]) == 0
    baseline = json.loads((paths / "baseline.json").read_text())
    speed = {"minutes p95 ms": 9_000.0, "search series recall@5 %": 90.0}
    (paths / "baseline.json").write_text(json.dumps(baseline | {"speed": speed}))
    assert ev.main(["--save-baseline"]) == 0
    assert json.loads((paths / "baseline.json").read_text())["speed"] == speed


def test_mom04_a_clean_live_run_rebases_the_replay_on_the_answers_it_recorded(paths: Path) -> None:
    """New recordings are a new sample of the model: the replay baseline is rebuilt from them (and the live speed
    row kept), so the replay keeps measuring the code, not the model's run-to-run spread (grading round 2)."""
    assert ev.main([]) == 0
    # A baseline from an earlier, luckier sample of the model: these recordings replay worse than it.
    older = json.loads((paths / "baseline.json").read_text())
    older["qa"]["answer ROUGE-L %"] = 100.5
    (paths / "baseline.json").write_text(json.dumps(older))
    assert ev.main([]) == 1
    speed = {"minutes p95 ms": 9_000.0}
    ev.rebase(speed)
    baseline = json.loads((paths / "baseline.json").read_text())
    assert baseline["speed"] == speed
    assert baseline["qa"]["answer ROUGE-L %"] == 100.0
    assert ev.main([]) == 0
