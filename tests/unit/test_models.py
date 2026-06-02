"""Unit tests for core domain models, events, and configuration.

Tests verify:
    * Model creation with defaults and all fields
    * Validation constraints (types, ranges, lengths)
    * Enum correctness
    * Event envelope factory and payload round-trip
    * URL hash normalisation
    * Config defaults and derived properties
"""

import json
import math
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError


# ════════════════════════════════════════════════════════════════════════════
# URL Hash Utility
# ════════════════════════════════════════════════════════════════════════════


class TestComputeUrlHash:
    """Tests for ``compute_url_hash()``."""

    def test_basic_hash(self) -> None:
        from src.domain.models import compute_url_hash

        h = compute_url_hash("https://example.com/article/123")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest

    def test_deterministic(self) -> None:
        from src.domain.models import compute_url_hash

        url = "https://example.com/news/breaking"
        assert compute_url_hash(url) == compute_url_hash(url)

    def test_case_insensitive_scheme_and_host(self) -> None:
        from src.domain.models import compute_url_hash

        h1 = compute_url_hash("HTTPS://WWW.EXAMPLE.COM/article")
        h2 = compute_url_hash("https://www.example.com/article")
        assert h1 == h2

    def test_trailing_slash_stripped(self) -> None:
        from src.domain.models import compute_url_hash

        h1 = compute_url_hash("https://example.com/article/")
        h2 = compute_url_hash("https://example.com/article")
        assert h1 == h2

    def test_root_path_preserved(self) -> None:
        from src.domain.models import compute_url_hash

        h = compute_url_hash("https://example.com/")
        assert isinstance(h, str)
        assert len(h) == 64

    def test_fragment_discarded(self) -> None:
        from src.domain.models import compute_url_hash

        h1 = compute_url_hash("https://example.com/page#section1")
        h2 = compute_url_hash("https://example.com/page#section2")
        assert h1 == h2

    def test_query_string_preserved(self) -> None:
        from src.domain.models import compute_url_hash

        h1 = compute_url_hash("https://example.com/search?q=hello")
        h2 = compute_url_hash("https://example.com/search?q=world")
        assert h1 != h2

    def test_different_urls_produce_different_hashes(self) -> None:
        from src.domain.models import compute_url_hash

        h1 = compute_url_hash("https://example.com/article-1")
        h2 = compute_url_hash("https://example.com/article-2")
        assert h1 != h2


# ════════════════════════════════════════════════════════════════════════════
# Enums
# ════════════════════════════════════════════════════════════════════════════


class TestFeedStatus:
    def test_valid_values(self) -> None:
        from src.domain.models import FeedStatus

        assert FeedStatus.ACTIVE == "active"
        assert FeedStatus.PAUSED == "paused"
        assert FeedStatus.ERROR == "error"
        assert FeedStatus.DEAD == "dead"

    def test_all_values_count(self) -> None:
        from src.domain.models import FeedStatus

        assert len(FeedStatus) == 4


class TestClusterStatus:
    def test_valid_values(self) -> None:
        from src.domain.models import ClusterStatus

        assert ClusterStatus.ACTIVE == "active"
        assert ClusterStatus.DECAYED == "decayed"
        assert ClusterStatus.ARCHIVED == "archived"

    def test_all_values_count(self) -> None:
        from src.domain.models import ClusterStatus

        assert len(ClusterStatus) == 3


# ════════════════════════════════════════════════════════════════════════════
# Feed Model
# ════════════════════════════════════════════════════════════════════════════


