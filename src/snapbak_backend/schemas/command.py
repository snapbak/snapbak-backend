"""The /v1/command request and response (docs/api.md)."""

from pydantic import BaseModel, Field


class CommandRequest(BaseModel):
    selection: str = Field(min_length=1, max_length=20_000)
    instruction: str = Field(min_length=1, max_length=2_000)
    app: str = Field(max_length=100)
    language: str
    deadline_ms: int = Field(ge=300, le=10_000)


class CommandResponse(BaseModel):
    text: str
    engine: str
    ms: int
    tokens: int
