"""One async engine per process from DATABASE_URL (docs/cloud.md)."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def engine(url: str) -> AsyncEngine:
    made = create_async_engine(url, pool_size=10, max_overflow=5, pool_pre_ping=True)
    return made


def sessions(bound: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bound, expire_on_commit=False)
