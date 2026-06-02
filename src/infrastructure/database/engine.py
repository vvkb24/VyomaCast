"""Async SQLAlchemy engine and session factory.

Provides a centralized engine creation function and session factory
configured for PostgreSQL + asyncpg with connection pooling parameters
from application settings.

Usage::

    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    # In a repository or handler:
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(select(ArticleRow))
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings


def create_db_engine(*, echo: bool | None = None) -> AsyncEngine:
    """Create and return a configured async SQLAlchemy engine.

    Args:
        echo: Override SQL echo logging.  Defaults to ``True`` when
              ``settings.log_level == "DEBUG"``.

    Returns:
        A fully configured ``AsyncEngine`` with connection pooling.
    """
    if echo is None:
        echo = settings.log_level.upper() == "DEBUG"

    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,       # Detect stale connections
        pool_recycle=3600,         # Recycle connections every hour
        echo=echo,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine.

    Sessions created by this factory:
        * Do NOT expire attributes on commit (safe for async).
        * Do NOT auto-flush (explicit control in repositories).

    Args:
        engine: The async database engine.

    Returns:
        A callable session factory.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
