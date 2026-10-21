"""Waitlist routes for the Snapbak marketing site: status, signups and spot claims."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.api.cloud_deps import session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.config.settings import WAITLIST_SPOTS_DEFAULT
from snapbak_backend.db.models import EarlyAccessSpot, WaitlistEntry
from snapbak_backend.schemas.waitlist import (
    EarlyAccessStatusResponse,
    WaitlistSignupRequest,
    WaitlistSignupResponse,
)
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)

Session = Annotated[AsyncSession, Depends(session)]


def _waitlist_token(request: Request) -> str:
    return request.app.state.cloud.config.waitlist_token


def _bearer_token(header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return None
    return token


async def _get_or_create_spots(db: AsyncSession) -> EarlyAccessSpot:
    row = await db.scalar(select(EarlyAccessSpot))
    if row is None:
        row = EarlyAccessSpot(total_spots=WAITLIST_SPOTS_DEFAULT, claimed_spots=0)
        db.add(row)
        await db.flush()
    return row


@router.get("/v1/waitlist/status")
async def waitlist_status(db: Session) -> EarlyAccessStatusResponse:
    """The current early-access state: total spots, claimed spots, and whether the waitlist is still open."""
    spots = await _get_or_create_spots(db)
    await db.commit()
    return EarlyAccessStatusResponse(
        spots_total=spots.total_spots,
        spots_claimed=spots.claimed_spots,
        waitlist_open=spots.claimed_spots < spots.total_spots,
    )


@router.post("/v1/waitlist", status_code=201)
async def waitlist_signup(body: WaitlistSignupRequest, db: Session) -> WaitlistSignupResponse:
    """Join the waitlist. Duplicate emails are idempotent: the stored row is kept unchanged."""
    upsert = (
        insert(WaitlistEntry)
        .values(
            id=uuid.uuid4(),
            email=str(body.email).lower().strip(),
            use_case=body.use_case,
            source=body.source,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[WaitlistEntry.email])
    )
    await db.execute(upsert)
    await db.commit()
    return WaitlistSignupResponse(message="Thanks for joining the Snapbak waitlist.")


@router.post("/v1/waitlist/claim")
async def claim_spot(
    db: Session,
    request: Request,
    authorization: Annotated[str, Header()] = "",
) -> EarlyAccessStatusResponse:
    """Increment the claimed-spot counter, protected by a bearer token."""
    if _bearer_token(authorization) != _waitlist_token(request):
        raise CloudFailure(401, "unauthorized")

    spots = await db.scalar(select(EarlyAccessSpot).with_for_update())
    if spots is None:
        spots = EarlyAccessSpot(total_spots=WAITLIST_SPOTS_DEFAULT, claimed_spots=0)
        db.add(spots)
    if spots.claimed_spots < spots.total_spots:
        spots.claimed_spots += 1
    spots.updated_at = datetime.now(UTC)
    await db.commit()
    return EarlyAccessStatusResponse(
        spots_total=spots.total_spots,
        spots_claimed=spots.claimed_spots,
        waitlist_open=spots.claimed_spots < spots.total_spots,
    )
