"""GET /healthz (docs/api.md)."""

from fastapi import APIRouter, BackgroundTasks, Request

from snapbak_backend.ai.providers.base import Http
from snapbak_backend.api import deps

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    """The app calls this on key-down; after answering, each tier's provider connection is opened."""
    tiers = deps.tiers(request)
    for provider in (tiers.small, tiers.big):
        if isinstance(provider, Http):
            background.add_task(provider.warm)
    return {"ok": True}
