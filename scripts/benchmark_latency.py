"""Latency Benchmark: Ingestion -> Clustering completion.

Measures end-to-end processing latency by injecting N synthetic articles
through the Dedup -> Clustering pipeline using in-memory fakes.

This script does NOT require live infrastructure. It benchmarks the
pure Python processing path (embedding mocked, no network I/O).

Usage:
    python scripts/benchmark_latency.py              # default 100 articles
    python scripts/benchmark_latency.py --count 500  # custom count
"""

import argparse
import asyncio
import hashlib
import logging
import math
import statistics
import time
from datetime import UTC, datetime
from unittest.mock import patch

# Ensure test config is loaded
import os
os.environ.setdefault("VYOMACAST_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5433/test")
os.environ.setdefault("VYOMACAST_REDIS_URL", "redis://localhost:6380/15")
os.environ.setdefault("VYOMACAST_NATS_URL", "nats://localhost:4223")

from src.domain.events import (
    ArticleUniquePayload,
    EventType,
    ExtractCompletedPayload,
)
from src.domain.models import compute_url_hash
from src.services.cluster_service import ClusterService
from src.services.dedup_service import DedupService
from tests.fakes.fake_cache import FakeCacheStore
from tests.fakes.fake_event_bus import FakeEventBus
from tests.fakes.fake_repository import (
    FakeArticleRepository,
    FakeClusterRepository,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# -- Helpers -------------------------------------------------------------------


def _deterministic_embedding(seed: str, cluster_group: int = 0) -> list[float]:
    """Produce a 384-dim L2-normalized embedding from a seed.

    cluster_group shifts the embedding so articles in the same group
    produce similar vectors (simulating real-world clustering).
    """
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    offset = (h % 10000) * 0.001
    # cluster_group creates distinct "topic clusters"
    group_offset = cluster_group * 3.14159
    vec = [math.sin((i + offset + group_offset) * 0.1) * 0.5 for i in range(384)]
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return [0.0] * 384
    return [v / norm for v in vec]


def _generate_payload(index: int, cluster_group: int) -> tuple[ExtractCompletedPayload, list[float]]:
    """Generate a synthetic ExtractCompletedPayload and its embedding."""
    url = f"https://news-{cluster_group}.example.com/article/{index}"
    url_hash = compute_url_hash(url)
    content = (
        f"Article {index} from cluster group {cluster_group}. "
        f"This is synthetic content for benchmarking the ingestion pipeline. "
        f"It contains enough text to pass quality filters and produce "
        f"meaningful SimHash fingerprints for deduplication testing. "
        f"Unique identifier: {url_hash[:16]}."
    )

    payload = ExtractCompletedPayload(
        url=url,
        url_hash=url_hash,
        title=f"Benchmark Article {index} (Group {cluster_group})",
        content=content,
        content_length=len(content),
        quality_score=0.9,
        published_at=datetime.now(UTC),
    )
    embedding = _deterministic_embedding(url_hash, cluster_group)
    return payload, embedding


# -- Benchmark Runner ----------------------------------------------------------


async def run_benchmark(count: int) -> None:
    """Execute the full benchmark."""
    print(f"\n{'=' * 50}")
    print(f"  VyomaCast Pipeline Latency Benchmark")
    print(f"  Articles: {count}")
    print(f"{'=' * 50}\n")

    # Initialize in-memory infrastructure
    cache = FakeCacheStore()
    await cache.connect()
    article_repo = FakeArticleRepository()
    cluster_repo = FakeClusterRepository()
    bus = FakeEventBus()

    dedup = DedupService(cache, article_repo, bus)
    cluster_svc = ClusterService(cache, cluster_repo, article_repo, bus)

    # Distribute articles across ~10 topic clusters for realistic merging
    num_clusters = max(1, count // 10)

    # Distribute articles across ~10 topic clusters for realistic merging
    num_clusters = max(1, count // 10)

    print(f"  Generating and processing {count} synthetic articles on the fly...")
    print(f"  Across {num_clusters} topic groups")
    print(f"  Running pipeline...\n")

    latencies_ms: list[float] = []
    dedup_latencies_ms: list[float] = []
    cluster_latencies_ms: list[float] = []
    errors = 0
    new_clusters = 0
    merged_into = 0

    wall_start = time.perf_counter()

    for idx in range(count):
        # Generate payload dynamically to save RAM
        cluster_group = idx % num_clusters
        payload, embedding = _generate_payload(idx, cluster_group)
        
        t0 = time.perf_counter()

        try:
            # -- Dedup Stage --
            t_dedup_start = time.perf_counter()
            with patch.object(dedup, "_compute_embedding", return_value=embedding):
                await dedup.process_article(payload)
            t_dedup_end = time.perf_counter()
            dedup_latencies_ms.append((t_dedup_end - t_dedup_start) * 1000)

            # -- Cluster Stage --
            unique_events = bus.get_events(EventType.ARTICLE_UNIQUE)
            if unique_events:
                latest = unique_events[-1]
                unique_payload = latest.parse_payload(ArticleUniquePayload)

                t_cluster_start = time.perf_counter()
                await cluster_svc.process_article(unique_payload)
                t_cluster_end = time.perf_counter()
                cluster_latencies_ms.append((t_cluster_end - t_cluster_start) * 1000)
                
            # Count cluster actions generated in THIS iteration
            clustered_events = bus.get_events(EventType.ARTICLE_CLUSTERED)
            for e in clustered_events:
                if e.payload.get("is_new_cluster", False):
                    new_clusters += 1
                else:
                    merged_into += 1
                    
            # CRITICAL: Clear the bus to prevent memory leak and O(N^2) search times
            bus.clear()

        except Exception as e:
            errors += 1
            if errors <= 3:
                logger.warning("Error processing article %d: %s", idx, e)

        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000)

        # Progress indicator every 10%
        if count >= 10 and (idx + 1) % (count // 10) == 0:
            pct = ((idx + 1) / count) * 100
            print(f"  [{pct:5.1f}%] Processed {idx + 1}/{count} articles...")

    wall_end = time.perf_counter()
    wall_seconds = wall_end - wall_start

    # -- Compute Statistics --

    if not latencies_ms:
        print("\n  ERROR: No articles were processed successfully.")
        return

    sorted_latencies = sorted(latencies_ms)
    p50 = sorted_latencies[int(len(sorted_latencies) * 0.50)]
    p90 = sorted_latencies[int(len(sorted_latencies) * 0.90)]
    p99 = sorted_latencies[min(int(len(sorted_latencies) * 0.99), len(sorted_latencies) - 1)]
    mean = statistics.mean(latencies_ms)
    throughput = count / wall_seconds if wall_seconds > 0 else 0

    # Stage-level breakdowns
    dedup_p50 = sorted(dedup_latencies_ms)[int(len(dedup_latencies_ms) * 0.50)] if dedup_latencies_ms else 0
    cluster_p50 = sorted(cluster_latencies_ms)[int(len(cluster_latencies_ms) * 0.50)] if cluster_latencies_ms else 0

    # -- Output --

    print(f"\n{'-' * 50}")
    print(f"  RESULTS")
    print(f"{'-' * 50}")
    print(f"  Processed:    {count} articles")
    print(f"  Errors:       {errors}")
    print(f"  Wall time:    {wall_seconds:.2f}s")
    print(f"  Throughput:   {throughput:.1f} articles/sec")
    print()
    print(f"  End-to-End Latency (Dedup + Cluster):")
    print(f"    P50:  {p50:.1f}ms")
    print(f"    P90:  {p90:.1f}ms")
    print(f"    P99:  {p99:.1f}ms")
    print(f"    Mean: {mean:.1f}ms")
    print()
    print(f"  Stage Breakdown (P50):")
    print(f"    Dedup:    {dedup_p50:.1f}ms")
    print(f"    Cluster:  {cluster_p50:.1f}ms")
    print()
    print(f"  Clustering:")
    print(f"    New clusters:   {new_clusters}")
    print(f"    Merged into:    {merged_into}")
    print(f"{'-' * 50}\n")


# -- Entry Point ---------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark VyomaCast pipeline latency"
    )
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=100,
        help="Number of articles to process (default: 100)",
    )
    args = parser.parse_args()

    if args.count < 1:
        print("ERROR: --count must be >= 1")
        return

    asyncio.run(run_benchmark(args.count))


if __name__ == "__main__":
    main()
