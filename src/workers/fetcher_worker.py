"""Worker entry point for RSS Fetching and Extraction."""

import asyncio
import logging
import signal
import sys
from typing import Optional, Any

from pydantic import ValidationError

from src.domain.events import EventEnvelope, EventType, FeedProcessCommandPayload
from src.domain.exceptions import PermanentError
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.services.fetcher_service import FetcherService

logger = logging.getLogger(__name__)

# Basic module-level reference for signal shutdown
_bus: Optional[NatsEventBus] = None


async def run_worker() -> None:
    """Initialize NATS, subscribe to the feed events, and listen indefinitely."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Fetcher Worker...")

    global _bus
    _bus = NatsEventBus()
    
    try:
        await _bus.connect()
    except Exception as e:
        logger.critical("Failed to connect to NATS backbone: %s", e)
        sys.exit(1)

    fetcher_service = FetcherService(_bus)

    from src.domain.events import FeedItemsNewPayload

    async def _handle_feed_process_command(envelope: EventEnvelope) -> None:
        try:
            payload = envelope.parse_payload(FeedProcessCommandPayload)
        except ValidationError as e:
            logger.error("Invalid data schema: %s", e)
            raise PermanentError("Poison pill schema") from e
        
        logger.info("Executing FEED_PROCESS_COMMAND for %s", payload.feed_url)
        
        # We await process_feed. Any unhandled exceptions bubble to NatsEventBus 
        # which logs and correctly triggers `msg.nak()` for JetStream redelivery.
        await fetcher_service.process_feed(payload.feed_url, payload.feed_id)

    async def _handle_feed_items_new(envelope: EventEnvelope) -> None:
        try:
            payload = envelope.parse_payload(FeedItemsNewPayload)
        except ValidationError as e:
            logger.error("Invalid data schema: %s", e)
            raise PermanentError("Poison pill schema") from e
        
        logger.info("Executing FEED_ITEMS_NEW for %s", payload.item_url)
        await fetcher_service.process_article(payload.item_url, payload.feed_id)

    # Note: Use queue_group to ensure load balancing across replicas
    await _bus.subscribe(
        subject=EventType.FEED_PROCESS_COMMAND,
        handler=_handle_feed_process_command,
        queue_group="fetcher_workers",
        durable_name="fetcher_workers",
    )

    await _bus.subscribe(
        subject=EventType.FEED_ITEMS_NEW,
        handler=_handle_feed_items_new,
        queue_group="fetcher_workers_items_new",
        durable_name="fetcher_workers_items_new",
    )

    logger.info("Fetcher worker ready. Listening on %s...", EventType.FEED_PROCESS_COMMAND)
    
    try:
        # Keep the event loop alive until shutdown/SIGTERM
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Worker run loop cancelled. Shutting down NATS connections...")
    finally:
        await _bus.disconnect()


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
        # Ensure task cancellation propagates properly for manual ctrl+c
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
