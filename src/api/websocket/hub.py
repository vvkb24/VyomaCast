"""WebSocket hub for real-time client updates."""

import asyncio
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages active WebSocket connections and broadcasting."""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new connection and store it securely."""
        await websocket.accept()
        async with self.lock:
            self.active_connections.add(websocket)
        logger.debug("Client connected. Active WebSockets: %d", len(self.active_connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a connection safely."""
        async with self.lock:
            self.active_connections.discard(websocket)
        logger.debug("Client disconnected. Active WebSockets: %d", len(self.active_connections))

    async def broadcast(self, message: dict) -> None:
        """Broadcast a JSON message to all active clients concurrently."""
        async with self.lock:
            # Copy snapshot to avoid mutation during concurrency
            connections = list(self.active_connections)

        if not connections:
            return

        async def send_msg(ws: WebSocket) -> None:
            try:
                # Strict push and move on
                await asyncio.wait_for(ws.send_json(message), timeout=1.0)
            except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError, Exception) as e:
                logger.warning("Broadcast failed to client, removing dead connection: %s", type(e).__name__)
                await self.disconnect(ws)

        # Safe concurrency: gather tasks concurrently preventing unbounded growth
        # and silent task failures.
        results = await asyncio.gather(
            *(send_msg(ws) for ws in connections), 
            return_exceptions=True
        )
        
        for res in results:
            if isinstance(res, Exception):
                logger.error("Unexpected error during broadcast execution: %s", type(res).__name__)

    async def close_all(self) -> None:
        """Cleanly close all connections during shutdown."""
        async with self.lock:
            connections = list(self.active_connections)
        
        for ws in connections:
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                pass
            await self.disconnect(ws)


# Global instance for the FastAPI application
manager = ConnectionManager()
ws_router = APIRouter()

@ws_router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket) -> None:
    """Real-time cluster updates endpoint.

    Keeps the connection open with a server-side ping every 20 seconds.
    The browser does not need to send anything to stay connected.
    """
    await manager.connect(websocket)
    try:
        while True:
            # Send a lightweight server-side ping every 20 seconds to
            # keep the connection alive. The browser can ignore it.
            await asyncio.sleep(20)
            try:
                await asyncio.wait_for(
                    websocket.send_json({"event": "ping"}), timeout=5.0
                )
            except (asyncio.TimeoutError, Exception):
                # Connection is gone - stop the loop
                break
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("WebSocket unhandled error: %s", e)
    finally:
        await manager.disconnect(websocket)
