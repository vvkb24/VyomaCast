"""Unit tests for the Real-Time Clustering Engine."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.domain.events import ArticleUniquePayload, EventType
from src.domain.exceptions import RetryableError
from src.domain.models import Article, Cluster, ClusterStatus
from src.services.cluster_service import ClusterService


@pytest.fixture
def mocks():
    cache = AsyncMock()
    article_repo = AsyncMock()
    cluster_repo = AsyncMock()
    bus = AsyncMock()
    # Provide safe defaults
    cache.get_all_cluster_centroids.return_value = {}
    return cache, cluster_repo, article_repo, bus


@pytest.fixture
def payload():
    return ArticleUniquePayload(
        url="https://test.com/news/123",
        url_hash="abcdef123456",
        title="Testing Real-Time Clustering Engines",
        content="This is the successfully Extracted and Duplication-Guarded text.",
        content_length=70,
        authors=[],
        published_at=datetime.now(UTC),
        language="en",
        top_image_url="http://test.com/img.jpg",
        quality_score=0.9,
        extraction_method="trafilatura",
        simhash=1234567890,
        embedding=[0.5] * 384,
    )


@pytest.fixture
def dummy_article(payload):
    return Article(
        id=uuid4(),
        url=payload.url,
        url_hash=payload.url_hash,
        title=payload.title,
        content=payload.content,
        simhash=payload.simhash,
    )


@pytest.mark.asyncio
async def test_article_with_no_similar_clusters_creates_new(mocks, payload, dummy_article):
    cache, cluster_repo, article_repo, bus = mocks
    service = ClusterService(cache, cluster_repo, article_repo, bus)
    
    # Empty cache means no similar clusters
    cache.get_all_cluster_centroids.return_value = {}
    article_repo.get_by_url_hash.return_value = dummy_article

    # Assume cluster_repo passes successfully
    cluster_repo.save.side_effect = lambda c: c

    await service.process_article(payload)

    # Asserts
    cluster_repo.save.assert_called_once()
    saved_cluster = cluster_repo.save.call_args[0][0]
    
    assert saved_cluster.article_count == 1
    assert "test.com" in saved_cluster.top_sources
    assert len(saved_cluster.centroid) == 384

    # Event Asserts
    bus.publish.assert_called_once()
    publish_args = bus.publish.call_args[0]
    assert publish_args[0] == EventType.ARTICLE_CLUSTERED
    envelope = publish_args[1]
    assert envelope.payload["is_new_cluster"] is True
    
    assert article_repo.save.call_args[0][0].cluster_id == saved_cluster.id


@pytest.mark.asyncio
async def test_high_similarity_updates_existing_cluster_centroid(mocks, payload, dummy_article):
    cache, cluster_repo, article_repo, bus = mocks
    service = ClusterService(cache, cluster_repo, article_repo, bus)

    # Existing cluster with exactly 0.5 embeds matches 1.0 > 0.75 threshold
    existing_c_id = str(uuid4())
    existing_centroid = [0.5] * 384
    cache.get_active_clusters.return_value = [
        {
            "id": existing_c_id,
            "centroid": existing_centroid,
            "article_count": 2
        }
    ]
    
    cache.get_cluster.return_value = {
        "id": existing_c_id,
        "label": "Old Cluster",
        "centroid": existing_centroid,
        "article_count": 2,
        "top_sources": ["a.com", "b.com"],
        "status": "active",
        "version": 1,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "decay_score": 1.0,
        "last_activity": "2024-01-01T00:00:00Z",
    }
    
    article_repo.get_by_url_hash.return_value = dummy_article

    # Assume cluster_repo saves correctly mirroring exactly what is sent
    cluster_repo.save.side_effect = lambda c: c

    await service.process_article(payload)

    # Asserts Updating
    cluster_repo.save.assert_called_once()
    saved_cluster = cluster_repo.save.call_args[0][0]
    
    assert saved_cluster.id == frozenset([existing_c_id]) or str(saved_cluster.id) == existing_c_id
    assert saved_cluster.version == 2
    assert saved_cluster.article_count == 3
    assert "test.com" in saved_cluster.top_sources

    # Event Asserts
    bus.publish.assert_called_once()
    publish_args = bus.publish.call_args[0]
    assert publish_args[0] == EventType.ARTICLE_CLUSTERED
    envelope = publish_args[1]
    assert envelope.payload["is_new_cluster"] is False
    assert envelope.payload["cluster_id"] == existing_c_id
    
    assert str(article_repo.save.call_args[0][0].cluster_id) == existing_c_id


@pytest.mark.asyncio
async def test_cluster_version_conflict_raises_retryable_error(mocks, payload, dummy_article):
    cache, cluster_repo, article_repo, bus = mocks
    service = ClusterService(cache, cluster_repo, article_repo, bus)

    existing_c_id = str(uuid4())
    cache.get_active_clusters.return_value = [
        {"id": existing_c_id, "centroid": [0.5] * 384, "article_count": 1}
    ]
    cache.get_cluster.return_value = {
        "id": existing_c_id, "label": "Old Cluster", "centroid": [0.5] * 384,
        "article_count": 1, "top_sources": [], "status": "active",
        "version": 2, "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "decay_score": 1.0, "last_activity": "2024-01-01T00:00:00Z"
    }

    article_repo.get_by_url_hash.return_value = dummy_article

    # Simulate pessimistic overwrite by another worker logic locally 
    # Returning a database-stored cluster instance where the version did NOT update 
    def mock_db_return(c: Cluster):
        import copy
        saved = copy.deepcopy(c)
        saved.version = 5  # Conflicted Database Override Simulation
        return saved
        
    cluster_repo.save.side_effect = mock_db_return

    with pytest.raises(RetryableError) as exc_info:
        await service.process_article(payload)
    
    assert "Cluster" in str(exc_info.value)
    assert "updated concurrently" in str(exc_info.value)

@pytest.mark.asyncio
async def test_tie_breaking_picks_highest_similarity_cluster(mocks, payload, dummy_article):
    cache, cluster_repo, article_repo, bus = mocks
    service = ClusterService(cache, cluster_repo, article_repo, bus)

    # Two competing clusters. 
    # To tie break, we have article vector = [0.5]*384
    # c1 = exactly matches [0.5]*384 -> sim = 1.0 (Wait, cosine is 1.0)
    # c2 = [0.4]*384 -> sim = 1.0 since it's collinear.
    # Let's use normalized vectors.
    c1_id = str(uuid4())
    c2_id = str(uuid4())
    
    vec1 = [0.5]*384
    vec2 = [0.0]*384
    vec2[0] = 1.0
    
    payload.embedding = vec1

    cache.get_active_clusters.return_value = [
        {"id": c1_id, "centroid": vec2, "article_count": 1}, # Lower match
        {"id": c2_id, "centroid": vec1, "article_count": 1}, # Exact match
    ]
    
    cache.get_cluster.return_value = {
        "id": c2_id, "label": "Winner", "centroid": vec1,
        "article_count": 1, "top_sources": [], "status": "active",
        "version": 1, "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "decay_score": 1.0, "last_activity": "2024-01-01T00:00:00Z"
    }

    article_repo.get_by_url_hash.return_value = dummy_article
    cluster_repo.save.side_effect = lambda c: c

    await service.process_article(payload)

    # Asserts
    cluster_repo.save.assert_called_once()
    saved_cluster = cluster_repo.save.call_args[0][0]
    
    # Assert it joined c2
    assert str(saved_cluster.id) == c2_id



@pytest.mark.asyncio
async def test_invalid_embedding_triggers_term_signal(mocks, payload, dummy_article):
    """Proves the NATS contract: PermanentError -> TERM instead of ACK."""
    cache, cluster_repo, article_repo, bus = mocks
    service = ClusterService(cache, cluster_repo, article_repo, bus)

    # 1. Provide an explicitly invalid embedding (Zero-Vector triggers PermanentError)
    payload.embedding = [0.0] * 384

    from src.infrastructure.messaging.nats_bus import NatsEventBus
    from src.domain.events import EventEnvelope, EventType
    from src.workers.cluster_worker import ValidationError
    from src.domain.exceptions import PermanentError
    import json
    
    # 2. Replicate Worker's Handler mapping logic
    async def worker_handler(envelope: EventEnvelope) -> None:
        try:
            pl = envelope.parse_payload(ArticleUniquePayload)
        except ValidationError as e:
            raise PermanentError("Poison pill schema") from e
        await service.process_article(pl)
        
    # 3. Simulate an incoming JetStream message inside NatsEventBus 
    mock_msg = AsyncMock()
    import pydantic_core
    # Note EventEnvelope.create natively outputs json-safe dicts for the payload arg.
    env = EventEnvelope.create(EventType.ARTICLE_UNIQUE, payload, "test_client")
    mock_msg.data = env.model_dump_json().encode("utf-8")

    nats_bus = NatsEventBus()
    
    # 4. Trigger the message router simulating actual production consumption
    await nats_bus._message_handler(mock_msg, worker_handler)

    # 5. Strictly verify TERM was invoked blocking the loop indefinitely
    mock_msg.term.assert_called_once()
    mock_msg.ack.assert_not_called()
    mock_msg.nak.assert_not_called()
