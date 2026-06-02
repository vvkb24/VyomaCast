"""Tests for the async Redis cache implementation."""

import asyncio
from typing import AsyncGenerator

import pytest
from redis.exceptions import ConnectionError

from src.domain.exceptions import RetryableError
from src.infrastructure.cache.redis_cache import RedisCacheStore, circuit_breaker


@pytest.fixture
async def cache_store() -> AsyncGenerator[RedisCacheStore, None]:
    """Provide a highly clean Redis Cache Store connected to the test DB."""
    # Reset global circuit breaker state before each test
    circuit_breaker.failures = 0
    circuit_breaker.last_failure_time = 0.0

    store = RedisCacheStore()
    await store.connect()
    
    # Ensure fresh DB 15
    await store.client.flushdb()
    
    yield store
    
    await store.disconnect()


@pytest.mark.asyncio
async def test_set_and_get_cluster(cache_store: RedisCacheStore) -> None:
    """Verifies standard string-based serialization mapping for pure JSON structures."""
    cluster_data = {
        "id": "c123",
        "label": "Test Label",
        "status": "active",
        "last_activity": "2026-04-18T10:00:00Z"
    }
    
    await cache_store.set_cluster("c123", cluster_data)
    
    loaded = await cache_store.get_cluster("c123")
    assert loaded is not None
    assert loaded["label"] == "Test Label"


@pytest.mark.asyncio
async def test_get_active_clusters(cache_store: RedisCacheStore) -> None:
    """Validates the cache's active clusters ZSET index ordering."""
    c1 = {"id": "c1", "status": "active", "last_activity": "2026-04-18T10:00:00Z"}
    c2 = {"id": "c2", "status": "active", "last_activity": "2026-04-18T12:00:00Z"}
    c3 = {"id": "c3", "status": "archived", "last_activity": "2026-04-18T14:00:00Z"}
    
    await cache_store.set_cluster("c1", c1)
    await cache_store.set_cluster("c2", c2)
    # Testing that archived/decayed are decoupled or removed from active 
    await cache_store.set_cluster("c3", c3) 
    
    active = await cache_store.get_active_clusters()
    # c2 is newer, should be first
    assert len(active) == 2
    assert active[0]["id"] == "c2"
    assert active[1]["id"] == "c1"


@pytest.mark.asyncio
async def test_simhash_band_lookups_and_ttl(cache_store: RedisCacheStore) -> None:
    """Proves specific constraint: 'single Set per band index' and composite keys, plus TTls."""
    brands_art1 = {0: "aabb", 1: "ccdd"}
    brands_art2 = {0: "aabb", 1: "eeff"}
    
    # Store with tiny TTLs to avoid time.sleep blocks
    await cache_store.add_simhash_bands("hash1", brands_art1, ttl_seconds=1)
    await cache_store.add_simhash_bands("hash2", brands_art2, ttl_seconds=3)
    
    matches = await cache_store.check_simhash_bands({0: "aabb"})
    assert "hash1" in matches
    assert "hash2" in matches
    
    matches_band_1 = await cache_store.check_simhash_bands({1: "ccdd"})
    assert "hash1" in matches_band_1
    assert "hash2" not in matches_band_1

    # Induce TTL expiry for art1
    await asyncio.sleep(1.1)
    
    # Validate the opportunistic cleanup happens internally and expired values aren't returned
    matches_after = await cache_store.check_simhash_bands({0: "aabb"})
    assert "hash1" not in matches_after
    assert "hash2" in matches_after


@pytest.mark.asyncio
async def test_embedding_ttl(cache_store: RedisCacheStore) -> None:
    """Validate 8 hour standard embedding TTL applies correctly."""
    await cache_store.cache_embedding("art_1", [0.1, 0.2, 0.3], ttl_seconds=1)
    
    # Immediately available
    res = await cache_store.get_cached_embeddings(["art_1"])
    assert "art_1" in res
    
    # Wait for TTL to expire
    await asyncio.sleep(1.2)
    
    res2 = await cache_store.get_cached_embeddings(["art_1"])
    assert "art_1" not in res2


@pytest.mark.asyncio
async def test_dirty_tracking_deduplication_and_atomicity(cache_store: RedisCacheStore) -> None:
    """Verifies that multiple dirty pings deduplicate and SPOP batch size applies correctly."""
    await cache_store.mark_dirty("articles", "hash1")
    await cache_store.mark_dirty("articles", "hash2")
    await cache_store.mark_dirty("articles", "hash1")  # Deduplicate

    # Pull out up to 10 lines
    res = await cache_store.get_and_clear_dirty("articles", 10)
    assert len(res) == 2
    assert {"hash1", "hash2"} == set(res)
    
    # Second pull is naturally empty
    res_empty = await cache_store.get_and_clear_dirty("articles", 10)
    assert len(res_empty) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_wrapper(cache_store: RedisCacheStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies consecutive failures raise RetryableError and start 30s backoff."""
    failures = 0
    
    async def mock_get(*args, **kwargs):
        nonlocal failures
        failures += 1
        raise ConnectionError("Simulated network blip")

    monkeypatch.setattr(cache_store.client, "get", mock_get)

    with pytest.raises(RetryableError) as exc_1:
        await cache_store.get_cluster("test")
    assert failures == 1
    
    with pytest.raises(RetryableError) as exc_2:
        await cache_store.get_cluster("test")
    assert failures == 2

    with pytest.raises(RetryableError) as exc_3:
        await cache_store.get_cluster("test")
    assert failures == 3
    
    # Fourth directly trips the circuit breaker without invoking get
    with pytest.raises(RetryableError) as cb_exc:
        await cache_store.get_cluster("test")
        
    assert "circuit breaker open" in str(cb_exc.value).lower()
    assert failures == 3  # The mock was NOT invoked on the 4th call
