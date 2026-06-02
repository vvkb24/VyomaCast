# VyomaCast — Startup & Testing Guide

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | ≥ 3.12 | `python --version` |
| Docker Desktop | Running | `docker info` |
| pip | Latest | `pip --version` |

---

## Step 1 — Create `.env` from the template

You don't have a `.env` file yet. The app reads config from it at startup.

```powershell
# From e:\news
Copy-Item .env.example .env
```

The defaults in `.env.example` are already correct for local Docker ports (5433/6380/4223). No edits needed.

---

## Step 2 — Start Infrastructure (Docker)

This boots **PostgreSQL + pgvector**, **Redis 7**, and **NATS JetStream**.

```powershell
cd e:\news
docker compose up -d
```

Wait ~10 seconds for health checks, then verify:

```powershell
python scripts/check_infra.py
```

You should see all three services report **HEALTHY**.

---

## Step 3 — Install Python Dependencies

```powershell
cd e:\news
pip install -e ".[dev]"
```

This installs:
- Core: FastAPI, SQLAlchemy, asyncpg, redis, nats-py, sentence-transformers, trafilatura
- Dev: pytest, pytest-asyncio, httpx, ruff

> [!NOTE]
> First run will download the `all-MiniLM-L6-v2` embedding model (~80MB). This is normal.

---

## Step 4 — Run Database Migrations

```powershell
cd e:\news
alembic upgrade head
```

Verify the schema was created:

```powershell
python scripts/verify_schema.py
```

---

## Step 5 — Run Tests

### Unit Tests (no infrastructure needed)

These test domain models, fakes, dedup logic, clustering math, and fetcher service:

```powershell
pytest tests/unit/ -v
```

**Expected:** ~160+ tests passing (models, fakes, dedup, clustering, fetcher).

### Integration Tests (requires Docker running)

These test against live Redis, NATS, and the FastAPI app:

```powershell
# Redis cache tests
pytest tests/unit/test_redis_cache.py -v

# NATS JetStream tests (ACK/NAK/TERM verification)
pytest tests/integration/test_nats_bus.py -v

# API endpoint tests
pytest tests/integration/test_api.py -v

# WebSocket tests
pytest tests/integration/test_websocket.py -v
```

### Run Everything

```powershell
pytest tests/ -v --tb=short
```

---

## Step 6 — Start the API Server

```powershell
cd e:\news
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Once running, verify:

| Endpoint | URL |
|---|---|
| Health check | http://localhost:8000/health |
| API docs (Swagger) | http://localhost:8000/docs |
| Clusters list | http://localhost:8000/api/v1/clusters |
| Articles list | http://localhost:8000/api/v1/articles |

> [!IMPORTANT]
> The API also starts the **Notifier Worker** in the background (via FastAPI lifespan). It will auto-subscribe to NATS `ArticleClustered` events and broadcast to connected WebSocket clients.

---

## Step 7 — Open the Dashboard

The dashboard is static HTML — no build tools needed. Just open it in your browser:

```powershell
start e:\news\dashboard\index.html
```

Or serve it via Python's built-in HTTP server for proper WebSocket/fetch support:

```powershell
cd e:\news\dashboard
python -m http.server 3000
```

Then open: **http://localhost:3000**

### What you'll see:

| State | Behavior |
|---|---|
| API not running | "Failed to load clusters" empty state, status = 🔴 Offline |
| API running, no data | "No clusters yet" empty state, status = 🟢 Live |
| API running, with data | Cluster cards populate the grid automatically |
| WebSocket receives event | Card flashes cyan, count updates, card moves to front |

---

## Step 8 — Run Pipeline Workers (Optional — for live data)

To get actual articles flowing through the pipeline, you'd start the individual workers in separate terminals:

```powershell
# Terminal 1: Fetcher Worker
python -m src.workers.fetcher_worker

# Terminal 2: Dedup Worker  
python -m src.workers.dedup_worker

# Terminal 3: Cluster Worker
python -m src.workers.cluster_worker
```

> [!NOTE]
> Workers don't have `__main__.py` entry points yet (that's a future task). You may need to run them as:
> ```powershell
> python -c "import asyncio; from src.workers.cluster_worker import run_worker; asyncio.run(run_worker())"
> ```

---

## Quick Reference — Command Cheatsheet

```
# ── Infrastructure ──
docker compose up -d              # Start Postgres, Redis, NATS
docker compose down -v            # Stop + delete volumes
python scripts/check_infra.py     # Health check all 3 services

# ── Database ──
alembic upgrade head              # Apply migrations
alembic downgrade -1              # Rollback last migration
python scripts/verify_schema.py   # Verify tables/indexes

# ── Tests ──
pytest tests/unit/ -v             # Unit tests only (fast, no Docker)
pytest tests/ -v                  # Full suite (needs Docker)
pytest tests/ -v -k "test_api"   # Run specific test pattern

# ── API ──
uvicorn src.api.main:app --reload --port 8000

# ── Dashboard ──
cd dashboard && python -m http.server 3000
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Run `pip install -e ".[dev]"` from `e:\news` |
| `ConnectionRefusedError` on port 5433 | Run `docker compose up -d` first |
| `alembic` command not found | Ensure pip installed with `pip install -e ".[dev]"` |
| NATS tests fail with timeout | Check `docker compose ps` — NATS container must be running |
| Dashboard shows "Offline" | API must be running on port 8000 |
| Dashboard shows "Failed to load" | API is running but CORS or network issue — use `http.server` instead of `file://` |
