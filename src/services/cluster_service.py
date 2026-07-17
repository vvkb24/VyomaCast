"""Real-Time Clustering Engine."""

import logging
import urllib.parse
from datetime import UTC, datetime
from typing import Optional
from uuid import uuid4

import numpy as np

from src.domain.events import (
    ArticleClusteredPayload,
    ArticleUniquePayload,
    EventEnvelope,
    EventType,
)
from src.domain.exceptions import RetryableError, PermanentError
from src.domain.interfaces import CacheStore, EventBus
from src.domain.models import Article, Cluster, ClusterStatus
from src.infrastructure.database.repositories import ArticleRepository, ClusterRepository

logger = logging.getLogger(__name__)


class ClusterService:
    def __init__(
        self,
        cache: CacheStore,
        cluster_repo: ClusterRepository,
        article_repo: ArticleRepository,
        bus: EventBus,
    ) -> None:
        self.cache = cache
        self.cluster_repo = cluster_repo
        self.article_repo = article_repo
        self.bus = bus
        self.SIM_THRESHOLD = 0.75

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        a = np.array(vec1)
        b = np.array(vec2)
        score = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        return min(1.0, score)

    def l2_normalize(self, vec: list[float]) -> list[float]:
        a = np.array(vec)
        return (a / np.linalg.norm(a)).tolist()

    async def process_article(self, payload: ArticleUniquePayload) -> None:
        """Assign article to an existing cluster or create a new one."""
        article_embedding = payload.embedding
        if not article_embedding or len(article_embedding) == 0:
            raise PermanentError(f"Missing or empty embedding for {payload.url_hash}.")
        if all(v == 0.0 for v in article_embedding):
            raise PermanentError(f"Zero-vector embedding provided for {payload.url_hash}.")

        # 1. Cache-First Lookup: Bounded subset of TOP 100 active clusters
        clusters_list = await self.cache.get_active_clusters(limit=100)
        centroids_dict = {}
        for cdata in clusters_list:
            c_id = cdata.get("id")
            cent = cdata.get("centroid")
            if c_id and cent and len(cent) == 384:
                centroids_dict[c_id] = (cent, cdata.get("article_count", 0))

        best_cluster_id: Optional[str] = None
        best_score = -1.0

        for c_id, (centroid, _) in centroids_dict.items():
            sim = self.cosine_similarity(article_embedding, centroid)
            if sim > best_score:
                best_score = sim
                best_cluster_id = c_id

        cluster: Optional[Cluster] = None
        cluster_id_to_use = None
        is_new_cluster = False

        if best_cluster_id and best_score >= self.SIM_THRESHOLD:
            # Join existing cluster
            logger.info("Found matching cluster %s with score %.3f", best_cluster_id, best_score)
            cached_data = await self.cache.get_cluster(best_cluster_id)
            if cached_data:
                # Convert back to domain model for reliable update logic
                cluster = Cluster(**cached_data)
        
        if cluster:
            # We must use optimistic concurrency. 
            # If we increment version and another worker also did, DB ignores ours.
            cluster.version += 1
            cluster.article_count += 1
            
            # Weighted average for strictly L2-normalized centroid maintenance
            old_count = cluster.article_count - 1
            weighted_centroid = np.array(cluster.centroid) * old_count
            new_centroid = weighted_centroid + np.array(article_embedding)
            avg_centroid = new_centroid / cluster.article_count
            
            cluster.centroid = self.l2_normalize(avg_centroid.tolist())
            cluster.last_activity = datetime.now(UTC)
            cluster.updated_at = datetime.now(UTC)
            cluster_id_to_use = cluster.id

            # Extract source domain safely
            domain = urllib.parse.urlparse(payload.url).netloc
            if domain not in cluster.top_sources:
                cluster.top_sources.append(domain)
                # Keep top sources bounded 
                cluster.top_sources = cluster.top_sources[:10]

        else:
            # Create completely new cluster
            is_new_cluster = True
            logger.info("No matching cluster found. Creating new cluster for %s", payload.url_hash)
            
            cluster = Cluster(
                id=uuid4(),
                label=payload.title,
                centroid=self.l2_normalize(article_embedding),
                article_count=1,
                top_sources=[urllib.parse.urlparse(payload.url).netloc],
                version=1,
            )
            cluster_id_to_use = cluster.id

        # 2. Extract specific Article Info 
        # Convert ArticleUniquePayload to ClusterArticleInfo
        domain = urllib.parse.urlparse(payload.url).netloc
        
        # We need the article ID. We can retrieve it by url_hash from DB!
        article = await self.article_repo.get_by_url_hash(payload.url_hash)
        if not article:
            logger.warning("Article %s not found in DB! Cannot bind to cluster.", payload.url_hash)
            return

        target_version = cluster.version
        target_last_activity = cluster.last_activity

        # 3 & 4. Atomic Synchronous Saves
        # We ensure both the cluster and the article are saved in a single transaction.
        session_factory = getattr(self.cluster_repo, "_session_factory", None)
        
        async def _save_transaction(session=None):
            saved_cluster = await self.cluster_repo.save(cluster, session=session) if session else await self.cluster_repo.save(cluster)
            
            # Optimistic Concurrency check using timestamps / version boundaries 
            if not is_new_cluster:
                # In our PostgreSQL repository, a blocked update returns the old row.
                # Thus saved_cluster.last_activity will be older/different than our local.
                # Similarly, if another worker bumped the version exactly as we did, we can 
                # detect difference in last_activity.
                if saved_cluster.version != target_version or saved_cluster.last_activity != target_last_activity:
                    logger.warning("Optimistic concurrency failure detected for cluster %s (target v: %d). Retrying...", cluster.id, target_version)
                    raise RetryableError(
                        f"Cluster {cluster.id} updated concurrently. Retry required.",
                        details={"target_v": target_version, "saved_v": saved_cluster.version}
                    )

            # Save article mapped to cluster
            article.cluster_id = cluster_id_to_use
            await self.article_repo.save(article, session=session) if session else await self.article_repo.save(article)
            
            return saved_cluster

        from sqlalchemy.ext.asyncio import async_sessionmaker
        if session_factory and isinstance(session_factory, async_sessionmaker):
            async with session_factory() as session:
                async with session.begin():
                    saved_cluster = await _save_transaction(session)
        else:
            saved_cluster = await _save_transaction()

        # Update Cache ensuring fast-lane lookups are maintained
        cache_data = {
            "id": str(cluster.id),
            "label": cluster.label,
            "centroid": cluster.centroid,
            "article_count": cluster.article_count,
            "decay_score": cluster.decay_score,
            "last_activity": cluster.last_activity.replace(tzinfo=None).isoformat() + "Z",
            "top_sources": cluster.top_sources,
            "status": cluster.status.value,
            "version": cluster.version,
            "created_at": cluster.created_at.replace(tzinfo=None).isoformat() + "Z",
            "updated_at": cluster.updated_at.replace(tzinfo=None).isoformat() + "Z",
        }
        await self.cache.set_cluster(str(cluster.id), cache_data)
        await self.cache.mark_dirty("clusters", str(cluster.id))
        await self.cache.mark_dirty("articles", article.url_hash)

        # 5. Event Publishing!
        ev = ArticleClusteredPayload(
            cluster_id=cluster.id,
            article_id=article.id,
            title=article.title,
            source_domain=domain,
            version=cluster.version,
            is_new_cluster=is_new_cluster,
            similarity_score=best_score if not is_new_cluster else None,
        )
        await self.bus.publish(
            EventType.ARTICLE_CLUSTERED,
            EventEnvelope.create(EventType.ARTICLE_CLUSTERED, ev, "cluster_service")
        )

        logger.info("Successfully clustered %s to %s", article.id, cluster.id)
