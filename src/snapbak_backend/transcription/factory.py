"""A strategy from `SNAPBAK_TRANSCRIPTION`, as `make_provider` reads engines (docs/cloud.md)."""

from snapbak_backend.config.settings import CloudConfig
from snapbak_backend.services.storage import Storage
from snapbak_backend.transcription.assemblyai_pyannote import AssemblyAiPyannoteStrategy
from snapbak_backend.transcription.fake import Enqueue, FakeTranscriptionStrategy
from snapbak_backend.transcription.strategy import TranscriptionStrategy


def make_strategy(config: CloudConfig, storage: Storage, enqueue: Enqueue) -> TranscriptionStrategy:
    """`assemblyai+pyannote`, or `fake:<fixture>` for tests and local development."""
    spec = config.transcription
    if spec == AssemblyAiPyannoteStrategy.name:
        return AssemblyAiPyannoteStrategy(config.assemblyai_key, config.pyannote_key, storage)
    kind, _, fixture = spec.partition(":")
    if kind != "fake" or not fixture:
        raise ValueError(f"Set SNAPBAK_TRANSCRIPTION to assemblyai+pyannote or fake:<fixture>, not '{spec}'.")
    return FakeTranscriptionStrategy(fixture, enqueue)
