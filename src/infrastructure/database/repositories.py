"""PostgreSQL repository implementations using SQLAlchemy 2.0 async.

Each repository strictly implements the abstract interface from
``src.domain.interfaces`` and uses ``INSERT ... ON CONFLICT`` with
version guards (``WHERE version < excluded.version``) for idempotent
upserts, exactly matching the fake repositories' semantics.

Design decisions:
    * Repositories receive an ``async_sessionmaker`` at construction time.
    * Each public method creates its own session (no shared state).
    * ``save_batch()`` uses SAVEPOINTs so individual row failures do not
      abort the entire batch (matching the interface contract).
    * ORM-style ``select(Row)`` for reads, Core-style ``pg_insert()`` for
      upserts (cleanest way to express ON CONFLICT in SQLAlchemy).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional, override
from urllib.parse import urlparse
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.domain.interfaces import (
    ArticleRepository,
    ClusterRepository,
    FeedRepository,
)
from src.domain.models import (
    Article,
    Cluster,
    ClusterStatus,
    Feed,
    FeedStatus,
    SearchResult,
)
from src.infrastructure.database.tables import (
    ArticleRow,
    ClusterRow,
    FeedRow,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Conversion Helpers
# ════════════════════════════════════════════════════════════════════════════


def _row_to_article(row: ArticleRow) -> Article:
    """Convert an ORM ArticleRow to a domain Article model."""
    return Article(
        id=row.id,
        url=row.url,
        url_hash=row.url_hash,
        feed_id=row.feed_id,
        title=row.title,
        content=row.content,
        authors=list(row.authors) if row.authors else [],
        language=row.language,
        top_image_url=row.top_image_url,
        simhash=int(row.simhash) if isinstance(row.simhash, Decimal) else (row.simhash or 0),
        embedding=list(row.embedding) if row.embedding is not None else [],
        cluster_id=row.cluster_id,
        quality_score=float(row.quality_score),
        extraction_method=row.extraction_method,
        raw_html_size=row.raw_html_size,
        content_length=row.content_length,
        published_at=row.published_at,
        extracted_at=row.extracted_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _article_to_dict(article: Article) -> dict:
    """Convert a domain Article to a dict for INSERT VALUES."""
    return {
        "id": article.id,
        "url": article.url,
        "url_hash": article.url_hash,
        "feed_id": article.feed_id,
        "title": article.title,
        "content": article.content,
        "authors": article.authors,
        "language": article.language,
        "top_image_url": article.top_image_url,
        "simhash": article.simhash,
        "embedding": article.embedding if article.embedding else None,
        "cluster_id": article.cluster_id,
        "quality_score": article.quality_score,
        "extraction_method": article.extraction_method,
        "raw_html_size": article.raw_html_size,
        "content_length": article.content_length,
        "published_at": article.published_at,
        "extracted_at": article.extracted_at,
        "version": article.version,
        "created_at": article.created_at,
        "updated_at": article.updated_at,
    }


def _row_to_cluster(row: ClusterRow) -> Cluster:
    """Convert an ORM ClusterRow to a domain Cluster model."""
    return Cluster(
        id=row.id,
        label=row.label,
        centroid=list(row.centroid) if row.centroid is not None else [],
        article_count=row.article_count,
        decay_score=float(row.decay_score),
        last_activity=row.last_activity,
        top_sources=list(row.top_sources) if row.top_sources else [],
        status=ClusterStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _cluster_to_dict(cluster: Cluster) -> dict:
    """Convert a domain Cluster to a dict for INSERT VALUES."""
    return {
        "id": cluster.id,
        "label": cluster.label,
        "centroid": list(cluster.centroid) if cluster.centroid else None,
        "article_count": cluster.article_count,
        "decay_score": cluster.decay_score,
        "last_activity": cluster.last_activity,
        "top_sources": cluster.top_sources,
        "status": cluster.status.value,
        "version": cluster.version,
        "created_at": cluster.created_at,
        "updated_at": cluster.updated_at,
    }


def _row_to_feed(row: FeedRow) -> Feed:
    """Convert an ORM FeedRow to a domain Feed model."""
    return Feed(
        id=row.id,
        url=row.url,
        name=row.name,
        last_polled_at=row.last_polled_at,
        poll_interval=row.poll_interval,
        next_poll_at=row.next_poll_at,
        status=FeedStatus(row.status),
        error_count=row.error_count,
        last_error=row.last_error,
        etag=row.etag,
        last_modified=row.last_modified,
        article_count=row.article_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _feed_to_dict(feed: Feed) -> dict:
    """Convert a domain Feed to a dict for INSERT VALUES."""
    return {
        "id": feed.id,
        "url": feed.url,
        "name": feed.name,
        "last_polled_at": feed.last_polled_at,
        "poll_interval": feed.poll_interval,
        "next_poll_at": feed.next_poll_at,
        "status": feed.status.value,
        "error_count": feed.error_count,
        "last_error": feed.last_error,
        "etag": feed.etag,
        "last_modified": feed.last_modified,
        "article_count": feed.article_count,
        "created_at": feed.created_at,
        "updated_at": feed.updated_at,
    }


# ════════════════════════════════════════════════════════════════════════════
# Article columns updated on conflict (excludes PK, url_hash, created_at)
# ════════════════════════════════════════════════════════════════════════════

_ARTICLE_UPDATE_COLS = [
    "url", "feed_id", "title", "content", "authors", "language",
    "top_image_url", "simhash", "embedding", "cluster_id",
    "quality_score", "extraction_method", "raw_html_size",
    "content_length", "published_at", "extracted_at",
    "version", "updated_at",
]

_CLUSTER_UPDATE_COLS = [
    "label", "centroid", "article_count", "decay_score",
    "last_activity", "top_sources", "status", "version", "updated_at",
]


# ════════════════════════════════════════════════════════════════════════════
# PgArticleRepository
# ════════════════════════════════════════════════════════════════════════════


class PgArticleRepository(ArticleRepository):
    """PostgreSQL article repository with version-guarded UPSERT.

    UPSERT contract::

        INSERT INTO articles (...) VALUES (...)
        ON CONFLICT (url_hash) DO UPDATE SET ...
        WHERE articles.version < excluded.version
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @override
    async def get_by_id(self, article_id: UUID) -> Optional[Article]:
        async with self._session_factory() as session:
            row = await session.get(ArticleRow, article_id)
            return _row_to_article(row) if row else None

    @override
    async def get_by_url_hash(self, url_hash: str) -> Optional[Article]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ArticleRow).where(ArticleRow.url_hash == url_hash)
            )
            row = result.scalar_one_or_none()
            return _row_to_article(row) if row else None

    @override
    async def get_recent(self, *, limit: int = 20, offset: int = 0) -> list[Article]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ArticleRow)
                .order_by(ArticleRow.published_at.desc(), ArticleRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_row_to_article(r) for r in result.scalars()]

    @override
    async def get_by_cluster_id(
        self,
        cluster_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Article]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ArticleRow)
                .where(ArticleRow.cluster_id == cluster_id)
                .order_by(ArticleRow.published_at.desc().nullslast())
                .limit(limit)
                .offset(offset)
            )
            return [_row_to_article(r) for r in result.scalars().all()]

    @override
    async def save(self, article: Article, session: Optional[AsyncSession] = None) -> Article:
        table = ArticleRow.__table__
        data = _article_to_dict(article)

        stmt = pg_insert(table).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["url_hash"],
            set_={col: stmt.excluded[col] for col in _ARTICLE_UPDATE_COLS},
            where=table.c.version < stmt.excluded.version,
        )

        async def _execute(sess: AsyncSession) -> Article:
            await sess.execute(stmt)
            result = await sess.execute(
                select(ArticleRow).where(ArticleRow.url_hash == article.url_hash)
            )
            row = result.scalar_one()
            return _row_to_article(row)

        if session:
            return await _execute(session)
        
        async with self._session_factory() as s:
            async with s.begin():
                return await _execute(s)

    @override
    async def save_batch(self, articles: list[Article]) -> int:
        table = ArticleRow.__table__
        count = 0

        async with self._session_factory() as session:
            async with session.begin():
                for article in articles:
                    try:
                        async with session.begin_nested():
                            data = _article_to_dict(article)
                            stmt = pg_insert(table).values(**data)
                            stmt = stmt.on_conflict_do_update(
                                index_elements=["url_hash"],
                                set_={
                                    col: stmt.excluded[col]
                                    for col in _ARTICLE_UPDATE_COLS
                                },
                                where=table.c.version < stmt.excluded.version,
                            )
                            result = await session.execute(stmt)
                            if result.rowcount > 0:
                                count += 1
                    except Exception as exc:
                        logger.warning(
                            "Batch article insert failed for %s: %s",
                            article.url_hash,
                            exc,
                        )
                        continue
        return count

    @override
    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        limit: int = 20,
        threshold: float = 0.7,
    ) -> list[SearchResult]:
        distance_expr = ArticleRow.embedding.cosine_distance(embedding)
        similarity_expr = (sa.literal(1.0) - distance_expr).label("similarity")

        stmt = (
            select(ArticleRow, similarity_expr)
            .where(ArticleRow.embedding.is_not(None))
            .where(sa.literal(1.0) - distance_expr >= threshold)
            .order_by(distance_expr.asc())
            .limit(limit)
        )

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.all()

            results: list[SearchResult] = []
            for row, similarity in rows:
                domain = urlparse(row.url).netloc
                results.append(
                    SearchResult(
                        article_id=row.id,
                        title=row.title,
                        content_preview=row.content[:200],
                        similarity_score=round(float(similarity), 4),
                        cluster_id=row.cluster_id,
                        published_at=row.published_at,
                        source_domain=domain,
                    )
                )
            return results

    @override
    async def get_recent_embeddings(
        self,
        *,
        since: datetime,
        limit: int = 10_000,
    ) -> list[tuple[str, list[float]]]:
        stmt = (
            select(ArticleRow.url_hash, ArticleRow.embedding)
            .where(ArticleRow.embedding.is_not(None))
            .where(ArticleRow.extracted_at >= since)
            .order_by(ArticleRow.extracted_at.desc())
            .limit(limit)
        )

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [
                (url_hash, list(emb))
                for url_hash, emb in result.all()
            ]

    @override
    async def count(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(ArticleRow)
            )
            return result.scalar_one()


