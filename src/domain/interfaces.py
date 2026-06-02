"""Abstract interface contracts for VyomaCast infrastructure.

Every concrete adapter in ``src.infrastructure`` implements one or more of
these ABCs.  Business-logic services in ``src.services`` depend **only** on
these interfaces, never on concrete implementations.  This enables:

    1. Unit testing with in-memory fakes (``tests/fakes/``).
    2. Swapping infrastructure (e.g. Kafka for NATS) without touching
       business logic.
    3. Clear dependency boundaries enforced at import time.

Rules
-----
*  ``src/services/*.py`` may import from ``src.domain`` only.
*  ``src/infrastructure/*.py`` implements these interfaces.
*  ``src/workers/*.py`` are the composition roots that wire interfaces
   to concrete implementations.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional
from uuid import UUID

from src.domain.events import EventEnvelope
from src.domain.models import (
    Article,
    Cluster,
    ClusterStatus,
    ExtractedContent,
    Feed,
    FeedStatus,
    FetchResult,
    SearchResult,
)

# Type alias for event handler callbacks used by EventBus.subscribe().
# The handler receives a fully parsed EventEnvelope and must raise
# RetryableError / PermanentError to signal failure semantics.
EventHandler = Callable[[EventEnvelope], Awaitable[None]]


# ────────────────────────────────────────────────────────────────────────────
# Repository Interfaces  (Cold State — PostgreSQL)
# ────────────────────────────────────────────────────────────────────────────


class ArticleRepository(ABC):
    """Durable storage for articles (PostgreSQL + pgvector)."""

    @abstractmethod
    async def get_by_id(self, article_id: UUID) -> Optional[Article]:
        """Fetch a single article by primary key.

        Returns:
            The article, or ``None`` if not found.
        """

    @abstractmethod
    async def get_by_url_hash(self, url_hash: str) -> Optional[Article]:
        """Fetch a single article by its normalised-URL hash.

        This is the primary identity lookup used by the dedup engine.
        """

    @abstractmethod
    async def get_by_cluster_id(
        self,
        cluster_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Article]:
        """Fetch articles belonging to a cluster, ordered by published_at DESC."""

    @abstractmethod
    async def save(self, article: Article) -> Article:
        """Upsert a single article.

        Idempotency contract:
            ``INSERT … ON CONFLICT (url_hash) DO UPDATE SET … WHERE version < excluded.version``

        Returns:
            The persisted article (may have updated fields if conflict-resolved).
        """

    @abstractmethod
    async def save_batch(self, articles: list[Article]) -> int:
        """Batch-upsert multiple articles in a single transaction.

        Individual row failures must NOT abort the entire batch; failed rows
        are logged and skipped.

        Returns:
            Number of rows successfully upserted.
        """

    @abstractmethod
    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        limit: int = 20,
        threshold: float = 0.7,
    ) -> list[SearchResult]:
        """Semantic search via pgvector cosine similarity.

        Returns:
            Matching articles sorted by descending similarity, filtered
            by the threshold.
        """

    @abstractmethod
    async def get_recent_embeddings(
        self,
        *,
        since: datetime,
        limit: int = 10_000,
    ) -> list[tuple[str, list[float]]]:
        """Fetch (url_hash, embedding) pairs for recent articles.

        Used by the dedup engine as a fallback when the Redis embedding
        cache misses.

        Returns:
            List of (url_hash, embedding_vector) tuples.
        """

    @abstractmethod
    async def count(self) -> int:
        """Return total number of articles in cold storage."""


class ClusterRepository(ABC):
    """Durable storage for story clusters (PostgreSQL)."""

    @abstractmethod
    async def get_by_id(self, cluster_id: UUID) -> Optional[Cluster]:
        """Fetch a single cluster by primary key."""

    @abstractmethod
    async def get_active(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Cluster]:
        """Fetch active clusters ordered by last_activity DESC."""

    @abstractmethod
    async def save(self, cluster: Cluster) -> Cluster:
        """Upsert a single cluster with version guard.

        Idempotency: ``ON CONFLICT (id) DO UPDATE … WHERE version < excluded.version``
        """

    @abstractmethod
    async def save_batch(self, clusters: list[Cluster]) -> int:
        """Batch-upsert multiple clusters.  Same semantics as ArticleRepository.save_batch."""

    @abstractmethod
    async def update_status(self, cluster_id: UUID, status: ClusterStatus) -> bool:
        """Transition a cluster to a new lifecycle status.

        Returns:
            ``True`` if the row was updated, ``False`` if not found.
        """

    @abstractmethod
    async def count_active(self) -> int:
        """Return number of clusters with status='active'."""


class FeedRepository(ABC):
    """Durable storage for RSS/Atom feed sources (PostgreSQL)."""

    @abstractmethod
    async def get_by_id(self, feed_id: UUID) -> Optional[Feed]:
        """Fetch a single feed by primary key."""

    @abstractmethod
    async def get_by_url(self, url: str) -> Optional[Feed]:
        """Fetch a feed by its URL (unique constraint)."""

    @abstractmethod
    async def get_due_for_poll(self, *, limit: int = 50) -> list[Feed]:
        """Fetch feeds whose ``next_poll_at <= NOW()`` and ``status = 'active'``.

        Ordered by ``next_poll_at ASC`` so the most overdue feeds are polled first.
        """

    @abstractmethod
    async def save(self, feed: Feed) -> Feed:
        """Insert or update a feed.

        Uses ``ON CONFLICT (url) DO UPDATE`` for idempotent re-registration.
        """

    @abstractmethod
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
        """Update polling bookkeeping after a successful poll cycle.

        Returns:
            ``True`` if updated, ``False`` if feed not found.
        """

    @abstractmethod
    async def update_error_state(
        self,
        feed_id: UUID,
        *,
        error_count: int,
        last_error: str,
        status: FeedStatus,
        next_poll_at: datetime,
    ) -> bool:
        """Record a polling failure and (optionally) transition status.

        Returns:
            ``True`` if updated, ``False`` if feed not found.
        """

    @abstractmethod
    async def get_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Feed]:
        """List all feeds ordered by created_at DESC."""

    @abstractmethod
    async def count(self) -> int:
        """Return total number of registered feeds."""


# ────────────────────────────────────────────────────────────────────────────
# Event Bus Interface  (NATS JetStream)
# ────────────────────────────────────────────────────────────────────────────


class EventBus(ABC):
    """Publish/subscribe backbone for inter-service communication.

    Implementations must provide:
        * Exactly-once publish semantics (dedup window on ``event_id``).
        * At-least-once delivery with manual ACK.
        * Automatic DLQ routing after ``max_deliver`` retries.
        * Graceful drain on shutdown.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the message broker.

        Must be called once during application startup.
        Raises ``RetryableError`` on connection failure.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully drain all subscriptions and close connections.

        Must be called during application shutdown to avoid message loss.
        """

    @abstractmethod
    async def publish(self, subject: str, envelope: EventEnvelope) -> None:
        """Publish an event to a subject.

        The implementation must serialise the envelope to JSON and publish
        with a NATS message dedup ID set to ``envelope.event_id`` to
        guarantee idempotent publishing within the dedup window.

        Raises:
            ``RetryableError`` on transient publish failure.
        """

    @abstractmethod
    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue_group: Optional[str] = None,
        durable_name: Optional[str] = None,
    ) -> None:
        """Subscribe to a subject with a message handler.

        The implementation must:
            1. Deserialise incoming bytes into an ``EventEnvelope``.
            2. Call ``handler(envelope)``.
            3. ACK on success.
            4. NAK with exponential back-off on ``RetryableError``.
            5. ACK + route to ``dlq.{subject}`` on ``PermanentError``.
            6. After ``max_deliver`` retries, route to DLQ automatically.

        Args:
            subject: The NATS subject pattern (may include wildcards).
            handler: Async callable that processes the event.
            queue_group: Optional consumer group name for load-balanced
                delivery across multiple worker instances.
            durable_name: Optional durable consumer name for surviving
                worker restarts.
        """

    @abstractmethod
    async def unsubscribe(self, subject: str) -> None:
        """Remove subscription for a subject.

        Pending messages for this subject are drained before unsubscribing.
        """


# ────────────────────────────────────────────────────────────────────────────
# Cache Interface  (Hot State — Redis)
# ────────────────────────────────────────────────────────────────────────────


class CacheStore(ABC):
    """Hot-state cache providing sub-millisecond reads for the live pipeline.

    Key namespaces (from §10.2):
        * ``cluster:{id}``           — Cluster JSON objects
        * ``article:{url_hash}``     — Article metadata hashes
        * ``simhash:band:{i}:{val}`` — SimHash LSH band Sets
        * ``embedding:{id}``         — Cached embedding vectors
        * ``timeline:global``        — Sorted set of article hashes by timestamp
        * ``writeback:dirty:*``      — Dirty-tracking sets
        * ``metrics:*``              — Atomic counters

    The implementation must use ``volatile-lru`` eviction and ensure that
    cluster keys and dirty-tracking keys have **no TTL** so they are
    protected from LRU eviction (per Sanity Check §2.1 Assumption 3).
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish Redis connection pool."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection pool and release resources."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Ping Redis and return ``True`` if responsive."""

    # ── Cluster Operations ────────────────────────────────────────────────

    @abstractmethod
    async def set_cluster(self, cluster_id: str, data: dict) -> None:
        """Store or overwrite a cluster in hot state.

        The key ``cluster:{cluster_id}`` must have **no TTL** to survive
        ``volatile-lru`` eviction.
        """

    @abstractmethod
    async def get_cluster(self, cluster_id: str) -> Optional[dict]:
        """Retrieve a cluster from hot state.

        Returns:
            The cluster data dict, or ``None`` if not in hot state
            (evicted or never cached).
        """

    @abstractmethod
    async def get_active_clusters(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Retrieve a paginated list of all active clusters in hot state.

        Clusters should be ordered by last_activity descending.
        """

    @abstractmethod
    async def get_all_cluster_centroids(self) -> dict[str, tuple[list[float], int]]:
        """Retrieve all active cluster centroids for vectorised similarity.

        Returns:
            Mapping of ``cluster_id → (centroid_vector, article_count)``.
            Used by the clustering engine to build the centroid matrix
            for numpy-vectorised cosine comparison.
        """

    @abstractmethod
    async def delete_cluster(self, cluster_id: str) -> bool:
        """Remove a cluster from hot state (after decay + persistence).

        Returns:
            ``True`` if the key existed and was deleted.
        """

    # ── Timeline ──────────────────────────────────────────────────────────

    @abstractmethod
    async def add_to_timeline(self, article_hash: str, timestamp: float) -> None:
        """Add an article to the global timeline sorted set.

        ``score`` = Unix timestamp of ``published_at`` (or ``extracted_at``).
        """

    @abstractmethod
    async def get_timeline(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[str]:
        """Retrieve article hashes from the timeline, most recent first."""

    # ── SimHash Band Lookup ───────────────────────────────────────────────

    @abstractmethod
    async def add_simhash_bands(
        self,
        article_hash: str,
        bands: dict[int, str],
        ttl_seconds: int,
    ) -> None:
        """Register an article's SimHash band values in Redis Sets.

        For each ``(band_index, band_hex_value)`` pair, adds
        ``article_hash`` to the Set ``simhash:band:{band_index}:{band_hex_value}``
        with the given TTL.

        Args:
            article_hash: The article's ``url_hash``.
            bands: Mapping of band index (0–15) to hex-encoded band value.
            ttl_seconds: TTL for each band Set (SimHash window).
        """

    @abstractmethod
    async def check_simhash_bands(self, bands: dict[int, str]) -> set[str]:
        """Check which existing articles share at least one SimHash band value.

        For each ``(band_index, band_hex_value)`` pair, retrieves members
        of ``simhash:band:{band_index}:{band_hex_value}`` and returns the
        union of all matching article hashes.

        Returns:
            Set of ``url_hash`` strings for potential near-duplicate candidates.
            Empty set means no SimHash collision (article passes Stage 1).
        """

    # ── Embedding Cache ───────────────────────────────────────────────────

    @abstractmethod
    async def cache_embedding(
        self,
        article_id: str,
        embedding: list[float],
        ttl_seconds: int,
    ) -> None:
        """Cache a 384-dim embedding vector for fast Stage-2 dedup lookups.

        Key: ``embedding:{article_id}``.  Has TTL so it can be evicted by
        ``volatile-lru`` when Redis is under memory pressure.
        """

    @abstractmethod
    async def get_cached_embeddings(
        self,
        article_ids: list[str],
    ) -> dict[str, list[float]]:
        """Batch-retrieve cached embeddings.

        Returns:
            Mapping of ``article_id → embedding_vector`` for IDs that
            were found in cache.  Missing IDs are silently omitted.
        """

    # ── Dirty Tracking ────────────────────────────────────────────────────

    @abstractmethod
    async def mark_dirty(self, entity_type: str, entity_id: str) -> None:
        """Mark an entity as needing write-back to PostgreSQL.

        Adds ``entity_id`` to the Set ``writeback:dirty:{entity_type}``.
        The dirty set must have **no TTL** to be protected from eviction.

        Args:
            entity_type: ``"articles"`` or ``"clusters"``.
            entity_id: The url_hash (articles) or UUID (clusters).
        """

    @abstractmethod
    async def get_and_clear_dirty(
        self,
        entity_type: str,
        batch_size: int,
    ) -> list[str]:
        """Atomically pop up to ``batch_size`` IDs from the dirty set.

        Uses ``SPOP`` for atomic remove-and-return.

        Returns:
            List of entity IDs that need to be flushed to Postgres.
        """

    # ── Metrics Counters ──────────────────────────────────────────────────

    @abstractmethod
    async def increment_metric(self, name: str, value: int = 1) -> None:
        """Atomically increment a named counter (``metrics:{name}``)."""


# ────────────────────────────────────────────────────────────────────────────
# Content Extraction Interface
# ────────────────────────────────────────────────────────────────────────────


class ContentExtractor(ABC):
    """Extracts clean text content from raw HTML."""

    @abstractmethod
    async def extract(self, html: str, url: str) -> ExtractedContent:
        """Extract structured content from raw HTML.

        The implementation should:
            1. Apply its extraction strategy (or fallback chain).
            2. Normalise text (NFKC, whitespace collapse).
            3. Compute a quality score.

        Args:
            html: Raw HTML string.
            url: The source URL (used for relative-link resolution
                and quality heuristics).

        Returns:
            Extracted and normalised content.

        Raises:
            ``PermanentError`` if no strategy can extract usable content.
        """


# ────────────────────────────────────────────────────────────────────────────
# HTTP Fetcher Interface
# ────────────────────────────────────────────────────────────────────────────


class HttpFetcher(ABC):
    """Async HTTP client for fetching article HTML."""

    @abstractmethod
    async def fetch(self, url: str, *, feed_id: Optional[UUID] = None) -> FetchResult:
        """Fetch a single URL with retry, rate-limiting, and validation.

        The implementation must:
            1. Respect per-domain rate limits.
            2. Retry transient failures with exponential back-off.
            3. Validate response content-type and size.
            4. Follow redirects and report the ``final_url``.

        Args:
            url: The article URL to fetch.
            feed_id: Optional feed reference for logging/tracing.

        Returns:
            A ``FetchResult`` — check ``result.success`` before accessing
            ``html_content``.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release connection pool and other resources.

        Must be called during application shutdown.
        """


# ────────────────────────────────────────────────────────────────────────────
# Embedding Service Interface
# ────────────────────────────────────────────────────────────────────────────


class EmbeddingService(ABC):
    """Generates semantic embedding vectors from text.

    Abstracts the sentence-transformers model so that:
        1. Unit tests use a deterministic fake (no model loading).
        2. The backend can be swapped (local model → API service)
           without touching the dedup/clustering business logic.
    """

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate a single embedding vector from text.

        The implementation must:
            1. Tokenise and truncate to ``max_tokens`` (default 512).
            2. Generate a unit-normalised embedding vector.

        Args:
            text: Raw article text (title + content, or content only).

        Returns:
            A list of floats with length == ``settings.embedding_dim``.
        """

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a single batch.

        Batch inference is significantly more efficient than sequential
        calls for the local sentence-transformers backend.

        Args:
            texts: List of raw text strings.

        Returns:
            List of embedding vectors, same order as input.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release model and any GPU/CPU resources."""
