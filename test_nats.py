import asyncio
import logging
from datetime import datetime, UTC
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.domain.events import EventType, EventEnvelope, ExtractCompletedPayload

logging.basicConfig(level=logging.INFO)

async def test_pub_sub():
    bus = NatsEventBus()
    await bus.connect()
    
    # 1. Purge stream to ensure clean state
    await bus.js.purge_stream("VYOMACAST")
    
    event_received = asyncio.Event()

    async def _handler(env):
        print(f"GOT EVENT: {env.event_type}")
        event_received.set()

    # Create a unique queue group to avoid any old consumers swallowing it
    await bus.subscribe("extract.completed", _handler, queue_group="test_isolate")
    await asyncio.sleep(1)

    payload = ExtractCompletedPayload(
        url="http://test.com/new",
        url_hash="12345",
        title="T",
        content="C",
        content_length=1,
        published_at=datetime.now(UTC),
        quality_score=0.9
    )
    env = EventEnvelope.create(EventType.EXTRACT_COMPLETED, payload, "test")
    await bus.publish("extract.completed", env)

    try:
        await asyncio.wait_for(event_received.wait(), 5.0)
        print("SUCCESS")
    except asyncio.TimeoutError:
        print("TIMEOUT - NO MESSAGE DELIVERED")
    finally:
        await bus.disconnect()

if __name__ == "__main__":
    asyncio.run(test_pub_sub())
