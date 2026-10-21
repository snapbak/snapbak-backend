"""The transcript eval's copies of the app's pause rule (14 A6) and caption rule (15 C1) give exactly what the app's
own code gave on every golden vector in `tests/contract/transcription-rules.json`, the file both repos keep
byte-identical (13 LLD, Evals). The app runs the same vectors against its own code, so the three never drift."""

import hashlib
import json
from pathlib import Path
from typing import Any

import eval_transcript as ev
import pytest

from snapbak_backend.transcription.merge import Word

VECTORS = json.loads((Path(__file__).parents[1] / "contract" / "transcription-rules.json").read_text())
APP = Path(__file__).parents[3] / "snapbak"


def words_of(lines: list[Any]) -> list[Word]:
    """The lines' words in reading order; a caption line names its speaker, a pause line is a bare list."""
    made = []
    for line in lines:
        speaker, said = (line["speaker"], line["words"]) if isinstance(line, dict) else ("A", line)
        made += [Word(item["w"], item["s"], item["e"], item["filler"], 1.0, speaker) for item in said]
    return made


@pytest.mark.parametrize("vector", VECTORS["pauses"], ids=[vector["name"] for vector in VECTORS["pauses"]])
def test_trx04_the_pause_copy_offers_what_the_apps_cleanup_offers(vector: dict[str, Any]) -> None:
    offered = ev.offered_pauses(words_of(vector["lines"]))
    assert [list(pause) for pause in offered] == vector["expected"]


@pytest.mark.parametrize("vector", VECTORS["captions"], ids=[vector["name"] for vector in VECTORS["captions"]])
def test_trx02_the_caption_copy_groups_words_as_the_apps_layout_does(vector: dict[str, Any]) -> None:
    groups = [[item.w for item in caption] for caption in ev.captions(words_of(vector["lines"]))]
    expected = [[said for row in cue for said in row] for cue in vector["expected"]]
    assert groups == expected


@pytest.mark.skipif(not APP.is_dir(), reason="the app repo is not checked out next to this one")
@pytest.mark.parametrize("source", sorted(VECTORS["sources"]))
def test_trx02_the_vectors_come_from_the_apps_current_code(source: str) -> None:
    digest = hashlib.sha256((APP / source).read_bytes()).hexdigest()
    assert digest == VECTORS["sources"][source], f"{source} changed: print the vectors again (13 LLD, Evals)"
