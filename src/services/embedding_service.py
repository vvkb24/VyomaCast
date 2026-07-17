# VyomaCast: A real-time, event-driven news clustering engine.
# Copyright (C) 2026 Valluri Vamshi Krishna Bharadwaj
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Concrete implementation of the EmbeddingService interface."""

import asyncio
import logging
from typing import Optional, override

from src.config import settings
from src.domain.interfaces import EmbeddingService as IEmbeddingService

logger = logging.getLogger(__name__)


class EmbeddingService(IEmbeddingService):
    """Generates semantic embedding vectors from text using SentenceTransformer.

    Loads the model lazily and runs embedding generation in a thread pool.
    """

    _model = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self.model_name = settings.embedding_model

    async def _get_model(self):
        """Lazy loader for the SentenceTransformer model instance."""
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    logger.info("Lazy-loading SentenceTransformer model: %s", self.model_name)
                    from sentence_transformers import SentenceTransformer

                    # Load model in a separate thread pool to prevent blocking event loop
                    self._model = await asyncio.to_thread(
                        SentenceTransformer, self.model_name, device="cpu"
                    )
        return self._model

    @override
    async def embed(self, text: str) -> list[float]:
        """Generate a single embedding vector from text."""
        model = await self._get_model()
        embeds = await asyncio.to_thread(
            model.encode,
            text,
            max_length=settings.embedding_max_tokens,
            truncation=True,
        )
        return embeds.tolist()

    @override
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a single batch."""
        model = await self._get_model()
        embeds = await asyncio.to_thread(
            model.encode,
            texts,
            max_length=settings.embedding_max_tokens,
            truncation=True,
        )
        return embeds.tolist()

    @override
    async def close(self) -> None:
        """Release reference to the model instance."""
        self._model = None

    async def get_embedding(self, text: str) -> list[float]:
        """Retrieve a single embedding vector (backward compatibility wrapper)."""
        return await self.embed(text)
