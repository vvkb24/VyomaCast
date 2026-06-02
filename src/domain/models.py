"""Core domain models for VyomaCast.

Every model in this module is a pure Pydantic ``BaseModel`` with no
infrastructure coupling.  Models map 1-to-1 with the data contracts defined
in the architecture document §10.

Conventions
-----------
* All ``datetime`` fields are timezone-aware UTC.
* ``UUID`` fields default to auto-generated v4 UUIDs.
* ``version`` fields enable optimistic-concurrency write-back guards.
* ``compute_url_hash`` is the canonical URL normalisation function used
  system-wide for identity and deduplication.
"""

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, Optional, TypeVar
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

# ────────────────────────────────────────────────────────────────────────────
# Utility
# ────────────────────────────────────────────────────────────────────────────


def compute_url_hash(url: str) -> str:
    """Compute a deterministic SHA-256 hash of a normalised URL.

    Normalisation rules:
        1. Lower-case scheme and netloc.
        2. Strip trailing slash from path (keep root ``/``).
        3. Remove fragment (``#…``).
        4. Preserve query string as-is (order-sensitive by design — most news
           URLs use path-based routing and rarely have meaningful query params).

    Returns:
        64-char lowercase hex digest.
    """
    parsed = urlparse(url)
    normalised = urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            parsed.query,
            "",  # discard fragment
        )
    )
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────────────────────────────────


class FeedStatus(StrEnum):
    """Lifecycle state of an RSS/Atom feed source."""

    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DEAD = "dead"


class ClusterStatus(StrEnum):
    """Lifecycle state of a news-story cluster."""

    ACTIVE = "active"
    DECAYED = "decayed"
    ARCHIVED = "archived"


# ────────────────────────────────────────────────────────────────────────────
# Core Domain Models  (match Postgres schema §10.1)
# ────────────────────────────────────────────────────────────────────────────


class Feed(BaseModel):
    """An RSS/Atom feed source that the system polls for new articles."""

    id: UUID = Field(default_factory=uuid4)
    url: str
    name: Optional[str] = None

    # Polling state
    last_polled_at: Optional[datetime] = None
    poll_interval: int = Field(default=600, ge=30, description="Seconds between polls")
    next_poll_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Tracking
    status: FeedStatus = FeedStatus.ACTIVE
    error_count: int = Field(default=0, ge=0)
    last_error: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    # Metadata
    article_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Article(BaseModel):
    """A single news article extracted from a feed.

    This is the canonical domain representation used across the pipeline.
    The ``embedding`` field is populated by the dedup engine; it may be
    empty during earlier pipeline stages.
    """

    id: UUID = Field(default_factory=uuid4)

    # Identity
    url: str
    url_hash: str = Field(description="SHA-256 of normalised URL via compute_url_hash()")
    feed_id: Optional[UUID] = None

    # Content
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    language: str = Field(default="en", min_length=2, max_length=10)
    top_image_url: Optional[str] = None

    # Deduplication fingerprints  (populated by dedup engine)
    # SimHash is a single 128-bit integer (Python int is arbitrary-precision)
    simhash: int = 0
    embedding: list[float] = Field(default_factory=list)

    # Clustering  (populated by clustering engine)
    cluster_id: Optional[UUID] = None

    # Quality & extraction metadata
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_method: str = "trafilatura"
    raw_html_size: Optional[int] = Field(default=None, ge=0)
    content_length: int = Field(default=0, ge=0)

    # Timestamps
    published_at: Optional[datetime] = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Optimistic-concurrency version for idempotent write-back
    version: int = Field(default=1, ge=1)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("embedding")
    @classmethod
    def validate_embedding_length(cls, v: list[float]) -> list[float]:
        """Embedding must be empty (pre-dedup) or exactly 384 dimensions."""
        if len(v) != 0 and len(v) != 384:
            msg = f"Embedding must be empty or 384-dimensional, got {len(v)}"
            raise ValueError(msg)
        return v


