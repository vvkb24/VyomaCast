"""Feed Manager — Continuous polling scheduler and ingestion heartbeat.

Responsibilities:
    1. Continuous scheduler loop (configurable interval, default 60s).
    2. Fetch due feeds via FeedRepository.get_due_for_poll().
    3. HTTP fetch with conditional GET (If-None-Match / If-Modified-Since).
    4. Parse RSS via feedparser in thread pool.
    5. Deduplicate items per-cycle via GUID/link normalization.
    6. Emit EventType.FEED_ITEMS_NEW for each new item.
    7. Exponential backoff on failure (capped at 24 hours).
    8. Graceful shutdown via asyncio cancellation.

Architectural Constraints:
    - ZERO modifications to src/domain/interfaces.py.
    - Depends on the ``FeedXmlFetcher`` Protocol (structural abstraction),
      NOT on any concrete HttpFetcher implementation.
    - Uses existing EventEnvelope / EventType for strict event typing.
"""

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from time import mktime
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import feedparser

from src.config import settings
from src.domain.events import (
    EventEnvelope,
    EventType,
    FeedItemsNewPayload,
)
from src.domain.interfaces import EventBus, FeedRepository
from src.domain.models import Feed, FeedStatus
from src.infrastructure.http.aiohttp_fetcher import FeedFetchResult

logger = logging.getLogger(__name__)

# Maximum backoff delay: 24 hours
MAX_BACKOFF_SECONDS = 86_400

# Items per feed cap
MAX_ITEMS_PER_FEED = 20

# Concurrent feed processing limit
DEFAULT_FEED_CONCURRENCY = 10

# LRU cache size for cross-cycle deduplication
SEEN_GUID_CACHE_MAX = 50_000

# UTM tracking parameter pattern
_UTM_PATTERN = re.compile(r"^utm_", re.IGNORECASE)


class SeenGuidCache:
    """Bounded LRU cache for tracking processed article GUIDs across cycles.

    Uses OrderedDict for O(1) insertion and eviction with bounded memory.
    No interface mutations needed — this is a pure in-memory utility.
    """

    def __init__(self, maxsize: int = SEEN_GUID_CACHE_MAX) -> None:
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._maxsize = maxsize

    def contains(self, key: str) -> bool:
        if key in self._cache:
            self._cache.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return
        self._cache[key] = True
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    @property
    def size(self) -> int:
        return len(self._cache)


