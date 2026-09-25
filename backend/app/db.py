"""Async SQLAlchemy engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=not settings.database_url.startswith("sqlite"),
    future=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Build the schema from the models, and record that it is at head.

    `create_all` produces exactly the schema the newest migration produces —
    it is built from the same models — but it used to leave `alembic_version`
    empty. A database in that state is at head and does not say so, and the
    next `alembic upgrade head` replays the initial revision against objects
    that already exist and dies on `DuplicateObjectError`.

    Stamping here is what makes the two paths agree. Creating the schema and
    recording where it stands are one operation, in one transaction, so the
    unstamped state cannot be produced at all.
    """
    from app import models  # noqa: F401  (register mappers)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_stamp_if_unversioned)


def _stamp_if_unversioned(conn: Any) -> None:
    from sqlalchemy import inspect

    if inspect(conn).has_table("alembic_version"):
        return  # Alembic is already in charge; never touch its bookkeeping.
    from app.db_bootstrap import stamp

    stamp(conn)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
