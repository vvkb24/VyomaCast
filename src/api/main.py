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

"""Main entry point for the FastAPI application."""


import asyncio
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from contextlib import asynccontextmanager

from src.api.v1.endpoints import router as v1_router
from src.api.websocket.hub import ws_router
from src.domain.exceptions import PermanentError, RetryableError
from src.workers.notifier_worker import run_notifier

logger = logging.getLogger(__name__)

async def run_notifier_safe():
    import traceback
    try:
        print("[LIFESPAN] Starting notifier...")
        await run_notifier()
        print("[LIFESPAN] Notifier running successfully!")
    except Exception as e:
        print(f"[LIFESPAN] Notifier failed to start: {e}")
        traceback.print_exc()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle hook for starting background API workers."""
    # Start the broadcast notifier task in the background
    notifier_task = asyncio.create_task(run_notifier_safe())
    yield
    # Shutdown
    notifier_task.cancel()

app = FastAPI(
    title="VyomaCast API",
    description="Real-Time Event-Driven Clustering Engine backend interface.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Middlewares ──────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Production configs would restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global Exception Handlers ──────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Override standard mapping to ensure format matches domain constraints securely."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Payload validation failed"},
    )


@app.exception_handler(PermanentError)
async def permanent_error_handler(request: Request, exc: PermanentError):
    """Domain failure mappings that require user-intervention (HTTP 400)."""
    # Safe error message passed from domain logic (e.g., 'Query cannot be entirely whitespace.')
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(RetryableError)
async def retryable_error_handler(request: Request, exc: RetryableError):
    """Transient failures mapping to Gateway/Server timeout ranges (HTTP 503)."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Temporary service failure, please try again."},
    )


# ── Routers ──────────────

app.include_router(v1_router, prefix="/api/v1")
app.include_router(ws_router)


@app.get("/health", response_model=dict[str, str])
async def health_check():
    """Simple L7 liveness endpoint."""
    return {"status": "healthy"}