def normalize_url(url: str) -> str:
    """Normalize a URL by stripping tracking params (utm_*) and fragments."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if not _UTM_PATTERN.match(k)}
    clean_query = urlencode(cleaned, doseq=True) if cleaned else ""
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.params,
        clean_query,
        "",  # discard fragment
    ))


def compute_item_key(entry: dict) -> Optional[str]:
    """Derive a dedup key from an RSS entry using GUID or normalized link."""
    guid = entry.get("id") or entry.get("guid")
    if guid and guid.strip():
        return f"guid:{guid.strip()}"
    link = entry.get("link")
    if link and link.strip():
        return f"link:{normalize_url(link.strip())}"
    return None


# ── Service-level Protocol ──────────────────────────────────────────────────
# Defines exactly what FeedManager needs from its HTTP layer.
# AioHttpFetcher satisfies this structurally — no inheritance required.
# This is NOT duck typing: it is a formal, type-checker-enforced contract.


@runtime_checkable
class FeedXmlFetcher(Protocol):
    """Protocol declaring the HTTP capabilities FeedManager requires.

    Any class that implements ``fetch_feed_xml`` and ``close`` with these
    signatures satisfies the contract at both runtime and static-analysis
    time.  ``AioHttpFetcher`` is the production implementation.
    """

    async def fetch_feed_xml(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FeedFetchResult: ...

    async def close(self) -> None: ...


class FeedManager:
    """Continuous feed polling scheduler.

    Injects:
        - FeedRepository: for querying due feeds and updating poll state.
        - EventBus: for publishing FEED_ITEMS_NEW events.
        - FeedXmlFetcher: Protocol abstraction for HTTP feed fetching.

    Does NOT modify domain interfaces.
    """

    def __init__(
        self,
        feed_repo: FeedRepository,
        event_bus: EventBus,
        http_fetcher: FeedXmlFetcher,
        *,
        poll_interval: int = 60,
        max_concurrency: int = DEFAULT_FEED_CONCURRENCY,
    ) -> None:
        self._feed_repo = feed_repo
        self._event_bus = event_bus
        self._http_fetcher = http_fetcher
        self._poll_interval = poll_interval
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._seen_guids = SeenGuidCache()
        self._running = False

    async def run(self) -> None:
        """Main scheduler loop. Runs until cancelled."""
        self._running = True
        logger.info(
            "FeedManager started (interval=%ds, concurrency=%d)",
            self._poll_interval,
            self._max_concurrency,
        )

        try:
            while self._running:
                try:
                    await self._poll_cycle()
                except Exception as e:
                    logger.error("Poll cycle failed unexpectedly: %s", e)

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("FeedManager received shutdown signal")
            self._running = False

    async def stop(self) -> None:
        """Signal graceful shutdown."""
        self._running = False

    async def _poll_cycle(self) -> None:
        """Single poll cycle: fetch due feeds and process them concurrently."""
        due_feeds = await self._feed_repo.get_due_for_poll(limit=50)

        if not due_feeds:
            logger.debug("No feeds due for polling")
            return

        logger.info("Poll cycle: %d feeds due", len(due_feeds))

        tasks = [
            asyncio.create_task(self._bounded_process(feed))
            for feed in due_feeds
        ]

        # Gather with return_exceptions to prevent one feed from crashing others
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Feed processing task failed for %s: %s",
                    due_feeds[i].url,
                    result,
                )

    async def _bounded_process(self, feed: Feed) -> None:
        """Process a single feed behind the concurrency semaphore."""
        async with self._semaphore:
            await self._process_feed(feed)

    async def _process_feed(self, feed: Feed) -> None:
        """Fetch, parse, dedup, and emit events for a single feed."""
        now = datetime.now(UTC)

        try:
            # Step 1: HTTP fetch with conditional GET headers
            xml_content, etag, last_modified, was_modified = await self._fetch_feed_xml(feed)

            if not was_modified:
                # HTTP 304 — skip parsing, just update next_poll_at
                next_poll = now + timedelta(seconds=feed.poll_interval)
                await self._feed_repo.update_poll_state(
                    feed.id,
                    last_polled_at=now,
                    next_poll_at=next_poll,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                )
                logger.debug("Feed %s returned 304 Not Modified", feed.url)
                return

            # Step 2: Parse RSS in thread pool (feedparser is synchronous/blocking)
            parsed = await asyncio.to_thread(feedparser.parse, xml_content)

            # Step 3: Bound to top N items
            entries = parsed.entries[:MAX_ITEMS_PER_FEED]

            # Step 4: Dedup and emit
            new_count = 0
            for entry in entries:
                item_key = compute_item_key(entry)
                if item_key is None:
                    continue

                # Check LRU cache for cross-cycle dedup
                if self._seen_guids.contains(item_key):
                    continue

                link = entry.get("link")
                if not link or not link.strip():
                    continue

                # Mark as seen
                self._seen_guids.add(item_key)

                # Parse publication date
                pub_date = None
                if entry.get("published_parsed"):
                    try:
                        pub_date = datetime.fromtimestamp(
                            mktime(entry.published_parsed), tz=UTC
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass

                # Emit FEED_ITEMS_NEW event
                payload = FeedItemsNewPayload(
                    feed_id=feed.id,
                    feed_url=feed.url,
                    item_url=link.strip(),
                    item_guid=entry.get("id") or entry.get("guid"),
                    item_title=entry.get("title"),
                    item_published=pub_date,
                )

                envelope = EventEnvelope.create(
                    event_type=EventType.FEED_ITEMS_NEW,
                    payload=payload,
                    source_service="feed_manager",
                )

                await self._event_bus.publish(EventType.FEED_ITEMS_NEW, envelope)
                new_count += 1

            # Step 5: Success — update poll state
            next_poll = now + timedelta(seconds=feed.poll_interval)
            await self._feed_repo.update_poll_state(
                feed.id,
                last_polled_at=now,
                next_poll_at=next_poll,
                etag=etag,
                last_modified=last_modified,
                article_count_delta=new_count,
            )

            logger.info(
                "Feed %s (%s): %d new items emitted",
                feed.id,
                feed.url,
                new_count,
            )

        except Exception as e:
            # Failure — exponential backoff
            new_error_count = feed.error_count + 1
            backoff_seconds = min(
                feed.poll_interval * (2 ** new_error_count),
                MAX_BACKOFF_SECONDS,
            )
            next_poll = now + timedelta(seconds=backoff_seconds)

            # Transition to ERROR status if error count exceeds threshold
            new_status = FeedStatus.ERROR if new_error_count >= 5 else feed.status

            await self._feed_repo.update_error_state(
                feed.id,
                error_count=new_error_count,
                last_error=f"{type(e).__name__}: {e}",
                status=new_status,
                next_poll_at=next_poll,
            )

            logger.warning(
                "Feed %s failed (error_count=%d, backoff=%ds): %s",
                feed.url,
                new_error_count,
                backoff_seconds,
                e,
            )

    async def _fetch_feed_xml(
        self, feed: Feed
    ) -> tuple[str, Optional[str], Optional[str], bool]:
        """Fetch RSS XML via HttpFetcher with conditional GET support.

        Returns:
            (xml_content, etag, last_modified, was_modified)
            If 304: xml_content is empty, was_modified is False.
        """
        result = await self._http_fetcher.fetch_feed_xml(
            url=feed.url,
            etag=feed.etag,
            last_modified=feed.last_modified,
        )

        if not result.was_modified:
            return "", feed.etag, feed.last_modified, False

        return result.body, result.etag, result.last_modified, True
