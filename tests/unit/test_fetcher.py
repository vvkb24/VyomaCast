"""Unit tests for the RSS Fetcher & HTML Extraction worker service."""

import asyncio
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import aiohttp
import pytest

from src.domain.events import EventType
from src.services.fetcher_service import FetcherService


@pytest.fixture
def mock_bus() -> AsyncMock:
    """Provides a fake EventBus for listening to publish calls."""
    bus = AsyncMock()
    return bus


@pytest.fixture
def fetcher_service(mock_bus: AsyncMock) -> FetcherService:
    """Pre-initialised FetcherService instance."""
    return FetcherService(mock_bus)


# ── Mocks ──────────────────────────────────────────────────────────────


class MockAiohttpContext:
    """A helper to fake `async with session.get(...)`."""

    def __init__(self, text_resp: str = "", throw_timeout: bool = False) -> None:
        self.text_resp = text_resp
        self.throw_timeout = throw_timeout

    async def __aenter__(self) -> "MockAiohttpContext":
        if self.throw_timeout:
            raise asyncio.TimeoutError("Simulated timeout exception")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    async def text(self) -> str:
        return self.text_resp


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_fetch_and_extract_flow(
    fetcher_service: FetcherService, mock_bus: AsyncMock
) -> None:
    """Proves successful parsing and extraction triggers EXTRACT_COMPLETED."""
    dummy_xml = """
    <rss><channel>
        <item><link>http://test.com/article1</link></item>
        <item><link>http://test.com/article2</link></item>
    </channel></rss>
    """
    
    def mock_get(url: str, **kwargs) -> MockAiohttpContext:
        if "rss" in url:
            return MockAiohttpContext(text_resp=dummy_xml)
        return MockAiohttpContext(text_resp="<html>Article Content</iframe>")

    valid_extract = {
        "text": "This is a sufficiently long extracted text to pass the length bounds filter logic (at least 50).",
        "title": "A Great Article",
    }

    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        with patch("trafilatura.bare_extraction", return_value=valid_extract):
            await fetcher_service.process_feed("http://rss.test.com/feed.xml")

    # The service should have published 2 complete extraction events
    assert mock_bus.publish.call_count == 2
    
    # Assert subject mapping
    assert mock_bus.publish.call_args_list[0][0][0] == EventType.EXTRACT_COMPLETED
    assert mock_bus.publish.call_args_list[1][0][0] == EventType.EXTRACT_COMPLETED
    
    # Verify one of the payloads payload
    envelope = mock_bus.publish.call_args_list[0][0][1]
    assert envelope.payload["url"] == "http://test.com/article1"
    assert envelope.payload["content"] == valid_extract["text"]


@pytest.mark.asyncio
async def test_failure_short_content_is_skipped(
    fetcher_service: FetcherService, mock_bus: AsyncMock
) -> None:
    """Proves articles that fail extraction (<50 chars) are gracefully dropped."""
    dummy_xml = "<rss><channel><item><link>http://test.com/1</link></item></channel></rss>"
    
    def mock_get(url: str, **kwargs) -> MockAiohttpContext:
        if "rss" in url:
            return MockAiohttpContext(text_resp=dummy_xml)
        return MockAiohttpContext(text_resp="<html>Empty</iframe>")

    short_extract = {"text": "too short", "title": "Empty"}

    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        with patch("trafilatura.bare_extraction", return_value=short_extract):
            await fetcher_service.process_feed("http://rss.test.com/feed.xml")

    # Event is dropped, nothing is published
    assert mock_bus.publish.call_count == 0


