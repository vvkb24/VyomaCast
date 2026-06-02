"""One-time setup script to initialize NATS JetStream stream.

Connects to NATS, cleans up overlapping legacy streams, and ensures
the VYOMACAST stream is configured with correct subjects and retention.
"""

import asyncio
import logging

from nats import connect
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError

from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def setup_nats() -> None:
    """Initialize the NATS JetStream stream for VyomaCast."""
    logger.info("Connecting to NATS at %s...", settings.nats_url)
    nc = await connect(settings.nats_url)
    js = nc.jetstream()

    stream_name = "VYOMACAST"
    subjects = [
        "events.>",
        "feed.>", 
        "fetch.>", 
        "extract.>", 
        "article.>", 
        "cluster.>", 
        "writeback.>"
    ]

    # Aggressive cleanup: delete any existing stream that overlaps or matches names
    # to avoid BadRequestError on subject collisions.
    targets_for_deletion = []
    
    try:
        existing_streams = await js.streams_info()
        for s in existing_streams:
            has_overlap = any(subj in s.config.subjects for subj in subjects)
            if has_overlap or s.config.name in (stream_name, "vyomacast_events"):
                targets_for_deletion.append(s.config.name)
    except Exception as e:
        logger.warning(f"Failed fetching streams info: {e}")

    for target in targets_for_deletion:
        logger.warning("Forcefully deleting overlapping/legacy stream: %s", target)
        try:
            await js.delete_stream(target)
        except Exception as e:
            logger.warning("Failed to delete stream %s: %s", target, e)

    max_age_seconds = 7 * 24 * 60 * 60
    max_bytes = 1 * 1024 * 1024 * 1024
    
    config = StreamConfig(
        name=stream_name,
        subjects=subjects,
        retention=RetentionPolicy.LIMITS,
        max_age=max_age_seconds,
        max_bytes=max_bytes,
    )

    logger.info("Creating stream %s from scratch...", stream_name)
    try:
        await js.add_stream(config)
        logger.info("Stream %s created successfully.", stream_name)
    except Exception as e:
        logger.error("Failed to create stream %s: %s", stream_name, e)

    logger.info("  Subjects: %s", ", ".join(subjects))
    logger.info("  Retention: max_age=7d, max_bytes=1GB")
    
    await nc.close()


if __name__ == "__main__":
    asyncio.run(setup_nats())
