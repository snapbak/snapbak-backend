"""GET /healthz (docs/api.md)."""

from fastapi import APIRouter, BackgroundTasks, Request

from snapbak_backend.ai.providers.base import Http
from snapbak_backend.api import deps
from snapbak_backend.config.settings import code_version

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, background: BackgroundTasks) -> dict[str, bool | str]:
    """The app calls this on key-down; after answering, each model's provider connection is opened, the fallback's
    and the correction's too, so a backup call or a retry skips the TLS handshake too."""
    for provider in deps.tiers(request).providers():
        if isinstance(provider, Http):
            background.add_task(provider.warm)
    # The code the process runs, so a server left running on old code shows (`config.settings.code_version`).
    return {"ok": True, "code": code_version()}