class TestFeedModel:
    def test_create_with_required_fields_only(self) -> None:
        from src.domain.models import Feed

        feed = Feed(url="https://rss.example.com/feed.xml")
        assert feed.url == "https://rss.example.com/feed.xml"
        assert isinstance(feed.id, UUID)
        assert feed.status == "active"
        assert feed.poll_interval == 600
        assert feed.error_count == 0
        assert feed.article_count == 0

    def test_create_with_all_fields(self) -> None:
        from src.domain.models import Feed, FeedStatus

        feed_id = uuid4()
        now = datetime.now(UTC)
        feed = Feed(
            id=feed_id,
            url="https://rss.example.com/feed.xml",
            name="Example Feed",
            last_polled_at=now,
            poll_interval=300,
            next_poll_at=now,
            status=FeedStatus.PAUSED,
            error_count=2,
            last_error="HTTP 500",
            etag='"abc123"',
            last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
            article_count=150,
            created_at=now,
            updated_at=now,
        )
        assert feed.id == feed_id
        assert feed.name == "Example Feed"
        assert feed.status == FeedStatus.PAUSED
        assert feed.error_count == 2
        assert feed.etag == '"abc123"'

    def test_json_round_trip(self) -> None:
        from src.domain.models import Feed

        feed = Feed(url="https://rss.example.com/feed.xml", name="Test")
        data = json.loads(feed.model_dump_json())
        restored = Feed.model_validate(data)
        assert restored.url == feed.url
        assert restored.id == feed.id


# ════════════════════════════════════════════════════════════════════════════
# Article Model
# ════════════════════════════════════════════════════════════════════════════


