# MVP Blockers Fixed

This document records the resolution of the three critical MVP architectural blockers identified in the system audit.

## 1. Redis OOM Memory Leaks

### Problem
The `timeline:global` sorted set continuously grew with every new article because elements were appended without any truncation. Similarly, cluster hashes were stored without a Time-To-Live (TTL), meaning inactive clusters would reside in Redis memory indefinitely, ensuring an eventual Out-Of-Memory (OOM) crash under production load.

### Solution
- Updated `add_to_timeline` in `src/infrastructure/cache/redis_cache.py` to cap the sorted set size immediately after adding a new article using `pipe.zremrangebyrank("timeline:global", 0, -1001)`.
- Updated `set_cluster` in `src/infrastructure/cache/redis_cache.py` to append an `ex=86400` (24-hour) TTL when saving cluster hashes so that inactive clusters eventually age out of Redis memory.

## 2. Event Loop Starvation

### Problem
The deduplication process relied on `compute_simhash()` which ran synchronously within the `asyncio` event loop. Calculating 128 bitwise loops over thousands of article shingles using `hashlib.md5()` entirely blocked the event loop, breaking NATS heartbeats and causing relentless message redelivery loops.

### Solution
- Updated `process_article` in `src/services/dedup_service.py` to offload the synchronous hashing process to a background thread using `asyncio.to_thread(self.compute_simhash, norm_text)`. This allows the event loop to continue breathing and processing concurrent websocket and NATS heartbeat events.

## 3. Data Corruption (Missing Atomic Transactions)

### Problem
The Cluster worker updated the cluster's article count (`cluster_repo.save`) and associated the article to the cluster (`article_repo.save`) in completely independent database sessions. A service crash or transient database failure between these two discrete transactions would corrupt the database (e.g., the cluster size increases, but the article is left unlinked).

### Solution
- Modified the repository pattern in `src/infrastructure/database/repositories.py` so that `PgClusterRepository.save()` and `PgArticleRepository.save()` accept an optional SQLAlchemy `AsyncSession` parameter.
- Updated `process_article` in `src/services/cluster_service.py` to check for the repository's internal `_session_factory`. If present, both saves are executed inside a unified `async with session.begin():` block, guaranteeing ACID compliance and rolling back both operations if an optimistic concurrency exception is raised.
