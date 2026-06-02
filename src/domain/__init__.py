"""Core domain layer — models, events, exceptions, and interface contracts.

This package contains ONLY pure business-domain definitions with zero
infrastructure dependencies.  Nothing in ``src.domain`` may import from
``src.infrastructure`` or ``src.services``.
"""

from src.domain.exceptions import (
    VyomaCastError,
    PermanentError,
    PoisonPillError,
    RetryableError,
)
from src.domain.models import (
    Article,
    ArticleSummary,
    Cluster,
    ClusterDetail,
    ClusterStatus,
    ClusterSummary,
    ExtractedContent,
    Feed,
    FeedItem,
    FeedStatus,
    FetchResult,
    PaginatedResponse,
    PaginationMeta,
    SearchResult,
    compute_url_hash,
)

__all__ = [
    # Models
    "Article",
    "ArticleSummary",
    "Cluster",
    "ClusterDetail",
    "ClusterStatus",
    "ClusterSummary",
    "ExtractedContent",
    "Feed",
    "FeedItem",
    "FeedStatus",
    "FetchResult",
    "PaginatedResponse",
    "PaginationMeta",
    "SearchResult",
    "compute_url_hash",
    # Exceptions
    "VyomaCastError",
    "RetryableError",
    "PermanentError",
    "PoisonPillError",
]
