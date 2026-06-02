"""Two-Stage Deduplication Engine."""

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Optional

from sentence_transformers import SentenceTransformer

from src.config import settings
from src.domain.events import (
    ArticleDuplicatePayload,
    ArticleUniquePayload,
    EventEnvelope,
    EventType,
    ExtractCompletedPayload,
)
from src.domain.interfaces import CacheStore, EventBus
from src.domain.models import Article
from src.infrastructure.database.repositories import ArticleRepository

logger = logging.getLogger(__name__)

# Model goes global to avoid huge initialisation penalty per request/worker
MODEL_NAME = "all-MiniLM-L6-v2"
model = SentenceTransformer(MODEL_NAME, device="cpu")


class DedupService:
    """Implement Two-Stage Deduplication using SimHash and Sentence-Transformers."""

    def __init__(
        self, cache: CacheStore, repository: ArticleRepository, bus: EventBus
    ) -> None:
        self.cache = cache
        self.repo = repository
        self.bus = bus
        self.BAND_COUNT = settings.simhash_bands
        self.BAND_BITS = settings.simhash_band_bits
        self.COSINE_THRESHOLD = settings.vector_cosine_threshold
        self.SIMHASH_TTL = 3 * 24 * 3600  # 72 hours
        self.EMBED_TTL = 24 * 3600  # 24 hours

    def normalize_text(self, text: str) -> str:
        """Strictly normalized: lowercase, strip, remove excessive newlines/whitespace."""
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def compute_simhash(self, text: str) -> int:
        """Computes a 128-bit SimHash of the text using 3-word shingles."""
        words = text.split()
        if not words:
            return 0

        v = [0] * 128
        for i in range(max(1, len(words) - 2)):
            shingle = " ".join(words[i : i + 3])
            shingle_hash = int(hashlib.md5(shingle.encode("utf-8")).hexdigest(), 16)
            for b in range(128):
                if (shingle_hash >> b) & 1:
                    v[b] += 1
                else:
                    v[b] -= 1

        fingerprint = 0
        for b in range(128):
            if v[b] > 0:
                fingerprint |= 1 << b
        return fingerprint

    def get_bands(self, simhash_val: int) -> dict[int, str]:
        """Split 128-bit hash into 8 bands of 16-bits each, hex encoded."""
        bands = {}
        mask = 0xFFFF
        for i in range(self.BAND_COUNT):
            band_int = (simhash_val >> (i * self.BAND_BITS)) & mask
            bands[i] = hex(band_int)[2:]
        return bands

    def hamming_distance(self, hash1: int, hash2: int) -> int:
        """Compute bitwise Hamming distance between two 128-bit integers."""
        return bin(hash1 ^ hash2).count("1")

    async def _compute_embedding(self, text: str) -> Optional[list[float]]:
        """Generate 384-dimensional dense representation of text.

        CPU-bound wrapper offloads to standard ThreadPool.
        Strictly truncates to max 512 tokens.
        """
        try:
            embeds = await asyncio.to_thread(
                model.encode, text, max_length=512, truncation=True
            )
            return embeds.tolist()
        except Exception as e:
            logger.warning("Embedding generation failed: %s", e)
            return None

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        import numpy as np

        a = np.array(vec1)
        b = np.array(vec2)
        score = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        return min(1.0, score)

    async def process_article(self, payload: ExtractCompletedPayload) -> None:
        """Execute the two-stage dedup checking against Cache and persistence stores."""
        url_hash = payload.url_hash

        # 1. Deterministic Idempotency Guard
        existing_article = await self.repo.get_by_url_hash(url_hash)
        if existing_article:
            logger.info("Url hash '%s' already exists in postgres. Skipping.", url_hash)
            return

        # 2. Extract & Normalize Text
        norm_text = self.normalize_text(payload.content)

        # 3. Stage 1: SimHash MVP Calculation
        logger.debug("[TRACE] Computing SimHash bands...")
        simhash_val = await asyncio.to_thread(self.compute_simhash, norm_text)
        bands = self.get_bands(simhash_val)

        # Stage 1 execution using CacheStore Sliding Window
        # Note: the stored key is `{url_hash}:{full_hash_hex}` to enable inline Hamming distance checks
        # without expensive external lookups inside the hot-path loop.
        logger.debug("[TRACE] Checking SimHash bands in Redis...")
        collisions = await self.cache.check_simhash_bands(bands)
        logger.debug("[TRACE] Cache check returned %d collisions", len(collisions))

        is_simhash_duplicate = False
        duplicate_of = ""
        hamming_dist = -1

        for collision in collisions:
            parts = collision.split(":")
            if len(parts) == 2:
                cand_url_hash, cand_sim_hex = parts
                cand_simhash = int(cand_sim_hex, 16)

                dist = self.hamming_distance(simhash_val, cand_simhash)
                # Ensure > 3 distance avoids dropping false positives
                if dist <= 3:
                    is_simhash_duplicate = True
                    duplicate_of = cand_url_hash
                    hamming_dist = dist
                    break

        if is_simhash_duplicate:
            logger.info(
                "Stage 1 (SimHash) Duplicate detected for %s (dist: %d)",
                url_hash,
                hamming_dist,
            )
            dup_ev = ArticleDuplicatePayload(
                url=payload.url,
                url_hash=url_hash,
                duplicate_of=duplicate_of,
                stage="simhash",
                hamming_distance=hamming_dist,
            )
            await self.bus.publish(
                EventType.ARTICLE_DUPLICATE,
                EventEnvelope.create(
                    EventType.ARTICLE_DUPLICATE, dup_ev, "dedup_service"
                ),
            )
            return

        # 4. Stage 2: Heavy Semantic Embedding (Sentence-Transformers)
        embedding = await self._compute_embedding(payload.content)
        logger.debug("[TRACE] Embedding computed. Starting exact vector duplicate check...")
        if not embedding:
            logger.warning(
                "Embedding timed out or failed for %s. Dropping candidate.", url_hash
            )
            return

        # Cache early to prevent concurrent race-condition drops for massive duplicated feeds
        await self.cache.cache_embedding(url_hash, embedding, self.EMBED_TTL)

        is_vector_duplicate = False
        vector_sim_score = 0.0

        # Tier 4a: Cache Memory Fallback (Scoped Top Timeline Elements)
        # Prevent database checks for purely hot-cycle replication feeds
        timeline_hashes = await self.cache.get_timeline(limit=100)
        recent_embeddings = await self.cache.get_cached_embeddings(timeline_hashes)

        for cand_hash, cand_vec in recent_embeddings.items():
            if cand_hash == url_hash:
                continue
            sim = self.cosine_similarity(embedding, cand_vec)
            if sim > self.COSINE_THRESHOLD:
                is_vector_duplicate = True
                vector_sim_score = sim
                duplicate_of = cand_hash
                break

        # Tier 4b: DB Query Fallback bounded by PostGres operations
        if not is_vector_duplicate:
            # Note limit=1 guarantees efficiency check
            search_results = await self.repo.search_by_embedding(
                embedding, limit=1, threshold=self.COSINE_THRESHOLD
            )
            # The search implicitly uses vector cosine distance ops via pgvector (ArticleRow.embedding.cosine_distance)
            if search_results:
                top_match = search_results[0]
                is_vector_duplicate = True
                vector_sim_score = top_match.similarity_score
                duplicate_of = str(top_match.article_id)

        if is_vector_duplicate:
            logger.info(
                "Stage 2 (Vector) Duplicate detected for %s (sim: %.3f)",
                url_hash,
                vector_sim_score,
            )
            dup_ev = ArticleDuplicatePayload(
                url=payload.url,
                url_hash=url_hash,
                duplicate_of=duplicate_of,
                stage="vector",
                similarity_score=vector_sim_score,
            )
            await self.bus.publish(
                EventType.ARTICLE_DUPLICATE,
                EventEnvelope.create(
                    EventType.ARTICLE_DUPLICATE, dup_ev, "dedup_service"
                ),
            )
            return

        # 5. Pipeline Registration Uniques Only
        # Track 128-bit simhash in bands for fast-cache checks, packed inside string.
        compound_member = f"{url_hash}:{hex(simhash_val)[2:]}"
        await self.cache.add_simhash_bands(
            compound_member, bands, self.SIMHASH_TTL
        )

        # Explicitly append to timeline global cache for faster Cosine checks futurely
        await self.cache.add_to_timeline(url_hash, datetime.now(UTC).timestamp())

        article = Article(
            url=payload.url,
            url_hash=payload.url_hash,
            feed_id=payload.feed_id,
            title=payload.title,
            content=payload.content,
            authors=payload.authors,
            language=payload.language,
            top_image_url=payload.top_image_url,
            simhash=simhash_val & 0xFFFFFFFFFFFFFFFF,  # Clamp BigInt limit on insert to avoid PSQL Overflow
            embedding=embedding,
            quality_score=payload.quality_score,
            extraction_method=payload.extraction_method,
            content_length=payload.content_length,
            published_at=payload.published_at or datetime.now(UTC),
            extracted_at=datetime.now(UTC),
        )

        logger.debug("[TRACE] Saving article to PostgreSQL...")
        saved = await self.repo.save(article)
        logger.debug("[TRACE] Article persistently saved to Postgres.")
        
        # Track dirty cache
        await self.cache.mark_dirty("articles", url_hash)

        # Notify Clustering Layer of the Verified Semantic uniqueness
        success_payload = ArticleUniquePayload(
            url=saved.url,
            url_hash=saved.url_hash,
            feed_id=saved.feed_id,
            title=saved.title,
            content=saved.content,
            content_length=saved.content_length,
            authors=saved.authors,
            published_at=saved.published_at,
            language=saved.language,
            top_image_url=saved.top_image_url,
            quality_score=saved.quality_score,
            extraction_method=saved.extraction_method,
            simhash=simhash_val,  # Publish the full 128-bit hash locally to avoid dropping bits
            embedding=embedding,
        )

        logger.debug("[TRACE] Publishing ARTICLE_UNIQUE to NATS bus.")
        await self.bus.publish(
            EventType.ARTICLE_UNIQUE,
            EventEnvelope.create(
                EventType.ARTICLE_UNIQUE, success_payload, "dedup_service"
            ),
        )
