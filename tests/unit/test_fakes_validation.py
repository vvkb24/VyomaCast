"""Critical validation tests for in-memory fakes.

These tests specifically target edge cases and potential correctness issues
identified during the pre-approval review:
    1. Version guard: lower/equal/higher
    2. Cache TTL exact boundary
    3. SimHash band collision isolation
    4. Embedding cosine similarity geometry
    5. Dirty tracking atomicity simulation
    6. Repository dual-index consistency
"""

import math
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.domain.models import Article, Cluster, ClusterStatus, compute_url_hash
from tests.fakes import (
    FakeArticleRepository,
    FakeCacheStore,
    FakeClusterRepository,
    FakeEmbeddingService,
    FakeEventBus,
)


# ════════════════════════════════════════════════════════════════════════════
# 1. Version Guard — exhaustive edge cases
# ════════════════════════════════════════════════════════════════════════════


class TestVersionGuardExhaustive:
    """Verify all three version comparison outcomes match Postgres semantics.

    Postgres SQL: ``WHERE version < excluded.version``
        - existing.version < new.version → UPDATE (accept)
        - existing.version = new.version → SKIP (reject)
        - existing.version > new.version → SKIP (reject)
    """

    @pytest.fixture()
    def url_hash(self) -> str:
        return compute_url_hash("https://example.com/version-test")

    # ── ArticleRepository ─────────────────────────────────────────────────

    async def test_article_lower_version_rejected(self, url_hash: str) -> None:
        repo = FakeArticleRepository()
        v5 = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5", content="C", version=5)
        await repo.save(v5)

        v3 = Article(url="https://example.com/version-test", url_hash=url_hash, title="V3", content="C", version=3)
        result = await repo.save(v3)

        assert result.version == 5, "Lower version should be rejected"
        assert result.title == "V5"

    async def test_article_equal_version_rejected(self, url_hash: str) -> None:
        repo = FakeArticleRepository()
        v5a = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5-first", content="C", version=5)
        await repo.save(v5a)

        v5b = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5-second", content="C", version=5)
        result = await repo.save(v5b)

        assert result.title == "V5-first", "Equal version should be rejected (Postgres: version < excluded.version)"

    async def test_article_higher_version_accepted(self, url_hash: str) -> None:
        repo = FakeArticleRepository()
        v5 = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5", content="C", version=5)
        await repo.save(v5)

        v7 = Article(url="https://example.com/version-test", url_hash=url_hash, title="V7", content="C", version=7)
        result = await repo.save(v7)

        assert result.version == 7, "Higher version should be accepted"
        assert result.title == "V7"

    # ── ClusterRepository ─────────────────────────────────────────────────

    async def test_cluster_lower_version_rejected(self) -> None:
        repo = FakeClusterRepository()
        cid = uuid4()
        await repo.save(Cluster(id=cid, label="V5", version=5))

        result = await repo.save(Cluster(id=cid, label="V3", version=3))
        assert result.label == "V5"

    async def test_cluster_equal_version_rejected(self) -> None:
        repo = FakeClusterRepository()
        cid = uuid4()
        await repo.save(Cluster(id=cid, label="V5-first", version=5))

        result = await repo.save(Cluster(id=cid, label="V5-second", version=5))
        assert result.label == "V5-first", "Equal version should be rejected"

    async def test_cluster_higher_version_accepted(self) -> None:
        repo = FakeClusterRepository()
        cid = uuid4()
        await repo.save(Cluster(id=cid, label="V5", version=5))

        result = await repo.save(Cluster(id=cid, label="V7", version=7))
        assert result.label == "V7"

    # ── Batch version guard ───────────────────────────────────────────────

    async def test_batch_equal_version_rejected(self, url_hash: str) -> None:
        repo = FakeArticleRepository()
        v5 = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5", content="C", version=5)
        await repo.save(v5)

        # Batch insert with same version should be skipped
        v5_dup = Article(url="https://example.com/version-test", url_hash=url_hash, title="V5-batch", content="C", version=5)
        count = await repo.save_batch([v5_dup])
        assert count == 0, "Batch: equal version should be rejected"

        fetched = await repo.get_by_url_hash(url_hash)
        assert fetched is not None
        assert fetched.title == "V5"


# ════════════════════════════════════════════════════════════════════════════
# 2. Cache TTL edge cases
# ════════════════════════════════════════════════════════════════════════════


