"""FastAPI routes for Articles and Clusters."""

import asyncio
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.api.dependencies import ArticleRepoDep, ClusterRepoDep, EmbeddingDep
from src.domain.exceptions import PermanentError

router = APIRouter()

# ────────────────────────────────────────────────────────────────────────────
# API Response Models (Safe, user-facing only)
# ────────────────────────────────────────────────────────────────────────────

class SafeArticleResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    url: str
    title: str
    content: str
    content_length: int
    authors: list[str]
    published_at: Optional[datetime]
    language: str
    top_image_url: Optional[str]
    quality_score: float
    cluster_id: Optional[UUID]
    created_at: datetime

class SafeClusterResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    label: str
    article_count: int
    top_sources: list[str]
    status: str
    created_at: datetime
    updated_at: datetime

class ClusterDetailsResponse(SafeClusterResponse):
    articles: list[SafeArticleResponse]

class SearchQuery(BaseModel):
    query: str = Field(..., max_length=1000)

# ────────────────────────────────────────────────────────────────────────────
# Articles Endpoints

@router.get("/articles", response_model=list[SafeArticleResponse])
async def list_articles(
    article_repo: ArticleRepoDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Retrieve paginated recent articles."""
    articles = await article_repo.get_recent(limit=limit, offset=offset)
    return articles


@router.get("/articles/{article_id}", response_model=SafeArticleResponse)
async def get_article(article_repo: ArticleRepoDep, article_id: UUID):
    """Retrieve full article details by ID."""
    article = await article_repo.get_by_id(article_id)
    if not article:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    return article


@router.post("/articles/search", response_model=list[SafeArticleResponse])
async def search_articles(
    payload: SearchQuery,
    article_repo: ArticleRepoDep,
    embedding_service: EmbeddingDep,
):
    """Semantic vector search using PgVector's HNSW index."""
    if len(payload.query.strip()) == 0:
        raise PermanentError("Query cannot be entirely whitespace.")

    try:
        # Strict timeout guard on generation to prevent service stalling
        embedding = await asyncio.wait_for(
            embedding_service.get_embedding(payload.query), timeout=5.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, 
            detail="Embedding generation timeout"
        )
    
    results = await article_repo.search_by_embedding(embedding, limit=10, threshold=0.3)
    
    articles = []
    for r in results:
        art = await article_repo.get_by_id(r.article_id)
        if art:
            articles.append(art)
            
    return articles


# ────────────────────────────────────────────────────────────────────────────
# Clusters Endpoints
# ────────────────────────────────────────────────────────────────────────────

@router.get("/clusters", response_model=list[SafeClusterResponse])
async def list_clusters(
    cluster_repo: ClusterRepoDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Retrieve paginated active clusters."""
    clusters = await cluster_repo.get_active(limit=limit, offset=offset)
    # The domain cluster model has .status as an enum. Convert string internally!
    return [
        SafeClusterResponse(
            id=c.id,
            label=c.label,
            article_count=c.article_count,
            top_sources=c.top_sources,
            status=c.status.value,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in clusters
    ]


@router.get("/clusters/{cluster_id}", response_model=ClusterDetailsResponse)
async def get_cluster_details(
    cluster_repo: ClusterRepoDep,
    article_repo: ArticleRepoDep,
    cluster_id: UUID,
):
    """Retrieve cluster metadata alongside all its resolved articles."""
    cluster = await cluster_repo.get_by_id(cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")

    articles = await article_repo.get_by_cluster_id(cluster_id, limit=100)

    return ClusterDetailsResponse(
        id=cluster.id,
        label=cluster.label,
        article_count=cluster.article_count,
        top_sources=cluster.top_sources,
        status=cluster.status.value,
        created_at=cluster.created_at,
        updated_at=cluster.updated_at,
        articles=articles,
    )
