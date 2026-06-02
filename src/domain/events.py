"""Event-driven messaging types for the NATS JetStream backbone.

This module defines:

1.  ``EventType`` — canonical enum of every subject on the bus.
2.  ``EventMetadata`` / ``EventEnvelope`` — the wire-format envelope that
    wraps every message.  The envelope carries a ``dict`` payload so the bus
    layer stays type-agnostic; consumers call ``envelope.parse_payload(T)``
    to obtain a validated, typed model.
3.  Typed **payload models** — one per event type, matching the JSON schemas
    defined in §10.3 of the architecture document.

Usage (producer)::

    payload = FetchCompletedPayload(url=..., html_content=...)
    envelope = EventEnvelope.create(
        event_type=EventType.FETCH_COMPLETED,
        payload=payload,
        source_service="fetch-worker",
    )
    await bus.publish("fetch.completed", envelope)

Usage (consumer)::

    async def handle(envelope: EventEnvelope) -> None:
        payload = envelope.parse_payload(FetchCompletedPayload)
        ...
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Optional, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


# ────────────────────────────────────────────────────────────────────────────
# Event Type Enum
# ────────────────────────────────────────────────────────────────────────────


class EventType(StrEnum):
    """Every subject published on the NATS JetStream backbone."""

    FEED_DISCOVERED = "feed.discovered"
    FEED_ITEMS_NEW = "feed.items.new"
    FETCH_COMPLETED = "fetch.completed"
    FETCH_FAILED = "fetch.failed"
    EXTRACT_COMPLETED = "extract.completed"
    EXTRACT_FAILED = "extract.failed"
    ARTICLE_UNIQUE = "article.unique"
    ARTICLE_DUPLICATE = "article.duplicate"
    CLUSTER_CREATED = "cluster.created"
    CLUSTER_UPDATED = "cluster.updated"
    CLUSTER_DECAYED = "cluster.decayed"
    ARTICLE_CLUSTERED = "article.clustered"
    WRITEBACK_BATCH = "writeback.batch"
    FEED_PROCESS_COMMAND = "feed.process.command"


# ────────────────────────────────────────────────────────────────────────────
# Envelope
# ────────────────────────────────────────────────────────────────────────────


class EventMetadata(BaseModel):
    """Transport-level metadata carried on every event."""

    source_service: str = Field(min_length=1)
    retry_count: int = Field(default=0, ge=0)
    trace_id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1)


class EventEnvelope(BaseModel):
    """Wire-format wrapper for all inter-service messages.

    The ``payload`` is stored as an untyped ``dict`` so the event-bus
    infrastructure never needs to know about specific payload schemas.
    Consumers validate the payload into the expected type via
    :meth:`parse_payload`.
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]
    metadata: EventMetadata

    # ── Factory ───────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        event_type: EventType,
        payload: BaseModel,
        source_service: str,
        trace_id: UUID | None = None,
    ) -> "EventEnvelope":
        """Build an envelope from a typed payload model.

        The payload is serialised to a JSON-safe ``dict`` automatically.

        Args:
            event_type: The canonical event subject.
            payload: Any Pydantic model — serialised via ``model_dump(mode="json")``.
            source_service: Identifier of the producing worker/service.
            trace_id: Optional correlation ID; a fresh UUID is generated if
                omitted.  **Downstream consumers must propagate the same
                trace_id** to maintain end-to-end traceability.

        Returns:
            A fully populated ``EventEnvelope`` ready for publishing.
        """
        return cls(
            event_type=event_type,
            payload=payload.model_dump(mode="json"),
            metadata=EventMetadata(
                source_service=source_service,
                trace_id=trace_id or uuid4(),
            ),
        )

    # ── Consumer helper ───────────────────────────────────────────────────

    def parse_payload(self, payload_type: type[T]) -> T:
        """Validate the raw ``payload`` dict into a typed Pydantic model.

        Raises:
            ``pydantic.ValidationError`` on schema mismatch — callers should
            let this propagate so the bus wrapper routes it as a
            ``PoisonPillError``.
        """
        return payload_type.model_validate(self.payload)


# ────────────────────────────────────────────────────────────────────────────
# Shared Nested Types  (used inside multiple payloads)
# ────────────────────────────────────────────────────────────────────────────


class ClusterArticleInfo(BaseModel):
    """Lightweight article reference embedded in cluster events and WS frames."""

    id: UUID
    url: str
    title: str
    source_domain: str = ""
    published_at: Optional[datetime] = None


# ────────────────────────────────────────────────────────────────────────────
# Typed Payload Models  (match §10.3 JSON schemas exactly)
# ────────────────────────────────────────────────────────────────────────────


class FeedDiscoveredPayload(BaseModel):
    """A new feed URL to be registered and polled."""

    feed_url: str
    feed_name: Optional[str] = None


class FeedProcessCommandPayload(BaseModel):
    """Command to fetch and process a feed."""

    feed_url: str
    feed_id: Optional[UUID] = None


class FeedItemsNewPayload(BaseModel):
    """A new article URL discovered from a feed poll."""

    feed_id: UUID
    feed_url: str
    item_url: str
    item_guid: Optional[str] = None
    item_title: Optional[str] = None
    item_published: Optional[datetime] = None


class FetchCompletedPayload(BaseModel):
    """Raw HTML successfully fetched for an article URL."""

    url: str
    url_hash: str
    feed_id: Optional[UUID] = None
    status_code: int = Field(ge=100, le=599)
    content_type: str
    html_content: str
    html_size_bytes: int = Field(ge=0)
    fetch_duration_ms: float = Field(ge=0.0)
    final_url: str


