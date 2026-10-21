"""The cloud routes' error, raised by services and routes alike and answered by `api/errors.py`. It has no imports, so
the dictation-only server loads nothing of the cloud to handle it."""

from typing import Any


class CloudFailure(Exception):
    """A cloud route's error; `extra` joins the body beside `error`, such as the 409's `job` or the 429's
    `resetsAt`."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra
