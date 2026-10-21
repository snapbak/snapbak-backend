"""`just test-cloud`: real AssemblyAI and pyannoteAI on real audio, on demand only (D19). It costs money, so it never
runs in `just test`, CI or hooks (docs/cloud.md).

`uv run scripts/test_cloud.py [--record] [--runs N] [AUDIO ...]`, default every `bench/transcription/*.m4a`. Each file
goes to the bucket named by S3_BUCKET, both vendors run through `AssemblyAiPyannoteStrategy`, and the script asks
them for status every 2 s, so no tunnel is needed. It prints, per file: submit-to-merged time, each vendor's time, the
word error rate against `<name>.txt` if present, the language, the speakers and how many got names, and the fillers
found. That checks the facts RESEARCH.md left open: names without a `speakers` list, `disfluencies` on U-3.5 Pro, the
error strings and the delete endpoint. `--runs 5` on the 10-minute file prints p50 and p95 against the 60 s goal and 2
min cap. `--record` writes the vendor JSON into `tests/fixtures/transcription/<name>/` for the fake strategy.
"""

import argparse
import asyncio
import os
from pathlib import Path

from vendor_run import CAP_S, GOAL_S, heard, percentile, save_answers, word_error_rate

from snapbak_backend.config.settings import load
from snapbak_backend.services.storage import Storage, s3_client
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy
from snapbak_backend.transcription.merge import merge, text

ROOT = Path(__file__).parents[1]
BENCH = ROOT / "bench" / "transcription"
FIXTURES = ROOT / "tests" / "fixtures" / "transcription"


async def run_once(strategy: AssemblyAiPyannoteStrategy, storage: Storage, audio: Path, record: bool) -> float:
    elapsed, transcript, output = await heard(strategy, storage, audio)
    merged = merge(transcript, output.get("exclusiveDiarization") or [], "auto")
    said = " ".join(text(line, merged.language) for line in merged.lines)
    reference = audio.with_suffix(".txt")
    if reference.is_file():
        print(f"  WER {word_error_rate(reference.read_text(), said):.1%}")
    fillers = sum(word.filler for line in merged.lines for word in line)
    named = sum(speaker.name is not None for speaker in merged.speakers)
    print(f"  language {merged.language}, {len(merged.speakers)} speakers, {named} named, {fillers} fillers")
    if record:
        save_answers(FIXTURES / audio.stem, transcript, output)
    return elapsed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Real AssemblyAI and pyannoteAI on real audio (costs money).")
    parser.add_argument("audio", nargs="*", type=Path)
    parser.add_argument("--record", action="store_true", help="write the vendor JSON as fake-strategy fixtures")
    parser.add_argument("--runs", type=int, default=1, help="repeat each file, for p50 and p95")
    args = parser.parse_args()
    config = load(os.environ).cloud
    if config is None or not config.assemblyai_key or not config.pyannote_key:
        raise SystemExit("Set DATABASE_URL, S3_BUCKET, ASSEMBLYAI_API_KEY and PYANNOTE_API_KEY (docs/cloud.md).")
    files = args.audio or sorted(BENCH.glob("*.m4a"))
    if not files:
        raise SystemExit(f"No audio: put .m4a files (and .txt references) in {BENCH}.")
    storage = Storage(s3_client(config), config.bucket)
    strategy = AssemblyAiPyannoteStrategy(config.assemblyai_key, config.pyannote_key, storage)
    try:
        for audio in files:
            times = []
            for run in range(args.runs):
                print(f"{audio.name} run {run + 1}")
                times.append(await run_once(strategy, storage, audio, args.record and run == 0))
            p50, p95 = percentile(times, 50), percentile(times, 95)
            print(f"{audio.name}: p50 {p50:.1f} s, p95 {p95:.1f} s (goal {GOAL_S} s, cap {CAP_S} s)")
    finally:
        await strategy.aclose()


if __name__ == "__main__":
    asyncio.run(main())
