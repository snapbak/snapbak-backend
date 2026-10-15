"""GET /healthz (docs/api.md)."""

from fastapi import APIRouter, BackgroundTasks, Request

from snapbak_backend.ai.providers.base import Http
from snapbak_backend.api import deps

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    """The app calls this on key-down; after answering, each model's provider connection is opened, the fallback's
    and the correction's too, so a backup call or a retry skips the TLS handshake too."""
    for provider in deps.tiers(request).providers():
        if isinstance(provider, Http):
            background.add_task(provider.warm)
    return {"ok": True}
