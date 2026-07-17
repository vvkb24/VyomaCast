"""Worker task for pushing NATS events to WebSocket clients."""

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from src.api.websocket.hub import ConnectionManager
from src.api.websocket.hub import manager as default_manager
from src.domain.events import ArticleClusteredPayload, EventEnvelope, EventType
from src.domain.exceptions import PermanentError
from src.infrastructure.messaging.nats_bus import NatsEventBus

logger = logging.getLogger(__name__)

_bus: Optional[NatsEventBus] = None


async def run_notifier(
    manager: Optional[ConnectionManager] = None,
    bus: Optional[NatsEventBus] = None,
) -> None:
    """Connect to NATS and broadcast lightweight events to connected WS clients.
    
    Dependencies can be optionally injected for tests. When running in production,
    default to the global FastAPI `manager` and actual infrastructure.
    """
    logger.info("Starting WebSocket Notifier Worker...")

    manager = manager or default_manager
    bus = bus or NatsEventBus()
    
    try:
        try:
            connected = bus.nc.is_connected
        except RuntimeError:
            connected = False

        if not connected:
            await bus.connect()
    except Exception as e:
        logger.critical("Failed to connect to NATS: %s", e)
        raise

    async def _handle_article_clustered(envelope: EventEnvelope) -> None:
        """Process cluster events and fanout to websockets."""
        try:
            payload = envelope.parse_payload(ArticleClusteredPayload)
                
        except ValidationError as e:
            logger.error("Invalid data schema: %s", e)
            raise PermanentError("Poison pill schema") from e

        lightweight_payload = {
            "event": "cluster_update",
            "data": {
                "cluster_id": str(payload.cluster_id),
                "article_id": str(payload.article_id),
                "title": payload.title,
                "source_domain": payload.source_domain,
                "is_new_cluster": payload.is_new_cluster
            }
        }

        logger.debug("Broadcasting update for cluster_id=%s", payload.cluster_id)
        # Fire and forget into the broadcast engine
        await manager.broadcast(lightweight_payload)

    await bus.subscribe(
        subject=f"events.{EventType.ARTICLE_CLUSTERED.value}",
        handler=_handle_article_clustered,
        queue_group="vyomacast_notifier",
        durable_name="vyomacast_notifier",
    )
    logger.info("Subscribed to %s", EventType.ARTICLE_CLUSTERED.value)

    # We yield control back to the caller instead of blocking forever if we are running
    # simply as an initialization step (FastAPI lifespan). If we are a standalone worker,
    # it would loop. However, since NATS JetStream handlers are async tasks, just returning
    # is sufficient if the event loop remains open.
