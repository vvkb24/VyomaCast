"""End-to-End Pipeline Integration Test.

Validates the full ingestion flow using in-memory fakes:

    ExtractCompleted → DedupService → ClusterService → Notifier → WebSocket

This test does NOT use live infrastructure (NATS, Redis, Postgres).
Instead, it wires the real service classes against FakeEventBus,
FakeCacheStore, and FakeArticleRepository/FakeClusterRepository,
proving that a single article flows through dedup, clustering,
and broadcast without any step being bypassed.

The FakeEventBus auto-dispatches published events to registered
handlers, creating a synchronous simulation of the async pipeline.
"""

import asyncio
import hashlib
import math
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.domain.events import (
    ArticleClusteredPayload,
    ArticleUniquePayload,
    ClusterCreatedPayload,
    EventEnvelope,
    EventType,
    ExtractCompletedPayload,
)
from src.domain.models import Article, Cluster
from src.services.cluster_service import ClusterService
from src.services.dedup_service import DedupService
from tests.fakes.fake_cache import FakeCacheStore
from tests.fakes.fake_event_bus import FakeEventBus
from tests.fakes.fake_repository import (
    FakeArticleRepository,
    FakeClusterRepository,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _url_hash(url: str) -> str:
    """Compute the canonical SHA-256 url_hash matching domain logic."""
    from src.domain.models import compute_url_hash
    return compute_url_hash(url)


def _deterministic_embedding(seed: str) -> list[float]:
    """Produce a 384-dim embedding deterministically from a seed string.

    Uses a hash-seeded sine wave — same approach as conftest.sample_embedding
    but parameterized for uniqueness.
    """
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    offset = (h % 10000) * 0.001
    vec = [math.sin((i + offset) * 0.1) * 0.5 for i in range(384)]
    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def cache():
    c = FakeCacheStore()
    asyncio.get_event_loop_policy()  # ensure loop policy exists
    return c


@pytest.fixture()
def article_repo():
    return FakeArticleRepository()


@pytest.fixture()
def cluster_repo():
    return FakeClusterRepository()


@pytest.fixture()
def bus():
    return FakeEventBus()


# ── Test: Full Pipeline Flow ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_extract_to_cluster(
    cache, article_repo, cluster_repo, bus
):
    """Simulate the complete pipeline: Extract → Dedup → Cluster → Event.

    Steps:
        1. Construct an ExtractCompletedPayload (the output of fetcher_worker).
        2. Run DedupService.process_article — verifies dedup passes (unique).
        3. Capture the ArticleUniquePayload emitted by dedup.
        4. Run ClusterService.process_article — verifies clustering executes.
        5. Assert:
           - Article is persisted in the repository with a cluster_id.
           - A Cluster exists and has article_count = 1.
           - ARTICLE_CLUSTERED event was emitted.
           - The event payload contains correct cluster_id, article_id, is_new_cluster.
    """

    await cache.connect()

    # ── Stage 1: Build realistic ExtractCompletedPayload ──────────────────

    test_url = "https://www.reuters.com/technology/ai-breakthrough-2026/"
    url_hash = _url_hash(test_url)
    content = (
        "Researchers announced a major breakthrough in artificial intelligence "
        "today, demonstrating a system capable of reasoning across multiple "
        "domains with unprecedented accuracy. The findings, published in a "
        "peer-reviewed journal, suggest significant implications for fields "
        "ranging from drug discovery to climate modeling. Industry experts "
        "called the results transformative."
    )

    extract_payload = ExtractCompletedPayload(
        url=test_url,
        url_hash=url_hash,
        title="AI Breakthrough Announced by Leading Research Lab",
        content=content,
        content_length=len(content),
        authors=["Jane Doe"],
        published_at=datetime.now(UTC),
        quality_score=0.95,
    )

    # ── Stage 2: Run Dedup (with mocked embedding generation) ─────────────

    embedding = _deterministic_embedding(url_hash)

    dedup_service = DedupService(cache, article_repo, bus)

    # Patch the embedding computation to return our deterministic vector
    import numpy as np
    from unittest.mock import patch
    with patch("src.services.dedup_service.model.encode", return_value=np.array([embedding])):
        await dedup_service.process_article(extract_payload)

    # ── Stage 2 Assertions ────────────────────────────────────────────────

    # Article must be persisted
    saved_article = await article_repo.get_by_url_hash(url_hash)
    assert saved_article is not None, "Article was not persisted by DedupService"
    assert saved_article.title == "AI Breakthrough Announced by Leading Research Lab"
    assert saved_article.embedding == embedding

    # ARTICLE_UNIQUE event must have been emitted
    unique_events = bus.get_events(EventType.ARTICLE_UNIQUE)
    assert len(unique_events) == 1, (
        f"Expected 1 ARTICLE_UNIQUE event, got {len(unique_events)}"
    )
    unique_payload = unique_events[0].parse_payload(ArticleUniquePayload)
    assert unique_payload.url_hash == url_hash
    assert unique_payload.embedding == embedding

    # No duplicate events
    dup_events = bus.get_events(EventType.ARTICLE_DUPLICATE)
    assert len(dup_events) == 0, "Article was incorrectly flagged as duplicate"

    # ── Stage 3: Run Clustering ───────────────────────────────────────────

    cluster_service = ClusterService(cache, cluster_repo, article_repo, bus)
    await cluster_service.process_article(unique_payload)

    # ── Stage 3 Assertions ────────────────────────────────────────────────

    # Article must now have a cluster_id
    updated_article = await article_repo.get_by_url_hash(url_hash)
    assert updated_article is not None
    assert updated_article.cluster_id is not None, "Article was not assigned to a cluster"

    # Cluster must exist in repository
    cluster = await cluster_repo.get_by_id(updated_article.cluster_id)
    assert cluster is not None, "Cluster was not persisted"
    assert cluster.article_count == 1
    assert cluster.label == "AI Breakthrough Announced by Leading Research Lab"

    # Cluster must exist in cache
    cached_cluster = await cache.get_cluster(str(cluster.id))
    assert cached_cluster is not None, "Cluster was not cached"
    assert cached_cluster["article_count"] == 1

    # ARTICLE_CLUSTERED event must have been emitted
    clustered_events = bus.get_events(EventType.ARTICLE_CLUSTERED)
    assert len(clustered_events) == 1, (
        f"Expected 1 ARTICLE_CLUSTERED event, got {len(clustered_events)}"
    )
    clustered_payload = clustered_events[0].parse_payload(ArticleClusteredPayload)
    assert clustered_payload.cluster_id == cluster.id
    assert clustered_payload.article_id == saved_article.id
    assert clustered_payload.is_new_cluster is True


