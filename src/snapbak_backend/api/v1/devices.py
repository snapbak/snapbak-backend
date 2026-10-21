"""POST /v1/devices and PUT /v1/devices/me/consent (CLD-01, TRX-08; docs/cloud.md)."""

import secrets
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from sqlalchemy.dialects.postgresql import insert

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Caller, Session, token_hash
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.config.settings import REGISTRATIONS_PER_HOUR
from snapbak_backend.db.models import Device
from snapbak_backend.schemas.cloud import ConsentRequest, ConsentResponse, DeviceRequest, DeviceResponse
from snapbak_backend.services import retention
from snapbak_backend.services.failures import CloudFailure

router = APIRouter(route_class=LoggedRoute)


async def within_rate(request: Request) -> bool:
    """Counts this address's registrations in the current hour, in Redis; False past the limit. Behind the load
    balancer, uvicorn's `--proxy-headers` makes `client.host` the caller's address (docs/cloud.md)."""
    address = request.client.host if request.client else "-"
    key = f"register:{address}:{int(time.time()) // 3600}"
    redis = cloud_deps.cloud(request).redis
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 3600)
    return count <= REGISTRATIONS_PER_HOUR


@router.post("/v1/devices", status_code=201)
async def register(body: DeviceRequest, db: Session, request: Request) -> DeviceResponse:
    """A new token for the install; a known install keeps its device, and with it today's usage, and its old token
    stops working. The token itself is never stored or logged."""
    if not await within_rate(request):
        raise CloudFailure(429, "rate_limited")
    token = secrets.token_urlsafe(32)
    upsert = insert(Device).values(id=uuid.uuid4(), install_id=body.installId, token_hash=token_hash(token))
    upsert = upsert.on_conflict_do_update(index_elements=[Device.install_id], set_={"token_hash": token_hash(token)})
    row = (await db.execute(upsert.returning(Device.id, Device.voiceprint_consent))).one()
    await db.commit()
    return DeviceResponse(deviceId=row.id, token=token, voiceprintConsent=row.voiceprint_consent)


@router.put("/v1/devices/me/consent")
async def consent(body: ConsentRequest, device: Caller, db: Session) -> ConsentResponse:
    now = datetime.now(UTC)
    device.voiceprint_consent = body.voiceprints
    device.consent_at = now
    if not body.voiceprints:
        await retention.on_consent_withdrawn(db, device.id)
    await db.commit()
    return ConsentResponse(voiceprintConsent=body.voiceprints, consentAt=now)
