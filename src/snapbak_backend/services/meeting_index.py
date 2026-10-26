"""The meetings' embedding index and search (MEETINGS.md 5.3, D38): each minutes section is one vector; a search
embeds the query, takes the nearest candidates, and the polish model re-ranks them. No model is called while a
database transaction is open: the callers read, end the transaction, then call."""

import json
import time
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai.chain import Tiers, ask
from snapbak_backend.ai.prompts import ask as prompt
from snapbak_backend.db.models import MeetingChunk
from snapbak_backend.schemas.cloud import SearchHit

type Chunk = tuple[str, int, str]

INSERT_CHUNK = text(
    "INSERT INTO meeting_chunks (id, meeting_id, kind, idx, text, embedding) "
    "VALUES (:id, :meeting, :kind, :idx, :text, (:emb)::vector)"
)
# An exact scan over the device's own live sections (dozens per meeting): an HNSW walk would take the nearest
# sections of every device first and filter after, so at scale a device could find nothing. MATERIALIZED keeps the
# planner from pushing the order into the index.
CANDIDATES = text(
    "WITH mine AS MATERIALIZED ("
    "SELECT m.id AS meeting_id, m.recording_id, m.title, c.kind, c.text AS snippet, c.embedding, "
    # An action section's idx is its place in the minutes' actionItems (minutes.chunks_of), so its owner is there.
    "CASE WHEN c.kind = 'action' THEN m.minutes -> 'actionItems' -> c.idx ->> 'owner' END AS owner "
    "FROM meeting_chunks c "
    "JOIN meetings m ON m.id = c.meeting_id AND m.deleted_at IS NULL "
    "JOIN recordings r ON r.id = m.recording_id AND r.device_id = :device AND r.deleted_at IS NULL "
    # Ask's own sections (kind `ask`, services/qa.py) are never a search hit.
    "WHERE c.deleted_at IS NULL AND c.kind <> 'ask') "
    "SELECT meeting_id, recording_id, title, kind, snippet, owner, 1 - (embedding <=> (:q)::vector) AS score "
    "FROM mine ORDER BY embedding <=> (:q)::vector LIMIT :k"
)
# A meeting's stored Ask sections nearest a question, as their places in `qa.sections_of`.
NEAREST_ASK = text(
    "SELECT idx FROM meeting_chunks WHERE meeting_id = :meeting AND kind = 'ask' AND deleted_at IS NULL "
    "ORDER BY embedding <=> (:q)::vector LIMIT :k"
)
MAX_TOKENS = 2000
# A search answers inside MOM-06's 5 s too: hedged before it, given up soon after.
# Past the 5 s P95 on purpose: at 5 s a slow call under load was an error, not a slow answer (26 Oct 2026).
DEADLINE_S = 8.0
HEDGE_S = 2.5


async def write_sections(
    session: AsyncSession, meeting_id: UUID, sections: list[Chunk], vectors: list[list[float]]
) -> None:
    """The meeting's sections replaced by these vectors, made beforehand with no transaction open. The caller
    commits."""
    await session.execute(delete(MeetingChunk).where(MeetingChunk.meeting_id == meeting_id))
    rows = [
        {"id": uuid.uuid4(), "meeting": meeting_id, "kind": kind, "idx": idx, "text": body, "emb": json.dumps(vector)}
        for (kind, idx, body), vector in zip(sections, vectors, strict=True)
    ]
    if rows:
        await session.execute(INSERT_CHUNK, rows)


async def candidates(session: AsyncSession, device_id: UUID, vector: list[float], limit: int) -> list[Any]:
    """The device's `3 x limit` sections nearest the query's vector (D38)."""
    rows = await session.execute(CANDIDATES, {"q": json.dumps(vector), "device": device_id, "k": limit * 3})
    return list(rows.mappings())


def hit_of(row: Any) -> SearchHit:
    made = SearchHit(
        meetingId=row["meeting_id"],
        recordingId=row["recording_id"],
        title=row["title"],
        kind=row["kind"],
        snippet=row["snippet"],
        owner=row["owner"],
        score=float(row["score"]),
    )
    return made


def kept_numbers(answer: str, count: int) -> list[int]:
    """The candidate numbers a re-rank answer keeps, in its order, each in 1..count once; none from a bad answer,
    so it can never invent a hit."""
    try:
        said = [int(n) for n in json.loads(answer)["kept"]]
    except ValueError, KeyError, TypeError:
        said = []
    kept = list(dict.fromkeys(n for n in said if 1 <= n <= count))
    return kept


async def reranked(tiers: Tiers, query: str, limit: int, rows: list[Any]) -> list[SearchHit]:
    """The candidates the polish model ranks best, with each meeting's title beside its section, then the rest in
    nearest order, `limit` of them (D38)."""
    if not rows:
        return []
    numbered = [{"n": n + 1, "text": f"{row['title']}: {row['snippet']}"} for n, row in enumerate(rows)]
    deadline = time.monotonic() + DEADLINE_S
    turns = prompt.rerank_turns(query, limit, numbered)
    answer = await ask(tiers.chain(tiers.polish), turns, MAX_TOKENS, deadline, [], hedge_s=HEDGE_S)
    order = kept_numbers(answer, len(rows))
    order.extend(n for n in range(1, len(rows) + 1) if n not in order)
    hits = [hit_of(rows[n - 1]) for n in order[:limit]]
    return hits


async def nearest_ask(session: AsyncSession, meeting_id: UUID, vector: list[float], limit: int) -> list[int]:
    """The meeting's stored Ask sections nearest `vector`, as their places in `qa.sections_of`, nearest first."""
    rows = await session.execute(NEAREST_ASK, {"meeting": meeting_id, "q": json.dumps(vector), "k": limit})
    return [row[0] for row in rows]
