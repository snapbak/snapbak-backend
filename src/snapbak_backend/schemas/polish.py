"""The /v1/polish request and response (docs/api.md)."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from snapbak_backend.ai import routing

type Style = Literal["chat", "email", "code", "docs", "other"]
type Term = Annotated[str, StringConstraints(min_length=1, max_length=100)]


MAX_PRIOR_WORDS = 300


class PolishRequest(BaseModel):
    text: str = Field(max_length=8_000)
    prior: str
    terms: list[Term] = Field(max_length=200)
    app: str = Field(max_length=100)
    style: Style
    language: str
    # The first chunk goes in mid-sentence: the text before the cursor ends with no sentence mark.
    continues: bool = False
    # The app waits up to 20 s for the AI: accuracy over speed (D2, 11 Oct).
    deadline_ms: int = Field(ge=300, le=20_000)

    @model_validator(mode="after")
    def text_or_prior(self) -> PolishRequest:
        if not self.text and not self.prior:
            raise ValueError("text is empty and there is no prior text")
        if len(self.prior.split()) > MAX_PRIOR_WORDS:
            raise ValueError(f"prior holds more than {MAX_PRIOR_WORDS} words")
        return self


class PolishResponse(BaseModel):
    text: str
    revision: str | None
    rejected: bool
    engine: str
    tier: routing.Tier
    ms: int
    tokens: int
