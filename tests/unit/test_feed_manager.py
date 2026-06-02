"""Unit tests for FeedManager — polling scheduler and ingestion heartbeat.

Tests verify:
    1. Successful feed emits correct number of FEED_ITEMS_NEW events.
    2. HTTP 304 Not Modified skips parsing and emits 0 events.
    3. Duplicate items (by GUID or link) are NOT emitted again.
    4. HTTP failures trigger exponential backoff state update.
    5. Multiple feeds process concurrently without blocking.
    6. URL normalization strips tracking parameters correctly.
    7. SeenGuidCache respects max size bounds.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.domain.events import EventType, FeedItemsNewPayload
from src.domain.models import Feed, FeedStatus
from src.infrastructure.http.aiohttp_fetcher import FeedFetchResult
from src.services.feed_manager import (
    FeedManager,
    SeenGuidCache,
    compute_item_key,
    normalize_url,
)
from tests.fakes.fake_event_bus import FakeEventBus
from tests.fakes.fake_repository import FakeFeedRepository


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def feed_repo():
    return FakeFeedRepository()


@pytest.fixture
def event_bus():
    return FakeEventBus()


@pytest.fixture
def http_fetcher():
    """Mock AioHttpFetcher with explicit fetch_feed_xml method."""
    mock = AsyncMock()
    mock.close = AsyncMock()
    mock.fetch_feed_xml = AsyncMock(return_value=FeedFetchResult(
        status=200,
        body=SAMPLE_RSS_SINGLE,
        etag='"default-etag"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
    ))
    return mock


def make_feed(*, url="https://example.com/feed.xml", poll_interval=600, error_count=0, **kwargs):
    """Factory for creating test Feed objects."""
    return Feed(
        id=uuid4(),
        url=url,
        name=kwargs.get("name", "Test Feed"),
        poll_interval=poll_interval,
        next_poll_at=datetime.now(UTC) - timedelta(minutes=5),  # overdue
        status=FeedStatus.ACTIVE,
        error_count=error_count,
        etag=kwargs.get("etag"),
        last_modified=kwargs.get("last_modified"),
    )


SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Article One</title>
    <link>https://example.com/article-1</link>
    <guid>guid-001</guid>
  </item>
  <item>
    <title>Article Two</title>
    <link>https://example.com/article-2</link>
    <guid>guid-002</guid>
  </item>
  <item>
    <title>Article Three</title>
    <link>https://example.com/article-3</link>
    <guid>guid-003</guid>
  </item>
</channel>
</rss>"""


SAMPLE_RSS_SINGLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Solo Article</title>
    <link>https://example.com/solo</link>
    <guid>guid-solo</guid>
  </item>
