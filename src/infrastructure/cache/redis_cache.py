"""Async Redis implementation of the CacheStore interface."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, UTC
from functools import wraps
from typing import Any, Callable, Optional, Union

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from src.config import settings
from src.domain.exceptions import RetryableError
from src.domain.interfaces import CacheStore

logger = logging.getLogger(__name__)


class RedisCircuitBreaker:
    """A simple circuit breaker to protect the backend from cascading Redis failures."""

    def __init__(self, max_failures: int = 3, backoff_seconds: float = 30.0) -> None:
        self.failures = 0
        self.last_failure_time = 0.0
        self.max_failures = max_failures
        self.backoff_seconds = backoff_seconds

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            now = time.time()

            # Check if breaker is OPEN
            if self.failures >= self.max_failures:
                if now - self.last_failure_time < self.backoff_seconds:
                    raise RetryableError(
                        "Redis circuit breaker open",
                        details={
                            "failures": self.failures,
                            "backoff_remaining": self.backoff_seconds - (now - self.last_failure_time),
                        },
                    )
                # Half-open state: allow 1 request to pass and potentially reset

            try:
                result = await func(*args, **kwargs)
                if self.failures > 0:
                    logger.info("Redis circuit breaker RESET after successful response.")
                    self.failures = 0
                return result
            except RedisError as e:
                self.failures += 1
                self.last_failure_time = time.time()
                logger.error("Redis operation failed (%d/%d): %s", self.failures, self.max_failures, e)
                raise RetryableError("Redis temporary failure", details={"error": str(e)}) from e

        return wrapper


circuit_breaker = RedisCircuitBreaker()


class RedisCacheStore(CacheStore):
    """Hot-state cache implementation using async Redis."""

    def __init__(self) -> None:
        self._pool: Optional[ConnectionPool] = None
        self._client: Optional[Redis] = None

    @property
    def client(self) -> Redis:
        """Get the connected Redis client instance."""
        if self._client is None:
            raise RuntimeError("RedisCacheStore is not connected. Call connect() first.")
        return self._client

    @circuit_breaker
    async def connect(self) -> None:
        """Establish Redis connection pool."""
        self._pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        self._client = Redis(connection_pool=self._pool)
        # Verify connection immediately
        await self._client.ping()

    async def disconnect(self) -> None:
        """Close connection pool and release resources."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._pool:
            await self._pool.disconnect()
            self._pool = None

    @circuit_breaker
    async def health_check(self) -> bool:
        """Ping Redis and return ``True`` if responsive."""
        try:
            return await self.client.ping()
        except Exception:
            return False

    # ── Cluster Operations ────────────────────────────────────────────────

    @circuit_breaker
    async def set_cluster(self, cluster_id: str, data: dict) -> None:
        """Store or overwrite a cluster in hot state. No TTL to survive volatile-lru."""
        key = f"cache:cluster:{cluster_id}"
        
        # Maintain active clusters in a ZSET scored by last_activity for efficient retrieval
        last_activity_str = data.get("last_activity")
        if last_activity_str:
            # Handle standard ISO formats, ensuring UTC
            score = datetime.fromisoformat(last_activity_str.replace("Z", "+00:00")).timestamp()
        else:
            score = time.time()

        async with self.client.pipeline(transaction=True) as pipe:
            pipe.set(key, json.dumps(data), ex=86400)
            # Track in active clusters index
            if data.get("status") == "active":
                pipe.zadd("cache:clusters:active", {cluster_id: score})
            else:
                pipe.zrem("cache:clusters:active", cluster_id)
            await pipe.execute()

    @circuit_breaker
    async def get_cluster(self, cluster_id: str) -> Optional[dict]:
        """Retrieve a cluster from hot state."""
        key = f"cache:cluster:{cluster_id}"
        val = await self.client.get(key)
        return json.loads(val) if val else None

    @circuit_breaker
    async def get_active_clusters(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Retrieve a paginated list of all active clusters in hot state."""
        # Active clusters index keeps IDs ordered by last_activity timestamp (score)
        cluster_ids = await self.client.zrevrange("cache:clusters:active", offset, offset + limit - 1)
        if not cluster_ids:
            return []

        keys = [f"cache:cluster:{cid}" for cid in cluster_ids]
        raw_clusters = await self.client.mget(keys)

        results = []
        for raw in raw_clusters:
            if raw:
                results.append(json.loads(raw))
        return results

    @circuit_breaker
    async def get_all_cluster_centroids(self) -> dict[str, tuple[list[float], int]]:
        """Retrieve all active cluster centroids for vectorised similarity."""
        # Grab all active cluster IDs
        cluster_ids = await self.client.zrange("cache:clusters:active", 0, -1)
        if not cluster_ids:
            return {}

        keys = [f"cache:cluster:{cid}" for cid in cluster_ids]
        
        # Batch MGET in chunks of 500 to avoid blocking Redis for too long
        results: dict[str, tuple[list[float], int]] = {}
        batch_size = 500
        
        for i in range(0, len(keys), batch_size):
            batch_keys = keys[i : i + batch_size]
            batch_cids = cluster_ids[i : i + batch_size]
            raw_data = await self.client.mget(batch_keys)
            
            for cid, raw in zip(batch_cids, raw_data):
                if raw:
                    cdata = json.loads(raw)
                    cent = cdata.get("centroid")
                    cnt = cdata.get("article_count", 0)
                    if cent and len(cent) == 384:
                        results[cid] = (cent, cnt)
        
        return results

    @circuit_breaker
    async def delete_cluster(self, cluster_id: str) -> bool:
        """Remove a cluster from hot state."""
        key = f"cache:cluster:{cluster_id}"
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.zrem("cache:clusters:active", cluster_id)
            res = await pipe.execute()
        return bool(res[0])

    # ── Timeline ──────────────────────────────────────────────────────────

    @circuit_breaker
    async def add_to_timeline(self, article_hash: str, timestamp: float) -> None:
        """Add an article to the global timeline sorted set."""
        async with self.client.pipeline(transaction=False) as pipe:
            pipe.zadd("timeline:global", {article_hash: timestamp})
            pipe.zremrangebyrank("timeline:global", 0, -1001)
            await pipe.execute()

    @circuit_breaker
    async def get_timeline(self, *, limit: int = 50, offset: int = 0) -> list[str]:
        """Retrieve article hashes from the timeline, most recent first."""
        return await self.client.zrevrange("timeline:global", offset, offset + limit - 1)

    # ── SimHash Band Lookup ───────────────────────────────────────────────

    @circuit_breaker
    async def add_simhash_bands(self, article_hash: str, bands: dict[int, str], ttl_seconds: int) -> None:
        """Register an article's SimHash band values in Redis.

        Optimization: Uses a single ZSET per band index. Combines band_value and article_hash
        into the member and uses the expiration timestamp as the score.
        """
        now = time.time()
        expire_at = now + ttl_seconds

        async with self.client.pipeline(transaction=False) as pipe:
            for band_idx, band_val in bands.items():
                key = f"simhash:band:{band_idx}"
                member = f"{band_val}:{article_hash}"
                # Add/update the hash in this band
                pipe.zadd(key, {member: expire_at})
                # Opportunistic cleanup of expired items (strictly enforce boundary)
                pipe.zremrangebyscore(key, "-inf", now)
            await pipe.execute()

    @circuit_breaker
    async def check_simhash_bands(self, bands: dict[int, str]) -> set[str]:
        """Check which existing articles share at least one SimHash band value."""
        collisions = set()
        now = time.time()

        async def check_band(b_idx: int, b_val: str) -> list[str]:
            key = f"simhash:band:{b_idx}"
            prefix = f"{b_val}:"
            cursor = 0
            found = []
            
            # Use ZSCAN to efficiently match the prefix within the band's ZSET
            while True:
                cursor, items = await self.client.zscan(key, cursor=cursor, match=f"{prefix}*", count=5000)
                for member, score in items:
                    if score > now:  # Enforce TTL exact boundary
                        parts = member.split(":", 1)
                        if len(parts) == 2:
                            found.append(parts[1])
                if cursor == 0:
                    break
            return found

        # Execute all band checks concurrently
        tasks = [check_band(idx, val) for idx, val in bands.items()]
        results = await asyncio.gather(*tasks)

        for res in results:
            collisions.update(res)

        return collisions

    # ── Embedding Cache ───────────────────────────────────────────────────

    @circuit_breaker
    async def cache_embedding(self, article_id: str, embedding: list[float], ttl_seconds: int) -> None:
        """Cache a 384-dim embedding vector."""
        key = f"cache:embedding:{article_id}"
        # Standard Pydantic/JSON serialization into a generic Redis String
        await self.client.setex(key, ttl_seconds, json.dumps(embedding))

    @circuit_breaker
    async def get_cached_embeddings(self, article_ids: list[str]) -> dict[str, list[float]]:
        """Batch-retrieve cached embeddings."""
        if not article_ids:
            return {}

        keys = [f"cache:embedding:{aid}" for aid in article_ids]
        raw_vals = await self.client.mget(keys)

        results = {}
        for aid, raw in zip(article_ids, raw_vals):
            if raw:
                results[aid] = json.loads(raw)
        return results

    # ── Dirty Tracking ────────────────────────────────────────────────────

    @circuit_breaker
    async def mark_dirty(self, entity_type: str, entity_id: str) -> None:
        """Mark an entity as needing write-back to PostgreSQL."""
        key = f"cache:dirty:{entity_type}"
        await self.client.sadd(key, entity_id)

    @circuit_breaker
    async def get_and_clear_dirty(self, entity_type: str, batch_size: int) -> list[str]:
        """Atomically pop up to ``batch_size`` IDs from the dirty set."""
        key = f"cache:dirty:{entity_type}"
        res = await self.client.spop(key, batch_size)
        
        if not res:
            return []
        # Support both scalar and list returns based on the redis-py version/arguments
        return [res] if isinstance(res, str) else list(res)

    # ── Metrics Counters ──────────────────────────────────────────────────

    @circuit_breaker
    async def increment_metric(self, name: str, value: int = 1) -> None:
        """Atomically increment a named counter."""
        key = f"metrics:{name}"
        await self.client.incrby(key, value)
