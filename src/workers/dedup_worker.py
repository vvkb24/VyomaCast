"""Worker entry point for the Deduplication Engine."""

import asyncio
import logging
import signal
import sys
from typing import Optional, Any

from pydantic import ValidationError

from src.domain.events import EventEnvelope, EventType, ExtractCompletedPayload
from src.domain.exceptions import PermanentError
from src.infrastructure.cache.redis_cache import RedisCacheStore
from src.infrastructure.database.engine import create_db_engine, create_session_factory
from src.infrastructure.database.repositories import PgArticleRepository
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.services.dedup_service import DedupService

logger = logging.getLogger(__name__)

# Basic module-level reference for signal shutdown
_bus: Optional[NatsEventBus] = None
_cache: Optional[RedisCacheStore] = None


async def run_worker() -> None:
    """Initialize dependencies and subscribe to ExtractCompleted events."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Dedup Engine Worker...")

    global _bus, _cache
    _bus = NatsEventBus()
    _cache = RedisCacheStore()

    try:
        await _bus.connect()
        await _cache.connect()
    except Exception as e:
        logger.critical("Failed to connect to Infrastructure: %s", e)
        sys.exit(1)

    # Initialize Repository with the Engine globally available
    engine = create_db_engine()
    session_maker = create_session_factory(engine)
    repo = PgArticleRepository(session_maker)

    dedup_service = DedupService(_cache, repo, _bus)

    async def _handle_extract_completed(envelope: EventEnvelope) -> None:
        try:
            payload = envelope.parse_payload(ExtractCompletedPayload)
        except ValidationError as e:
            logger.error("Invalid data schema: %s", e)
            raise PermanentError("Poison pill schema") from e
        
        logger.info("De-duplicating extracted article: %s", payload.url_hash)
        
        # Any domain exception here bubbles to bus and gracefully handles NAK/TERM behaviors
        await dedup_service.process_article(payload)

    # Note: Use queue_group to ensure load balancing across replicas identically to Task 6 architecture constraints
    await _bus.subscribe(
        subject=EventType.EXTRACT_COMPLETED,
        handler=_handle_extract_completed,
        queue_group="dedup_workers",
        durable_name="dedup_workers",
    )

    logger.info("Dedup worker ready. Listening on %s...", EventType.EXTRACT_COMPLETED)
    
    try:
        # Keep the event loop alive until shutdown/SIGTERM
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Worker run loop cancelled. Shutting down gracefully...")
    finally:
        await _bus.disconnect()
        await _cache.disconnect()


def handle_sigterm(sig: int, frame: Any) -> None:
    """Graceful cancellation wrapper for kubernetes/docker SIGTERM signals."""
    logger.info("Received signal %s. Initiating graceful shutdown...", sig)
    # The event loop is grabbed dynamically
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
