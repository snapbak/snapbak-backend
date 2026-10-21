"""What happens to voiceprints when a recording is trashed or consent is withdrawn (docs/cloud.md). The founder
chose to keep everything, voiceprints included (Q101, "Soft forever"); HLD open question 2 would null
`speakers.voiceprint` here, one statement in each."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


async def on_recording_deleted(session: AsyncSession, recording_id: UUID) -> None:
    """Voiceprints stay with the soft-deleted recording (Q101)."""


async def on_consent_withdrawn(session: AsyncSession, device_id: UUID) -> None:
    """New voiceprints stop at once (create_voiceprints reads the consent); stored ones stay (Q101)."""