class TestArticleModel:
    def test_create_with_required_fields(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        article = Article(
            url=sample_url,
            url_hash=sample_url_hash,
            title="Test headline",
            content="Some news content here.",
        )
        assert article.url == sample_url
        assert article.url_hash == sample_url_hash
        assert isinstance(article.id, UUID)
        assert article.version == 1
        assert article.quality_score == 0.0
        assert article.embedding == []
        assert article.cluster_id is None

    def test_create_with_all_fields(
        self,
        sample_url: str,
        sample_url_hash: str,
        sample_embedding: list[float],
    ) -> None:
        from src.domain.models import Article

        cluster_id = uuid4()
        now = datetime.now(UTC)
        article = Article(
            url=sample_url,
            url_hash=sample_url_hash,
            feed_id=uuid4(),
            title="Global Markets Rally",
            content="Markets surged on Wednesday...",
            authors=["John Smith", "Jane Doe"],
            language="en",
            top_image_url="https://cdn.example.com/img.jpg",
            simhash=123456789,
            embedding=sample_embedding,
            cluster_id=cluster_id,
            quality_score=0.87,
            extraction_method="trafilatura",
            raw_html_size=45000,
            content_length=2847,
            published_at=now,
            extracted_at=now,
            version=3,
        )
        assert article.cluster_id == cluster_id
        assert article.quality_score == 0.87
        assert len(article.embedding) == 384
        assert article.version == 3

    def test_embedding_must_be_empty_or_384(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        # Empty is valid (pre-dedup state)
        a = Article(url=sample_url, url_hash=sample_url_hash, title="T", content="C", embedding=[])
        assert len(a.embedding) == 0

        # 384 is valid
        a2 = Article(
            url=sample_url,
            url_hash=sample_url_hash,
            title="T",
            content="C",
            embedding=[0.1] * 384,
        )
        assert len(a2.embedding) == 384

        # Any other length is invalid
        with pytest.raises(ValidationError, match="384"):
            Article(
                url=sample_url,
                url_hash=sample_url_hash,
                title="T",
                content="C",
                embedding=[0.1] * 100,
            )

    def test_quality_score_range(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        with pytest.raises(ValidationError):
            Article(
                url=sample_url,
                url_hash=sample_url_hash,
                title="T",
                content="C",
                quality_score=1.5,
            )
        with pytest.raises(ValidationError):
            Article(
                url=sample_url,
                url_hash=sample_url_hash,
                title="T",
                content="C",
                quality_score=-0.1,
            )

    def test_title_cannot_be_empty(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        with pytest.raises(ValidationError):
            Article(url=sample_url, url_hash=sample_url_hash, title="", content="C")

    def test_content_cannot_be_empty(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        with pytest.raises(ValidationError):
            Article(url=sample_url, url_hash=sample_url_hash, title="T", content="")

    def test_json_round_trip(self, sample_url: str, sample_url_hash: str) -> None:
        from src.domain.models import Article

        article = Article(
            url=sample_url,
            url_hash=sample_url_hash,
            title="Test",
            content="Content here",
            authors=["Author One"],
        )
        data = json.loads(article.model_dump_json())
        restored = Article.model_validate(data)
        assert restored.url == article.url
        assert restored.id == article.id
        assert restored.authors == ["Author One"]


# ════════════════════════════════════════════════════════════════════════════
# Cluster Model
# ════════════════════════════════════════════════════════════════════════════


class TestClusterModel:
    def test_create_with_label(self, sample_centroid: list[float]) -> None:
        from src.domain.models import Cluster

        cluster = Cluster(label="Global Markets Rally", centroid=sample_centroid)
        assert cluster.label == "Global Markets Rally"
        assert cluster.article_count == 1
        assert cluster.decay_score == 1.0
        assert cluster.status == "active"
        assert cluster.version == 1
        assert len(cluster.centroid) == 384

    def test_centroid_must_be_empty_or_384(self) -> None:
        from src.domain.models import Cluster

        # Empty is valid
        c = Cluster(label="Test", centroid=[])
        assert len(c.centroid) == 0

        # 384 is valid
        c2 = Cluster(label="Test", centroid=[0.1] * 384)
        assert len(c2.centroid) == 384

        # Other lengths invalid
        with pytest.raises(ValidationError, match="384"):
            Cluster(label="Test", centroid=[0.1] * 200)

    def test_label_cannot_be_empty(self) -> None:
        from src.domain.models import Cluster

        with pytest.raises(ValidationError):
            Cluster(label="")

    def test_decay_score_range(self) -> None:
        from src.domain.models import Cluster

        with pytest.raises(ValidationError):
            Cluster(label="Test", decay_score=1.5)

    def test_json_round_trip(self) -> None:
        from src.domain.models import Cluster

        cluster = Cluster(
            label="Test Cluster",
            centroid=[0.01] * 384,
            article_count=5,
            top_sources=["reuters.com", "bbc.com"],
        )
        data = json.loads(cluster.model_dump_json())
        restored = Cluster.model_validate(data)
        assert restored.label == cluster.label
        assert restored.article_count == 5
        assert restored.top_sources == ["reuters.com", "bbc.com"]


# ════════════════════════════════════════════════════════════════════════════
# FeedItem Model
# ════════════════════════════════════════════════════════════════════════════


class TestFeedItemModel:
    def test_create(self) -> None:
        from src.domain.models import FeedItem

        feed_id = uuid4()
        item = FeedItem(
            feed_id=feed_id,
            feed_url="https://rss.example.com/feed.xml",
            item_url="https://example.com/article/1",
            item_guid="guid-123",
            item_title="Breaking News",
        )
        assert item.feed_id == feed_id
        assert item.item_url == "https://example.com/article/1"
        assert item.item_published is None


# ArticleFingerprint was removed in self-review (dead model, never referenced).


# ════════════════════════════════════════════════════════════════════════════
# FetchResult Model
# ════════════════════════════════════════════════════════════════════════════


class TestFetchResultModel:
    def test_successful_fetch(self) -> None:
        from src.domain.models import FetchResult

        result = FetchResult(
            url="https://example.com/article",
            url_hash="abc123",
            success=True,
            status_code=200,
            content_type="text/html",
            html_content="<html>test</html>",
            html_size_bytes=17,
            fetch_duration_ms=823.5,
            final_url="https://example.com/article",
        )
        assert result.success is True
        assert result.status_code == 200

    def test_failed_fetch(self) -> None:
        from src.domain.models import FetchResult

        result = FetchResult(
            url="https://example.com/article",
            url_hash="abc123",
            success=False,
            error_type="timeout",
            error_message="Connection timed out after 10s",
        )
        assert result.success is False
        assert result.html_content is None

    def test_status_code_range(self) -> None:
        from src.domain.models import FetchResult

        with pytest.raises(ValidationError):
            FetchResult(url="u", url_hash="h", success=True, status_code=99)


# ════════════════════════════════════════════════════════════════════════════
# ExtractedContent Model
# ════════════════════════════════════════════════════════════════════════════


class TestExtractedContentModel:
    def test_create(self) -> None:
        from src.domain.models import ExtractedContent

        ec = ExtractedContent(
            url="https://example.com/article",
            url_hash="abc123",
            title="Breaking News",
            content="Full article text here...",
            content_length=25,
            quality_score=0.85,
        )
        assert ec.title == "Breaking News"
        assert ec.quality_score == 0.85
        assert ec.extraction_method == "trafilatura"


# ════════════════════════════════════════════════════════════════════════════
# API Response Models
# ════════════════════════════════════════════════════════════════════════════


class TestPaginationMeta:
    def test_valid(self) -> None:
        from src.domain.models import PaginationMeta

        pm = PaginationMeta(page=1, per_page=50, total=342, total_pages=7)
        assert pm.total_pages == 7

    def test_page_must_be_positive(self) -> None:
        from src.domain.models import PaginationMeta

        with pytest.raises(ValidationError):
            PaginationMeta(page=0, per_page=50, total=100, total_pages=2)

    def test_per_page_max(self) -> None:
        from src.domain.models import PaginationMeta

        with pytest.raises(ValidationError):
            PaginationMeta(page=1, per_page=201, total=100, total_pages=1)


class TestPaginatedResponse:
    def test_generic_with_cluster_summary(self) -> None:
        from src.domain.models import ClusterSummary, PaginatedResponse, PaginationMeta

        now = datetime.now(UTC)
        cluster = ClusterSummary(
            id=uuid4(),
            label="Test Cluster",
            article_count=5,
            decay_score=0.8,
            created_at=now,
            updated_at=now,
        )
        response = PaginatedResponse[ClusterSummary](
            data=[cluster],
            pagination=PaginationMeta(page=1, per_page=50, total=1, total_pages=1),
        )
        assert len(response.data) == 1
        assert response.data[0].label == "Test Cluster"

    def test_empty_data(self) -> None:
        from src.domain.models import ClusterSummary, PaginatedResponse, PaginationMeta

        response = PaginatedResponse[ClusterSummary](
            data=[],
            pagination=PaginationMeta(page=1, per_page=50, total=0, total_pages=0),
        )
        assert response.data == []


class TestSearchResult:
    def test_create(self) -> None:
        from src.domain.models import SearchResult

        sr = SearchResult(
            article_id=uuid4(),
            title="Test Article",
            content_preview="Preview text...",
            similarity_score=0.89,
            source_domain="reuters.com",
        )
        assert sr.similarity_score == 0.89

    def test_similarity_score_range(self) -> None:
        from src.domain.models import SearchResult

        with pytest.raises(ValidationError):
            SearchResult(
                article_id=uuid4(),
                title="T",
                content_preview="P",
                similarity_score=1.5,
            )


# ════════════════════════════════════════════════════════════════════════════
# Event Type Enum
# ════════════════════════════════════════════════════════════════════════════


class TestEventType:
    def test_all_subjects(self) -> None:
        from src.domain.events import EventType

        assert len(EventType) >= 14, "EventType should contain at least all base events"
        assert EventType.FETCH_COMPLETED == "fetch.completed"
        assert EventType.CLUSTER_UPDATED == "cluster.updated"
        # Newly added ones
        assert EventType.FEED_ITEMS_NEW == "feed.items.new"

    def test_string_value(self) -> None:
        from src.domain.events import EventType

        # StrEnum values can be used as plain strings
        subject: str = EventType.ARTICLE_UNIQUE
        assert subject == "article.unique"


# ════════════════════════════════════════════════════════════════════════════
# Event Envelope
# ════════════════════════════════════════════════════════════════════════════


class TestEventEnvelope:
    def test_create_factory(self) -> None:
        from src.domain.events import (
            EventEnvelope,
            EventType,
            FetchCompletedPayload,
        )

        payload = FetchCompletedPayload(
            url="https://example.com/article",
            url_hash="abc123",
            status_code=200,
            content_type="text/html",
            html_content="<html>test</html>",
            html_size_bytes=17,
            fetch_duration_ms=823.5,
            final_url="https://example.com/article",
        )
        envelope = EventEnvelope.create(
            event_type=EventType.FETCH_COMPLETED,
            payload=payload,
            source_service="fetch-worker",
        )
        assert envelope.event_type == EventType.FETCH_COMPLETED
        assert isinstance(envelope.event_id, UUID)
        assert envelope.metadata.source_service == "fetch-worker"
        assert isinstance(envelope.metadata.trace_id, UUID)
        assert envelope.payload["url"] == "https://example.com/article"
        assert envelope.payload["status_code"] == 200

    def test_create_with_explicit_trace_id(self) -> None:
        from src.domain.events import (
            EventEnvelope,
            EventType,
            FeedDiscoveredPayload,
        )

        trace = uuid4()
        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=FeedDiscoveredPayload(feed_url="https://rss.example.com/feed"),
            source_service="feed-manager",
            trace_id=trace,
        )
        assert envelope.metadata.trace_id == trace

    def test_parse_payload(self) -> None:
        from src.domain.events import (
            EventEnvelope,
            EventType,
            FetchCompletedPayload,
        )

        payload = FetchCompletedPayload(
            url="https://example.com/article",
            url_hash="abc123",
            status_code=200,
            content_type="text/html",
            html_content="<html>test</html>",
            html_size_bytes=17,
            fetch_duration_ms=823.5,
            final_url="https://example.com/article",
        )
        envelope = EventEnvelope.create(
            event_type=EventType.FETCH_COMPLETED,
            payload=payload,
            source_service="test",
        )
        parsed = envelope.parse_payload(FetchCompletedPayload)
        assert isinstance(parsed, FetchCompletedPayload)
        assert parsed.url == "https://example.com/article"
        assert parsed.status_code == 200
        assert parsed.html_content == "<html>test</html>"

    def test_json_serialization_round_trip(self) -> None:
        from src.domain.events import (
            EventEnvelope,
            EventType,
            ExtractCompletedPayload,
        )

        payload = ExtractCompletedPayload(
            url="https://example.com/article",
            url_hash="abc123",
            title="Test Article",
            content="Article content here",
            content_length=20,
            authors=["Author One"],
            quality_score=0.85,
        )
        envelope = EventEnvelope.create(
            event_type=EventType.EXTRACT_COMPLETED,
            payload=payload,
            source_service="extract-worker",
        )

        # Serialise to JSON and back
        json_str = envelope.model_dump_json()
        restored = EventEnvelope.model_validate_json(json_str)

        assert restored.event_id == envelope.event_id
        assert restored.event_type == EventType.EXTRACT_COMPLETED
        assert restored.metadata.source_service == "extract-worker"

        # Parse the payload from the restored envelope
        parsed = restored.parse_payload(ExtractCompletedPayload)
        assert parsed.title == "Test Article"
        assert parsed.authors == ["Author One"]

    def test_parse_payload_wrong_type_raises(self) -> None:
        from src.domain.events import (
            EventEnvelope,
            EventType,
            FeedDiscoveredPayload,
            FetchCompletedPayload,
        )

        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=FeedDiscoveredPayload(feed_url="https://rss.example.com"),
            source_service="test",
        )
        # Trying to parse as wrong type should raise ValidationError
        # (FetchCompletedPayload requires status_code, content_type, etc.)
        with pytest.raises(ValidationError):
            envelope.parse_payload(FetchCompletedPayload)


# ════════════════════════════════════════════════════════════════════════════
# Typed Payload Models
# ════════════════════════════════════════════════════════════════════════════


class TestFeedItemsNewPayload:
    def test_create(self) -> None:
        from src.domain.events import FeedItemsNewPayload

        p = FeedItemsNewPayload(
            feed_id=uuid4(),
            feed_url="https://rss.reuters.com/topNews",
            item_url="https://www.reuters.com/article/example",
            item_guid="tag:reuters.com,2026:example",
            item_title="Breaking News",
        )
        assert p.item_url == "https://www.reuters.com/article/example"


class TestArticleUniquePayload:
    def test_create_with_embedding(self) -> None:
        from src.domain.events import ArticleUniquePayload

        embedding = [math.sin(i * 0.1) * 0.5 for i in range(384)]
        p = ArticleUniquePayload(
            url="https://example.com/article",
            url_hash="abc123",
            title="Test Article",
            content="Article content",
            content_length=15,
            quality_score=0.8,
            simhash=123456,
            embedding=embedding,
        )
        assert len(p.embedding) == 384
        assert p.simhash == 123456

    def test_embedding_must_be_384(self) -> None:
        from src.domain.events import ArticleUniquePayload

        with pytest.raises(ValidationError):
            ArticleUniquePayload(
                url="u",
                url_hash="h",
                title="T",
                content="C",
                content_length=1,
                quality_score=0.5,
                simhash=1,
                embedding=[0.1] * 100,
            )


class TestArticleDuplicatePayload:
    def test_simhash_duplicate(self) -> None:
        from src.domain.events import ArticleDuplicatePayload

        p = ArticleDuplicatePayload(
            url="https://example.com/dup",
            url_hash="hash_dup",
            duplicate_of="hash_original",
            stage="simhash",
            hamming_distance=2,
        )
        assert p.stage == "simhash"
        assert p.hamming_distance == 2
        assert p.similarity_score is None

    def test_vector_duplicate(self) -> None:
        from src.domain.events import ArticleDuplicatePayload

        p = ArticleDuplicatePayload(
            url="https://example.com/dup",
            url_hash="hash_dup",
            duplicate_of="hash_original",
            stage="vector",
            similarity_score=0.94,
        )
        assert p.stage == "vector"
        assert p.similarity_score == 0.94


class TestClusterCreatedPayload:
    def test_create(self) -> None:
        from src.domain.events import ClusterArticleInfo, ClusterCreatedPayload

        article_info = ClusterArticleInfo(
            id=uuid4(),
            url="https://example.com/article",
            title="Breaking News",
            source_domain="example.com",
        )
        p = ClusterCreatedPayload(
            cluster_id=uuid4(),
            label="Breaking News",
            centroid=[0.01] * 384,
            first_article=article_info,
        )
        assert p.article_count == 1
        assert len(p.centroid) == 384
        assert p.first_article.source_domain == "example.com"


class TestClusterUpdatedPayload:
    def test_create(self) -> None:
        from src.domain.events import ClusterArticleInfo, ClusterUpdatedPayload

        p = ClusterUpdatedPayload(
            cluster_id=uuid4(),
            label="Global Markets Rally",
            article_count=7,
            new_article=ClusterArticleInfo(
                id=uuid4(),
                url="https://bbc.com/markets",
                title="Stocks Soar",
                source_domain="bbc.com",
            ),
            similarity_score=0.84,
            top_sources=["reuters.com", "bbc.com"],
        )
        assert p.article_count == 7
        assert p.similarity_score == 0.84


class TestClusterDecayedPayload:
    def test_create(self) -> None:
        from src.domain.events import ClusterDecayedPayload

        p = ClusterDecayedPayload(
            cluster_id=uuid4(),
            label="Old Story",
            article_count=12,
            final_decay_score=0.043,
            lifetime_hours=8.2,
        )
        assert p.final_decay_score == 0.043
        assert p.lifetime_hours == 8.2


# ════════════════════════════════════════════════════════════════════════════
# Payload Registry
# ════════════════════════════════════════════════════════════════════════════


class TestPayloadRegistry:
    def test_all_event_types_registered(self) -> None:
        from src.domain.events import PAYLOAD_REGISTRY, EventType

        for event_type in EventType:
            assert event_type in PAYLOAD_REGISTRY, f"Missing registry entry for {event_type}"

    def test_registry_values_are_basemodel_subclasses(self) -> None:
        from pydantic import BaseModel

        from src.domain.events import PAYLOAD_REGISTRY

        for event_type, model_class in PAYLOAD_REGISTRY.items():
            assert issubclass(model_class, BaseModel), (
                f"Registry entry for {event_type} is not a BaseModel subclass"
            )


# ════════════════════════════════════════════════════════════════════════════
# WebSocket Frame Models
# ════════════════════════════════════════════════════════════════════════════


class TestWsFrames:
    def test_heartbeat_frame(self) -> None:
        from src.domain.events import WsHeartbeatFrame

        frame = WsHeartbeatFrame(active_clusters=342, connected_clients=5)
        assert frame.type == "heartbeat"
        assert frame.active_clusters == 342

    def test_cluster_update_frame(self) -> None:
        from src.domain.events import WsClusterUpdateFrame

        frame = WsClusterUpdateFrame(
            action="updated",
            data={"cluster_id": str(uuid4()), "label": "Test"},
        )
        assert frame.type == "cluster_update"
        assert frame.action == "updated"

    def test_cluster_remove_frame(self) -> None:
        from src.domain.events import WsClusterRemoveFrame

        frame = WsClusterRemoveFrame(data={"cluster_id": str(uuid4())})
        assert frame.type == "cluster_remove"


# ════════════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_hierarchy(self) -> None:
        from src.domain.exceptions import (
            VyomaCastError,
            PermanentError,
            PoisonPillError,
            RetryableError,
        )

        assert issubclass(RetryableError, VyomaCastError)
        assert issubclass(PermanentError, VyomaCastError)
        assert issubclass(PoisonPillError, PermanentError)

    def test_retryable_is_not_permanent(self) -> None:
        from src.domain.exceptions import PermanentError, RetryableError

        assert not issubclass(RetryableError, PermanentError)

    def test_details_dict(self) -> None:
        from src.domain.exceptions import RetryableError

        err = RetryableError("connection failed", details={"host": "localhost", "port": 5432})
        assert err.details["host"] == "localhost"
        assert str(err) == "connection failed"

    def test_default_empty_details(self) -> None:
        from src.domain.exceptions import VyomaCastError

        err = VyomaCastError("test")
        assert err.details == {}

    def test_catchable_as_base(self) -> None:
        from src.domain.exceptions import VyomaCastError, PoisonPillError

        try:
            raise PoisonPillError("bad message")
        except VyomaCastError as e:
            assert "bad message" in str(e)


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════


class TestConfig:
    def test_defaults_load(self) -> None:
        from src.config import Settings

        s = Settings()
        assert s.embedding_dim == 384
        assert s.nats_ack_wait_seconds == 120  # Sanity check correction
        assert s.redis_maxmemory == "768mb"  # Sanity check correction
        assert s.embedding_cache_ttl_hours == 8  # Sanity check correction
        assert s.redis_eviction_policy == "volatile-lru"  # Sanity check correction

    def test_derived_properties(self) -> None:
        from src.config import Settings

        s = Settings()
        assert s.simhash_total_bits == 128  # 16 bands × 8 bits
        assert s.simhash_window_seconds == 72 * 3600
        assert s.embedding_cache_ttl_seconds == 8 * 3600

    def test_validation_constraints(self) -> None:
        from src.config import Settings

        with pytest.raises(ValidationError):
            Settings(db_pool_size=0)
        with pytest.raises(ValidationError):
            Settings(vector_cosine_threshold=0.3)
        with pytest.raises(ValidationError):
            Settings(nats_ack_wait_seconds=5)


# ════════════════════════════════════════════════════════════════════════════
# Interface Contracts (verify ABCs are well-formed)
# ════════════════════════════════════════════════════════════════════════════


class TestEmbeddingServiceInterface:
    """Verify the EmbeddingService ABC added in self-review."""

    def test_is_abstract(self) -> None:
        from src.domain.interfaces import EmbeddingService

        with pytest.raises(TypeError, match="abstract"):
            EmbeddingService()  # type: ignore[abstract]

    def test_has_required_methods(self) -> None:
        from src.domain.interfaces import EmbeddingService

        assert hasattr(EmbeddingService, "embed")
        assert hasattr(EmbeddingService, "embed_batch")
        assert hasattr(EmbeddingService, "close")

