"""Fake repository implementations for in-memory cold-state testing.

Each fake strictly implements its abstract interface from
``src.domain.interfaces`` and simulates PostgreSQL UPSERT semantics
with version-based conflict guards.

Storage is backed by plain Python dicts keyed by primary key (UUID)
and secondary indices (url_hash, url) for O(1) lookups.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Optional, override
from uuid import UUID

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


# ────────────────────────────────────────────────────────────────────────────
# Cosine similarity helper (pure Python, no numpy needed for fakes)
# ────────────────────────────────────────────────────────────────────────────


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ════════════════════════════════════════════════════════════════════════════
# FakeArticleRepository
# ════════════════════════════════════════════════════════════════════════════


class FakeArticleRepository(ArticleRepository):
    """In-memory article store with UPSERT version guards.

    Indices
    -------
    ``_by_id``        : dict[UUID, Article]   — primary key
    ``_by_url_hash``  : dict[str, Article]    — unique secondary index

    UPSERT semantics:
        * New ``url_hash`` → INSERT.
        * Existing ``url_hash`` with ``new.version > existing.version`` → UPDATE.
        * Existing ``url_hash`` with ``new.version <= existing.version`` → SKIP
          (stale write rejected, returns existing article).
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, Article] = {}
        self._by_url_hash: dict[str, Article] = {}

    # ── Interface ─────────────────────────────────────────────────────────

    @override
    async def get_by_id(self, article_id: UUID) -> Optional[Article]:
        return self._by_id.get(article_id)

    @override
    async def get_by_url_hash(self, url_hash: str) -> Optional[Article]:
        return self._by_url_hash.get(url_hash)

    @override
    async def get_recent(self, *, limit: int = 20, offset: int = 0) -> list[Article]:
        sorted_articles = sorted(
            self._by_id.values(),
            key=lambda a: (a.published_at or datetime.min, a.created_at or datetime.min),
            reverse=True,
        )
        return sorted_articles[offset : offset + limit]

    @override
    async def get_by_cluster_id(
        self,
        cluster_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Article]:
        matches = [
            a for a in self._by_id.values()
            if a.cluster_id == cluster_id
        ]
        # Order by published_at DESC (None sorts last)
        matches.sort(
            key=lambda a: a.published_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return matches[offset : offset + limit]

    @override
    async def save(self, article: Article) -> Article:
        existing = self._by_url_hash.get(article.url_hash)
        if existing is not None:
            if article.version <= existing.version:
                # Stale write — version guard rejects
                return existing
            # Remove stale ID mapping if UUID changed (Postgres has one row)
            if existing.id != article.id:
                self._by_id.pop(existing.id, None)

        # INSERT or UPDATE (new version wins)
        self._by_id[article.id] = article
        self._by_url_hash[article.url_hash] = article
        return article

    @override
    async def save_batch(self, articles: list[Article]) -> int:
        count = 0
        for article in articles:
            try:
                existing = self._by_url_hash.get(article.url_hash)
                if existing is not None:
                    if article.version <= existing.version:
                        continue  # version guard rejects
                    # Remove stale ID mapping if UUID changed
                    if existing.id != article.id:
                        self._by_id.pop(existing.id, None)
                self._by_id[article.id] = article
                self._by_url_hash[article.url_hash] = article
                count += 1
            except Exception:
                # Individual row failures don't abort the batch
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
        scored: list[tuple[float, Article]] = []
        for article in self._by_id.values():
            if not article.embedding:
                continue
            sim = _cosine_similarity(embedding, article.embedding)
            if sim >= threshold:
                scored.append((sim, article))

        # Sort by similarity descending
        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[SearchResult] = []
        for sim, article in scored[:limit]:
            from urllib.parse import urlparse
            domain = urlparse(article.url).netloc
            results.append(
                SearchResult(
                    article_id=article.id,
                    title=article.title,
                    content_preview=article.content[:200],
                    similarity_score=round(sim, 4),
                    cluster_id=article.cluster_id,
                    published_at=article.published_at,
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
        results: list[tuple[datetime, str, list[float]]] = []
        for article in self._by_id.values():
            if not article.embedding:
                continue
            extracted_at = article.extracted_at
            if extracted_at >= since:
                results.append((extracted_at, article.url_hash, article.embedding))

        # Sort by extracted_at descending, take limit
        results.sort(key=lambda x: x[0], reverse=True)
        return [(url_hash, emb) for _, url_hash, emb in results[:limit]]

    @override
    async def count(self) -> int:
        return len(self._by_id)

    # ── Test helpers ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all stored articles."""
        self._by_id.clear()
        self._by_url_hash.clear()

    @property
    def all_articles(self) -> list[Article]:
        """Return all articles (for test assertions)."""
        return list(self._by_id.values())


# ════════════════════════════════════════════════════════════════════════════
# FakeClusterRepository
# ════════════════════════════════════════════════════════════════════════════


class FakeClusterRepository(ClusterRepository):
    """In-memory cluster store with UPSERT version guards.

    UPSERT semantics:
        * New ``id`` → INSERT.
        * Existing ``id`` with ``new.version > existing.version`` → UPDATE.
        * Existing ``id`` with ``new.version <= existing.version`` → SKIP.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, Cluster] = {}

    @override
    async def get_by_id(self, cluster_id: UUID) -> Optional[Cluster]:
        return self._by_id.get(cluster_id)

    @override
    async def get_active(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Cluster]:
        active = [
            c for c in self._by_id.values()
            if c.status == ClusterStatus.ACTIVE
        ]
        active.sort(key=lambda c: c.last_activity, reverse=True)
        return active[offset : offset + limit]

    @override
    async def save(self, cluster: Cluster) -> Cluster:
        existing = self._by_id.get(cluster.id)
        if existing is not None and cluster.version <= existing.version:
            return existing
        self._by_id[cluster.id] = cluster
        return cluster

    @override
    async def save_batch(self, clusters: list[Cluster]) -> int:
        count = 0
        for cluster in clusters:
            try:
                existing = self._by_id.get(cluster.id)
                if existing is None or cluster.version > existing.version:
                    self._by_id[cluster.id] = cluster
                    count += 1
            except Exception:
                continue
        return count

    @override
    async def update_status(self, cluster_id: UUID, status: ClusterStatus) -> bool:
        cluster = self._by_id.get(cluster_id)
        if cluster is None:
            return False

        # Pydantic models are immutable by default; create updated copy
        updated = cluster.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
        self._by_id[cluster_id] = updated
        return True

    @override
    async def count_active(self) -> int:
        return sum(
            1 for c in self._by_id.values()
            if c.status == ClusterStatus.ACTIVE
        )

    # ── Test helpers ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all stored clusters."""
        self._by_id.clear()

    @property
    def all_clusters(self) -> list[Cluster]:
        """Return all clusters (for test assertions)."""
        return list(self._by_id.values())


# ════════════════════════════════════════════════════════════════════════════
# FakeFeedRepository
# ════════════════════════════════════════════════════════════════════════════


class FakeFeedRepository(FeedRepository):
    """In-memory feed store with secondary index on URL.

    UPSERT semantics:
        * New ``url`` → INSERT.
        * Existing ``url`` → UPDATE (overwrite).
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, Feed] = {}
        self._by_url: dict[str, Feed] = {}

    @override
    async def get_by_id(self, feed_id: UUID) -> Optional[Feed]:
        return self._by_id.get(feed_id)

    @override
    async def get_by_url(self, url: str) -> Optional[Feed]:
        return self._by_url.get(url)

    @override
    async def get_due_for_poll(self, *, limit: int = 50) -> list[Feed]:
        now = datetime.now(UTC)
        due = [
            f for f in self._by_id.values()
            if f.status == FeedStatus.ACTIVE and f.next_poll_at <= now
        ]
        # Most overdue first
        due.sort(key=lambda f: f.next_poll_at)
        return due[:limit]

    @override
    async def save(self, feed: Feed) -> Feed:
        # ON CONFLICT (url) DO UPDATE
        existing = self._by_url.get(feed.url)
        if existing is not None:
            # Remove old ID mapping if the ID changed
            if existing.id != feed.id:
                self._by_id.pop(existing.id, None)

        self._by_id[feed.id] = feed
        self._by_url[feed.url] = feed
        return feed

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
        feed = self._by_id.get(feed_id)
        if feed is None:
            return False

        updated = feed.model_copy(
            update={
                "last_polled_at": last_polled_at,
                "next_poll_at": next_poll_at,
                "etag": etag if etag is not None else feed.etag,
                "last_modified": last_modified if last_modified is not None else feed.last_modified,
                "article_count": feed.article_count + article_count_delta,
                "error_count": 0,  # Reset on success
                "last_error": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[feed_id] = updated
        self._by_url[updated.url] = updated
        return True

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
        feed = self._by_id.get(feed_id)
        if feed is None:
            return False

        updated = feed.model_copy(
            update={
                "error_count": error_count,
                "last_error": last_error,
                "status": status,
                "next_poll_at": next_poll_at,
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[feed_id] = updated
        self._by_url[updated.url] = updated
        return True

    @override
    async def get_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Feed]:
        feeds = sorted(self._by_id.values(), key=lambda f: f.created_at, reverse=True)
        return feeds[offset : offset + limit]

    @override
    async def count(self) -> int:
        return len(self._by_id)

    # ── Test helpers ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all stored feeds."""
        self._by_id.clear()
        self._by_url.clear()

    @property
    def all_feeds(self) -> list[Feed]:
        """Return all feeds (for test assertions)."""
        return list(self._by_id.values())
