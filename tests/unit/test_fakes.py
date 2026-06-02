"""Smoke tests for in-memory test fakes.

Verifies that every fake:
    1. Instantiates without infrastructure.
    2. Is an instance of its interface ABC.
    3. Exercises core operations (save/get, publish/inspect, embed).
    4. Correctly implements UPSERT version guards.
    5. Correctly simulates TTL expiry (cache).
    6. Correctly models dirty tracking and SimHash bands.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.events import (
    EventEnvelope,
    EventType,
    FeedDiscoveredPayload,
    FetchCompletedPayload,
)
from src.domain.interfaces import (
    ArticleRepository,
    CacheStore,
    ClusterRepository,
    EmbeddingService,
    EventBus,
    FeedRepository,
)
from src.domain.models import (
    Article,
    Cluster,
    ClusterStatus,
    Feed,
    FeedStatus,
    compute_url_hash,
)
from tests.fakes import (
    FakeArticleRepository,
    FakeCacheStore,
    FakeClusterRepository,
    FakeEmbeddingService,
    FakeEventBus,
    FakeFeedRepository,
)


# ════════════════════════════════════════════════════════════════════════════
# Interface Compliance
# ════════════════════════════════════════════════════════════════════════════


class TestInterfaceCompliance:
    """Verify every fake is a proper subclass of its abstract interface."""

    def test_article_repo_isinstance(self) -> None:
        assert isinstance(FakeArticleRepository(), ArticleRepository)

    def test_cluster_repo_isinstance(self) -> None:
        assert isinstance(FakeClusterRepository(), ClusterRepository)

    def test_feed_repo_isinstance(self) -> None:
        assert isinstance(FakeFeedRepository(), FeedRepository)

    def test_event_bus_isinstance(self) -> None:
        assert isinstance(FakeEventBus(), EventBus)

    def test_cache_store_isinstance(self) -> None:
        assert isinstance(FakeCacheStore(), CacheStore)

    def test_embedding_service_isinstance(self) -> None:
        assert isinstance(FakeEmbeddingService(), EmbeddingService)


# ════════════════════════════════════════════════════════════════════════════
# FakeArticleRepository
# ════════════════════════════════════════════════════════════════════════════


class TestFakeArticleRepository:
    @pytest.fixture()
    def repo(self) -> FakeArticleRepository:
        return FakeArticleRepository()

    @pytest.fixture()
    def sample_article(self) -> Article:
        url = "https://example.com/article/1"
        return Article(
            url=url,
            url_hash=compute_url_hash(url),
            title="Test Article",
            content="Some content here.",
            version=1,
        )

    async def test_save_and_get_by_id(self, repo: FakeArticleRepository, sample_article: Article) -> None:
        saved = await repo.save(sample_article)
        assert saved.id == sample_article.id

        fetched = await repo.get_by_id(sample_article.id)
        assert fetched is not None
        assert fetched.title == "Test Article"

    async def test_save_and_get_by_url_hash(self, repo: FakeArticleRepository, sample_article: Article) -> None:
        await repo.save(sample_article)
        fetched = await repo.get_by_url_hash(sample_article.url_hash)
        assert fetched is not None
        assert fetched.url == sample_article.url

    async def test_get_missing_returns_none(self, repo: FakeArticleRepository) -> None:
        assert await repo.get_by_id(uuid4()) is None
        assert await repo.get_by_url_hash("nonexistent") is None

    async def test_version_guard_rejects_stale_write(self, repo: FakeArticleRepository) -> None:
        url = "https://example.com/article/version-test"
        url_hash = compute_url_hash(url)

        v2 = Article(url=url, url_hash=url_hash, title="Version 2", content="C", version=2)
        await repo.save(v2)

        # Try to save an older version — should be rejected
        v1 = Article(url=url, url_hash=url_hash, title="Version 1 (stale)", content="C", version=1)
        result = await repo.save(v1)

        assert result.title == "Version 2"  # v2 preserved
        fetched = await repo.get_by_url_hash(url_hash)
        assert fetched is not None
        assert fetched.version == 2

    async def test_version_guard_accepts_newer_write(self, repo: FakeArticleRepository) -> None:
        url = "https://example.com/article/upgrade"
        url_hash = compute_url_hash(url)

        v1 = Article(url=url, url_hash=url_hash, title="V1", content="C", version=1)
        await repo.save(v1)

        v3 = Article(url=url, url_hash=url_hash, title="V3", content="Updated", version=3)
        result = await repo.save(v3)

        assert result.title == "V3"
        assert result.version == 3

    async def test_save_batch(self, repo: FakeArticleRepository) -> None:
        articles = []
        for i in range(5):
            url = f"https://example.com/batch/{i}"
            articles.append(
                Article(url=url, url_hash=compute_url_hash(url), title=f"Art {i}", content="C")
            )
        count = await repo.save_batch(articles)
        assert count == 5
        assert await repo.count() == 5

    async def test_save_batch_version_guard(self, repo: FakeArticleRepository) -> None:
        url = "https://example.com/batch-guard"
        url_hash = compute_url_hash(url)

        # Pre-save v2
        v2 = Article(url=url, url_hash=url_hash, title="V2", content="C", version=2)
        await repo.save(v2)

        # Batch with v1 (stale) and a new article
        v1_stale = Article(url=url, url_hash=url_hash, title="V1", content="C", version=1)
        new_url = "https://example.com/batch-guard/new"
        new_art = Article(url=new_url, url_hash=compute_url_hash(new_url), title="New", content="C")

        count = await repo.save_batch([v1_stale, new_art])
        assert count == 1  # Only the new article was inserted
        assert await repo.count() == 2

    async def test_get_by_cluster_id(self, repo: FakeArticleRepository) -> None:
        cluster_id = uuid4()
        for i in range(3):
            url = f"https://example.com/cluster/{i}"
            article = Article(
                url=url,
                url_hash=compute_url_hash(url),
                title=f"Clustered {i}",
                content="C",
                cluster_id=cluster_id,
            )
            await repo.save(article)

        results = await repo.get_by_cluster_id(cluster_id)
        assert len(results) == 3

    async def test_count(self, repo: FakeArticleRepository, sample_article: Article) -> None:
        assert await repo.count() == 0
        await repo.save(sample_article)
        assert await repo.count() == 1


# ════════════════════════════════════════════════════════════════════════════
# FakeClusterRepository
# ════════════════════════════════════════════════════════════════════════════


class TestFakeClusterRepository:
    @pytest.fixture()
    def repo(self) -> FakeClusterRepository:
        return FakeClusterRepository()

    async def test_save_and_get(self, repo: FakeClusterRepository) -> None:
        cluster = Cluster(label="Breaking News", centroid=[0.1] * 384)
        saved = await repo.save(cluster)
        assert saved.label == "Breaking News"

        fetched = await repo.get_by_id(cluster.id)
        assert fetched is not None
        assert fetched.label == "Breaking News"

    async def test_version_guard(self, repo: FakeClusterRepository) -> None:
        cid = uuid4()
        v2 = Cluster(id=cid, label="V2", version=2)
        await repo.save(v2)

        v1 = Cluster(id=cid, label="V1 stale", version=1)
        result = await repo.save(v1)
        assert result.label == "V2"

    async def test_get_active(self, repo: FakeClusterRepository) -> None:
        c1 = Cluster(label="Active 1")
        c2 = Cluster(label="Active 2")
        c3 = Cluster(label="Decayed", status=ClusterStatus.DECAYED)
        await repo.save(c1)
        await repo.save(c2)
        await repo.save(c3)

        active = await repo.get_active()
        assert len(active) == 2
        assert all(c.status == ClusterStatus.ACTIVE for c in active)

    async def test_update_status(self, repo: FakeClusterRepository) -> None:
        cluster = Cluster(label="Test")
        await repo.save(cluster)

        result = await repo.update_status(cluster.id, ClusterStatus.DECAYED)
        assert result is True

        fetched = await repo.get_by_id(cluster.id)
        assert fetched is not None
        assert fetched.status == ClusterStatus.DECAYED

    async def test_update_status_missing(self, repo: FakeClusterRepository) -> None:
        result = await repo.update_status(uuid4(), ClusterStatus.ARCHIVED)
        assert result is False

    async def test_count_active(self, repo: FakeClusterRepository) -> None:
        await repo.save(Cluster(label="A1"))
        await repo.save(Cluster(label="A2"))
        await repo.save(Cluster(label="D", status=ClusterStatus.DECAYED))
        assert await repo.count_active() == 2

    async def test_save_batch(self, repo: FakeClusterRepository) -> None:
        clusters = [Cluster(label=f"C{i}") for i in range(4)]
        count = await repo.save_batch(clusters)
        assert count == 4


# ════════════════════════════════════════════════════════════════════════════
# FakeFeedRepository
# ════════════════════════════════════════════════════════════════════════════


class TestFakeFeedRepository:
    @pytest.fixture()
    def repo(self) -> FakeFeedRepository:
        return FakeFeedRepository()

    async def test_save_and_get_by_id(self, repo: FakeFeedRepository) -> None:
        feed = Feed(url="https://rss.example.com/feed.xml", name="Test Feed")
        await repo.save(feed)

        fetched = await repo.get_by_id(feed.id)
        assert fetched is not None
        assert fetched.name == "Test Feed"

    async def test_save_and_get_by_url(self, repo: FakeFeedRepository) -> None:
        feed = Feed(url="https://rss.example.com/feed.xml")
        await repo.save(feed)

        fetched = await repo.get_by_url("https://rss.example.com/feed.xml")
        assert fetched is not None
        assert fetched.id == feed.id

    async def test_upsert_by_url(self, repo: FakeFeedRepository) -> None:
        """Re-saving a feed with the same URL should update, not duplicate."""
        feed_v1 = Feed(url="https://rss.example.com/feed.xml", name="Original")
        await repo.save(feed_v1)

        feed_v2 = Feed(url="https://rss.example.com/feed.xml", name="Updated")
        await repo.save(feed_v2)

        assert await repo.count() == 1
        fetched = await repo.get_by_url("https://rss.example.com/feed.xml")
        assert fetched is not None
        assert fetched.name == "Updated"

    async def test_get_due_for_poll(self, repo: FakeFeedRepository) -> None:
        past = datetime.now(UTC) - timedelta(minutes=5)
        future = datetime.now(UTC) + timedelta(hours=1)

        due_feed = Feed(url="https://rss.example.com/due", next_poll_at=past)
        not_due = Feed(url="https://rss.example.com/not-due", next_poll_at=future)
        paused = Feed(url="https://rss.example.com/paused", next_poll_at=past, status=FeedStatus.PAUSED)

        await repo.save(due_feed)
        await repo.save(not_due)
        await repo.save(paused)

        due = await repo.get_due_for_poll()
        assert len(due) == 1
        assert due[0].url == "https://rss.example.com/due"

    async def test_update_poll_state(self, repo: FakeFeedRepository) -> None:
        feed = Feed(url="https://rss.example.com/poll-test")
        await repo.save(feed)

        now = datetime.now(UTC)
        next_poll = now + timedelta(seconds=600)
        result = await repo.update_poll_state(
            feed.id,
            last_polled_at=now,
            next_poll_at=next_poll,
            etag='"new-etag"',
            article_count_delta=5,
        )
        assert result is True

        updated = await repo.get_by_id(feed.id)
        assert updated is not None
        assert updated.last_polled_at == now
        assert updated.etag == '"new-etag"'
        assert updated.article_count == 5
        assert updated.error_count == 0  # Reset on success

    async def test_update_error_state(self, repo: FakeFeedRepository) -> None:
        feed = Feed(url="https://rss.example.com/error-test")
        await repo.save(feed)

        next_poll = datetime.now(UTC) + timedelta(seconds=3600)
        result = await repo.update_error_state(
            feed.id,
            error_count=3,
            last_error="HTTP 503",
            status=FeedStatus.ERROR,
            next_poll_at=next_poll,
        )
        assert result is True

        updated = await repo.get_by_id(feed.id)
        assert updated is not None
        assert updated.error_count == 3
        assert updated.status == FeedStatus.ERROR

    async def test_update_missing_feed(self, repo: FakeFeedRepository) -> None:
        assert await repo.update_poll_state(
            uuid4(), last_polled_at=datetime.now(UTC), next_poll_at=datetime.now(UTC)
        ) is False

    async def test_count_and_get_all(self, repo: FakeFeedRepository) -> None:
        for i in range(3):
            await repo.save(Feed(url=f"https://rss.example.com/feed-{i}"))
        assert await repo.count() == 3
        assert len(await repo.get_all()) == 3


# ════════════════════════════════════════════════════════════════════════════
# FakeEventBus
# ════════════════════════════════════════════════════════════════════════════


class TestFakeEventBus:
    @pytest.fixture()
    def bus(self) -> FakeEventBus:
        return FakeEventBus()

    async def test_connect_disconnect(self, bus: FakeEventBus) -> None:
        await bus.connect()
        assert bus._connected is True
        await bus.disconnect()
        assert bus._connected is False

    async def test_publish_stores_events(self, bus: FakeEventBus) -> None:
        payload = FeedDiscoveredPayload(feed_url="https://rss.example.com")
        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=payload,
            source_service="test",
        )
        await bus.publish("feed.discovered", envelope)

        assert bus.event_count == 1
        assert "feed.discovered" in bus.subjects
        events = bus.get_events("feed.discovered")
        assert len(events) == 1
        assert events[0].event_type == EventType.FEED_DISCOVERED

    async def test_subscribe_and_auto_dispatch(self, bus: FakeEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(env: EventEnvelope) -> None:
            received.append(env)

        await bus.subscribe("feed.discovered", handler, queue_group="test-group")

        payload = FeedDiscoveredPayload(feed_url="https://rss.example.com")
        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=payload,
            source_service="test",
        )
        await bus.publish("feed.discovered", envelope)

        assert len(received) == 1
        assert received[0].event_id == envelope.event_id

    async def test_unsubscribe(self, bus: FakeEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(env: EventEnvelope) -> None:
            received.append(env)

        await bus.subscribe("test.subject", handler)
        await bus.unsubscribe("test.subject")

        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=FeedDiscoveredPayload(feed_url="https://rss.example.com"),
            source_service="test",
        )
        await bus.publish("test.subject", envelope)

        assert len(received) == 0  # Handler was removed
        assert bus.event_count == 1  # Event still stored

    async def test_get_events_by_type(self, bus: FakeEventBus) -> None:
        for i in range(3):
            envelope = EventEnvelope.create(
                event_type=EventType.FEED_DISCOVERED,
                payload=FeedDiscoveredPayload(feed_url=f"https://rss.example.com/{i}"),
                source_service="test",
            )
            await bus.publish("feed.discovered", envelope)

        # Publish a different type
        envelope2 = EventEnvelope.create(
            event_type=EventType.FETCH_COMPLETED,
            payload=FetchCompletedPayload(
                url="https://example.com/a", url_hash="h", status_code=200,
                content_type="text/html", html_content="<html/>",
                html_size_bytes=7, fetch_duration_ms=100, final_url="https://example.com/a",
            ),
            source_service="test",
        )
        await bus.publish("fetch.completed", envelope2)

        assert len(bus.get_events_by_type(EventType.FEED_DISCOVERED)) == 3
        assert len(bus.get_events_by_type(EventType.FETCH_COMPLETED)) == 1

    async def test_clear(self, bus: FakeEventBus) -> None:
        envelope = EventEnvelope.create(
            event_type=EventType.FEED_DISCOVERED,
            payload=FeedDiscoveredPayload(feed_url="https://rss.example.com"),
            source_service="test",
        )
        await bus.publish("feed.discovered", envelope)
        assert bus.event_count == 1

        bus.clear()
        assert bus.event_count == 0


# ════════════════════════════════════════════════════════════════════════════
# FakeCacheStore
# ════════════════════════════════════════════════════════════════════════════


class TestFakeCacheStore:
    @pytest.fixture()
    def cache(self) -> FakeCacheStore:
        return FakeCacheStore()

    async def test_lifecycle(self, cache: FakeCacheStore) -> None:
        await cache.connect()
        assert await cache.health_check() is True
        await cache.disconnect()
        assert await cache.health_check() is False

    # ── Cluster ops ───────────────────────────────────────────────────────

    async def test_cluster_set_get(self, cache: FakeCacheStore) -> None:
        data = {"label": "Breaking News", "status": "active", "article_count": 5}
        await cache.set_cluster("c1", data)
        result = await cache.get_cluster("c1")
        assert result == data

    async def test_cluster_get_missing(self, cache: FakeCacheStore) -> None:
        assert await cache.get_cluster("nonexistent") is None

    async def test_cluster_delete(self, cache: FakeCacheStore) -> None:
        await cache.set_cluster("c1", {"label": "Test", "status": "active"})
        assert await cache.delete_cluster("c1") is True
        assert await cache.get_cluster("c1") is None
        assert await cache.delete_cluster("c1") is False  # Already gone

    async def test_get_active_clusters(self, cache: FakeCacheStore) -> None:
        await cache.set_cluster("c1", {"label": "A", "status": "active", "last_activity": "2026-01-02"})
        await cache.set_cluster("c2", {"label": "B", "status": "active", "last_activity": "2026-01-01"})
        await cache.set_cluster("c3", {"label": "C", "status": "decayed"})

        active = await cache.get_active_clusters()
        assert len(active) == 2
        assert active[0]["label"] == "A"  # Most recent first

    async def test_get_all_cluster_centroids(self, cache: FakeCacheStore) -> None:
        centroid = [0.1] * 384
        await cache.set_cluster("c1", {"status": "active", "centroid": centroid, "article_count": 3})
        await cache.set_cluster("c2", {"status": "decayed", "centroid": centroid})

        centroids = await cache.get_all_cluster_centroids()
        assert len(centroids) == 1
        assert "c1" in centroids
        vec, count = centroids["c1"]
        assert len(vec) == 384
        assert count == 3

    # ── Timeline ──────────────────────────────────────────────────────────

    async def test_timeline(self, cache: FakeCacheStore) -> None:
        await cache.add_to_timeline("hash_old", 1000.0)
        await cache.add_to_timeline("hash_new", 2000.0)
        await cache.add_to_timeline("hash_mid", 1500.0)

        timeline = await cache.get_timeline(limit=10)
        assert timeline == ["hash_new", "hash_mid", "hash_old"]

    async def test_timeline_pagination(self, cache: FakeCacheStore) -> None:
        for i in range(10):
            await cache.add_to_timeline(f"h{i}", float(i))

        page = await cache.get_timeline(limit=3, offset=2)
        assert len(page) == 3

    # ── SimHash Bands ─────────────────────────────────────────────────────

    async def test_simhash_bands_match(self, cache: FakeCacheStore) -> None:
        bands_a = {0: "aa", 1: "bb", 2: "cc"}
        await cache.add_simhash_bands("article_a", bands_a, ttl_seconds=3600)

        # Check with overlapping band
        query = {0: "aa", 1: "xx", 2: "yy"}
        matches = await cache.check_simhash_bands(query)
        assert "article_a" in matches

    async def test_simhash_bands_no_match(self, cache: FakeCacheStore) -> None:
        bands_a = {0: "aa", 1: "bb"}
        await cache.add_simhash_bands("article_a", bands_a, ttl_seconds=3600)

        query = {0: "zz", 1: "yy"}
        matches = await cache.check_simhash_bands(query)
        assert len(matches) == 0

    async def test_simhash_bands_ttl_expiry(self, cache: FakeCacheStore) -> None:
        bands = {0: "aa"}
        await cache.add_simhash_bands("article_a", bands, ttl_seconds=60)

        # Before expiry — match
        matches = await cache.check_simhash_bands({0: "aa"})
        assert "article_a" in matches

        # Advance past TTL
        cache.advance_time(61)
        matches = await cache.check_simhash_bands({0: "aa"})
        assert len(matches) == 0

    # ── Embedding Cache ───────────────────────────────────────────────────

    async def test_embedding_cache_hit(self, cache: FakeCacheStore) -> None:
        emb = [0.5] * 384
        await cache.cache_embedding("art1", emb, ttl_seconds=3600)

        result = await cache.get_cached_embeddings(["art1", "art2"])
        assert "art1" in result
        assert "art2" not in result
        assert result["art1"] == emb

    async def test_embedding_cache_ttl_expiry(self, cache: FakeCacheStore) -> None:
        await cache.cache_embedding("art1", [0.1] * 384, ttl_seconds=30)

        cache.advance_time(31)
        result = await cache.get_cached_embeddings(["art1"])
        assert len(result) == 0

    # ── Dirty Tracking ────────────────────────────────────────────────────

    async def test_dirty_tracking(self, cache: FakeCacheStore) -> None:
        await cache.mark_dirty("articles", "hash1")
        await cache.mark_dirty("articles", "hash2")
        await cache.mark_dirty("articles", "hash1")  # Duplicate — set deduplicates

        batch = await cache.get_and_clear_dirty("articles", batch_size=10)
        assert len(batch) == 2
        assert set(batch) == {"hash1", "hash2"}

        # After clearing, should be empty
        batch2 = await cache.get_and_clear_dirty("articles", batch_size=10)
        assert len(batch2) == 0

    async def test_dirty_tracking_partial_pop(self, cache: FakeCacheStore) -> None:
        for i in range(5):
            await cache.mark_dirty("clusters", f"c{i}")

        batch = await cache.get_and_clear_dirty("clusters", batch_size=2)
        assert len(batch) == 2

        remaining = await cache.get_and_clear_dirty("clusters", batch_size=10)
        assert len(remaining) == 3

    async def test_dirty_empty_entity_type(self, cache: FakeCacheStore) -> None:
        batch = await cache.get_and_clear_dirty("nonexistent", batch_size=10)
        assert batch == []

    # ── Metrics ───────────────────────────────────────────────────────────

    async def test_metrics(self, cache: FakeCacheStore) -> None:
        await cache.increment_metric("articles_processed")
        await cache.increment_metric("articles_processed")
        await cache.increment_metric("articles_processed", value=5)

        assert cache.get_metric("articles_processed") == 7
        assert cache.get_metric("nonexistent") == 0

    # ── Reset ─────────────────────────────────────────────────────────────

    async def test_reset_clears_all(self, cache: FakeCacheStore) -> None:
        await cache.set_cluster("c1", {"label": "Test"})
        await cache.add_to_timeline("h1", 1000.0)
        await cache.mark_dirty("articles", "a1")
        await cache.increment_metric("count")

        cache.reset()
        assert cache.cluster_count == 0
        assert cache.timeline_count == 0
        assert cache.dirty_count == {}
        assert cache.get_metric("count") == 0


# ════════════════════════════════════════════════════════════════════════════
# FakeEmbeddingService
# ════════════════════════════════════════════════════════════════════════════


class TestFakeEmbeddingService:
    @pytest.fixture()
    def svc(self) -> FakeEmbeddingService:
        return FakeEmbeddingService()

    async def test_embed_returns_384_floats(self, svc: FakeEmbeddingService) -> None:
        vec = await svc.embed("hello world")
        assert len(vec) == 384
        assert all(isinstance(x, float) for x in vec)

    async def test_embed_is_deterministic(self, svc: FakeEmbeddingService) -> None:
        v1 = await svc.embed("reproducible text")
        v2 = await svc.embed("reproducible text")
        assert v1 == v2

    async def test_embed_different_texts_differ(self, svc: FakeEmbeddingService) -> None:
        v1 = await svc.embed("first article")
        v2 = await svc.embed("second article")
        assert v1 != v2

    async def test_embed_is_unit_normalised(self, svc: FakeEmbeddingService) -> None:
        import math
        vec = await svc.embed("test normalisation")
        magnitude = math.sqrt(sum(x * x for x in vec))
        assert abs(magnitude - 1.0) < 1e-9

    async def test_embed_batch(self, svc: FakeEmbeddingService) -> None:
        texts = ["article one", "article two", "article three"]
        results = await svc.embed_batch(texts)
        assert len(results) == 3
        assert all(len(v) == 384 for v in results)

        # Batch results should match individual calls
        svc2 = FakeEmbeddingService()
        for text, batch_vec in zip(texts, results):
            individual = await svc2.embed(text)
            assert batch_vec == individual

    async def test_call_count_tracking(self, svc: FakeEmbeddingService) -> None:
        await svc.embed("text1")
        await svc.embed_batch(["text2", "text3"])
        assert svc.call_count == 3
        assert svc.texts_seen == ["text1", "text2", "text3"]

    async def test_reset(self, svc: FakeEmbeddingService) -> None:
        await svc.embed("something")
        svc.reset()
        assert svc.call_count == 0
        assert svc.texts_seen == []

    async def test_close_is_noop(self, svc: FakeEmbeddingService) -> None:
        await svc.close()  # Should not raise