# ════════════════════════════════════════════════════════════════════════════
# PgClusterRepository
# ════════════════════════════════════════════════════════════════════════════


class PgClusterRepository(ClusterRepository):
    """PostgreSQL cluster repository with version-guarded UPSERT.

    UPSERT contract::

        INSERT INTO clusters (...) VALUES (...)
        ON CONFLICT (id) DO UPDATE SET ...
        WHERE clusters.version < excluded.version
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @override
    async def get_by_id(self, cluster_id: UUID) -> Optional[Cluster]:
        async with self._session_factory() as session:
            row = await session.get(ClusterRow, cluster_id)
            return _row_to_cluster(row) if row else None

    @override
    async def get_active(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Cluster]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ClusterRow)
                .where(ClusterRow.status == ClusterStatus.ACTIVE.value)
                .order_by(ClusterRow.last_activity.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_row_to_cluster(r) for r in result.scalars().all()]

    @override
    async def save(self, cluster: Cluster, session: Optional[AsyncSession] = None) -> Cluster:
        table = ClusterRow.__table__
        data = _cluster_to_dict(cluster)

        stmt = pg_insert(table).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={col: stmt.excluded[col] for col in _CLUSTER_UPDATE_COLS},
            where=table.c.version < stmt.excluded.version,
        )

        async def _execute(sess: AsyncSession) -> Cluster:
            await sess.execute(stmt)
            result = await sess.execute(
                select(ClusterRow).where(ClusterRow.id == cluster.id)
            )
            row = result.scalar_one()
            return _row_to_cluster(row)

        if session:
            return await _execute(session)
            
        async with self._session_factory() as s:
            async with s.begin():
                return await _execute(s)

    @override
    async def save_batch(self, clusters: list[Cluster]) -> int:
        table = ClusterRow.__table__
        count = 0

        async with self._session_factory() as session:
            async with session.begin():
                for cluster in clusters:
                    try:
                        async with session.begin_nested():
                            data = _cluster_to_dict(cluster)
                            stmt = pg_insert(table).values(**data)
                            stmt = stmt.on_conflict_do_update(
                                index_elements=["id"],
                                set_={
                                    col: stmt.excluded[col]
                                    for col in _CLUSTER_UPDATE_COLS
                                },
                                where=table.c.version < stmt.excluded.version,
                            )
                            result = await session.execute(stmt)
                            if result.rowcount > 0:
                                count += 1
                    except Exception as exc:
                        logger.warning(
                            "Batch cluster insert failed for %s: %s",
                            cluster.id,
                            exc,
                        )
                        continue
        return count

    @override
    async def update_status(self, cluster_id: UUID, status: ClusterStatus) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    sa.update(ClusterRow)
                    .where(ClusterRow.id == cluster_id)
                    .values(
                        status=status.value,
                        updated_at=datetime.now(UTC),
                    )
                )
                return result.rowcount > 0

    @override
    async def count_active(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ClusterRow)
                .where(ClusterRow.status == ClusterStatus.ACTIVE.value)
            )
            return result.scalar_one()


# ════════════════════════════════════════════════════════════════════════════
# PgFeedRepository
# ════════════════════════════════════════════════════════════════════════════


class PgFeedRepository(FeedRepository):
    """PostgreSQL feed repository with URL-based UPSERT.

    UPSERT contract::

        INSERT INTO feeds (...) VALUES (...)
        ON CONFLICT (url) DO UPDATE SET ...
    """

    _FEED_UPDATE_COLS = [
        "name", "poll_interval", "next_poll_at", "status",
        "error_count", "last_error", "etag", "last_modified",
        "article_count", "updated_at",
    ]

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @override
    async def get_by_id(self, feed_id: UUID) -> Optional[Feed]:
        async with self._session_factory() as session:
            row = await session.get(FeedRow, feed_id)
            return _row_to_feed(row) if row else None

    @override
    async def get_by_url(self, url: str) -> Optional[Feed]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FeedRow).where(FeedRow.url == url)
            )
            row = result.scalar_one_or_none()
            return _row_to_feed(row) if row else None

    @override
    async def get_due_for_poll(self, *, limit: int = 50) -> list[Feed]:
        from datetime import UTC, datetime
        async with self._session_factory() as session:
            result = await session.execute(
                select(FeedRow)
                .where(FeedRow.status == FeedStatus.ACTIVE.value)
                .where(FeedRow.next_poll_at <= datetime.now(UTC))
                .order_by(FeedRow.next_poll_at.asc())
                .limit(limit)
            )
            return [_row_to_feed(r) for r in result.scalars().all()]

    @override
    async def save(self, feed: Feed) -> Feed:
        table = FeedRow.__table__
        data = _feed_to_dict(feed)

        stmt = pg_insert(table).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["url"],
            set_={col: stmt.excluded[col] for col in self._FEED_UPDATE_COLS},
        )

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(stmt)
                result = await session.execute(
                    select(FeedRow).where(FeedRow.url == feed.url)
                )
                row = result.scalar_one()
                return _row_to_feed(row)

    @override
    async def update_poll_state(
        self,
        feed_id: UUID,
        *,
        last_polled_at: datetime,
        next_poll_at: datetime,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        article_count_delta: int = 0,
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    sa.update(FeedRow)
                    .where(FeedRow.id == feed_id)
                    .values(
                        last_polled_at=last_polled_at,
                        next_poll_at=next_poll_at,
                        etag=etag,
                        last_modified=last_modified,
                        article_count=FeedRow.article_count + article_count_delta,
                        error_count=0,
                        last_error=None,
                        updated_at=datetime.now(UTC),
                    )
                )
                return result.rowcount > 0

    @override
    async def update_error_state(
        self,
        feed_id: UUID,
        *,
        error_count: int,
        last_error: str,
        status: FeedStatus,
        next_poll_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    sa.update(FeedRow)
                    .where(FeedRow.id == feed_id)
                    .values(
                        error_count=error_count,
                        last_error=last_error,
                        status=status.value,
                        next_poll_at=next_poll_at,
                        updated_at=datetime.now(UTC),
                    )
                )
                return result.rowcount > 0

    @override
    async def get_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Feed]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FeedRow)
                .order_by(FeedRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_row_to_feed(r) for r in result.scalars().all()]

    @override
    async def count(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(FeedRow)
            )
            return result.scalar_one()
