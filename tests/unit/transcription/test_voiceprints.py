import shutil
import subprocess
from pathlib import Path

import pytest

from snapbak_backend.transcription.voiceprints import cut, spans


def turn(speaker: str, start: float, end: float) -> dict:
    return {"speaker": speaker, "start": start, "end": end}


def test_trx08_each_speaker_gets_its_longest_run_joining_turns_under_300_ms_apart() -> None:
    turns = [
        turn("S0", 0.0, 2.0),
        turn("S0", 2.2, 4.0),  # 200 ms gap: one run of 4 s
        turn("S1", 4.5, 8.0),
        turn("S0", 9.0, 10.0),  # 5 s later: a run of its own
    ]
    assert spans(turns) == {"S0": (0, 4000), "S1": (4500, 3500)}


def test_trx08_a_run_longer_than_30_s_is_clipped_to_its_first_30_s() -> None:
    assert spans([turn("S0", 10.0, 70.0)]) == {"S0": (10_000, 30_000)}


def test_trx08_a_speaker_with_no_run_of_3_s_gets_no_voiceprint() -> None:
    assert spans([turn("S0", 0.0, 2.9), turn("S1", 3.0, 6.0)]) == {"S1": (3000, 3000)}


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed (the worker image has it)")
async def test_trx08_ffmpeg_cuts_a_mono_16_khz_clip(tmp_path: Path) -> None:
    audio = tmp_path / "audio.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-ac", "1", str(audio)],
        check=True,
        capture_output=True,
    )
    clip = tmp_path / "clip.wav"
    await cut(audio, 2000, 3000, clip)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=sample_rate,channels:format=duration",
            "-of",
            "csv=p=0",
            str(clip),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert probe[0] == "16000,1"
    assert abs(float(probe[1]) - 3.0) < 0.05
