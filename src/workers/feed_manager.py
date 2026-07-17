# VyomaCast: A real-time, event-driven news clustering engine.
# Copyright (C) 2026 Valluri Vamshi Krishna Bharadwaj
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Worker entry point for continuous feed polling."""

import asyncio
import logging
import signal
import sys
from typing import Any, Optional

from src.infrastructure.database.engine import create_db_engine, create_session_factory
from src.infrastructure.database.repositories import PgFeedRepository
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.infrastructure.http.aiohttp_fetcher import AioHttpFetcher
from src.services.feed_manager import FeedManager

logger = logging.getLogger(__name__)

_bus: Optional[NatsEventBus] = None
_fetcher: Optional[AioHttpFetcher] = None


async def run_worker() -> None:
    """Initialize dependencies and run the FeedManager loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Feed Manager Worker...")

    global _bus, _fetcher
    _bus = NatsEventBus()
    _fetcher = AioHttpFetcher()

    try:
        await _bus.connect()
    except Exception as e:
        logger.critical("Failed to connect to NATS: %s", e)
        sys.exit(1)

    engine = create_db_engine()
    session_maker = create_session_factory(engine)
    feed_repo = PgFeedRepository(session_maker)

    manager = FeedManager(
        feed_repo=feed_repo,
        event_bus=_bus,
        http_fetcher=_fetcher,
        poll_interval=60,
    )

    try:
        await manager.run()
    except asyncio.CancelledError:
        logger.info("Feed Manager loop cancelled. Shutting down gracefully...")
    finally:
        await _bus.disconnect()
        await _fetcher.close()


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
