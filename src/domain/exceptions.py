"""Structured exception hierarchy for VyomaCast.

Every consumer and service uses this hierarchy to declare failure semantics.
The NATS event-bus wrapper (and any future transport) inspects exception types
to decide retry-vs-DLQ routing:

    RetryableError   → NAK with exponential back-off (up to max_deliver)
    PermanentError   → ACK + route to DLQ (no retry)
    PoisonPillError  → ACK + route to DLQ + emit CRITICAL log

Any *unexpected* ``Exception`` is treated as retryable with a CRITICAL log so
that transient bugs don't silently drop messages.
"""


class VyomaCastError(Exception):
    """Base exception for all VyomaCast domain errors.

    Catching ``VyomaCastError`` captures every known failure mode.
    """

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details: dict = details or {}


class RetryableError(VyomaCastError):
    """Transient failure that should be retried.

    Examples:
        * Network timeout when fetching a URL
        * Temporary database connection failure
        * Redis connection refused (circuit-breaker open)
        * Rate-limit 429 response from upstream
    """


class PermanentError(VyomaCastError):
    """Non-transient failure — retrying will not help.

    Examples:
        * Malformed HTML that no extractor can parse
        * Article content below minimum quality threshold
        * Schema validation failure in an event payload
    """


class PoisonPillError(PermanentError):
    """Message that crashes the consumer on every attempt.

    This is a sub-class of ``PermanentError`` but receives special treatment:
    the consumer emits a ``CRITICAL``-level structured log with the raw message
    bytes (truncated to 500 chars) before routing to the DLQ.

    Examples:
        * Completely un-parseable raw bytes on the wire
        * Payload that triggers an unhandled exception in business logic
    """
