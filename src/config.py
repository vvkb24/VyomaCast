"""Centralised application settings loaded from environment variables.

All settings are prefixed with ``VYOMACAST_`` and can be overridden via a
``.env`` file placed at the project root.  Defaults are tuned for local
Docker-Compose development; production deployments should override via
environment variables.

Key corrections from the Sanity Check analysis are applied here:
    * ``nats_ack_wait_seconds`` = 120  (prevents redelivery cascade under burst)
    * ``redis_maxmemory`` = 768mb      (accounts for Redis per-key overhead)
    * ``embedding_cache_ttl_hours`` = 8 (reduces Redis memory from 84→28 MB)
    * ``redis_eviction_policy`` = volatile-lru (protects non-TTL critical keys)
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated configuration for every VyomaCast component."""

    model_config = SettingsConfigDict(
        env_prefix="VYOMACAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database (PostgreSQL + pgvector) ──────────────────────────────────

    database_url: str = "postgresql+asyncpg://vyomacast:vyomacast@localhost:5433/vyomacast"
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)

    # ── Cache (Redis) ─────────────────────────────────────────────────────

    redis_url: str = "redis://localhost:6380/0"
    redis_maxmemory: str = "768mb"
    redis_eviction_policy: str = "volatile-lru"
    redis_dirty_db: int = Field(default=1, ge=0, le=15)

    # ── Event Bus (NATS JetStream) ────────────────────────────────────────

    nats_url: str = "nats://localhost:4223"
    nats_ack_wait_seconds: int = Field(default=120, ge=10, le=600)
    nats_max_deliver: int = Field(default=5, ge=1, le=20)
    nats_stream_max_bytes: int = Field(default=1_073_741_824, ge=1_048_576)  # min 1 MB

    # ── Embedding Model ───────────────────────────────────────────────────

    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = Field(default=384, ge=1)
    embedding_cache_ttl_hours: int = Field(default=8, ge=1, le=168)
    embedding_max_tokens: int = Field(default=512, ge=64, le=8192)

    # ── Deduplication ─────────────────────────────────────────────────────

    simhash_hamming_threshold: int = Field(default=3, ge=1, le=10)
    simhash_bands: int = Field(default=16, ge=4, le=32)
    simhash_band_bits: int = Field(default=8, ge=4, le=16)
    simhash_window_hours: int = Field(default=72, ge=1, le=720)
    vector_cosine_threshold: float = Field(default=0.92, ge=0.5, le=1.0)

    # ── Clustering ────────────────────────────────────────────────────────

    cluster_merge_threshold: float = Field(default=0.78, ge=0.5, le=0.99)
    cluster_max_articles: int = Field(default=500, ge=10, le=10_000)

    # ── Temporal Decay ────────────────────────────────────────────────────

    decay_half_life_hours: float = Field(default=6.0, ge=0.5, le=168.0)
    decay_eviction_threshold: float = Field(default=0.05, ge=0.001, le=0.5)
    decay_scan_interval_seconds: int = Field(default=60, ge=10, le=3600)

    # ── Write-Back ────────────────────────────────────────────────────────

    writeback_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    writeback_batch_size: int = Field(default=100, ge=1, le=10_000)
    writeback_max_dirty: int = Field(default=10_000, ge=100, le=1_000_000)

    # ── HTTP Fetcher ──────────────────────────────────────────────────────

    max_fetch_concurrency: int = Field(default=100, ge=1, le=500)
    fetch_timeout_seconds: int = Field(default=30, ge=5, le=120)
    fetch_connect_timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_html_size_bytes: int = Field(default=512_000, ge=10_000, le=10_000_000)
    max_content_length: int = Field(default=102_400, ge=1_000, le=1_000_000)
    fetch_max_retries: int = Field(default=3, ge=0, le=10)
    per_domain_rate_limit: float = Field(default=2.0, ge=0.1, le=100.0)

    # ── Feed Polling ──────────────────────────────────────────────────────

    feed_default_poll_interval: int = Field(default=600, ge=60, le=86_400)
    feed_min_poll_interval: int = Field(default=300, ge=30, le=3_600)
    feed_max_poll_interval: int = Field(default=7_200, ge=600, le=86_400)
    max_feeds: int = Field(default=2_000, ge=1, le=100_000)

    # ── API Server ────────────────────────────────────────────────────────

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    cors_origins: list[str] = Field(default=["*"])
    ws_heartbeat_interval: int = Field(default=30, ge=5, le=300)

    # ── Logging / Observability ───────────────────────────────────────────

    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "console"
    service_name: str = "vyomacast"

    # ── Derived Helpers ───────────────────────────────────────────────────

    @property
    def simhash_total_bits(self) -> int:
        """Total SimHash width in bits (bands × bits-per-band)."""
        return self.simhash_bands * self.simhash_band_bits

    @property
    def simhash_window_seconds(self) -> int:
        """SimHash dedup window converted to seconds for Redis TTL."""
        return self.simhash_window_hours * 3600

    @property
    def embedding_cache_ttl_seconds(self) -> int:
        """Embedding cache TTL converted to seconds for Redis TTL."""
        return self.embedding_cache_ttl_hours * 3600


# Module-level singleton — import this from anywhere.
# Each process creates exactly one Settings instance.
settings = Settings()
