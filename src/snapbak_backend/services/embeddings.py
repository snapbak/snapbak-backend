"""The embeddings the meetings search and Q&A run on (MEETINGS.md, D38): text-embedding-3-small pinned to
EMBED_DIM, or `fake:hashed`, whose deterministic token-bag vectors make the tests reproducible."""

import hashlib
import math
from typing import Any, Protocol

from snapbak_backend.ai.providers.base import Http, ProviderError, Usage
from snapbak_backend.config.settings import EMBED_DIM

OPENAI = "https://api.openai.com/v1/embeddings"
TIMEOUT_S = 30


class EmbedError(Exception):
    pass


class Embeddings(Protocol):
    engine: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class OpenAIEmbeddings:
    """The /v1/embeddings endpoint on the chat providers' pooled client and retry loop (ai/providers/base.py
    `Http`); any failure, the network's included, is an EmbedError."""

    def __init__(self, model: str, key: str) -> None:
        self.engine = f"openai:{model}"
        self.http = Http(self.engine, OPENAI, {"authorization": f"Bearer {key}"})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"model": self.engine.partition(":")[2], "input": texts, "dimensions": EMBED_DIM}
        found: list[list[list[float]]] = []

        def parse(answer: Any) -> tuple[str, Usage]:
            found.append(self.vectors_of(answer, len(texts)))
            return "", Usage(0, 0)

        try:
            await self.http.ask(body, TIMEOUT_S, parse)
        except ProviderError as error:
            raise EmbedError(str(error)) from error
        return found[0]

    def vectors_of(self, answer: dict[str, Any], count: int) -> list[list[float]]:
        """The vectors in input order, checked for count and width; a wrong answer is an error, never a guess."""
        data = answer.get("data")
        if not isinstance(data, list) or len(data) != count:
            raise EmbedError(f"{self.engine}: unexpected answer")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        try:
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbedError(f"{self.engine}: unexpected answer") from error
        if any(len(vector) != EMBED_DIM for vector in vectors):
            raise EmbedError(f"{self.engine}: wrong dimensions")
        return vectors

    async def aclose(self) -> None:
        await self.http.aclose()


class HashedEmbeddings:
    """Each token hashes to two of EMBED_DIM's cells, one added and one subtracted; the vector is normalized. The
    same text always makes the same vector in any process, so a search ranks by shared words alone."""

    def __init__(self) -> None:
        self.engine = "fake:hashed"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = [self.vector(text) for text in texts]
        return vectors

    def vector(self, text: str) -> list[float]:
        cells = [0.0] * EMBED_DIM
        for token in text.lower().split():
            digest = hashlib.md5(token.encode()).digest()
            first = int.from_bytes(digest[:4], "big") % EMBED_DIM
            second = int.from_bytes(digest[4:8], "big") % EMBED_DIM
            cells[first] += 1.0
            cells[second] -= 1.0
        norm = math.sqrt(sum(cell * cell for cell in cells))
        if norm == 0.0:
            cells[0] = 1.0
            vector = cells
        else:
            vector = [cell / norm for cell in cells]
        return vector

    async def aclose(self) -> None:
        pass


def make_embeddings(spec: str, keys: dict[str, str]) -> Embeddings:
    """`openai:<model>` or `fake:hashed`, as the settings name (SNAPBAK_EMBEDDINGS)."""
    name, _, model = spec.partition(":")
    if name == "fake" and model == "hashed":
        return HashedEmbeddings()
    if name == "openai" and model:
        key = keys.get("openai", "")
        if not key:
            raise ValueError("Set OPENAI_API_KEY to use openai embeddings.")
        return OpenAIEmbeddings(model, key)
    raise ValueError(f"Name embeddings as openai:<model> or fake:hashed, not '{spec}'.")
