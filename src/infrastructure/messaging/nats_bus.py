"""Async NATS JetStream implementation of the EventBus interface."""

from __future__ import annotations

import logging
from typing import Any, Optional

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.client import JetStreamContext

from src.config import settings
from src.domain.events import EventEnvelope
from src.domain.exceptions import PermanentError, RetryableError
from src.domain.interfaces import EventBus, EventHandler

logger = logging.getLogger(__name__)


class NatsEventBus(EventBus):
    """Event bus implementation using NATS JetStream.

    Provides exactly-once publishing (via dedup windows) and at-least-once
    delivery semantics. Redelivery is constrained by max_deliver and ack_wait
    to prevent infinite retry loops.
    """

    def __init__(self) -> None:
        self._nc: Optional[NATS] = None
        self._js: Optional[JetStreamContext] = None
        self._subs: dict[str, Any] = {}

    @property
    def nc(self) -> NATS:
        """Get the underlying NATS client."""
        if self._nc is None:
            raise RuntimeError("NATS client is not connected.")
        return self._nc

    @property
    def js(self) -> JetStreamContext:
        """Get the JetStream context."""
        if self._js is None:
            raise RuntimeError("JetStream context is not initialised.")
        return self._js

    async def connect(self) -> None:
        """Establish connection to NATS and initialise the core stream."""
        try:
            self._nc = await nats.connect(
                settings.nats_url,
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
            )
            self._js = self.nc.jetstream()

            # Idempotently create or update the global events stream.
            # Subjects are isolated to 'events.>' to prevent clash with KV or ObjectStore.
            await self._js.add_stream(
                name="VYOMACAST",
                subjects=[
                    "events.>",
                    "feed.>", 
                    "fetch.>", 
                    "extract.>", 
                    "article.>", 
                    "cluster.>", 
                    "writeback.>"
                ],
                # We enforce max message lifespan to avoid permanent buildup
                max_age=7 * 24 * 60 * 60,  # 7 days
                max_bytes=1 * 1024 * 1024 * 1024, # 1 GB
            )
            logger.info("Connected to NATS at %s", settings.nats_url)
        except Exception as e:
            logger.error("Failed to connect to NATS: %s", e)
            raise RetryableError("Failed to connect to NATS") from e

    async def disconnect(self) -> None:
        """Gracefully drain all subscriptions and close connections."""
        if self._js:
            # Drain subscriptions first
            for subject, sub in self._subs.items():
                try:
                    await sub.drain()
                except Exception as e:
                    logger.warning("Error draining subscription for %s: %s", subject, e)
            self._subs.clear()

        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            await self._nc.close()
            self._nc = None
            self._js = None

    async def publish(self, subject: str, envelope: EventEnvelope) -> None:
        """Publish an event idempotenly."""
        payload = envelope.model_dump_json().encode("utf-8")
        try:
            # Setting msg_id provides JetStream exactly-once publish deduplication
            await self.js.publish(
                subject,
                payload,
                headers={"Nats-Msg-Id": str(envelope.event_id)},
            )
        except Exception as e:
            raise RetryableError(f"Failed to publish event to {subject}") from e

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue_group: Optional[str] = None,
        durable_name: Optional[str] = None,
        deliver_policy: str = "all",
    ) -> None:
        """Subscribe to a subject with constrained redelivery limits."""

        from nats.js.api import DeliverPolicy
        _policy_map = {
            "all": DeliverPolicy.ALL,
            "new": DeliverPolicy.NEW,
            "last": DeliverPolicy.LAST,
        }
        policy = _policy_map.get(deliver_policy, DeliverPolicy.ALL)

        async def _wrapper_cb(msg: Msg) -> None:
            await self._message_handler(msg, handler)

        sub = await self.js.subscribe(
            subject=subject,
            queue=queue_group,
            cb=_wrapper_cb,
            durable=durable_name,
            config=ConsumerConfig(
                ack_wait=120.0,    # 120 seconds before JetStream redelivers an un-ACKed message
                max_deliver=5,     # Prevent infinite loop of redeliveries (DLQ trigger)
                deliver_group=queue_group,
                deliver_policy=policy,
            ),
        )
        self._subs[subject] = sub

    async def unsubscribe(self, subject: str) -> None:
        """Remove a specific subscription."""
        if subject in self._subs:
            await self._subs[subject].drain()
            del self._subs[subject]

    # ── Internal Handler Engine ──────────────────────────────────────────

    async def _message_handler(self, msg: Msg, handler: EventHandler) -> None:
        """Safely decode, execute, and translate mapped exceptions into ACK/NAK/TERM signals."""
        envelope: Optional[EventEnvelope] = None
        try:
            # 1. Parse EventEnvelope
            try:
                payload = msg.data.decode("utf-8")
                envelope = EventEnvelope.model_validate_json(payload)
            except Exception as e:
                logger.error("Failed to decode or validate event payload: %s", e)
                # Poison pill (corrupt JSON or malformed schema)
                await msg.term()
                return

            # 2. Execute handler
            await handler(envelope)

            # 3. ACK on absolute success
            await msg.ack()

        except RetryableError as e:
            event_id = envelope.event_id if envelope else "unknown"
            logger.warning("Retryable error processing event %s: %s", event_id, e)
            # NAK immediately releases the message for backoff/redelivery
            await msg.nak()

        except PermanentError as e:
            event_id = envelope.event_id if envelope else "unknown"
            logger.error("Permanent error processing event %s: %s", event_id, e)
            # TERM definitively deletes the message to break the loop
            await msg.term()

        except Exception as e:
            event_id = envelope.event_id if envelope else "unknown"
            logger.exception("Unexpected error processing event %s: %s", event_id, e)
            # For unforeseen panics, NAK to allow safety-net redelivery
            # JetStream's max_deliver=5 guarantees this won't hang indefinitely.
            await msg.nak()
