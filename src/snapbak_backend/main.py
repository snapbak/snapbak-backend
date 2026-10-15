"""The app: settings and providers at startup, the error handlers and the routes (docs/api.md)."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.api.errors import Failure, failed, internal, invalid
from snapbak_backend.api.v1 import command, health, polish
from snapbak_backend.config.settings import load


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = load(os.environ)
    fallback = make_provider(config.fallback, config.keys) if config.fallback else None
    correction = make_provider(config.correction, config.keys) if config.correction else None
    tiers = Tiers(
        polish=make_provider(config.polish, config.keys, config.temperature),
        command=make_provider(config.big, config.keys),
        fallback=fallback,
        correction=correction,
        effort=config.effort,
        examples=config.examples,
    )
    app.state.token = config.token
    app.state.tiers = tiers
    yield
    for provider in tiers.providers():
        await provider.aclose()


app = FastAPI(
    lifespan=lifespan,
    title="Snapbak Cloud AI",
    exception_handlers={Failure: failed, RequestValidationError: invalid, Exception: internal},
)
# The routes join the app's own list: FastAPI 0.143's include_router matches each request through one more layer per
# router, about 6 us a request.
for router in (health.router, polish.router, command.router):
    app.router.routes.extend(router.routes)
