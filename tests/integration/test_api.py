"""Integration tests for the FastAPI web server leveraging in-memory fakes."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_article_repo, get_cluster_repo, get_embedding_service, get_session
from src.api.main import app
from src.domain.models import Article, Cluster, ClusterStatus
from tests.fakes.fake_repository import FakeArticleRepository, FakeClusterRepository
from unittest.mock import AsyncMock


@pytest.fixture
def fake_repos():
    article_repo = FakeArticleRepository()
    cluster_repo = FakeClusterRepository()

    # Pre-seed Data
    c1 = Cluster(
        id=uuid4(),
        label="Test Cluster Alpha",
        centroid=[0.5] * 384,
        article_count=1,
        top_sources=["test.com"],
        status=ClusterStatus.ACTIVE,
        version=1,
    )
    cluster_repo._by_id[c1.id] = c1

    a1 = Article(
        id=uuid4(),
        url="https://test.com/1",
        url_hash="hash1",
        title="Breaking Tech News",
        content="Testing the API routes successfully.",
        simhash=123,
        cluster_id=c1.id,
    )
    article_repo._by_id[a1.id] = a1
    article_repo._by_url_hash[a1.url_hash] = a1

    # Mock EmbeddingService
    mock_embedding = AsyncMock()
    mock_embedding.get_embedding.return_value = [0.5] * 384

    # Overrides 
    app.dependency_overrides[get_article_repo] = lambda: article_repo
    app.dependency_overrides[get_cluster_repo] = lambda: cluster_repo
    app.dependency_overrides[get_embedding_service] = lambda: mock_embedding
    
    # We yield the repos to let tests modify state if desired
    yield article_repo, cluster_repo, mock_embedding
    
    # Teardown
    app.dependency_overrides.clear()


@pytest.fixture
async def client(fake_repos):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_get_articles_returns_paginated_response(client):
    response = await client.get("/api/v1/articles?limit=5&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    
    # 6. Verify internal fields are safely excluded
    item = data[0]
    assert "version" not in item
    assert "embedding" not in item
    assert "simhash" not in item
    assert item["title"] == "Breaking Tech News"


@pytest.mark.asyncio
async def test_pagination_limits_enforced_strictly(client):
    response = await client.get("/api/v1/articles?limit=500")
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert data["detail"] == "Payload validation failed"


@pytest.mark.asyncio
async def test_get_article_not_found(client):
    bad_id = str(uuid4())
    response = await client.get(f"/api/v1/articles/{bad_id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Article not found"


@pytest.mark.asyncio
async def test_articles_search_returns_success(client, fake_repos):
    article_repo, _, mock_embedding = fake_repos
    # Fake search results since standard fake repo just returns empty lists for search_by_embedding
    # Wait, fake_repository doesn't actually implement search_by_embedding correctly (it returns empty).
    # We can patch it natively here:
    from src.domain.models import SearchResult
    a1 = list(article_repo._by_id.values())[0]
    
    async def mock_search(*args, **kwargs):
        return [SearchResult(
            article_id=a1.id,
            title=a1.title,
            content_preview=a1.content[:50],
            similarity_score=0.9,
            cluster_id=a1.cluster_id,
            published_at=a1.published_at,
            source_domain="test.com"
        )]
    
    article_repo.search_by_embedding = mock_search

    payload = {"query": "Latest tech developments"}
    response = await client.post("/api/v1/articles/search", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["url"] == "https://test.com/1"
    
    # Verify EmbeddingService was properly called
    mock_embedding.get_embedding.assert_called_once_with("Latest tech developments")


@pytest.mark.asyncio
async def test_articles_search_empty_query_returns_permanent_error_clean_400(client):
    payload = {"query": "   "} # Whitespace 
    response = await client.post("/api/v1/articles/search", json=payload)
    
    assert response.status_code == 400
    # Verifies internal python leakages are prevented and only clean JSON with `detail` is surfaced
    assert response.json() == {"detail": "Query cannot be entirely whitespace."}


@pytest.mark.asyncio
async def test_articles_search_rejects_oversized_query(client):
    payload = {"query": "a" * 1500} # Exceeds 1000 characters limit
    response = await client.post("/api/v1/articles/search", json=payload)
    
    assert response.status_code == 400
    assert "Payload validation failed" in response.json()["detail"]


@pytest.mark.asyncio
async def test_clusters_endpoint_returns_details_excluding_internals(client, fake_repos):
    _, cluster_repo, _ = fake_repos
    c1 = list(cluster_repo._by_id.values())[0]
    
    response = await client.get(f"/api/v1/clusters/{c1.id}")
    assert response.status_code == 200
    data = response.json()
    
    assert data["label"] == "Test Cluster Alpha"
    assert "version" not in data
    assert "centroid" not in data
    assert "decay_score" not in data
    
    # Associated articles embedded
    assert "articles" in data
    assert len(data["articles"]) == 1
    assert data["articles"][0]["title"] == "Breaking Tech News"
