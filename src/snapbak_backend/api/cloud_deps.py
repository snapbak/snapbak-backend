"""What a cloud route needs from the request: the cloud opened at startup, a database session, and the device behind
the bearer token (docs/cloud.md)."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.db.models import Device
from snapbak_backend.services.cloud import Cloud
from snapbak_backend.services.failures import CloudFailure

SEEN_EVERY = timedelta(minutes=1)


def cloud(request: Request) -> Cloud:
    """Opened at startup; the cloud routes exist only when it is."""
    opened: Cloud = request.app.state.cloud
    return opened


async def session(request: Request) -> AsyncIterator[AsyncSession]:
    async with cloud(request).sessions() as opened:
        yield opened


Session = Annotated[AsyncSession, Depends(session)]


def token_hash(token: str) -> bytes:
    """A hash lookup leaks nothing about the token, so the lookup needs no constant-time compare."""
    digest = hashlib.sha256(token.encode()).digest()
    return digest


async def device(request: Request, db: Session) -> Device:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme != "Bearer" or not token:
        raise CloudFailure(401, "unauthorized")
    found = await db.scalar(select(Device).where(Device.token_hash == token_hash(token)))
    if found is None or found.deleted_at is not None:
        raise CloudFailure(401, "unauthorized")
    now = datetime.now(UTC)
    if now - found.last_seen_at > SEEN_EVERY:
        found.last_seen_at = now
    # Ends the lookup's transaction, which hands the connection back to the pool: a long-poll holds none while it
    # waits, and the route's next statement takes one again.
    await db.commit()
    return found


Caller = Annotated[Device, Depends(device)]
