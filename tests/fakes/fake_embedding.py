"""Fake embedding service for deterministic, infrastructure-free tests.

Generates repeatable 384-dimensional unit vectors by seeding a PRNG with the
SHA-256 hash of the input text.  Same text → same vector, always.  Different
texts → different (pseudo-random) vectors.

This avoids loading the ~90 MB sentence-transformers model during tests while
still producing geometrically meaningful vectors (unit-normalised, Gaussian
components) that exercise cosine-similarity code paths correctly.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import override

from src.domain.interfaces import EmbeddingService

_EMBEDDING_DIM = 384


class FakeEmbeddingService(EmbeddingService):
    """Deterministic in-memory embedding service.

    Test helpers
    ------------
    ``call_count``
        Total number of individual texts embedded (including via ``embed_batch``).
    ``texts_seen``
        Ordered list of every text that was embedded, for assertion convenience.
    """

    def __init__(self, dim: int = _EMBEDDING_DIM) -> None:
        self._dim = dim
        self.call_count: int = 0
        self.texts_seen: list[str] = []

    # ── Interface ─────────────────────────────────────────────────────────

    @override
    async def embed(self, text: str) -> list[float]:
        self.call_count += 1
        self.texts_seen.append(text)
        return self._deterministic_vector(text)

    @override
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            results.append(await self.embed(text))
        return results

    @override
    async def close(self) -> None:
        pass  # nothing to release

    # ── Test helpers ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear counters and history.  Call between tests if reusing the fake."""
        self.call_count = 0
        self.texts_seen.clear()

    # ── Internal ──────────────────────────────────────────────────────────

    def _deterministic_vector(self, text: str) -> list[float]:
        """Generate a unit-normalised float vector seeded by the text hash."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rng = random.Random(digest)

        raw = [rng.gauss(0.0, 1.0) for _ in range(self._dim)]

        # L2-normalise to unit length (same as real model output)
        magnitude = math.sqrt(sum(x * x for x in raw))
        if magnitude == 0:
            return [0.0] * self._dim
        return [x / magnitude for x in raw]
