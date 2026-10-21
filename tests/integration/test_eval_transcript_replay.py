"""`just eval-transcript` end to end on a tiny AMI-shaped corpus made here (13 LLD, Evals): the checksummed cache,
the AMI word files, the seeded vendor inputs, the real merge, the scores, the baseline and the results file. Nothing
is downloaded."""

import hashlib
import json
import zipfile
from pathlib import Path

import eval_transcript as ev
import pytest

# Two speakers, AMI's layout: one file per speaker, marks as their own `punc` tokens, sounds and an untimed word to
# skip.
SPEAKER_A = """<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>
<nite:root nite:id="T1001a.A.words" xmlns:nite="http://nite.sourceforge.net/">
   <w nite:id="a0" starttime="1.00" endtime="1.30">Hi</w>
   <w nite:id="a1" starttime="1.30" endtime="1.30" punc="true">,</w>
   <w nite:id="a2" starttime="1.30" endtime="1.60">I&#39;m</w>
   <w nite:id="a3" starttime="1.60" endtime="2.10">David</w>
   <w nite:id="a4" starttime="2.10" endtime="2.10" punc="true">.</w>
   <vocalsound nite:id="a5" starttime="2.20" endtime="2.40" type="laugh"/>
   <w nite:id="a6" starttime="6.00" endtime="6.40">Um</w>
   <w nite:id="a7" starttime="6.40" endtime="6.40" punc="true">,</w>
   <w nite:id="a8" starttime="6.50" endtime="6.90">shall</w>
   <w nite:id="a9" starttime="6.90" endtime="7.10">we</w>
   <w nite:id="a10" starttime="7.10" endtime="7.60">start</w>
   <w nite:id="a11" starttime="7.60" endtime="7.60" punc="true">?</w>
   <w nite:id="a12">untimed</w>
</nite:root>
"""
SPEAKER_B = """<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>
<nite:root nite:id="T1001a.B.words" xmlns:nite="http://nite.sourceforge.net/">
   <w nite:id="b0" starttime="2.50" endtime="2.80">Hello</w>
   <w nite:id="b1" starttime="2.80" endtime="3.20">David</w>
   <w nite:id="b2" starttime="3.20" endtime="3.20" punc="true">.</w>
   <w nite:id="b3" starttime="3.40" endtime="3.60">I</w>
   <w nite:id="b4" starttime="3.60" endtime="3.80">am</w>
   <w nite:id="b5" starttime="3.80" endtime="4.30">Laura</w>
   <w nite:id="b6" starttime="4.30" endtime="4.30" punc="true">.</w>
   <w nite:id="b7" starttime="8.20" endtime="8.60">Yes</w>
   <w nite:id="b8" starttime="8.60" endtime="8.60" punc="true">.</w>
</nite:root>
"""


