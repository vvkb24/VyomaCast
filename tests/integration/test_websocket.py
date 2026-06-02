"""Integration tests for Real-Time WebSocket Broadcasts."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.websocket.hub import manager
from src.domain.events import ArticleClusteredPayload, EventEnvelope, EventType
from src.domain.models import Article
from src.workers.notifier_worker import run_notifier

client = TestClient(app)

@pytest.fixture
def clean_manager():
    """Ensure the manager starts clean before testing."""
    manager.active_connections.clear()
    yield manager
    manager.active_connections.clear()


def test_websocket_connect_and_disconnect(clean_manager):
    """Test 1 & 4: Client connects successfully and disconnect removes safely."""
    assert len(clean_manager.active_connections) == 0

    with client.websocket_connect("/ws/updates") as websocket:
        # Connection succeeds and is tracked
        assert len(clean_manager.active_connections) == 1
    
    # After closure, disconnected immediately
    assert len(clean_manager.active_connections) == 0


def test_broadcast_concurrent_delivery(clean_manager):
    """Test 2 & 3: Payload delivery using exact lightweight schema across multiple clients."""
    with client.websocket_connect("/ws/updates") as ws1, \
         client.websocket_connect("/ws/updates") as ws2:
        
        payload = {
            "event": "cluster_update",
            "data": {
                "cluster_id": str(uuid4()),
                "article_id": str(uuid4()),
                "title": "Markets Rally",
                "source_domain": "reuters.com",
                "is_new_cluster": True
            }
        }
        
        # Trigger broadcast manually. We must use a separate event loop mechanism
        # since TestClient is blocking. Starlette TestClient runs its own thread,
        # but we can safely call async code via asyncio.run for local unit logic.
        asyncio.run(clean_manager.broadcast(payload))

        # Both clients should receive the exact lightweight schema
        data1 = ws1.receive_json()
        data2 = ws2.receive_json()

        assert data1 == payload
        assert data2 == payload
        assert "embeddings" not in data1["data"]


@pytest.mark.asyncio
async def test_broadcast_continues_on_client_failure(clean_manager):
    """Test 5: Broadcast continues even if one client heavily fails or timeouts."""
    
    class FailedClient:
        async def send_json(self, data):
            raise RuntimeError("Client disconnected forcefully!")
            
    class SuccessClient:
        def __init__(self):
            self.received = False
        async def send_json(self, data):
            # Acknowledge receipt
            self.received = True

    bad_client = FailedClient()
    good_client = SuccessClient()

    # Pre-populate manager directly bypassing connect() accept logs
    async with clean_manager.lock:
        clean_manager.active_connections.add(bad_client) # type: ignore
        clean_manager.active_connections.add(good_client) # type: ignore

    assert len(clean_manager.active_connections) == 2

    await clean_manager.broadcast({"test": "data"})

    # The good client still received the payload
    assert good_client.received is True
    # The bad client was cleanly evicted dynamically
    assert len(clean_manager.active_connections) == 1
    assert good_client in clean_manager.active_connections


@pytest.mark.asyncio
async def test_notifier_worker_maps_and_broadcasts(clean_manager):
    """Test that Notifier translates NATS ArticleClustered into manager broadcasts purely from envelope."""
    
    mock_bus = AsyncMock()
    mock_bus.client.is_connected = True
    
    # Run notifier init bindings (no db repo needed anymore)
    await run_notifier(manager=clean_manager, bus=mock_bus)

    # Grab the subscriber callback bound to NATS securely
    _, kwargs = mock_bus.subscribe.call_args
    handler = kwargs["handler"]
    
    # Generate the simulated JetStream incoming Event
    article_id = uuid4()
    cluster_id = uuid4()
    envelope = EventEnvelope.create(
        event_type=EventType.ARTICLE_CLUSTERED,
        payload=ArticleClusteredPayload(
            cluster_id=cluster_id,
            article_id=article_id,
            title="Financial News Surge",
            source_domain="reuters.com",
            version=2,
            is_new_cluster=False,
            similarity_score=0.88
        ),
        source_service="cluster-engine"
    )
    
    # Mock the manager broadcast to verify the translated schema directly
    with patch.object(clean_manager, "broadcast", new_callable=AsyncMock) as mock_broadcast:
        # Trigger the callback simulating NATS delivery natively
        await handler(envelope)
        
        mock_broadcast.assert_called_once()
        sent_payload = mock_broadcast.call_args[0][0]
        
        # Verify strict payload translations executed accurately bypassing repositories completely
        assert sent_payload["event"] == "cluster_update"
        assert sent_payload["data"]["title"] == "Financial News Surge"
        assert sent_payload["data"]["source_domain"] == "reuters.com"
        assert sent_payload["data"]["is_new_cluster"] is False
        assert "version" not in sent_payload["data"]
        assert "embedding" not in sent_payload["data"]