</channel>
</rss>"""


# ── URL Normalization Tests ─────────────────────────────────────────────────


class TestNormalizeUrl:
    def test_strips_utm_params(self):
        url = "https://example.com/article?utm_source=twitter&utm_medium=social&id=123"
        result = normalize_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "id=123" in result

    def test_strips_fragment(self):
        url = "https://example.com/article#section-2"
        result = normalize_url(url)
        assert "#" not in result

    def test_lowercases_scheme_and_host(self):
        url = "HTTPS://Example.COM/Article"
        result = normalize_url(url)
        assert result.startswith("https://example.com")

    def test_removes_trailing_slash(self):
        url = "https://example.com/article/"
        result = normalize_url(url)
        assert result.endswith("/article")

    def test_preserves_root_path(self):
        url = "https://example.com/"
        result = normalize_url(url)
        assert result.endswith("/")


# ── Item Key Computation ────────────────────────────────────────────────────


class TestComputeItemKey:
    def test_uses_guid_when_present(self):
        entry = {"id": "unique-guid-123", "link": "https://example.com"}
        key = compute_item_key(entry)
        assert key == "guid:unique-guid-123"

    def test_falls_back_to_link(self):
        entry = {"link": "https://example.com/article?utm_source=rss"}
        key = compute_item_key(entry)
        assert key is not None
        assert key.startswith("link:")
        assert "utm_source" not in key

    def test_returns_none_for_empty_entry(self):
        key = compute_item_key({})
        assert key is None


# ── SeenGuidCache Tests ─────────────────────────────────────────────────────


class TestSeenGuidCache:
    def test_add_and_contains(self):
        cache = SeenGuidCache(maxsize=10)
        cache.add("key1")
        assert cache.contains("key1")
        assert not cache.contains("key2")

    def test_evicts_oldest_when_full(self):
        cache = SeenGuidCache(maxsize=3)
        cache.add("a")
        cache.add("b")
        cache.add("c")
        cache.add("d")  # Should evict "a"
        assert not cache.contains("a")
        assert cache.contains("b")
        assert cache.contains("d")
        assert cache.size == 3

    def test_access_refreshes_order(self):
        cache = SeenGuidCache(maxsize=3)
        cache.add("a")
        cache.add("b")
        cache.add("c")
        cache.contains("a")  # Refresh "a"
        cache.add("d")  # Should evict "b" (now oldest)
        assert cache.contains("a")
        assert not cache.contains("b")


# ── FeedManager Tests ───────────────────────────────────────────────────────


class TestFeedManagerSuccessPath:
    """Test 1: Successful feed emits correct number of events."""

    @pytest.mark.asyncio
    async def test_successful_poll_emits_events(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed()
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.return_value = FeedFetchResult(
            status=200,
            body=SAMPLE_RSS_XML,
            etag='"abc123"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        )

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        # 3 items in the RSS = 3 events
        events = event_bus.get_events_by_type(EventType.FEED_ITEMS_NEW)
        assert len(events) == 3

        # Verify payload structure
        for env in events:
            payload = env.parse_payload(FeedItemsNewPayload)
            assert payload.feed_id == feed.id
            assert payload.feed_url == feed.url
            assert payload.item_url.startswith("https://")

    @pytest.mark.asyncio
    async def test_poll_state_updated_on_success(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed()
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.return_value = FeedFetchResult(
            status=200, body=SAMPLE_RSS_SINGLE,
        )

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        updated_feed = await feed_repo.get_by_id(feed.id)
        assert updated_feed is not None
        assert updated_feed.last_polled_at is not None
        assert updated_feed.error_count == 0
        assert updated_feed.article_count == 1


class TestFeedManager304:
    """Test 2: HTTP 304 skips parsing and emits 0 events."""

    @pytest.mark.asyncio
    async def test_304_skips_parsing(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed(etag='"existing-etag"')
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.return_value = FeedFetchResult(
            status=304, etag='"existing-etag"',
        )

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        # Zero events emitted
        assert event_bus.event_count == 0

        # But poll state still updated
        updated_feed = await feed_repo.get_by_id(feed.id)
        assert updated_feed is not None
        assert updated_feed.last_polled_at is not None


class TestFeedManagerDedup:
    """Test 3: Duplicate items are NOT emitted again."""

    @pytest.mark.asyncio
    async def test_duplicate_guids_skipped(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed()
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.return_value = FeedFetchResult(
            status=200, body=SAMPLE_RSS_XML,
        )

        manager = FeedManager(feed_repo, event_bus, http_fetcher)

        # First poll: 3 items emitted
        await manager._process_feed(feed)
        assert event_bus.event_count == 3

        # Second poll with same content: 0 new items (all cached)
        event_bus.clear()
        await manager._process_feed(feed)
        assert event_bus.event_count == 0


class TestFeedManagerFailure:
    """Test 4: HTTP failures trigger exponential backoff."""

    @pytest.mark.asyncio
    async def test_failure_increments_error_count(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed()
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.side_effect = Exception("HTTP 500")

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        # No events
        assert event_bus.event_count == 0

        # Error state updated
        updated_feed = await feed_repo.get_by_id(feed.id)
        assert updated_feed is not None
        assert updated_feed.error_count == 1
        assert updated_feed.last_error is not None

    @pytest.mark.asyncio
    async def test_repeated_failures_apply_backoff(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed(error_count=3)
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.side_effect = Exception("Timeout")

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        updated_feed = await feed_repo.get_by_id(feed.id)
        assert updated_feed is not None
        assert updated_feed.error_count == 4

        # next_poll_at should be in the future with exponential delay
        # delay = 600 * (2^4) = 9600 seconds
        expected_min_delay = timedelta(seconds=600 * (2 ** 4) - 10)
        assert updated_feed.next_poll_at > datetime.now(UTC) + expected_min_delay

    @pytest.mark.asyncio
    async def test_high_error_count_transitions_to_error_status(self, feed_repo, event_bus, http_fetcher):
        feed = make_feed(error_count=4)  # Next failure = 5 → ERROR
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.side_effect = Exception("DNS Resolution Failed")

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        updated_feed = await feed_repo.get_by_id(feed.id)
        assert updated_feed is not None
        assert updated_feed.status == FeedStatus.ERROR


class TestFeedManagerConcurrency:
    """Test 5: Multiple feeds process concurrently without blocking."""

    @pytest.mark.asyncio
    async def test_concurrent_feeds(self, feed_repo, event_bus, http_fetcher):
        feeds = [make_feed(url=f"https://feed-{i}.com/rss") for i in range(5)]
        for f in feeds:
            await feed_repo.save(f)

        manager = FeedManager(feed_repo, event_bus, http_fetcher, max_concurrency=5)

        async def dynamic_fetch(url, etag=None, last_modified=None):
            idx = url.split("feed-")[1].split(".")[0] if "feed-" in url else "0"
            rss = f"""<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>Art {idx}</title><link>https://feed-{idx}.com/a</link>
            <guid>unique-guid-feed-{idx}</guid></item></channel></rss>"""
            return FeedFetchResult(status=200, body=rss)

        http_fetcher.fetch_feed_xml.side_effect = dynamic_fetch

        await manager._poll_cycle()

        # 5 feeds × 1 unique item each = 5 events
        assert event_bus.event_count == 5

    @pytest.mark.asyncio
    async def test_one_feed_failure_does_not_block_others(self, feed_repo, event_bus, http_fetcher):
        good_feed = make_feed(url="https://good.com/rss")
        bad_feed = make_feed(url="https://bad.com/rss")
        await feed_repo.save(good_feed)
        await feed_repo.save(bad_feed)

        manager = FeedManager(feed_repo, event_bus, http_fetcher, max_concurrency=5)

        async def dynamic_fetch(url, etag=None, last_modified=None):
            if "bad.com" in url:
                raise Exception("Bad Server")
            return FeedFetchResult(status=200, body=SAMPLE_RSS_SINGLE)

        http_fetcher.fetch_feed_xml.side_effect = dynamic_fetch

        await manager._poll_cycle()

        # Good feed should have emitted 1 event despite bad feed failing
        assert event_bus.event_count >= 1

        # Bad feed should have error state
        updated_bad = await feed_repo.get_by_id(bad_feed.id)
        assert updated_bad is not None
        assert updated_bad.error_count == 1


class TestFeedManagerBounding:
    """Verify feed item bounding to MAX_ITEMS_PER_FEED."""

    @pytest.mark.asyncio
    async def test_limits_to_max_items(self, feed_repo, event_bus, http_fetcher):
        # Generate RSS with 25 items (> MAX_ITEMS_PER_FEED of 20)
        items = "\n".join([
            f"""<item>
                <title>Item {i}</title>
                <link>https://example.com/item-{i}</link>
                <guid>guid-{i}</guid>
            </item>"""
            for i in range(25)
        ])
        large_rss = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Big Feed</title>{items}</channel></rss>"""

        feed = make_feed()
        await feed_repo.save(feed)

        http_fetcher.fetch_feed_xml.return_value = FeedFetchResult(
            status=200, body=large_rss,
        )

        manager = FeedManager(feed_repo, event_bus, http_fetcher)
        await manager._process_feed(feed)

        # Should cap at 20 items
        assert event_bus.event_count == 20