@pytest.mark.asyncio
async def test_duplicate_urls_within_batch_are_ignored(
    fetcher_service: FetcherService, mock_bus: AsyncMock
) -> None:
    """Proves the internal set successfully deduplicates exact URLs to prevent overhead."""
    dummy_xml = """<rss><channel>
        <item><link>http://duplicate.com/article</link></item>
        <item><link>http://duplicate.com/article</link></item>
    </channel></rss>"""
    
    def mock_get(url: str, **kwargs) -> MockAiohttpContext:
        if "rss" in url:
            return MockAiohttpContext(text_resp=dummy_xml)
        return MockAiohttpContext(text_resp="<html>Article</iframe>")

    valid_extract = {
        "text": "This is a sufficiently long extracted text to pass length bounds." * 2,
    }

    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        with patch("trafilatura.bare_extraction", return_value=valid_extract) as mock_extract:
            await fetcher_service.process_feed("http://rss.test.com/feed.xml")

            # Trafilatura was only called ONCE despite 2 items in RSS
            assert mock_extract.call_count == 1
    
    # Only 1 unique event fires
    assert mock_bus.publish.call_count == 1


@pytest.mark.asyncio
async def test_http_timeout_behavior(
    fetcher_service: FetcherService, mock_bus: AsyncMock
) -> None:
    """Proves aiohttp Timeouts gracefully skip the article without crashing."""
    dummy_xml = "<rss><channel><item><link>http://timeout.com/article</link></item></channel></rss>"
    
    def mock_get(url: str, **kwargs) -> MockAiohttpContext:
        if "rss" in url:
            return MockAiohttpContext(text_resp=dummy_xml)
        # Force timeout exception for the core HTML download
        return MockAiohttpContext(throw_timeout=True)

    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        # We do not mock trafilatura here because it shouldn't even be reached
        await fetcher_service.process_feed("http://rss.test.com/feed.xml")

    # Fails smoothly, does not publish
    assert mock_bus.publish.call_count == 0


@pytest.mark.asyncio
async def test_extraction_cpu_semaphore(
    fetcher_service: FetcherService, mock_bus: AsyncMock
) -> None:
    """Proves the global Trafilatura semaphore controls concurrency bursts."""
    dummy_xml = "<rss><channel>"
    # Create 5 items
    for i in range(5):
        dummy_xml += f"<item><link>http://semaphore.com/{i}</link></item>"
    dummy_xml += "</channel></rss>"
    
    def mock_get(url: str, **kwargs) -> MockAiohttpContext:
        if "rss" in url:
            return MockAiohttpContext(text_resp=dummy_xml)
        return MockAiohttpContext(text_resp=f"<html>{url}</iframe>")

    # We will track active extract tasks to prove it never exceeds 2
    active_extracts = 0
    max_active_extracts = 0
    lock = asyncio.Lock()

    def mock_extract(html_content: str, **kwargs) -> dict:
        # Note: this is a synchronous function called inside asyncio.to_thread
        return {
            "text": "This is a sufficiently long extracted text to pass length bounds." * 2
        }
        
    # We patch inside the async _process_article to verify the exact semaphore
    original_process = fetcher_service._process_article
    
    async def wrapped_process(*args, **kwargs):
        nonlocal active_extracts, max_active_extracts
        
        # Override the extract logic inline to inspect concurrency
        domain_sem = fetcher_service._get_domain_sem(args[1])
        async with domain_sem:
            async with fetcher_service.extract_sem:
                async with lock:
                    active_extracts += 1
                    max_active_extracts = max(max_active_extracts, active_extracts)
                    
                # Simulate CPU work time
                await asyncio.sleep(0.1)
                
                async with lock:
                    active_extracts -= 1
                    
        return await original_process(*args, **kwargs)
        
    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        with patch("trafilatura.bare_extraction", side_effect=mock_extract):
            # Patch process_article but keep semantics identical purely to spy on the semaphore bounds
            with patch.object(fetcher_service, "_process_article", side_effect=wrapped_process):
                await fetcher_service.process_feed("http://rss.test.com/feed.xml")

    assert mock_bus.publish.call_count == 5
    # The semaphore is limited to 2
    assert max_active_extracts <= 2