class FetchFailedPayload(BaseModel):
    """An article URL that permanently failed fetching."""

    url: str
    url_hash: str
    feed_id: Optional[UUID] = None
    error_type: str
    error_message: str
    status_code: Optional[int] = Field(default=None, ge=100, le=599)
    retry_count: int = Field(ge=0)
    permanent: bool = True


class ExtractCompletedPayload(BaseModel):
    """Clean content successfully extracted from raw HTML."""

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
    quality_score: float = Field(ge=0.0, le=1.0)
    extraction_method: str = "trafilatura"


class ExtractFailedPayload(BaseModel):
    """Extraction failed or content fell below quality threshold."""

    url: str
    url_hash: str
    feed_id: Optional[UUID] = None
    error_type: str
    error_message: str
    extraction_method: str = "trafilatura"


class ArticleUniquePayload(BaseModel):
    """Article passed both dedup stages and is confirmed unique.

    Carries the full extracted content *plus* the computed fingerprints
    so downstream consumers (clustering, cold writer) don't need to
    recompute them.
    """

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
    quality_score: float = Field(ge=0.0, le=1.0)
    extraction_method: str = "trafilatura"
    simhash: int
    embedding: list[float] = Field(min_length=384, max_length=384)


class ArticleDuplicatePayload(BaseModel):
    """Article detected as a duplicate by either dedup stage."""

    url: str
    url_hash: str
    duplicate_of: str = Field(description="url_hash of the original article")
    stage: str = Field(description="'simhash' or 'vector'")
    similarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    hamming_distance: Optional[int] = Field(default=None, ge=0)


class ClusterCreatedPayload(BaseModel):
    """A new story cluster was formed around its first article."""

    cluster_id: UUID
    label: str
    article_count: int = Field(default=1, ge=1)
    centroid: list[float] = Field(min_length=384, max_length=384)
    first_article: ClusterArticleInfo
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ClusterUpdatedPayload(BaseModel):
    """An existing cluster merged a new article."""

    cluster_id: UUID
    label: str
    article_count: int = Field(ge=1)
    new_article: ClusterArticleInfo
    similarity_score: float = Field(ge=0.0, le=1.0)
    top_sources: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ClusterDecayedPayload(BaseModel):
    """A cluster decayed below the eviction threshold."""

    cluster_id: UUID
    label: str
    article_count: int = Field(ge=0)
    final_decay_score: float = Field(ge=0.0, le=1.0)
    lifetime_hours: float = Field(ge=0.0)
    decayed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArticleClusteredPayload(BaseModel):
    """An article was successfully assigned to a new or existing cluster."""

    cluster_id: UUID
    article_id: UUID
    title: str
    source_domain: str
    version: int = Field(ge=1, description="The new version of the cluster")
    is_new_cluster: bool
    similarity_score: Optional[float] = Field(default=None, description="Score if joined existing")


class WritebackBatchPayload(BaseModel):
    """Internal event: a write-back batch was flushed to Postgres."""

    articles_flushed: int = Field(default=0, ge=0)
    clusters_flushed: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    errors: int = Field(default=0, ge=0)


# ────────────────────────────────────────────────────────────────────────────
# Payload Registry
# ────────────────────────────────────────────────────────────────────────────

#: Mapping from ``EventType`` to the corresponding typed payload class.
#: Used by generic consumers that need to dynamically resolve payload types.
PAYLOAD_REGISTRY: dict[EventType, type[BaseModel]] = {
    EventType.FEED_DISCOVERED: FeedDiscoveredPayload,
    EventType.FEED_ITEMS_NEW: FeedItemsNewPayload,
    EventType.FETCH_COMPLETED: FetchCompletedPayload,
    EventType.FETCH_FAILED: FetchFailedPayload,
    EventType.EXTRACT_COMPLETED: ExtractCompletedPayload,
    EventType.EXTRACT_FAILED: ExtractFailedPayload,
    EventType.ARTICLE_UNIQUE: ArticleUniquePayload,
    EventType.ARTICLE_DUPLICATE: ArticleDuplicatePayload,
    EventType.CLUSTER_CREATED: ClusterCreatedPayload,
    EventType.CLUSTER_UPDATED: ClusterUpdatedPayload,
    EventType.CLUSTER_DECAYED: ClusterDecayedPayload,
    EventType.ARTICLE_CLUSTERED: ArticleClusteredPayload,
    EventType.WRITEBACK_BATCH: WritebackBatchPayload,
    EventType.FEED_PROCESS_COMMAND: FeedProcessCommandPayload,
}


# ────────────────────────────────────────────────────────────────────────────
# WebSocket Frame Models  (match §10.4 WS contract)
# ────────────────────────────────────────────────────────────────────────────


class WsSnapshotFrame(BaseModel):
    """Initial full-state snapshot sent to a newly-connected WS client."""

    type: str = "snapshot"
    data: dict[str, Any]


class WsClusterUpdateFrame(BaseModel):
    """Delta update pushed when a cluster is created or updated."""

    type: str = "cluster_update"
    action: str = Field(description="'created' or 'updated'")
    data: dict[str, Any]


class WsClusterRemoveFrame(BaseModel):
    """Removal notice when a cluster decays out of hot state."""

    type: str = "cluster_remove"
    data: dict[str, Any]


class WsHeartbeatFrame(BaseModel):
    """Periodic keepalive frame."""

    type: str = "heartbeat"
    server_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_clusters: int = Field(default=0, ge=0)
    connected_clients: int = Field(default=0, ge=0)
