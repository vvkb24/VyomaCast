"""In-memory test fakes for VyomaCast infrastructure interfaces.

Every fake in this package strictly implements the abstract interfaces
defined in ``src.domain.interfaces``.  They use standard Python data
structures (dicts, lists, sets) — zero infrastructure dependencies.

Usage in tests::

    from tests.fakes import (
        FakeArticleRepository,
        FakeCacheStore,
        FakeClusterRepository,
        FakeEmbeddingService,
        FakeEventBus,
        FakeFeedRepository,
    )
"""

from tests.fakes.fake_cache import FakeCacheStore
from tests.fakes.fake_embedding import FakeEmbeddingService
from tests.fakes.fake_event_bus import FakeEventBus
from tests.fakes.fake_repository import (
    FakeArticleRepository,
    FakeClusterRepository,
    FakeFeedRepository,
)

__all__ = [
    "FakeArticleRepository",
    "FakeCacheStore",
    "FakeClusterRepository",
    "FakeEmbeddingService",
    "FakeEventBus",
    "FakeFeedRepository",
]
