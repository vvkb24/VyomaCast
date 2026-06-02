"""Fake event bus for in-memory publish/subscribe testing.

Published events are stored in an internal list for assertion and are
auto-dispatched to any registered handlers, enabling both unit-style
inspection ("was event X published?") and integration-style flow testing
("does handler Y get called when event X is published?").
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional, override

from src.domain.events import EventEnvelope, EventType
from src.domain.interfaces import EventBus, EventHandler


class FakeEventBus(EventBus):
    """In-memory event bus with full publish/subscribe semantics.

    Attributes
    ----------
    published : list[tuple[str, EventEnvelope]]
        Every ``(subject, envelope)`` pair that was published, in order.
    handler_calls : list[tuple[str, EventEnvelope]]
        Every ``(subject, envelope)`` pair that a handler was invoked with.
    handler_errors : list[tuple[str, Exception]]
        Any exceptions raised by handlers during dispatch.

    Test Helpers
    ------------
    ``get_events(subject)``
        Filter published events by subject.
    ``get_payloads(subject, payload_type)``
        Extract and parse payload models from events matching a subject.
    ``clear()``
        Reset all state between tests.
    """

    def __init__(self) -> None:
        self._connected: bool = False
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

        # Observable test state
        self.published: list[tuple[str, EventEnvelope]] = []
        self.handler_calls: list[tuple[str, EventEnvelope]] = []
        self.handler_errors: list[tuple[str, Exception]] = []

    # ── Interface Implementation ──────────────────────────────────────────

    @override
    async def connect(self) -> None:
        self._connected = True

    @override
    async def disconnect(self) -> None:
        self._connected = False
        self._handlers.clear()

    @override
    async def publish(self, subject: str, envelope: EventEnvelope) -> None:
        self.published.append((subject, envelope))

        # Auto-dispatch to registered handlers
        for handler in self._handlers.get(subject, []):
            try:
                await handler(envelope)
                self.handler_calls.append((subject, envelope))
            except Exception as exc:
                self.handler_errors.append((subject, exc))

    @override
    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue_group: Optional[str] = None,
        durable_name: Optional[str] = None,
    ) -> None:
        # queue_group and durable_name are NATS-specific; no-ops in fake
        self._handlers[subject].append(handler)

    @override
    async def unsubscribe(self, subject: str) -> None:
        self._handlers.pop(subject, None)

    # ── Test Helpers ──────────────────────────────────────────────────────

    def get_events(self, subject: str) -> list[EventEnvelope]:
        """Return all envelopes published to a specific subject."""
        return [env for subj, env in self.published if subj == subject]

    def get_events_by_type(self, event_type: EventType) -> list[EventEnvelope]:
        """Return all envelopes matching a specific EventType."""
        return [env for _, env in self.published if env.event_type == event_type]

    def get_payloads(self, subject: str, payload_type: type) -> list:
        """Parse and return typed payloads for events on a subject."""
        return [env.parse_payload(payload_type) for env in self.get_events(subject)]

    @property
    def event_count(self) -> int:
        """Total number of events published."""
        return len(self.published)

    @property
    def subjects(self) -> set[str]:
        """Set of all subjects that have received at least one event."""
        return {subj for subj, _ in self.published}

    def clear(self) -> None:
        """Reset all published events, handler calls, and errors."""
        self.published.clear()
        self.handler_calls.clear()
        self.handler_errors.clear()