class Cluster(BaseModel):
    """A group of semantically related articles representing a single story.

    The ``centroid`` is a rolling-average unit-normalised vector updated on
    every merge.  See the Sanity Check for why re-normalisation after every
    update is mandatory.
    """

    id: UUID = Field(default_factory=uuid4)

    # Cluster state
    label: str = Field(min_length=1, description="Title of the highest-quality article")
    centroid: list[float] = Field(default_factory=list)
    article_count: int = Field(default=1, ge=1)

    # Temporal decay
    decay_score: float = Field(default=1.0, ge=0.0, le=1.0)
    last_activity: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Metadata
    top_sources: list[str] = Field(default_factory=list)

    # Status
    status: ClusterStatus = ClusterStatus.ACTIVE

    # Optimistic-concurrency version
    version: int = Field(default=1, ge=1)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("centroid")
    @classmethod
    def validate_centroid_length(cls, v: list[float]) -> list[float]:
        """Centroid must be empty (initial) or exactly 384 dimensions."""
        if len(v) != 0 and len(v) != 384:
            msg = f"Centroid must be empty or 384-dimensional, got {len(v)}"
            raise ValueError(msg)
        return v


# ────────────────────────────────────────────────────────────────────────────
# Processing-Intermediate Models
# ────────────────────────────────────────────────────────────────────────────


class FeedItem(BaseModel):
    """A single entry discovered from an RSS/Atom feed (pre-fetch)."""

    feed_id: UUID
    feed_url: str
    item_url: str
    item_guid: Optional[str] = None
    item_title: Optional[str] = None
    item_published: Optional[datetime] = None


# NOTE: ArticleFingerprint was removed in self-review — it was never
# referenced by any interface or event payload.  SimHash and embedding
# are accessed directly from the Article model.


class ExtractedContent(BaseModel):
    """Output of the content-extraction engine (pre-dedup)."""

    url: str
    url_hash: str
    feed_id: Optional[UUID] = None
    title: str
    content: str
    content_length: int = Field(ge=0)
    authors: list[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    language: str = "en"
    top_image_url: Optional[str] = None
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_method: str = "trafilatura"


class FetchResult(BaseModel):
    """Output of the async HTTP fetcher."""

    url: str
    url_hash: str
    feed_id: Optional[UUID] = None
    success: bool
    status_code: Optional[int] = Field(default=None, ge=100, le=599)
    content_type: Optional[str] = None
    html_content: Optional[str] = None
    html_size_bytes: Optional[int] = Field(default=None, ge=0)
    fetch_duration_ms: float = Field(default=0.0, ge=0.0)
    final_url: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────
# API Response Models  (match REST contracts §10.4)
# ────────────────────────────────────────────────────────────────────────────


class ArticleSummary(BaseModel):
    """Compact article representation embedded in cluster listings."""

    id: UUID
    url: str
    title: str
    authors: list[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    source_domain: str = ""
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    content_preview: str = ""


class ClusterSummary(BaseModel):
    """Dashboard-optimised cluster card (used in ``GET /api/clusters``)."""

    id: UUID
    label: str
    article_count: int = Field(ge=0)
    decay_score: float = Field(ge=0.0, le=1.0)
    top_sources: list[str] = Field(default_factory=list)
    latest_article: Optional[ArticleSummary] = None
    created_at: datetime
    updated_at: datetime


class ClusterDetail(BaseModel):
    """Full cluster view with nested articles (``GET /api/clusters/{id}``)."""

    id: UUID
    label: str
    article_count: int = Field(ge=0)
    decay_score: float = Field(ge=0.0, le=1.0)
    top_sources: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    articles: list[ArticleSummary] = Field(default_factory=list)


class SearchResult(BaseModel):
    """A single semantic-search result (``GET /api/search``)."""

    article_id: UUID
    title: str
    content_preview: str
    similarity_score: float = Field(ge=0.0, le=1.0)
    cluster_id: Optional[UUID] = None
    cluster_label: Optional[str] = None
    published_at: Optional[datetime] = None
    source_domain: str = ""


class PaginationMeta(BaseModel):
    """Pagination metadata included in every list response."""

    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)


T = TypeVar("T", bound=BaseModel)


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated list wrapper.

    Usage::

        PaginatedResponse[ClusterSummary](data=[...], pagination=PaginationMeta(...))
    """

    data: list[T]
    pagination: PaginationMeta