class TestCacheTTLEdgeCases:

    async def test_exact_boundary_is_expired(self) -> None:
        """At exactly TTL seconds, the entry should be expired.

        The check is ``expiry > now``, so at exact boundary (expiry == now)
        the entry is expired.
        """
        cache = FakeCacheStore()
        await cache.cache_embedding("art1", [0.1] * 384, ttl_seconds=60)

        # Advance to exactly the expiry boundary
        cache.advance_time(60.0)
        result = await cache.get_cached_embeddings(["art1"])
        assert len(result) == 0, "Entry at exact TTL boundary should be expired"

    async def test_one_ms_before_boundary_is_alive(self) -> None:
        cache = FakeCacheStore()
        await cache.cache_embedding("art1", [0.1] * 384, ttl_seconds=60)

        cache.advance_time(59.999)
        result = await cache.get_cached_embeddings(["art1"])
        assert "art1" in result, "Entry 1ms before TTL should still be alive"

    async def test_simhash_exact_boundary(self) -> None:
        cache = FakeCacheStore()
        await cache.add_simhash_bands("art1", {0: "aa"}, ttl_seconds=30)

        cache.advance_time(30.0)
        matches = await cache.check_simhash_bands({0: "aa"})
        assert len(matches) == 0, "SimHash band at exact TTL should be expired"

    async def test_ttl_renewal(self) -> None:
        """Re-caching with new TTL should extend the expiry."""
        cache = FakeCacheStore()
        await cache.cache_embedding("art1", [0.1] * 384, ttl_seconds=60)
        cache.advance_time(50.0)  # 10s remaining

        # Re-cache with fresh TTL
        await cache.cache_embedding("art1", [0.2] * 384, ttl_seconds=60)
        cache.advance_time(50.0)  # Would have expired under old TTL

        result = await cache.get_cached_embeddings(["art1"])
        assert "art1" in result, "Re-caching should renew the TTL"
        assert result["art1"][0] == 0.2, "Re-caching should update the value"


# ════════════════════════════════════════════════════════════════════════════
# 3. SimHash band collision isolation
# ════════════════════════════════════════════════════════════════════════════


class TestSimHashBandIsolation:

    async def test_same_value_different_bands_no_collision(self) -> None:
        """Band 0 with value 'aa' should NOT match band 1 with value 'aa'."""
        cache = FakeCacheStore()
        await cache.add_simhash_bands("art1", {0: "aa"}, ttl_seconds=3600)

        matches = await cache.check_simhash_bands({1: "aa"})  # Different band index!
        assert len(matches) == 0, "Same value in different band index should not collide"

    async def test_multiple_articles_same_band(self) -> None:
        """Multiple articles sharing a band value should all be returned."""
        cache = FakeCacheStore()
        await cache.add_simhash_bands("art1", {0: "aa"}, ttl_seconds=3600)
        await cache.add_simhash_bands("art2", {0: "aa"}, ttl_seconds=3600)
        await cache.add_simhash_bands("art3", {0: "bb"}, ttl_seconds=3600)

        matches = await cache.check_simhash_bands({0: "aa"})
        assert matches == {"art1", "art2"}

    async def test_article_not_self_colliding_across_bands(self) -> None:
        """An article's own bands should not produce cross-band collisions."""
        cache = FakeCacheStore()
        await cache.add_simhash_bands("art1", {0: "aa", 1: "bb", 2: "cc"}, ttl_seconds=3600)

        # Querying with a completely different set of values
        matches = await cache.check_simhash_bands({0: "xx", 1: "yy", 2: "zz"})
        assert len(matches) == 0


# ════════════════════════════════════════════════════════════════════════════
# 4. Embedding geometry validation
# ════════════════════════════════════════════════════════════════════════════


class TestEmbeddingGeometry:

    async def test_self_similarity_is_one(self) -> None:
        """Cosine similarity of a vector with itself should be ~1.0."""
        svc = FakeEmbeddingService()
        vec = await svc.embed("test text")

        from tests.fakes.fake_repository import _cosine_similarity
        sim = _cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-9

    async def test_different_texts_have_low_similarity(self) -> None:
        """Random pseudo-embeddings should have low but non-zero similarity."""
        svc = FakeEmbeddingService()
        v1 = await svc.embed("technology and AI innovation")
        v2 = await svc.embed("cooking recipes for pasta")

        from tests.fakes.fake_repository import _cosine_similarity
        sim = _cosine_similarity(v1, v2)
        # Random 384-dim unit vectors: expected similarity ~0 ± 0.1
        assert abs(sim) < 0.3, f"Random vectors should have low similarity, got {sim}"

    async def test_embedding_search_integration(self) -> None:
        """Prove that search_by_embedding actually works with fake embeddings."""
        repo = FakeArticleRepository()
        svc = FakeEmbeddingService()

        # Insert articles with embeddings
        emb1 = await svc.embed("global markets rally today")
        url1 = "https://example.com/markets"
        a1 = Article(
            url=url1,
            url_hash=compute_url_hash(url1),
            title="Markets Rally",
            content="Global markets rallied sharply on Wednesday...",
            embedding=emb1,
        )
        await repo.save(a1)

        emb2 = await svc.embed("cooking recipes for pasta")
        url2 = "https://example.com/pasta"
        a2 = Article(
            url=url2,
            url_hash=compute_url_hash(url2),
            title="Pasta Recipes",
            content="Here are some delicious pasta recipes...",
            embedding=emb2,
        )
        await repo.save(a2)

        # Search with the same embedding as article 1
        results = await repo.search_by_embedding(emb1, threshold=0.5)
        assert len(results) >= 1
        assert results[0].title == "Markets Rally"
        assert results[0].similarity_score > 0.99  # Self-match

    async def test_all_components_nonzero(self) -> None:
        """Verify no degenerate vectors (all zeros or single hot)."""
        svc = FakeEmbeddingService()
        vec = await svc.embed("any text input")

        nonzero = sum(1 for x in vec if abs(x) > 1e-15)
        assert nonzero == 384, "All components should be nonzero for Gaussian vectors"


