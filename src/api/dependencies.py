"""Dependency Injection providers for the FastAPI app."""

import logging
from collections.abc import AsyncGenerator

from typing import Annotated
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.engine import create_db_engine, create_session_factory
from src.infrastructure.database.repositories import (
    PgArticleRepository,
    PgClusterRepository,
)
from src.domain.interfaces import ArticleRepository, ClusterRepository
from src.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# Singletons for the app lifecycle
engine = create_db_engine()
session_maker = create_session_factory(engine)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional scoped session securely tearing down after request."""
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_article_repo(session: AsyncSession = Depends(get_session)) -> ArticleRepository:
    """Inject the Article Repository wrapping the yielded session."""
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def factory():
        yield session
    return PgArticleRepository(factory)


async def get_cluster_repo(session: AsyncSession = Depends(get_session)) -> ClusterRepository:
    """Inject the Cluster Repository wrapping the yielded session."""
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def factory():
        yield session
    return PgClusterRepository(factory)


def get_embedding_service() -> EmbeddingService:
    """Inject the Embedding Service."""
    return EmbeddingService()

ArticleRepoDep = Annotated[ArticleRepository, Depends(get_article_repo)]
ClusterRepoDep = Annotated[ClusterRepository, Depends(get_cluster_repo)]
EmbeddingDep = Annotated[EmbeddingService, Depends(get_embedding_service)]
