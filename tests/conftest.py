"""Shared pytest fixtures for the VyomaCast test suite.

Fixtures here are available to **all** test modules without explicit import.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

# Ensure tests always use a predictable configuration.
# Set before any src imports so pydantic-settings picks them up.
os.environ.setdefault("VYOMACAST_DATABASE_URL", "postgresql+asyncpg://vyomacast:vyomacast@localhost:5433/vyomacast")
os.environ.setdefault("VYOMACAST_REDIS_URL", "redis://localhost:6380/15")
os.environ.setdefault("VYOMACAST_NATS_URL", "nats://localhost:4223")
os.environ.setdefault("VYOMACAST_LOG_LEVEL", "DEBUG")
os.environ.setdefault("VYOMACAST_LOG_FORMAT", "console")


@pytest.fixture()
def sample_url() -> str:
    """A realistic news article URL for testing."""
    return "https://www.reuters.com/world/us/example-article-2026-04-18/"


@pytest.fixture()
def sample_url_hash(sample_url: str) -> str:
    """The canonical url_hash for sample_url."""
    from src.domain.models import compute_url_hash

    return compute_url_hash(sample_url)


@pytest.fixture()
def sample_embedding() -> list[float]:
    """A deterministic 384-dimensional embedding vector for testing."""
    import math

    # Use sine wave to produce a realistic-looking vector.
    return [math.sin(i * 0.1) * 0.5 for i in range(384)]


@pytest.fixture()
def sample_centroid(sample_embedding: list[float]) -> list[float]:
    """A centroid vector (same as sample_embedding for single-article cluster)."""
    return sample_embedding[:]


@pytest.fixture()
def now_utc() -> datetime:
    """Current UTC timestamp for deterministic test assertions."""
    return datetime.now(UTC)


@pytest.fixture()
def sample_uuid():
    """A fresh UUID v4 for testing."""
    return uuid4()
