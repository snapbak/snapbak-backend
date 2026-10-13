"""GET /healthz (docs/api.md)."""

from fastapi import APIRouter, BackgroundTasks, Request

from snapbak_backend.ai.providers.base import Http
from snapbak_backend.api import deps

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    """The app calls this on key-down; after answering, each tier's and the fallback's provider connection is opened,
    so a backup call skips the TLS handshake too."""
    tiers = deps.tiers(request)
    for provider in (tiers.small, tiers.big, tiers.fallback):
        if isinstance(provider, Http):
            background.add_task(provider.warm)
    return {"ok": True}
