"""Worker entry point for the Real-Time Clustering Engine."""

import asyncio
import logging
import signal
import sys
from typing import Any, Optional

from pydantic import ValidationError

from src.domain.events import EventEnvelope, EventType, ArticleUniquePayload
from src.domain.exceptions import PermanentError
from src.infrastructure.cache.redis_cache import RedisCacheStore
from src.infrastructure.database.engine import create_db_engine, create_session_factory
from src.infrastructure.database.repositories import PgArticleRepository, PgClusterRepository
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.services.cluster_service import ClusterService

logger = logging.getLogger(__name__)

_bus: Optional[NatsEventBus] = None
_cache: Optional[RedisCacheStore] = None


async def run_worker() -> None:
    """Initialize dependencies and subscribe to ArticleUnique events."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    logger.info("Starting Clustering Engine Worker...")

    global _bus, _cache
    _bus = NatsEventBus()
    _cache = RedisCacheStore()

    try:
        await _bus.connect()
        await _cache.connect()
    except Exception as e:
        logger.critical("Failed to connect to Infrastructure: %s", e)
        sys.exit(1)

    engine = create_db_engine()
    session_maker = create_session_factory(engine)
    article_repo = PgArticleRepository(session_maker)
    cluster_repo = PgClusterRepository(session_maker)

    cluster_service = ClusterService(_cache, cluster_repo, article_repo, _bus)

    async def _handle_article_unique(envelope: EventEnvelope) -> None:
        """Parse NATS payload and execute clustering logic."""
        try:
            payload = envelope.parse_payload(ArticleUniquePayload)
        except ValidationError as e:
            logger.error("Invalid data schema: %s", e)
            raise PermanentError("Poison pill schema") from e

        logger.info("Evaluating uniqueness for article clustering: %s", payload.url_hash)
        
        # Any RetryableError thrown (e.g. Optimistic Concurrency) bubbles to NATS for redelivery 
        # PermanentError (e.g. invalid embeddings) delegates to TERM signal.
        await cluster_service.process_article(payload)

    # Note: Use queue_group to ensure load balancing across replicas identically to Task 6 architecture constraints
    await _bus.subscribe(
        subject=EventType.ARTICLE_UNIQUE,
        handler=_handle_article_unique,
        queue_group="cluster_workers",
        durable_name="cluster_workers",
    )

    logger.info("Cluster worker ready. Listening on %s...", EventType.ARTICLE_UNIQUE)
    
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Worker run loop cancelled. Shutting down gracefully...")
    finally:
        await _bus.disconnect()
        await _cache.disconnect()


def handle_sigterm(sig: int, frame: Any) -> None:
    """Graceful cancellation wrapper for signals."""
    logger.info("Received signal %s. Initiating graceful shutdown...", sig)
    loop = asyncio.get_running_loop()
    for task in asyncio.all_tasks(loop=loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, lambda: handle_sigterm(signal.SIGINT, None))
        loop.add_signal_handler(signal.SIGTERM, lambda: handle_sigterm(signal.SIGTERM, None))

    try:
        loop.run_until_complete(run_worker())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt triggered.")
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