def corpus(cache: Path) -> dict:
    """The zip in the cache and a manifest naming it with its checksum."""
    cache.mkdir()
    archive = cache / "tiny.zip"
    with zipfile.ZipFile(archive, "w") as made:
        made.writestr("words/T1001a.A.words.xml", SPEAKER_A)
        made.writestr("words/T1001a.B.words.xml", SPEAKER_B)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    annotations = {"url": "https://example.invalid/tiny.zip", "file": "tiny.zip", "sha256": digest}
    audio = {"url": "https://example.invalid/T1001a.wav", "file": "T1001a.wav", "sha256": "0" * 64}
    manifest = {"annotations": annotations, "excerpts": [{"meeting": "T1001a", "start_s": 0, "end_s": 60, **audio}]}
    return manifest


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(corpus(tmp_path / "cache")))
    monkeypatch.setattr(ev, "MANIFEST", manifest)
    monkeypatch.setattr(ev, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(ev, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ev, "RECORDED", tmp_path / "recorded")
    monkeypatch.setattr(ev, "OUT", tmp_path / "results.json")
    return tmp_path


def test_trx01_the_ami_words_read_as_timed_words_with_their_marks(paths: Path) -> None:
    truth = ev.ami_words(paths / "cache" / "tiny.zip", "T1001a", 0, 60)
    assert [item.w for item in truth] == [
        "Hi,", "I'm", "David.", "Hello", "David.", "I", "am", "Laura.", "Um,", "shall", "we", "start?", "Yes.",
    ]  # fmt: skip
    assert (truth[0].s, truth[0].e, truth[0].speaker) == (1000, 1300, "A")
    assert [item.w for item in truth if item.filler] == ["Um,"]


def test_trx01_an_excerpt_keeps_the_words_inside_it_timed_from_its_start(paths: Path) -> None:
    truth = ev.ami_words(paths / "cache" / "tiny.zip", "T1001a", 2, 5)
    assert [item.w for item in truth] == ["Hello", "David.", "I", "am", "Laura."]
    assert (truth[0].s, truth[-1].e) == (500, 2300)


def test_trx01_the_first_run_records_the_baseline_and_an_equal_run_passes(
    paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ev.main([]) == 0
    printed = capsys.readouterr().out
    assert "T1001a-0" in printed and "speaker words %" in printed and "baseline recorded" in printed
    baseline = json.loads((paths / "baseline.json").read_text())
    results = json.loads((paths / "results.json").read_text())
    assert baseline == results
    assert set(results) == {"T1001a-0", "all"}
    assert results["T1001a-0"]["filler recall %"] == 100.0
    assert ev.main([]) == 0


def test_trx01_a_run_worse_than_the_baseline_fails(paths: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ev.main([])
    baseline = json.loads((paths / "baseline.json").read_text())
    baseline["all"]["merge DER %"] = -1.0
    (paths / "baseline.json").write_text(json.dumps(baseline))
    assert ev.main([]) == 1
    assert "worse than the baseline: all: merge DER % -1.00 ->" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="worse"):
        ev.main(["--save-baseline"])


def test_trx01_save_baseline_moves_the_baseline_to_a_better_run(paths: Path) -> None:
    ev.main([])
    results = json.loads((paths / "results.json").read_text())
    worse_baseline = json.loads(json.dumps(results))
    worse_baseline["all"]["merge DER %"] = results["all"]["merge DER %"] + 5.0
    (paths / "baseline.json").write_text(json.dumps(worse_baseline))
    assert ev.main(["--save-baseline"]) == 0
    assert json.loads((paths / "baseline.json").read_text()) == results


def test_trx01_a_word_filling_the_excerpt_exactly_is_kept(tmp_path: Path) -> None:
    archive = tmp_path / "edge.zip"
    edge = '<root><w starttime="1.00" endtime="2.00">Hi</w><w starttime="0.50" endtime="1.00">Before</w></root>'
    with zipfile.ZipFile(archive, "w") as made:
        made.writestr("words/T1001a.A.words.xml", edge)
    truth = ev.ami_words(archive, "T1001a", 1, 2)
    assert [(item.w, item.s, item.e) for item in truth] == [("Hi", 0, 1000)]


def test_trx01_recorded_vendor_answers_are_scored_as_their_own_row(paths: Path) -> None:
    truth = ev.ami_words(paths / "cache" / "tiny.zip", "T1001a", 0, 60)
    transcript, output = ev.vendor_inputs(truth, None)
    folder = paths / "recorded" / "T1001a-0"
    folder.mkdir(parents=True)
    (folder / "assemblyai.json").write_text(json.dumps(transcript))
    (folder / "pyannote.json").write_text(json.dumps(output))
    ev.main([])
    results = json.loads((paths / "results.json").read_text())
    assert results["T1001a-0 recorded"]["speaker words %"] == 100.0
    assert results["T1001a-0 recorded"]["merge DER %"] == 0.0


def test_trx01_a_cached_file_with_the_wrong_checksum_is_refused(paths: Path) -> None:
    (paths / "cache" / "tiny.zip").write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="checksum"):
        ev.main([])
