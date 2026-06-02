import asyncio
import logging
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.services.fetcher_service import FetcherService

logging.basicConfig(level=logging.INFO)

async def main():
    bus = NatsEventBus()
    await bus.connect()
    
    fetcher = FetcherService(bus)
    await fetcher.process_feed("http://feeds.bbci.co.uk/news/rss.xml")
    
    await bus.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