# ════════════════════════════════════════════════════════════════════════════
# 5. Dirty tracking correctness
# ════════════════════════════════════════════════════════════════════════════


class TestDirtyTrackingCorrectness:

    async def test_dirty_is_deduped(self) -> None:
        """Marking the same entity dirty multiple times should count once."""
        cache = FakeCacheStore()
        for _ in range(10):
            await cache.mark_dirty("articles", "same_hash")

        batch = await cache.get_and_clear_dirty("articles", batch_size=10)
        assert len(batch) == 1

    async def test_dirty_pop_is_atomic(self) -> None:
        """Items returned by get_and_clear_dirty should be removed from the set."""
        cache = FakeCacheStore()
        for i in range(10):
            await cache.mark_dirty("clusters", f"c{i}")

        batch1 = await cache.get_and_clear_dirty("clusters", batch_size=3)
        batch2 = await cache.get_and_clear_dirty("clusters", batch_size=3)
        batch3 = await cache.get_and_clear_dirty("clusters", batch_size=10)

        # All three batches should be disjoint
        all_items = batch1 + batch2 + batch3
        assert len(set(all_items)) == 10, "All 10 items should be returned across batches"
        assert len(all_items) == 10, "No duplicates across batches"

    async def test_dirty_entity_types_are_isolated(self) -> None:
        cache = FakeCacheStore()
        await cache.mark_dirty("articles", "a1")
        await cache.mark_dirty("clusters", "c1")

        articles = await cache.get_and_clear_dirty("articles", batch_size=10)
        clusters = await cache.get_and_clear_dirty("clusters", batch_size=10)

        assert articles == ["a1"]
        assert clusters == ["c1"]


# ════════════════════════════════════════════════════════════════════════════
# 6. Repository dual-index consistency
# ════════════════════════════════════════════════════════════════════════════


class TestDualIndexConsistency:

    async def test_article_save_updates_both_indices(self) -> None:
        """After version upgrade, both by_id and by_url_hash should point to the new version."""
        repo = FakeArticleRepository()
        url = "https://example.com/dual-index"
        url_hash = compute_url_hash(url)

        v1 = Article(url=url, url_hash=url_hash, title="V1", content="C", version=1)
        await repo.save(v1)

        v2 = Article(url=url, url_hash=url_hash, title="V2", content="C", version=2)
        await repo.save(v2)

        by_hash = await repo.get_by_url_hash(url_hash)
        by_id = await repo.get_by_id(v2.id)

        assert by_hash is not None and by_hash.title == "V2"
        assert by_id is not None and by_id.title == "V2"

    async def test_article_version_upgrade_cleans_old_uuid(self) -> None:
        """FIXED: When V2 (different UUID) overwrites V1 via url_hash,
        V1's UUID is removed from _by_id, matching Postgres single-row semantics."""
        repo = FakeArticleRepository()
        url = "https://example.com/dual-index-bug"
        url_hash = compute_url_hash(url)

        v1 = Article(url=url, url_hash=url_hash, title="V1", content="C", version=1)
        await repo.save(v1)

        v2 = Article(url=url, url_hash=url_hash, title="V2", content="C", version=2)
        await repo.save(v2)

        # Old UUID should be cleaned up
        stale = await repo.get_by_id(v1.id)
        assert stale is None, "Old UUID should be removed from _by_id"

        # Count matches Postgres: 1 row
        assert await repo.count() == 1, "Should have exactly 1 article (Postgres semantics)"

        # New UUID works
        fetched = await repo.get_by_id(v2.id)
        assert fetched is not None
        assert fetched.title == "V2"
