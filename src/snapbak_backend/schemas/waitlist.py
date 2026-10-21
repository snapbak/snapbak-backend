"""The marketing site's request and response bodies (waitlist and contact), named as on the wire like the cloud
routes' (docs/cloud.md)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints


class WaitlistBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WaitlistSignupRequest(WaitlistBase):
    email: EmailStr
    useCase: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="website", max_length=100)


class WaitlistSignupResponse(WaitlistBase):
    message: str


class EarlyAccessStatusResponse(WaitlistBase):
    spotsTotal: int
    spotsClaimed: int
    waitlistOpen: bool


class ContactRequest(WaitlistBase):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
    source: str = Field(default="website", max_length=100)


class ContactResponse(WaitlistBase):
    message: str
