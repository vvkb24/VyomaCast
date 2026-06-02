"""Fake cache store for in-memory hot-state testing.

Simulates all Redis namespaces (clusters, timeline, SimHash bands, embedding
cache, dirty tracking, metrics) using plain Python data structures.

TTL behaviour is simulated via an internal fake clock that can be advanced
with ``advance_time()``, allowing tests to verify expiry logic without
actually sleeping.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional, override

from src.domain.interfaces import CacheStore


class FakeCacheStore(CacheStore):
    """In-memory ``CacheStore`` implementation with simulated TTLs.

    All data is stored in plain dicts/sets.  TTL-bearing entries store
    their expiry as a Unix timestamp.  The fake clock can be advanced
    via :meth:`advance_time` to test expiry paths without real delays.

    Test Helpers
    ------------
    ``advance_time(seconds)``
        Move the internal clock forward to trigger TTL expirations.
    ``get_metric(name)``
        Read the current value of a metrics counter.
    ``reset()``
        Clear all state (call between tests if reusing the instance).
    """

    def __init__(self) -> None:
        self._connected: bool = False
        self._time_offset: float = 0.0

        # Cluster store (no TTL — protected from volatile-lru)
        self._clusters: dict[str, dict] = {}

        # Timeline: list of (timestamp, article_hash), kept sorted
        self._timeline: list[tuple[float, str]] = []

        # SimHash bands: (band_index, band_value) -> {article_hash: expiry}
        self._simhash_bands: dict[tuple[int, str], dict[str, float]] = defaultdict(dict)

        # Embedding cache: article_id -> (embedding, expiry)
        self._embeddings: dict[str, tuple[list[float], float]] = {}

        # Dirty tracking (no TTL — protected from volatile-lru)
        self._dirty: dict[str, set[str]] = defaultdict(set)

        # Metrics counters
        self._metrics: dict[str, int] = defaultdict(int)

    # ── Internal clock ────────────────────────────────────────────────────

    def _now(self) -> float:
        """Return the fake-clock-aware current time."""
        return time.time() + self._time_offset

    # ── Interface: Lifecycle ──────────────────────────────────────────────

    @override
    async def connect(self) -> None:
        self._connected = True

    @override
    async def disconnect(self) -> None:
        self._connected = False

    @override
    async def health_check(self) -> bool:
        return self._connected

    # ── Interface: Cluster Operations ─────────────────────────────────────

    @override
    async def set_cluster(self, cluster_id: str, data: dict) -> None:
        self._clusters[cluster_id] = data

    @override
    async def get_cluster(self, cluster_id: str) -> Optional[dict]:
        return self._clusters.get(cluster_id)

    @override
    async def get_active_clusters(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        active = [
            c for c in self._clusters.values()
            if c.get("status", "active") == "active"
        ]
        # Sort by last_activity descending (fall back to empty string)
        active.sort(key=lambda c: c.get("last_activity", ""), reverse=True)
        return active[offset : offset + limit]

    @override
    async def get_all_cluster_centroids(self) -> dict[str, tuple[list[float], int]]:
        result: dict[str, tuple[list[float], int]] = {}
        for cluster_id, data in self._clusters.items():
            if data.get("status", "active") == "active" and "centroid" in data:
                result[cluster_id] = (
                    data["centroid"],
                    data.get("article_count", 1),
                )
        return result

    @override
    async def delete_cluster(self, cluster_id: str) -> bool:
        if cluster_id in self._clusters:
            del self._clusters[cluster_id]
            return True
        return False

    # ── Interface: Timeline ───────────────────────────────────────────────

    @override
    async def add_to_timeline(self, article_hash: str, timestamp: float) -> None:
        # Remove existing entry for the same hash (update scenario)
        self._timeline = [(ts, h) for ts, h in self._timeline if h != article_hash]
        self._timeline.append((timestamp, article_hash))
        self._timeline.sort(key=lambda x: x[0], reverse=True)

    @override
    async def get_timeline(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[str]:
        return [h for _, h in self._timeline[offset : offset + limit]]

    # ── Interface: SimHash Bands ──────────────────────────────────────────

    @override
    async def add_simhash_bands(
        self,
        article_hash: str,
        bands: dict[int, str],
        ttl_seconds: int,
    ) -> None:
        expiry = self._now() + ttl_seconds
        for band_idx, band_val in bands.items():
            key = (band_idx, band_val)
            self._simhash_bands[key][article_hash] = expiry

    @override
    async def check_simhash_bands(self, bands: dict[int, str]) -> set[str]:
        now = self._now()
        matches: set[str] = set()
        for band_idx, band_val in bands.items():
            key = (band_idx, band_val)
            if key in self._simhash_bands:
                for article_hash, expiry in self._simhash_bands[key].items():
                    if expiry > now:
                        matches.add(article_hash)
        return matches

    # ── Interface: Embedding Cache ────────────────────────────────────────

    @override
    async def cache_embedding(
        self,
        article_id: str,
        embedding: list[float],
        ttl_seconds: int,
    ) -> None:
        expiry = self._now() + ttl_seconds
        self._embeddings[article_id] = (embedding, expiry)

    @override
    async def get_cached_embeddings(
        self,
        article_ids: list[str],
    ) -> dict[str, list[float]]:
        now = self._now()
        result: dict[str, list[float]] = {}
        for article_id in article_ids:
            entry = self._embeddings.get(article_id)
            if entry is not None:
                embedding, expiry = entry
                if expiry > now:
                    result[article_id] = embedding
        return result

    # ── Interface: Dirty Tracking ─────────────────────────────────────────

    @override
    async def mark_dirty(self, entity_type: str, entity_id: str) -> None:
        self._dirty[entity_type].add(entity_id)

    @override
    async def get_and_clear_dirty(
        self,
        entity_type: str,
        batch_size: int,
    ) -> list[str]:
        dirty_set = self._dirty.get(entity_type)
        if not dirty_set:
            return []

        # Simulate SPOP: pop up to batch_size items
        batch: list[str] = []
        for _ in range(min(batch_size, len(dirty_set))):
            batch.append(dirty_set.pop())
        return batch

    # ── Interface: Metrics ────────────────────────────────────────────────

    @override
    async def increment_metric(self, name: str, value: int = 1) -> None:
        self._metrics[name] += value

    # ── Test Helpers (not part of interface) ───────────────────────────────

    def advance_time(self, seconds: float) -> None:
        """Move the fake clock forward by ``seconds``.

        After advancing, any cached entries whose TTL has expired will
        be reported as missing on subsequent reads.
        """
        self._time_offset += seconds

    def get_metric(self, name: str) -> int:
        """Read the current value of a metrics counter."""
        return self._metrics.get(name, 0)

    @property
    def cluster_count(self) -> int:
        """Number of clusters currently in hot state."""
        return len(self._clusters)

    @property
    def timeline_count(self) -> int:
        """Number of entries in the timeline."""
        return len(self._timeline)

    @property
    def dirty_count(self) -> dict[str, int]:
        """Number of dirty entities per type."""
        return {k: len(v) for k, v in self._dirty.items() if v}

    def reset(self) -> None:
        """Clear all state.  Call between tests if reusing the instance."""
        self._clusters.clear()
        self._timeline.clear()
        self._simhash_bands.clear()
        self._embeddings.clear()
        self._dirty.clear()
        self._metrics.clear()
        self._time_offset = 0.0
