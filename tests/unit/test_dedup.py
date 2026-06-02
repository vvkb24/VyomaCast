"""Unit tests for the Two-Stage Deduplication Engine."""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4

import pytest
import numpy as np

from src.domain.events import EventType, ExtractCompletedPayload
from src.services.dedup_service import DedupService
from src.domain.models import Article, SearchResult


@pytest.fixture
def mocks():
    cache = AsyncMock()
    repo = AsyncMock()
    bus = AsyncMock()
    # Provide a simple valid mock embedding mechanism so tests won't fail out when embedding natively
    cache.check_simhash_bands.return_value = set()
    cache.get_timeline.return_value = []
    cache.get_cached_embeddings.return_value = {}
    repo.search_by_embedding.return_value = []
    return cache, repo, bus


@pytest.fixture
def payload():
    return ExtractCompletedPayload(
        url="https://test.com/news/123",
        url_hash="abcdef123456",
        title="Breaking News",
        content="This is the fully extracted and parsed body of the article.",
        content_length=59,
        language="en",
        quality_score=1.0,
    )


@pytest.mark.asyncio
async def test_exact_url_duplicates_are_skipped_instantly(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)

    # Simulate existing exact URL in POSTGRES Database
    repo.get_by_url_hash.return_value = Article(
        url=payload.url,
        url_hash=payload.url_hash,
        title=payload.title,
        content=payload.content,
        simhash=12345,
    )

    await dedup.process_article(payload)

    # Ensure we never even calculate simhashes or embeddings computationally!
    cache.check_simhash_bands.assert_not_called()
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_stage1_band_collision_with_far_distance_passes(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)
    
    repo.get_by_url_hash.return_value = None

    # Normalise and get original hash
    simhash_val = dedup.compute_simhash(dedup.normalize_text(payload.content))
    
    # Generate a collision: flip 10 bits explicitly!
    collision_hash = simhash_val ^ 0b1111111111  # Hamming Dist = 10
    
    # Mock cache to return this collision
    cache.check_simhash_bands.return_value = {f"other_url_hash:{hex(collision_hash)[2:]}"}

    # Intercept embedding generation to avoid loading torch in simple test
    with patch.object(dedup, "_compute_embedding", return_value=[0.1]*384):
        # We need to simulate repo save correctly
        repo.save.return_value = Article(
            url=payload.url,
            url_hash=payload.url_hash,
            title=payload.title,
            content=payload.content,
            simhash=simhash_val,
        )
        
        await dedup.process_article(payload)

    # It survived Stage 1!
    calls = bus.publish.call_args_list
    assert len(calls) == 1
    # Successfully published Unique!
    assert calls[0][0][0] == EventType.ARTICLE_UNIQUE


@pytest.mark.asyncio
async def test_stage1_band_collision_with_near_distance_drops(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)
    repo.get_by_url_hash.return_value = None

    # Normalise and get original hash
    simhash_val = dedup.compute_simhash(dedup.normalize_text(payload.content))
    
    # Generate a collision: flip 2 bits explicitly! (Threshold <= 3)
    collision_hash = simhash_val ^ 0b11  # Hamming Dist = 2
    
    # Mock cache to return this collision
    cache.check_simhash_bands.return_value = {f"original_url_hash:{hex(collision_hash)[2:]}"}

    await dedup.process_article(payload)

    # It caught the Stage 1 violation!
    calls = bus.publish.call_args_list
    assert len(calls) == 1
    assert calls[0][0][0] == EventType.ARTICLE_DUPLICATE
    
    envelope = calls[0][0][1]
    assert envelope.payload["stage"] == "simhash"


@pytest.mark.asyncio
async def test_stage2_dense_embedding_failure_gracefully_drops(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)
    repo.get_by_url_hash.return_value = None

    # Intercept embedding generation and return None to simulate timeout/failure
    with patch.object(dedup, "_compute_embedding", return_value=None):
        await dedup.process_article(payload)

    # It just drops without crashing - and doesn't publish either
    cache.check_simhash_bands.assert_called_once()
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_dense_embedding_caught_in_cache(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)
    repo.get_by_url_hash.return_value = None

    # Pretend it passed Stage 1 perfectly
    cache.check_simhash_bands.return_value = set()
    
    # Cache scope checks (Return a highly-correlated recent timeline candidate)
    mock_embed = [0.5] * 384
    cache.get_timeline.return_value = ["recent_hash"]
    cache.get_cached_embeddings.return_value = {"recent_hash": mock_embed} # Exact same embed returns 1.0 > 0.85

    # Intercept embedding generation to avoid torch logic
    with patch.object(dedup, "_compute_embedding", return_value=mock_embed):
        await dedup.process_article(payload)

    # Assert dropping
    calls = bus.publish.call_args_list
    assert len(calls) == 1
    assert calls[0][0][0] == EventType.ARTICLE_DUPLICATE
    envelope = calls[0][0][1]
    assert envelope.payload["stage"] == "vector"
    assert envelope.payload["duplicate_of"] == "recent_hash"


@pytest.mark.asyncio
async def test_stage2_dense_embedding_caught_in_db_fallback(mocks, payload):
    cache, repo, bus = mocks
    dedup = DedupService(cache, repo, bus)
    repo.get_by_url_hash.return_value = None
    
    mock_embed = [0.5] * 384
    
    # DB repo check triggers heavily
    cluster_uuid = uuid4()
    repo.search_by_embedding.return_value = [
        SearchResult(
            article_id=cluster_uuid,
            title="Foo",
            content_preview="bar",
            similarity_score=0.92,
            cluster_id=None,
            source_domain="foo.com"
        )
    ]

    # Intercept embedding generation
    with patch.object(dedup, "_compute_embedding", return_value=mock_embed):
        await dedup.process_article(payload)

    calls = bus.publish.call_args_list
    assert len(calls) == 1
    assert calls[0][0][0] == EventType.ARTICLE_DUPLICATE
    envelope = calls[0][0][1]
    assert envelope.payload["stage"] == "vector"
    assert envelope.payload["duplicate_of"] == str(cluster_uuid)
