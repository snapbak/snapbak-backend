"""What the cloud routes and the worker share: the settings, the database, Redis (the arq queue and pub/sub), the
bucket, and the embeddings client. Opened once per process (docs/cloud.md)."""

from collections.abc import Mapping
from dataclasses import dataclass

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from snapbak_backend.config.settings import CloudConfig
from snapbak_backend.db import session
from snapbak_backend.services.embeddings import Embeddings, make_embeddings
from snapbak_backend.services.storage import Storage, s3_client


@dataclass
class Cloud:
    config: CloudConfig
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: ArqRedis
    storage: Storage
    embeddings: Embeddings

    async def close(self) -> None:
        await self.embeddings.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


async def open_cloud(config: CloudConfig, keys: Mapping[str, str]) -> Cloud:
    bound = session.engine(config.database_url)
    redis = await create_pool(RedisSettings.from_dsn(config.redis_url))
    storage = Storage(s3_client(config), config.bucket)
    embeddings = make_embeddings(config.embed, dict(keys))
    cloud = Cloud(config, bound, session.sessions(bound), redis, storage, embeddings)
    return cloud
