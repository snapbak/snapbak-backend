"""The waitlist routes' request and response bodies."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class WaitlistBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WaitlistSignupRequest(WaitlistBase):
    email: EmailStr
    use_case: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="website", max_length=100)


class WaitlistSignupResponse(WaitlistBase):
    message: str


class EarlyAccessStatusResponse(WaitlistBase):
    spots_total: int
    spots_claimed: int
    waitlist_open: bool