@pytest.mark.asyncio
async def test_second_article_joins_existing_cluster(
    cache, article_repo, cluster_repo, bus
):
    """Two semantically similar articles should land in the same cluster.

    Key insight: Articles must have DIFFERENT text (to pass SimHash) but the
    SAME embedding vector (to guarantee cosine similarity >= threshold).
    SimHash operates on raw text shingles; embeddings are mocked independently.

    Steps:
        1. Process article A through dedup + clustering (creates cluster).
        2. Process article B (different text, same embedding) through dedup + clustering.
        3. Assert article B joined the existing cluster (is_new_cluster=False).
    """

    await cache.connect()

    # ── Article A ─────────────────────────────────────────────────────────

    content_a = (
        "Scientists have developed a revolutionary new battery technology "
        "that promises to double the energy density of lithium-ion cells "
        "while reducing charging time by 80 percent. The breakthrough uses "
        "a novel solid-state electrolyte that remains stable at high "
        "temperatures and could be commercially available within three years."
    )

    url_a = "https://techcrunch.com/2026/04/19/battery-breakthrough/"
    hash_a = _url_hash(url_a)
    embed_a = _deterministic_embedding(hash_a)

    payload_a = ExtractCompletedPayload(
        url=url_a,
        url_hash=hash_a,
        title="New Battery Tech Doubles Energy Density",
        content=content_a,
        content_length=len(content_a),
        quality_score=0.9,
    )

    dedup = DedupService(cache, article_repo, bus)
    cluster_svc = ClusterService(cache, cluster_repo, article_repo, bus)

    import numpy as np
    from unittest.mock import patch
    with patch("src.services.dedup_service.model.encode", return_value=np.array([embed_a])):
        await dedup.process_article(payload_a)

    unique_a = bus.get_events(EventType.ARTICLE_UNIQUE)[0].parse_payload(
        ArticleUniquePayload
    )
    await cluster_svc.process_article(unique_a)

    # Verify cluster created
    article_a = await article_repo.get_by_url_hash(hash_a)
    assert article_a.cluster_id is not None
    cluster_id = article_a.cluster_id

    # ── Article B (different text to pass SimHash, same embedding to merge) ──

    # Completely different surface text → distinct SimHash fingerprint
    content_b = (
        "The global automotive industry faces a pivotal transformation as "
        "electric vehicle manufacturers announce partnerships with major "
        "research institutions to accelerate next-generation power storage "
        "solutions that could reshape transportation and grid infrastructure "
        "over the coming decade with cleaner renewable energy alternatives."
    )

    url_b = "https://arstechnica.com/2026/04/19/solid-state-battery/"
    hash_b = _url_hash(url_b)
    # Perturb article A's embedding: cosine similarity ~0.77
    # → above clustering threshold (0.75) → will merge into same cluster
    # → below dedup threshold (0.85) → will NOT be rejected as duplicate
    embed_b = [a_i + 0.06 * math.sin(i * 0.7) for i, a_i in enumerate(embed_a)]
    # Re-normalize to unit sphere
    norm_b = math.sqrt(sum(v * v for v in embed_b))
    embed_b = [v / norm_b for v in embed_b]

    payload_b = ExtractCompletedPayload(
        url=url_b,
        url_hash=hash_b,
        title="Solid-State Battery Breakthrough Promises Faster Charging",
        content=content_b,
        content_length=len(content_b),
        quality_score=0.88,
    )

    bus.clear()  # Clear events from article A

    import numpy as np
    from unittest.mock import patch
    with patch("src.services.dedup_service.model.encode", return_value=np.array([embed_b])):
        await dedup.process_article(payload_b)

    # Verify article B passed dedup (wasn't caught by SimHash or vector dedup)
    unique_events_b = bus.get_events(EventType.ARTICLE_UNIQUE)
    assert len(unique_events_b) == 1, (
        f"Expected ARTICLE_UNIQUE for article B, got {len(unique_events_b)} events. "
        f"Duplicate events: {len(bus.get_events(EventType.ARTICLE_DUPLICATE))}"
    )

    unique_b = unique_events_b[0].parse_payload(ArticleUniquePayload)
    await cluster_svc.process_article(unique_b)

    # ── Assertions ────────────────────────────────────────────────────────

    article_b = await article_repo.get_by_url_hash(hash_b)
    assert article_b.cluster_id == cluster_id, (
        "Article B should have joined article A's cluster"
    )

    cluster = await cluster_repo.get_by_id(cluster_id)
    assert cluster.article_count == 2, "Cluster should have 2 articles"

    # The ARTICLE_CLUSTERED event should indicate is_new_cluster=False
    clustered_events = bus.get_events(EventType.ARTICLE_CLUSTERED)
    assert len(clustered_events) == 1
    clustered = clustered_events[0].parse_payload(ArticleClusteredPayload)
    assert clustered.is_new_cluster is False
    assert clustered.similarity_score is not None
    assert clustered.similarity_score >= 0.75


