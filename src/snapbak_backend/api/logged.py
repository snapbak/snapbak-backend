"""One log line per cloud request: the route, the time and the status, never a body (CLAUDE.md rule 3). Errors are
logged by their handlers in api/errors.py."""

import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute

from snapbak_backend.services.metrics import log


class LoggedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()
        path = self.path

        async def logged(request: Request) -> Response:
            started = time.monotonic()
            response = await handle(request)
            log(path, "-", "-", int((time.monotonic() - started) * 1000), 0, str(response.status_code))
            return response

        return logged
