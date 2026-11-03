"""What a dictation route needs from the request: the bearer token checked (the server's or a device's), and the
tiers built at startup."""

import secrets

from fastapi import Request

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.api.errors import Failure
from snapbak_backend.services.metrics import log


async def authorize(request: Request) -> None:
    """The server's own token (debug builds and tests), else a registered device's: the downloaded app carries no
    shared secret."""
    expected = f"Bearer {request.app.state.token}".encode()
    given = request.headers.get("authorization", "").encode()
    if secrets.compare_digest(given, expected):
        return
    if request.app.state.cloud is not None and await registered(request):
        return
    log(request.url.path, "-", "-", 0, 0, "unauthorized")
    raise Failure(401, "unauthorized")


async def registered(request: Request) -> bool:
    """Whether the bearer token is a registered device's."""
    # Imported here, as in main.py: the dictation-only server loads none of the cloud's libraries.
    from snapbak_backend.api import cloud_deps
    from snapbak_backend.services.failures import CloudFailure

    async with cloud_deps.cloud(request).sessions() as db:
        try:
            await cloud_deps.device(request, db)
        except CloudFailure:
            return False
    return True


def tiers(request: Request) -> Tiers:
    return request.app.state.tiers
