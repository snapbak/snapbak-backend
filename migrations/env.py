"""Alembic in async mode against DATABASE_URL (docs/cloud.md). Production runs it once per deploy, never at start."""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from snapbak_backend.db.models import Base


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


asyncio.run(run())
