"""Aiohttp-based implementation of the HttpFetcher interface.

Centralizes ALL HTTP concerns:
    - Article HTML fetching (domain interface: ``fetch``)
    - RSS XML fetching with conditional GETs (``fetch_feed_xml``)

No other service should use aiohttp directly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

import aiohttp

from src.domain.interfaces import HttpFetcher
from src.domain.models import FetchResult


# ── Structured response for RSS feed fetching ───────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    """Typed, immutable result from an RSS feed fetch.

    This is an infrastructure-level value object — NOT a domain model.
    It exists solely to give ``fetch_feed_xml`` a proper return type
    instead of a raw dict.
    """

    status: int
    body: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def was_modified(self) -> bool:
        """True when the server returned new content (i.e. not 304)."""
        return self.status != 304


# ── HttpFetcher implementation ──────────────────────────────────────────────


class AioHttpFetcher(HttpFetcher):
    """Production HttpFetcher backed by aiohttp.

    Provides two explicit public methods:

    ``fetch(url, *, feed_id)``
        Domain-interface method for heavy article HTML fetching.

    ``fetch_feed_xml(url, etag, last_modified)``
        Infrastructure-level method for lightweight RSS XML polling
        with conditional GET support (If-None-Match / If-Modified-Since).

    Both methods share the same managed ``ClientSession`` for connection
    pooling and consistent timeout / User-Agent behaviour.
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy-initialize the client session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15.0)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "VyomaCastBot/1.0 (+https://github.com/vyomacast/vyomacast)"
                },
            )
        return self._session

    # ── Domain interface: article HTML fetching ──────────────────────────

    async def fetch(self, url: str, *, feed_id: Optional[UUID] = None) -> FetchResult:
        """Fetch article HTML.  Implements ``HttpFetcher.fetch``."""
        start = datetime.now(UTC)
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                success = resp.status < 400
                html_content = ""

                if success:
                    html_content = await resp.text()

                duration = (datetime.now(UTC) - start).total_seconds() * 1000.0

                return FetchResult(
                    url=url,
                    url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    feed_id=feed_id,
                    success=success,
                    status_code=resp.status,
                    content_type=resp.headers.get("Content-Type"),
                    html_content=html_content if html_content else None,
                    html_size_bytes=len(html_content) if html_content else 0,
                    fetch_duration_ms=duration,
                    final_url=str(resp.url),
                )
        except Exception as e:
            return FetchResult(
                url=url,
                url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                feed_id=feed_id,
                success=False,
                error_type=type(e).__name__,
                error_message=str(e),
            )

    # ── Infrastructure extension: RSS XML with conditional GETs ──────────

    async def fetch_feed_xml(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FeedFetchResult:
        """Fetch RSS/Atom XML with conditional GET headers.

        Sends ``If-None-Match`` and/or ``If-Modified-Since`` when the
        caller provides cached values.  Returns a typed ``FeedFetchResult``.

        Args:
            url: The feed URL to poll.
            etag: Previously cached ETag value (or ``None``).
            last_modified: Previously cached Last-Modified value (or ``None``).

        Returns:
            A ``FeedFetchResult``.  Check ``result.was_modified`` before
            accessing ``result.body``.

        Raises:
            aiohttp.ClientResponseError: On 4xx/5xx responses (except 304).
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        session = await self._get_session()

        async with session.get(url, headers=headers) as resp:
            if resp.status == 304:
                return FeedFetchResult(
                    status=304,
                    etag=etag,
                    last_modified=last_modified,
                )

            resp.raise_for_status()
            body = await resp.text()

            return FeedFetchResult(
                status=resp.status,
                body=body,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release connection pool.  Implements ``HttpFetcher.close``."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
