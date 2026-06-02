#!/usr/bin/env python3
"""VyomaCast — Infrastructure Health Checker.

Pings PostgreSQL, Redis, and NATS to verify that all Docker services are
running and healthy.  Reads connection strings from the ``.env`` file
via the application's ``Settings`` object.

Usage:
    python scripts/check_infra.py

Exit codes:
    0 — all services healthy
    1 — one or more services unreachable
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set env vars so Settings doesn't fail on import if .env is missing
import os

os.environ.setdefault("VYOMACAST_DATABASE_URL", "postgresql+asyncpg://vyomacast:vyomacast@localhost:5433/vyomacast")
os.environ.setdefault("VYOMACAST_REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("VYOMACAST_NATS_URL", "nats://localhost:4223")


# ────────────────────────────────────────────────────────────────────────────
# Color helpers for terminal output
# ────────────────────────────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {CYAN}ℹ{RESET} {msg}")


def header(msg: str) -> None:
    print(f"\n{BOLD}{msg}{RESET}")


# ────────────────────────────────────────────────────────────────────────────
# Health check functions
# ────────────────────────────────────────────────────────────────────────────


async def check_postgres(dsn: str) -> bool:
    """Connect to PostgreSQL, verify pgvector and uuid-ossp extensions."""
    try:
        import asyncpg  # type: ignore[import-untyped]

        # asyncpg uses raw postgres DSN (not SQLAlchemy format)
        raw_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

        t0 = time.monotonic()
        conn = await asyncio.wait_for(asyncpg.connect(raw_dsn), timeout=10.0)
        latency_ms = (time.monotonic() - t0) * 1000

        try:
            # Check server version
            version = await conn.fetchval("SELECT version()")
            ok(f"Connected ({latency_ms:.0f}ms)")
            info(f"Server: {version.split(',')[0] if version else 'unknown'}")

            # Verify pgvector extension
            has_vector = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
            if has_vector:
                ok("pgvector extension: installed")
            else:
                fail("pgvector extension: NOT installed")
                return False

            # Verify uuid-ossp extension
            has_uuid = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'uuid-ossp')"
            )
            if has_uuid:
                ok("uuid-ossp extension: installed")
            else:
                fail("uuid-ossp extension: NOT installed")
                return False

            return True
        finally:
            await conn.close()

    except asyncio.TimeoutError:
        fail("Connection timed out (10s)")
        return False
    except Exception as e:
        fail(f"Connection failed: {e}")
        return False


async def check_redis(url: str) -> bool:
    """Connect to Redis, verify maxmemory-policy is volatile-lru."""
    try:
        import redis.asyncio as aioredis

        t0 = time.monotonic()
        client = aioredis.from_url(url, socket_connect_timeout=10)
        latency_ms = (time.monotonic() - t0) * 1000

        try:
            # Ping
            pong = await asyncio.wait_for(client.ping(), timeout=10.0)
            latency_ms = (time.monotonic() - t0) * 1000
            if pong:
                ok(f"Connected ({latency_ms:.0f}ms)")
            else:
                fail("Ping failed")
                return False

            # Check server info
            info_data = await client.info("server")
            redis_version = info_data.get("redis_version", "unknown")
            info(f"Server: Redis {redis_version}")

            # Verify maxmemory-policy
            config = await client.config_get("maxmemory-policy")
            policy = config.get("maxmemory-policy", "unknown")
            if policy == "volatile-lru":
                ok(f"Eviction policy: {policy}")
            else:
                fail(f"Eviction policy: {policy} (expected volatile-lru)")
                return False

            # Verify maxmemory
            mem_config = await client.config_get("maxmemory")
            maxmem_bytes = int(mem_config.get("maxmemory", 0))
            maxmem_mb = maxmem_bytes / (1024 * 1024)
            if maxmem_bytes > 0:
                ok(f"Max memory: {maxmem_mb:.0f} MB")
            else:
                fail("Max memory: not configured (0 = unlimited)")
                return False

            return True
        finally:
            await client.aclose()

    except asyncio.TimeoutError:
        fail("Connection timed out (10s)")
        return False
    except Exception as e:
        fail(f"Connection failed: {e}")
        return False


async def check_nats(url: str) -> bool:
    """Connect to NATS, verify JetStream is enabled."""
    try:
        import nats  # type: ignore[import-untyped]

        t0 = time.monotonic()
        nc = await asyncio.wait_for(nats.connect(url), timeout=10.0)
        latency_ms = (time.monotonic() - t0) * 1000

        try:
            ok(f"Connected ({latency_ms:.0f}ms)")
            info(f"Server: {nc.connected_url}")

            # Verify JetStream is enabled by getting JetStream context
            js = nc.jetstream()
            account_info = await asyncio.wait_for(js.account_info(), timeout=5.0)
            if account_info:
                ok("JetStream: enabled")
                info(f"  Streams: {account_info.streams}, Consumers: {account_info.consumers}")
            else:
                fail("JetStream: disabled or unavailable")
                return False

            return True
        finally:
            await nc.drain()

    except asyncio.TimeoutError:
        fail("Connection timed out (10s)")
        return False
    except Exception as e:
        fail(f"Connection failed: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def main() -> int:
    """Run all health checks and return exit code."""
    from src.config import settings

    print(f"\n{BOLD}{'═' * 50}{RESET}")
    print(f"{BOLD}  VyomaCast Infrastructure Health Check{RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}")

    results: dict[str, bool] = {}

    # PostgreSQL
    header("PostgreSQL (pgvector)")
    info(f"DSN: {settings.database_url.split('@')[0].split('//')[0]}//***@{settings.database_url.split('@')[-1]}")
    results["postgres"] = await check_postgres(settings.database_url)

    # Redis
    header("Redis")
    info(f"URL: {settings.redis_url}")
    results["redis"] = await check_redis(settings.redis_url)

    # NATS
    header("NATS (JetStream)")
    info(f"URL: {settings.nats_url}")
    results["nats"] = await check_nats(settings.nats_url)

    # Summary
    print(f"\n{BOLD}{'─' * 50}{RESET}")
    all_healthy = all(results.values())

    for service, healthy in results.items():
        status = f"{GREEN}HEALTHY{RESET}" if healthy else f"{RED}FAILED{RESET}"
        print(f"  {service:12s} : {status}")

    if all_healthy:
        print(f"\n{GREEN}{BOLD}  All services healthy ✓{RESET}\n")
        return 0
    else:
        failed = [s for s, h in results.items() if not h]
        print(f"\n{RED}{BOLD}  Failed services: {', '.join(failed)} ✗{RESET}")
        print(f"  Run: {YELLOW}docker compose up -d{RESET} and try again.\n")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
