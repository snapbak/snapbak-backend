"""The cloud routes and what they share, started only when the cloud settings exist (docs/cloud.md). Importing this
module loads SQLAlchemy, boto3 and arq, so the dictation-only server never does (CLAUDE.md rule 5)."""

from fastapi import FastAPI

from snapbak_backend.api import share_page, waitlist
from snapbak_backend.api.v1 import (
    devices,
    recordings,
    shares,
    transcript,
    transcription,
    translations,
    uploads,
    webhooks,
)
from snapbak_backend.config.settings import CloudConfig
from snapbak_backend.services.cloud import open_cloud
from snapbak_backend.services.events import Events

ROUTERS = (
    devices.router,
    recordings.router,
    uploads.router,
    shares.router,
    transcription.router,
    transcript.router,
    translations.router,
    webhooks.router,
    share_page.router,
    waitlist.router,
)


async def start(app: FastAPI, config: CloudConfig) -> None:
    """Opens the database, Redis and S3, starts the long-poll listener, and adds the cloud routes after the dictation
    ones, so those match first and pay nothing for them. A process starts once."""
    for router in ROUTERS:
        app.router.routes.extend(router.routes)
    app.state.cloud = await open_cloud(config)
    app.state.events = Events(app.state.cloud.redis)
    await app.state.events.start()


async def stop(app: FastAPI) -> None:
    await app.state.events.stop()
    await app.state.cloud.close()
