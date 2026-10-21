"""The app: settings and providers at startup, the cloud when its settings exist, the error handlers and the routes
(docs/api.md, docs/cloud.md)."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from snapbak_backend.ai.providers.factory import make_tiers
from snapbak_backend.api.errors import Failure, cloud_failed, failed, internal, invalid, share_host_or_json
from snapbak_backend.api.v1 import command, health, polish
from snapbak_backend.config.settings import load
from snapbak_backend.services.failures import CloudFailure


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # Off however uvicorn is started: its lines print each path, a share's slug among them, and the caller's address.
    # Every request logs its own line by route instead (CLAUDE.md rule 3, docs/cloud.md).
    logging.getLogger("uvicorn.access").disabled = True
    config = load(os.environ)
    tiers = make_tiers(config)
    app.state.token = config.token
    app.state.tiers = tiers
    app.state.cloud = None
    started = None
    if config.cloud is not None:
        # Imported here: the cloud's libraries cost the dictation-only server 180 ms and 50 MB at start.
        from snapbak_backend.api import cloud

        await cloud.start(app, config.cloud)
        started = cloud
    yield
    if started is not None:
        await started.stop(app)
    for provider in tiers.providers():
        await provider.aclose()


app = FastAPI(
    lifespan=lifespan,
    title="Snapbak Cloud AI",
    exception_handlers={
        Failure: failed,
        CloudFailure: cloud_failed,
        RequestValidationError: invalid,
        404: share_host_or_json,
        405: share_host_or_json,
        Exception: internal,
    },
)
# The routes join the app's own list: FastAPI 0.143's include_router matches each request through one more layer per
# router, about 6 us a request.
for router in (health.router, polish.router, command.router):
    app.router.routes.extend(router.routes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://snapbak.site", "http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
