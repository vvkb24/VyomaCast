"""Seed script: insert 40 high-quality RSS feeds into the database.

Idempotent — uses FeedRepository.save() which performs ON CONFLICT (url) DO UPDATE.

Usage:
    python scripts/seed_feeds.py
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

# Ensure project root is importable
sys.path.insert(0, ".")

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.config import settings
from src.domain.models import Feed, FeedStatus
from src.infrastructure.database.engine import create_db_engine
from src.infrastructure.database.repositories import PgFeedRepository

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Feed Catalog ────────────────────────────────────────────────────────────
# 40 high-quality, publicly accessible RSS/Atom feeds spanning
# major news, technology, science, and business domains.

SEED_FEEDS = [
    # ── Major News Wires ──
    {"url": "https://feeds.reuters.com/reuters/topNews", "name": "Reuters — Top News"},
    {"url": "https://feeds.reuters.com/reuters/worldNews", "name": "Reuters — World News"},
    {"url": "https://feeds.reuters.com/reuters/technologyNews", "name": "Reuters — Technology"},
    {"url": "https://feeds.reuters.com/reuters/businessNews", "name": "Reuters — Business"},
    {"url": "https://feeds.bbci.co.uk/news/rss.xml", "name": "BBC News — Top Stories"},
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "name": "BBC News — World"},
    {"url": "https://feeds.bbci.co.uk/news/technology/rss.xml", "name": "BBC News — Technology"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "name": "NYT — Home Page"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "name": "NYT — World"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "name": "NYT — Technology"},

    # ── Technology ──
    {"url": "https://techcrunch.com/feed/", "name": "TechCrunch"},
    {"url": "https://www.theverge.com/rss/index.xml", "name": "The Verge"},
    {"url": "https://www.wired.com/feed/rss", "name": "Wired"},
    {"url": "https://arstechnica.com/feed/", "name": "Ars Technica"},
    {"url": "https://feeds.feedburner.com/TheHackersNews", "name": "The Hacker News (Security)"},
    {"url": "https://hnrss.org/frontpage", "name": "Hacker News — Front Page"},
    {"url": "https://www.engadget.com/rss.xml", "name": "Engadget"},
    {"url": "https://www.zdnet.com/news/rss.xml", "name": "ZDNet"},
    {"url": "https://feeds.feedburner.com/venturebeat/SZYF", "name": "VentureBeat"},
    {"url": "https://9to5mac.com/feed/", "name": "9to5Mac"},

    # ── Science & Innovation ──
    {"url": "https://www.sciencedaily.com/rss/all.xml", "name": "ScienceDaily"},
    {"url": "https://phys.org/rss-feed/", "name": "Phys.org"},
    {"url": "https://www.nature.com/nature.rss", "name": "Nature"},
    {"url": "https://www.newscientist.com/feed/home/", "name": "New Scientist"},

    # ── Business & Finance ──
    {"url": "https://feeds.a]nbloomberg.com/markets/news.rss", "name": "Bloomberg Markets"},
    {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "name": "CNBC — Top News"},
    {"url": "https://fortune.com/feed/", "name": "Fortune"},
    {"url": "https://feeds.marketwatch.com/marketwatch/topstories/", "name": "MarketWatch"},

    # ── Global & Geopolitics ──
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "name": "Al Jazeera"},
    {"url": "https://www.theguardian.com/world/rss", "name": "The Guardian — World"},
    {"url": "https://www.theguardian.com/uk/technology/rss", "name": "The Guardian — Technology"},
    {"url": "https://feeds.washingtonpost.com/rss/world", "name": "Washington Post — World"},

    # ── AI & Machine Learning ──
    {"url": "https://openai.com/blog/rss.xml", "name": "OpenAI Blog"},
    {"url": "https://blog.google/technology/ai/rss/", "name": "Google AI Blog"},
    {"url": "https://deepmind.google/blog/rss.xml", "name": "DeepMind Blog"},
    {"url": "https://huggingface.co/blog/feed.xml", "name": "Hugging Face Blog"},

    # ── Developer & Open Source ──
    {"url": "https://github.blog/feed/", "name": "GitHub Blog"},
    {"url": "https://stackoverflow.blog/feed/", "name": "Stack Overflow Blog"},
    {"url": "https://devblogs.microsoft.com/feed/", "name": "Microsoft Dev Blogs"},
    {"url": "https://aws.amazon.com/blogs/aws/feed/", "name": "AWS Blog"},
]


async def seed_feeds() -> None:
    """Insert all seed feeds into the database idempotently."""
    engine = create_db_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = PgFeedRepository(session_factory)

    inserted = 0
    for entry in SEED_FEEDS:
        feed = Feed(
            id=uuid4(),
            url=entry["url"],
            name=entry["name"],
            poll_interval=settings.feed_default_poll_interval,
            next_poll_at=datetime.now(UTC),
            status=FeedStatus.ACTIVE,
        )

        try:
            await repo.save(feed)
            inserted += 1
            logger.info("  ✓ %s", entry["name"])
        except Exception as e:
            logger.warning("  ✗ %s — %s", entry["name"], e)

    await engine.dispose()
    logger.info("\nSeeded %d / %d feeds successfully.", inserted, len(SEED_FEEDS))


if __name__ == "__main__":
    asyncio.run(seed_feeds())
