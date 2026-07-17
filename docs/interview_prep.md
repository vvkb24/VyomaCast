# VyomaCast: Interview Preparation Guide

This document is designed to help you explain VyomaCast to highly experienced (Senior/Staff) software engineers and technical hiring managers. It covers how to frame your architectural decisions, defend your tech stack choices, and anticipate deep-dive distributed systems questions.

---

## 1. The "Elevator Pitch" (How to introduce it)

**Don't say:** *"I built a website that pulls RSS feeds and shows news using AI."* (Sounds like a basic bootcamp CRUD app).

**Say this:** *"VyomaCast is a real-time, event-driven streaming pipeline. It autonomously ingests raw RSS data, deduplicates it using a two-stage lexical and semantic filtering engine, and broadcasts mathematically clustered storylines via WebSockets. I built it using a distributed microservices architecture communicating over NATS JetStream, backed by PostgreSQL with pgvector for the machine learning embeddings."*

**Why this works:** It immediately signals to an experienced engineer that you understand distributed systems, data pipelines, asynchronous processing, and vector search.

---

## 2. Tech Stack Justifications (Why X and not Y?)

Experienced interviewers *love* to ask "Why didn't you just use [Alternative]?" They are testing your ability to evaluate trade-offs.

### NATS JetStream (Event Broker)
* **Why we used it:** It is lightweight, incredibly fast, and provides "exactly-once" delivery semantics and consumer groups for our background workers. 
* **Why not Kafka?** Kafka is the industry standard but is notoriously heavy, requires massive JVM tuning, and is overkill for a single-developer project. NATS is a single binary written in Go that provides 90% of Kafka's features with 1% of the operational headache.
* **Why not RabbitMQ?** RabbitMQ is great for simple task queues (like Celery), but NATS JetStream provides durable logs (allowing us to replay events if a worker crashes) and is much faster for pub-sub broadcasting.

### PostgreSQL + pgvector (Database)
* **Why we used it:** We needed both rigid relational data (Articles, Timestamps) and high-dimensional vector search (384-dimension embeddings) for semantic clustering. `pgvector` allows us to do `ORDER BY embedding <=> query_vector` natively in SQL.
* **Why not a dedicated Vector DB (Pinecone / Milvus)?** Network overhead and data fragmentation. If we used Pinecone, we would have to keep our Postgres relational data and Pinecone vector data perfectly synced. If one network call failed, we'd have orphaned data. Using `pgvector` gives us ACID compliance—both the article metadata and the vector embedding are saved in a single database transaction.

### Redis (Caching & Rate Limiting)
* **Why we used it:** Deduplication requires sliding-window checks (SimHash) and cluster centroid lookups for every single incoming article. Hitting Postgres for every check would crash the database. Redis keeps the "Top 100 Active Clusters" in RAM (O(1) lookup).
* **Why not Memcached?** Memcached only supports simple strings. Redis supports complex data structures (Hashes, Sets, Sorted Sets) which we need for sliding windows and active cluster tracking.

### Sentence-Transformers (all-MiniLM-L6-v2)
* **Why we used it:** It runs 100% locally on CPU, is open-source, and produces 384-dimensional vectors in milliseconds.
* **Why not OpenAI (text-embedding-ada-002)?** Cost and Latency. OpenAI costs money per token, and making a network call to an external API for every incoming news article would introduce massive latency and rate-limiting bottlenecks. For news clustering, MiniLM is more than accurate enough.

### WebSockets (Real-Time Dashboard)
* **Why we used it:** True bi-directional, persistent connection. The millisecond the Cluster Worker merges an article, the Notifier Worker pushes it to the browser.
* **Why not Polling / REST?** Having thousands of browsers send a `GET /api/clusters` every 5 seconds would DDOS our own API server and waste massive amounts of bandwidth.
* **Why not Server-Sent Events (SSE)?** SSE is a viable alternative (it is one-way server-to-client), but WebSockets give us more flexibility if we want to add two-way interactivity (like chatting with the AI) in Phase 2.

---

## 3. Anticipated Hard Questions & How to Answer Them

### Q1: "What happens if a worker crashes halfway through processing an article?"
**Your Answer:** *"Because we use NATS JetStream, messages are not acknowledged (`ACK`) until the database transaction is successfully committed. If the worker crashes, a timeout occurs, and NATS automatically redelivers the message to another healthy worker. To prevent infinite loops (Poison Pills), if a message fails 5 times, it is sent to a Dead Letter Queue (DLQ)."*

### Q2: "How do you handle race conditions if two workers try to update the same Cluster at the exact same millisecond?"
**Your Answer:** *"We use Optimistic Concurrency Control (OCC) in PostgreSQL. The `clusters` table has a `version` column. When a worker reads a cluster, it notes the version (e.g., v4). When it writes back, it explicitly queries `UPDATE clusters SET ... WHERE id = X AND version = 4`. If another worker updated it to v5 in the meantime, our update affects 0 rows, and our worker knows it needs to fetch the fresh data and try again."*

### Q3: "Is your system scalable? What if I send you 100,000 articles right now?"
**Your Answer:** *"Yes, I actually built a benchmark suite injecting isolated 'fakes' (Dependency Injection) to test the pure CPU limits of the Python logic. The system processed 100,000 articles at a throughput of 490 articles per second with a median latency of 1.7 milliseconds. Because it's event-driven, if we hit network I/O limits, we can horizontally scale by just spinning up more Docker containers of the Cluster Worker."*

### Q4: "Why use two stages for deduplication (SimHash + Vectors)? Why not just use Vectors for everything?"
**Your Answer:** *"Cost of computation. Vector cosine similarity across a growing database is mathematically expensive (O(N) or O(log N) with HNSW). Computing an NLP embedding takes ~50ms. By putting a highly efficient lexical SimHash check in front, we filter out 70-80% of exact syndicated duplicates in under 1ms. The heavy vector math is reserved only for truly unique content, acting as a performance firewall."*

### Q5: "If this was going into a production enterprise environment tomorrow, what is the biggest flaw you would fix?"
**Your Answer:** *"Currently, if the WebSocket hub crashes, browsers lose connection and don't automatically backfill missed messages when they reconnect. In an enterprise environment, I would add a 'last_seen_timestamp' to the WebSocket reconnection handshake, so the API can query Postgres and send any clusters that were formed during the 5 seconds the user was disconnected."*

---

## Final Tip for the Interview
If they ask you a question you don't know the answer to, **do not guess**. Say: *"I haven't encountered that specific edge case yet, but based on the architecture, I would investigate [X] and look into implementing [Y]."* Senior engineers respect developers who know the boundaries of their system.
