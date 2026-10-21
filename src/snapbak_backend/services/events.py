"""Job changes over Redis pub/sub: a wake-up for long-polls, never the truth (the row is). docs/cloud.md."""

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

CHANNEL = "job:"
# After Redis drops the subscription, it is made again after 0.1 s, doubling to at most 5 s.
FIRST_BACKOFF_S = 0.1
MOST_BACKOFF_S = 5.0


async def publish(redis: Redis, job_id: UUID, version: int) -> None:
    """Called after each committed state change, by the API or the worker."""
    await redis.publish(f"{CHANNEL}{job_id}", version)


class Events:
    """One pattern subscription per API process; a message on `job:{id}` sets every waiting request's event."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.waiting: dict[str, set[asyncio.Event]] = {}
        self.listener: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.listener = asyncio.create_task(self.listen())

    async def listen(self) -> None:
        """Subscribes, and subscribes again whenever Redis drops the connection. Each new subscription wakes every
        waiting request, since a change may have been published while it was down; they read their row again."""
        backoff = FIRST_BACKOFF_S
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.psubscribe(f"{CHANNEL}*")
                    self.wake_all()
                    backoff = FIRST_BACKOFF_S
                    await self.relay(pubsub)
            except RedisError, OSError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MOST_BACKOFF_S)

    async def relay(self, pubsub: Any) -> None:
        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            job_id = message["channel"].decode().removeprefix(CHANNEL)
            for event in self.waiting.get(job_id, ()):
                event.set()

    def wake_all(self) -> None:
        for watchers in self.waiting.values():
            for event in watchers:
                event.set()

    @contextlib.contextmanager
    def watch(self, job_id: UUID) -> Iterator[asyncio.Event]:
        """An event set by the job's next change; registered before the caller reads the row, so a change between
        the read and the wait is not missed."""
        key = str(job_id)
        event = asyncio.Event()
        self.waiting.setdefault(key, set()).add(event)
        try:
            yield event
        finally:
            watchers = self.waiting[key]
            watchers.discard(event)
            if not watchers:
                del self.waiting[key]

    async def stop(self) -> None:
        if self.listener is not None:
            self.listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listener
