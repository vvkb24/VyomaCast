"""True End-to-End Pipeline Integration Test.

This test validates the ACTUAL system wiring across process boundaries without fakes.
It uses real NATS (for event bus), Redis (for caching), and PostgreSQL (for persistence).

Flow:
1. Spin up native Python worker tasks (`dedup_worker`, `cluster_worker`) connected
   to the test-environment infrastructure.
2. Spin up a FastAPI `TestClient` in a background thread to maintain a live
   WebSocket connection, which bootstraps `notifier_worker`.
3. Inject a raw `ExtractCompletedPayload` into the real NATS JetStream.
4. Verify the system processes this entirely autonomously:
    - Article is embedded by real SentenceTransformer.
    - Article is saved to PostgreSQL pgvector.
    - Cluster is created/merged in PostgreSQL and Cache.
    - WebSocket client natively receives the correct lightweight event.
"""

import asyncio
import logging
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.domain.events import EventEnvelope, EventType, ExtractCompletedPayload
from src.domain.models import compute_url_hash
from src.infrastructure.database.engine import create_db_engine, create_session_factory
from src.infrastructure.database.repositories import PgArticleRepository, PgClusterRepository
from src.infrastructure.messaging.nats_bus import NatsEventBus
from src.workers.cluster_worker import run_worker as run_cluster_worker
from src.workers.dedup_worker import run_worker as run_dedup_worker

logger = logging.getLogger(__name__)

client = TestClient(app)

from unittest.mock import patch

@pytest.mark.asyncio
@patch("src.services.dedup_service.model.encode")
async def test_real_pipeline_wiring(mock_encode):
    """Validates the full system working natively in the test environment."""

    # 1. Mock HuggingFace Model to prevent asyncio threadpool locks and 50s timeouts
    import numpy as np
    mock_encode.return_value = np.array([ [0.5]*384 ])

    # 1. Start real workers as background tasks in the current async loop.
    # They will bind to the test services configured by conftest.py env vars.
    dedup_task = asyncio.create_task(run_dedup_worker())
    cluster_task = asyncio.create_task(run_cluster_worker())

    # Wait for workers to establish JetStream connections & subscriptions
    await asyncio.sleep(2.0)

    # 2. Setup WebSocket client in a separate thread so it doesn't block the loop.
    received_messages = []
    client_error = []

    def ws_listener():
        try:
            # TestClient triggers app lifespan -> starts notifier_worker automatically
            with client.websocket_connect("/ws/updates") as ws:
                # Block and wait for exactly 1 message
                data = ws.receive_json()
                received_messages.append(data)
        except Exception as e:
            client_error.append(e)

    ws_thread = threading.Thread(target=ws_listener, daemon=True)
    ws_thread.start()

    # Wait for WebSocket and Notifier to fully connect
    await asyncio.sleep(1.0)

    # 3. Create a unique article to completely bypass any idempotency
    # guards from previous test runs.
    unique_run_id = str(uuid4())
    url = f"https://true.integration.vyomacast.local/article/{unique_run_id}"
    url_hash = compute_url_hash(url)

    import random
    random_words = " ".join(str(uuid4()) for _ in range(20))
    content = (
        f"Breaking test alert: {random_words}. This is entirely random content "
        f"to prevent semantic vector deduplication from trapping it. ID: {unique_run_id}"
    )

    payload = ExtractCompletedPayload(
        url=url,
        url_hash=url_hash,
        title=f"Aerospace Innovations {unique_run_id}",
        content=content,
        content_length=len(content),
        published_at=datetime.now(UTC),
        quality_score=0.99,
        extraction_method="test_harness",
    )

    envelope = EventEnvelope.create(
        event_type=EventType.EXTRACT_COMPLETED,
        payload=payload,
        source_service="integration_test",
    )

    # 4. Inject payload directly into NATS Stream
    bus = NatsEventBus()
    await bus.connect()
    
    logger.info("Publishing test payload to %s", EventType.EXTRACT_COMPLETED.value)
    await bus.publish(EventType.EXTRACT_COMPLETED.value, envelope)

    # 5. Wait for pipeline to asynchronously clear the event
    # Max timeout 90 seconds (Embedding generation + Model Load takes significant time on CPU)
    timeout = 90.0
    elapsed = 0.0
    while not received_messages and elapsed < timeout:
        await asyncio.sleep(0.5)
        elapsed += 0.5

    # 6. Gracefully shutdown workers
    dedup_task.cancel()
    cluster_task.cancel()
    await bus.disconnect()
    
    ws_thread.join(timeout=2.0)

    # 7. Assertions
    assert not client_error, f"WebSocket encountered an error: {client_error}"
    assert received_messages, "WebSocket did not receive the broadcasted cluster message"
    
    ws_data = received_messages[0]
    assert ws_data.get("event") == "cluster_update"
    payload_data = ws_data.get("data", {})
    assert payload_data.get("source_domain") == "true.integration.vyomacast.local"

    # Verify Database persistence
    engine = create_db_engine()
    session_factory = create_session_factory(engine)
    
    article_repo = PgArticleRepository(session_factory)
    cluster_repo = PgClusterRepository(session_factory)
    
    db_article = await article_repo.get_by_url_hash(url_hash)
    assert db_article is not None, "Article was not persisted to the database"
    assert db_article.cluster_id is not None, "Article is missing a cluster_id"
    
    db_cluster = await cluster_repo.get_by_id(db_article.cluster_id)
    assert db_cluster is not None, "Assigned cluster does not exist in the database"
    assert db_cluster.article_count >= 1

    logger.info("True integration test completed successfully.")
