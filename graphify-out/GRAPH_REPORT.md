# Graph Report - news  (2026-04-27)

## Corpus Check
- 60 files · ~89,119 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1104 nodes · 4852 edges · 21 communities detected
- Extraction: 23% EXTRACTED · 77% INFERRED · 0% AMBIGUOUS · INFERRED: 3718 edges (avg confidence: 0.56)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]

## God Nodes (most connected - your core abstractions)
1. `Article` - 212 edges
2. `EventEnvelope` - 196 edges
3. `Cluster` - 187 edges
4. `Feed` - 184 edges
5. `FeedStatus` - 172 edges
6. `ClusterStatus` - 164 edges
7. `EventType` - 150 edges
8. `SearchResult` - 133 edges
9. `FetchResult` - 113 edges
10. `ExtractedContent` - 100 edges

## Surprising Connections (you probably didn't know these)
- `FastAPI routes for Articles and Clusters.` --uses--> `PermanentError`  [INFERRED]
  src\api\v1\endpoints.py → src\domain\exceptions.py
- `Retrieve paginated recent articles.` --uses--> `PermanentError`  [INFERRED]
  src\api\v1\endpoints.py → src\domain\exceptions.py
- `Retrieve full article details by ID.` --uses--> `PermanentError`  [INFERRED]
  src\api\v1\endpoints.py → src\domain\exceptions.py
- `Semantic vector search using PgVector's HNSW index.` --uses--> `PermanentError`  [INFERRED]
  src\api\v1\endpoints.py → src\domain\exceptions.py
- `Retrieve paginated active clusters.` --uses--> `PermanentError`  [INFERRED]
  src\api\v1\endpoints.py → src\domain\exceptions.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (85): BaseModel, BaseSettings, Typed, validated configuration for every NewsForge component., Settings, SafeArticleResponse, SearchQuery, ArticleDuplicatePayload, ArticleUniquePayload (+77 more)

