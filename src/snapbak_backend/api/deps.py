"""What a route needs from the request: the bearer token checked, and the tiers built at startup."""

import secrets

from fastapi import Request

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.api.errors import Failure
from snapbak_backend.services.metrics import log


def authorize(request: Request) -> None:
    expected = f"Bearer {request.app.state.token}".encode()
    given = request.headers.get("authorization", "").encode()
    if not secrets.compare_digest(given, expected):
        log(request.url.path, "-", "-", 0, 0, "unauthorized")
        raise Failure(401, "unauthorized")


def tiers(request: Request) -> Tiers:
    return request.app.state.tiers
