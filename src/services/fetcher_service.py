"""RSS Fetching and HTML Extraction service."""

import asyncio
import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import DefaultDict, Optional
from urllib.parse import urlparse
from uuid import UUID

import aiohttp
import feedparser
import trafilatura

from src.domain.events import (
    EventEnvelope,
    EventType,
    ExtractCompletedPayload,
)
from src.domain.interfaces import EventBus

logger = logging.getLogger(__name__)


class FetcherService:
    """Service to fetch RSS, parse feeds, fetch HTML, and extract content via Trafilatura.
    
    Includes constraints:
    - 2 concurrent requests max per domain
    - global extraction CPU semaphore
    - 10-second strict HTTP timeout
    - Limits to 20 articles max per feed
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.extract_sem = asyncio.Semaphore(2)  # Global constraint to prevent CPU starvation
        self.domain_sems: DefaultDict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(2)  # Prevent hammering sources
        )

    def _get_domain_sem(self, url: str) -> asyncio.Semaphore:
        domain = urlparse(url).netloc or "unknown"
        return self.domain_sems[domain]

    def _hash_url(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    async def process_article(self, url: str, feed_id: Optional[UUID] = None) -> None:
        """Fetch HTML, extract text, and publish completion for a single article URL."""
        timeout = aiohttp.ClientTimeout(total=10.0)
        headers = {
            "User-Agent": "VyomaCastBot/1.0 (+https://github.com/vyomacast/vyomacast)"
        }
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers, connector=connector
        ) as session:
            url_hash = self._hash_url(url)
            await self._process_article(session, url, url_hash, feed_id)

    async def process_feed(self, feed_url: str, feed_id: Optional[UUID] = None) -> None:
        """Fetch RSS XML, parse links, and spawn extraction for top N items."""
        timeout = aiohttp.ClientTimeout(total=10.0)

        # Standardise realistic headers for better success rates
        headers = {
            "User-Agent": "VyomaCastBot/1.0 (+https://github.com/vyomacast/vyomacast)"
        }

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers, connector=connector
        ) as session:
            try:
                # 1. Fetch RSS XML
                async with session.get(feed_url) as resp:
                    resp.raise_for_status()
                    xml_content = await resp.text()
            except Exception as e:
                logger.warning("Failed to fetch RSS from %s: %s", feed_url, e)
                return

            # 2. Parse RSS in thread pool (feedparser is fully synchronous and blocking)
            parsed = await asyncio.to_thread(feedparser.parse, xml_content)

            # Warning if RSS structure is malformed but parsing succeeded partially
            if getattr(parsed, "bozo", False) and getattr(parsed.bozo_exception, "getMessage", lambda: "")() != "character encoding mismatch":
                logger.debug("Feedparser reported malformed XML for %s: %s", feed_url, parsed.bozo_exception)

            # 3. Limit processing batch to first 20 items (prevention of historical dumps)
            entries = parsed.entries[:20]

            processed_hashes = set()
            tasks = []

            for entry in entries:
                link = entry.get("link")
                if not link:
                    continue

                # Idempotency lock within the batch
                url_hash = self._hash_url(link)
                if url_hash in processed_hashes:
                    continue
                processed_hashes.add(url_hash)

                tasks.append(
                    asyncio.create_task(
                        self._process_article(session, link, url_hash, feed_id)
                    )
                )

            # 4. Wait for all article extractions to finish
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_article(
        self,
        session: aiohttp.ClientSession,
        url: str,
        url_hash: str,
        feed_id: Optional[UUID],
    ) -> None:
        """Fetch HTML, extract via Trafilatura, and publish completion."""
        domain_sem = self._get_domain_sem(url)

        # Ensure we don't bombard exactly one domain with too many sockets
        async with domain_sem:
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    # Ensure limit on maximum read payload length natively in aiohttp if possible
                    # but simple text() is fine for MVP
                    html_content = await resp.text()
            except asyncio.TimeoutError:
                logger.warning("Timeout fetching HTML for %s", url)
                return
            except Exception as e:
                logger.warning("Failed to fetch HTML for %s: %s", url, e)
                return

        # Explicitly control concurrent Trafilatura NLP logic to prevent thread-thrashing
        async with self.extract_sem:
            try:
                extracted = await asyncio.to_thread(
                    trafilatura.bare_extraction,
                    html_content,
                    url=url,
                    include_links=False,       # Skip links parsing, faster
                    include_comments=False,    # Do not extract comments
                    include_images=True,
                )
            except Exception as e:
                logger.warning("Trafilatura failed extraction for %s: %s", url, e)
                return

        # 3. Quality threshold + Failure Drop
        if not extracted or not extracted.get("text") or len(extracted["text"].strip()) < 50:
            logger.info("Extraction content dropped (empty or <50 chars) for %s", url)
            return

        content = extracted["text"].strip()

        # Try mapping publication date
        pub_date: Optional[datetime] = None
        if extracted.get("date"):
            try:
                pub_date = datetime.strptime(extracted["date"], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                pass

        # Try mapping authors
        authors = []
        if extracted.get("author"):
            authors = [a.strip() for a in extracted["author"].split(";") if a.strip()]

        # 4. Success — publish EXTRACT_COMPLETED to be picked up by Dedup Engine
        title = extracted.get("title")
        if not title or not title.strip():
            import re
            import html as html_lib
            og_match = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                html_content,
            ) or re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                html_content,
            )
            if og_match:
                title = html_lib.unescape(og_match.group(1).strip())
            else:
                title_match = re.search(
                    r'<title[^>]*>(.*?)</title>',
                    html_content,
                    re.IGNORECASE | re.DOTALL,
                )
                if title_match:
                    title = html_lib.unescape(title_match.group(1).strip())

        payload = ExtractCompletedPayload(
            url=url,
            url_hash=url_hash,
            feed_id=feed_id,
            title=title or "Unknown Title",
            content=content,
            content_length=len(content),
            authors=authors,
            published_at=pub_date,
            top_image_url=extracted.get("image"),
            quality_score=1.0,  # Single-method implies 1.0 confidence on success for MVP
            extraction_method="trafilatura",
        )

        envelope = EventEnvelope.create(
            event_type=EventType.EXTRACT_COMPLETED,
            payload=payload,
            source_service="fetcher_worker",
        )

        try:
            await self.bus.publish(EventType.EXTRACT_COMPLETED, envelope)
        except Exception as e:
            logger.error("Failed to publish EXTRACT_COMPLETED for %s: %s", url_hash, e)