### Community 1 - "Community 1"
Cohesion: 0.09
Nodes (139): ABC, Typed, immutable result from an RSS feed fetch.      This is an infrastructure-l, True when the server returned new content (i.e. not 304)., EventEnvelope, Wire-format wrapper for all inter-service messages.      The ``payload`` is stor, Fake repository implementations for in-memory cold-state testing.  Each fake str, Clear all stored articles., Return all articles (for test assertions). (+131 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (89): ArticleRepository, check_nats(), check_postgres(), check_redis(), fail(), header(), info(), main() (+81 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (81): _deterministic_embedding(), _generate_payload(), main(), Latency Benchmark: Ingestion -> Clustering completion.  Measures end-to-end proc, Produce a 384-dim L2-normalized embedding from a seed.      cluster_group shifts, Generate a synthetic ExtractCompletedPayload and its embedding., Execute the full benchmark., run_benchmark() (+73 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (75): FeedFetchResult, Two-Stage Deduplication Engine., Generate 384-dimensional dense representation of text.          CPU-bound wrappe, EventType, FeedItemsNewPayload, A new article URL discovered from a feed poll., Every subject published on the NATS JetStream backbone., Exception (+67 more)

### Community 5 - "Community 5"
Cohesion: 0.03
Nodes (58): CacheStore, Real-Time Clustering Engine., Assign article to an existing cluster or create a new one., Transient failure that should be retried.      Examples:         * Network timeo, RetryableError, Number of clusters currently in hot state., Number of entries in the timeline., Number of dirty entities per type. (+50 more)

### Community 6 - "Community 6"
Cohesion: 0.04
Nodes (35): ClusterDetailsResponse, get_article(), get_cluster_details(), list_articles(), list_clusters(), FastAPI routes for Articles and Clusters., Retrieve paginated active clusters., Retrieve cluster metadata alongside all its resolved articles. (+27 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (30): add_simhash_bands(), add_to_timeline(), cache_embedding(), check_simhash_bands(), delete_cluster(), get_active_clusters(), get_all_cluster_centroids(), get_and_clear_dirty() (+22 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (28): initial_schema  Revision ID: 0dbf3ce903e9 Revises:  Create Date: 2026-04-18 09:0, upgrade(), AioHttpFetcher, Aiohttp-based implementation of the HttpFetcher interface.  Centralizes ALL HTTP, Fetch RSS/Atom XML with conditional GET headers.          Sends ``If-None-Match`, Release connection pool.  Implements ``HttpFetcher.close``., Production HttpFetcher backed by aiohttp.      Provides two explicit public meth, Lazy-initialize the client session. (+20 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (15): Generate a 384-dimensional dense vector for semantic search., EmbeddingService, Semantic vector search using PgVector's HNSW index., search_articles(), embed(), embed_batch(), FakeEmbeddingService, Fake embedding service for deterministic, infrastructure-free tests.  Generates (+7 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (16): DeclarativeBase, do_run_migrations(), Alembic async migration environment for NewsForge.  Configured for:     * Async, Run migrations in 'offline' mode.      Generates SQL scripts without connecting, Configure and run migrations using the given sync connection., Run migrations in 'online' mode.      Creates an async engine and runs migration, run_migrations_offline(), run_migrations_online() (+8 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (19): _article_to_dict(), _cluster_to_dict(), count(), count_active(), _feed_to_dict(), get_active(), get_all(), get_by_cluster_id() (+11 more)

### Community 13 - "Community 13"
Cohesion: 0.24
Nodes (15): connectWebSocket(), createCardElement(), enforceMemoryBound(), fetchInitialClusters(), formatRelativeTime(), handleClusterUpdate(), moveCardToFront(), renderEmptyState() (+7 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (13): now_utc(), Shared pytest fixtures for the NewsForge test suite.  Fixtures here are availabl, A realistic news article URL for testing., The canonical url_hash for sample_url., A deterministic 384-dimensional embedding vector for testing., A centroid vector (same as sample_embedding for single-article cluster)., Current UTC timestamp for deterministic test assertions., A fresh UUID v4 for testing. (+5 more)

### Community 16 - "Community 16"
Cohesion: 0.4
Nodes (1): Centralised application settings loaded from environment variables.  All setting

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): Total SimHash width in bits (bands × bits-per-band).

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): SimHash dedup window converted to seconds for Redis TTL.

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Embedding cache TTL converted to seconds for Redis TTL.

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Build an envelope from a typed payload model.          The payload is serialised

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Embedding must be empty (pre-dedup) or exactly 384 dimensions.

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Centroid must be empty (initial) or exactly 384 dimensions.

## Knowledge Gaps
- **90 isolated node(s):** `initial_schema  Revision ID: 0dbf3ce903e9 Revises:  Create Date: 2026-04-18 09:0`, `Connect to PostgreSQL, verify pgvector and uuid-ossp extensions.`, `Connect to Redis, verify maxmemory-policy is volatile-lru.`, `Connect to NATS, verify JetStream is enabled.`, `Run all health checks and return exit code.` (+85 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 16`** (5 nodes): `embedding_cache_ttl_seconds()`, `Centralised application settings loaded from environment variables.  All setting`, `simhash_total_bits()`, `simhash_window_seconds()`, `config.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (1 nodes): `Total SimHash width in bits (bands × bits-per-band).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `SimHash dedup window converted to seconds for Redis TTL.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `Embedding cache TTL converted to seconds for Redis TTL.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Build an envelope from a typed payload model.          The payload is serialised`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Embedding must be empty (pre-dedup) or exactly 384 dimensions.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Centroid must be empty (initial) or exactly 384 dimensions.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Article` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 10`, `Community 11`, `Community 12`?**
  _High betweenness centrality (0.118) - this node is a cross-community bridge._
- **Why does `EventEnvelope` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `Feed` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 10`, `Community 11`, `Community 12`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Are the 209 inferred relationships involving `Article` (e.g. with `ArticleRepository` and `ClusterRepository`) actually correct?**
  _`Article` has 209 INFERRED edges - model-reasoned connections that need verification._
- **Are the 192 inferred relationships involving `EventEnvelope` (e.g. with `ArticleRepository` and `ClusterRepository`) actually correct?**
  _`EventEnvelope` has 192 INFERRED edges - model-reasoned connections that need verification._
- **Are the 184 inferred relationships involving `Cluster` (e.g. with `ArticleRepository` and `ClusterRepository`) actually correct?**
  _`Cluster` has 184 INFERRED edges - model-reasoned connections that need verification._
- **Are the 181 inferred relationships involving `Feed` (e.g. with `Seed script: insert 40 high-quality RSS feeds into the database.  Idempotent — u` and `Insert all seed feeds into the database idempotently.`) actually correct?**
  _`Feed` has 181 INFERRED edges - model-reasoned connections that need verification._