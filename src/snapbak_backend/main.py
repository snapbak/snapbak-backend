"""The app: settings and providers at startup, the cloud when its settings exist, the error handlers and the routes
(docs/api.md, docs/cloud.md)."""

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from snapbak_backend.ai.providers.factory import make_tiers
from snapbak_backend.api.errors import (
    Failure,
    cloud_body,
    cloud_failed,
    failed,
    internal,
    invalid,
    share_host_or_json,
)
from snapbak_backend.api.v1 import command, health, polish
from snapbak_backend.config.settings import MAX_MINUTES_BYTES, code_version, load
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.metrics import log

# An edited minutes body: the route, and the room `{"baseVersion": n, "minutes": ...}` takes beyond the minutes.
MINUTES_PATH = re.compile(r"/v1/meetings/[^/]+/minutes")
ENVELOPE_BYTES = 1024


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # Off however uvicorn is started: its lines print each path, a share's slug among them, and the caller's address.
    # Every request logs its own line by route instead (CLAUDE.md rule 3, docs/cloud.md).
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("snapbak_backend").info("code=%s", code_version())
    config = load(os.environ)
    tiers = make_tiers(config)
    app.state.token = config.token
    app.state.tiers = tiers
    app.state.cloud = None
    started = None
    if config.cloud is not None:
        # Imported here: the cloud's libraries cost the dictation-only server 180 ms and 50 MB at start.
        from snapbak_backend.api import cloud

        await cloud.start(app, config.cloud, config.keys)
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


@app.middleware("http")
async def minutes_size(request: Request, call_next: Any) -> Response:
    """An edited minutes body over its 2 MB bound (and the envelope's few bytes) is refused on its Content-Length,
    before it is read or parsed; the route checks the parsed bytes again (grading 4)."""
    declared = request.headers.get("content-length", "")
    big = declared.isdigit() and int(declared) > MAX_MINUTES_BYTES + ENVELOPE_BYTES
    if big and request.method == "PUT" and MINUTES_PATH.fullmatch(request.url.path):
        # The route's template, never the path with its ID, as every error line.
        log("/v1/meetings/{meeting_id}/minutes", "-", "-", 0, 0, "minutes_too_large")
        return JSONResponse(cloud_body("minutes_too_large"), status_code=413)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://snapbak.site", "http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