@pytest.mark.asyncio
async def test_duplicate_article_blocked_by_dedup(
    cache, article_repo, cluster_repo, bus
):
    """An exact duplicate (same URL) must be blocked by the idempotency guard.

    The dedup service checks get_by_url_hash before processing.
    A second submission of the same URL should produce zero new events.
    """

    await cache.connect()

    url = "https://bbc.com/news/world-conflict-2026"
    url_hash = _url_hash(url)
    content = "Major conflict erupts in region as tensions escalate."
    embedding = _deterministic_embedding(url_hash)

    payload = ExtractCompletedPayload(
        url=url,
        url_hash=url_hash,
        title="Conflict Erupts",
        content=content,
        content_length=len(content),
        quality_score=0.8,
    )

    dedup = DedupService(cache, article_repo, bus)

    # First pass — should succeed
    import numpy as np
    from unittest.mock import patch
    with patch("src.services.dedup_service.model.encode", return_value=np.array([embedding])):
        await dedup.process_article(payload)
    assert bus.event_count >= 1  # ARTICLE_UNIQUE emitted

    # Second pass — exact same url_hash already in DB → silently skipped
    bus.clear()
    import numpy as np
    from unittest.mock import patch
    with patch("src.services.dedup_service.model.encode", return_value=np.array([embedding])):
        await dedup.process_article(payload)

    assert bus.event_count == 0, (
        "Duplicate submission should produce zero events"
    )


@pytest.mark.asyncio
async def test_notifier_broadcasts_cluster_event(cache, bus):
    """Verify the notifier_worker translates ARTICLE_CLUSTERED into a WS broadcast.

    This tests the notifier handler in isolation with a mocked ConnectionManager,
    confirming the exact lightweight payload schema reaches WebSocket clients.
    """

    from src.api.websocket.hub import ConnectionManager
    from src.workers.notifier_worker import run_notifier

    mock_manager = AsyncMock(spec=ConnectionManager)
    mock_bus = AsyncMock()
    mock_bus.client.is_connected = True

    await run_notifier(manager=mock_manager, bus=mock_bus)

    # Extract the handler that was registered
    _, kwargs = mock_bus.subscribe.call_args
    handler = kwargs["handler"]

    # Build the envelope exactly as cluster_service would
    cluster_id = uuid4()
    article_id = uuid4()
    envelope = EventEnvelope.create(
        event_type=EventType.ARTICLE_CLUSTERED,
        payload=ArticleClusteredPayload(
            cluster_id=cluster_id,
            article_id=article_id,
            title="AI Breakthrough Announced",
            source_domain="reuters.com",
            version=1,
            is_new_cluster=True,
        ),
        source_service="cluster_service",
    )

    await handler(envelope)

    # Verify broadcast was called with the correct lightweight schema
    mock_manager.broadcast.assert_called_once()
    sent = mock_manager.broadcast.call_args[0][0]

    assert sent["event"] == "cluster_update"
    assert sent["data"]["cluster_id"] == str(cluster_id)
    assert sent["data"]["article_id"] == str(article_id)
    assert sent["data"]["title"] == "AI Breakthrough Announced"
    assert sent["data"]["source_domain"] == "reuters.com"
    assert sent["data"]["is_new_cluster"] is True
    # Internal fields must NOT leak to WebSocket clients
    assert "version" not in sent["data"]
    assert "embedding" not in sent["data"]
    assert "similarity_score" not in sent["data"]
