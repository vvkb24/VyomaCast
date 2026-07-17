"""
export_data.py -- Export all VyomaCast data to CSV files.

Usage:
    python scripts/export_data.py
    python scripts/export_data.py --output exports/
    python scripts/export_data.py --topic Technology
    python scripts/export_data.py --format csv
    python scripts/export_data.py --format json

Outputs:
    articles_export.csv  -- one row per article
    clusters_export.csv  -- one row per cluster
"""

import asyncio
import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Topic keyword map -- used to classify cluster titles into topics.
# Rules are checked in order; first match wins. "General" is the fallback.
# ---------------------------------------------------------------------------
TOPIC_RULES = [
    ("Technology", [
        "ai", "artificial intelligence", "machine learning", "software",
        "hardware", "apple", "google", "microsoft", "meta", "amazon",
        "nvidia", "chip", "semiconductor", "robot", "drone", "cyber",
        "hack", "data", "cloud", "startup", "app", "iphone", "android",
        "tesla", "electric vehicle", "ev", "5g", "quantum", "linux",
        "open source", "github", "api", "llm", "gpt", "model",
    ]),
    ("Business & Finance", [
        "stock", "market", "nasdaq", "dow", "s&p", "earnings", "revenue",
        "profit", "loss", "ipo", "acquisition", "merger", "bank", "fed",
        "federal reserve", "inflation", "gdp", "economy", "trade",
        "tariff", "billion", "trillion", "investment", "venture",
        "fund", "hedge", "crypto", "bitcoin", "ethereum", "currency",
        "dollar", "euro", "rate", "interest", "recession",
    ]),
    ("Politics", [
        "president", "prime minister", "parliament", "senate", "congress",
        "election", "vote", "democrat", "republican", "government",
        "minister", "policy", "law", "legislation", "bill", "court",
        "supreme court", "judge", "trump", "biden", "modi", "xi",
        "putin", "nato", "un", "united nations", "sanction", "diplomat",
        "treaty", "protest", "democracy",
    ]),
    ("World News", [
        "war", "attack", "military", "troops", "ukraine", "russia",
        "israel", "gaza", "iran", "china", "north korea", "taiwan",
        "conflict", "ceasefire", "missile", "bomb", "refugee",
        "humanitarian", "crisis", "earthquake", "flood", "disaster",
        "africa", "europe", "asia", "middle east",
    ]),
    ("Science", [
        "research", "study", "scientists", "discovery", "space", "nasa",
        "planet", "star", "galaxy", "climate", "carbon", "fossil",
        "species", "gene", "dna", "brain", "physics", "chemistry",
        "biology", "quantum", "experiment", "university", "journal",
        "nature", "cell", "protein", "virus", "bacteria",
    ]),
    ("Health & Medicine", [
        "health", "hospital", "doctor", "patient", "drug", "vaccine",
        "cancer", "disease", "diabetes", "heart", "mental health",
        "therapy", "surgery", "fda", "clinical trial", "treatment",
        "medicine", "covid", "outbreak", "epidemic", "obesity",
        "nutrition", "diet", "fitness", "sleep",
    ]),
    ("Sports", [
        "football", "soccer", "basketball", "tennis", "cricket",
        "baseball", "rugby", "golf", "olympic", "championship",
        "tournament", "league", "nba", "nfl", "fifa", "premier league",
        "player", "coach", "team", "match", "game", "score", "win",
        "defeat", "final", "cup", "athlete",
    ]),
    ("Entertainment", [
        "movie", "film", "music", "album", "singer", "actor", "actress",
        "celebrity", "award", "oscar", "grammy", "netflix", "streaming",
        "tv show", "series", "book", "novel", "author", "concert",
        "festival", "fashion", "art", "museum", "theatre",
    ]),
    ("Environment", [
        "climate change", "global warming", "emission", "renewable",
        "solar", "wind energy", "fossil fuel", "deforestation",
        "wildlife", "conservation", "ocean", "pollution", "plastic",
        "biodiversity", "endangered", "glacier", "temperature record",
    ]),
]


def classify_topic(text: str) -> str:
    """Classify a text string into a topic using keyword matching."""
    text_lower = text.lower()
    for topic, keywords in TOPIC_RULES:
        for keyword in keywords:
            if keyword in text_lower:
                return topic
    return "General"


async def fetch_all_data(database_url: str):
    """Fetch all clusters and articles from PostgreSQL."""
    try:
        import asyncpg
    except ImportError:
        print("[FAIL] asyncpg not installed. Run: pip install asyncpg")
        sys.exit(1)

    conn = await asyncpg.connect(
        database_url.replace("postgresql+asyncpg://", "postgresql://")
    )

    print("[INFO] Fetching clusters...")
    clusters = await conn.fetch("""
        SELECT
            c.id,
            c.label,
            c.article_count,
            c.created_at,
            c.last_activity,
            c.decay_score,
            c.status,
            array_to_string(c.top_sources, '|') as top_sources
        FROM clusters c
        ORDER BY c.last_activity DESC
    """)

    print(f"[INFO] Fetched {len(clusters)} clusters.")

    print("[INFO] Fetching articles...")
    articles = await conn.fetch("""
        SELECT
            a.id,
            a.url,
            a.title,
            a.language,
            a.content_length,
            a.quality_score,
            a.extracted_at,
            a.published_at,
            a.cluster_id
        FROM articles a
        ORDER BY a.extracted_at DESC
    """)

    print(f"[INFO] Fetched {len(articles)} articles.")

    await conn.close()

    return [dict(r) for r in clusters], [dict(r) for r in articles]


def export_clusters_csv(clusters: list, output_path: Path, topic_filter: str = None):
    """Export clusters to CSV with topic classification."""
    path = output_path / "clusters_export.csv"
    written = 0

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "cluster_id", "topic", "label", "article_count", "status",
            "top_sources", "created_at", "last_activity", "decay_score",
        ])
        writer.writeheader()

        for c in clusters:
            topic = classify_topic(c["label"] or "")
            if topic_filter and topic.lower() != topic_filter.lower():
                continue

            writer.writerow({
                "cluster_id": str(c["id"]),
                "topic": topic,
                "label": c["label"] or "",
                "article_count": c["article_count"] or 0,
                "status": c["status"] or "active",
                "top_sources": c["top_sources"] or "",
                "created_at": c["created_at"].isoformat() if c["created_at"] else "",
                "last_activity": c["last_activity"].isoformat() if c["last_activity"] else "",
                "decay_score": round(c["decay_score"] or 0.0, 4),
            })
            written += 1

    print(f"[PASS] clusters_export.csv -- {written} clusters written -> {path}")
    return path


def export_articles_csv(articles: list, clusters: list, output_path: Path, topic_filter: str = None):
    """Export articles to CSV with cluster info and topic classification."""
    path = output_path / "articles_export.csv"

    # Build cluster lookup: cluster_id -> (label, topic)
    cluster_map = {}
    for c in clusters:
        topic = classify_topic(c["label"] or "")
        cluster_map[c["id"]] = {
            "label": c["label"] or "",
            "topic": topic,
        }

    written = 0

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "article_id", "topic", "cluster_label", "cluster_id",
            "article_title", "url", "language",
            "content_length", "quality_score", "extracted_at", "published_at",
        ])
        writer.writeheader()

        for a in articles:
            cluster_info = cluster_map.get(a["cluster_id"], {})
            topic = cluster_info.get("topic", "Unclustered")
            cluster_label = cluster_info.get("label", "")

            if topic_filter and topic.lower() != topic_filter.lower():
                continue

            writer.writerow({
                "article_id": str(a["id"]),
                "topic": topic,
                "cluster_label": cluster_label,
                "cluster_id": str(a["cluster_id"]) if a["cluster_id"] else "",
                "article_title": a["title"] or "",
                "url": a["url"] or "",
                "language": a["language"] or "en",
                "content_length": a["content_length"] or 0,
                "quality_score": round(a["quality_score"] or 0.0, 4),
                "extracted_at": a["extracted_at"].isoformat() if a["extracted_at"] else "",
                "published_at": a["published_at"].isoformat() if a["published_at"] else "",
            })
            written += 1

    print(f"[PASS] articles_export.csv -- {written} articles written -> {path}")
    return path


def export_json(clusters: list, articles: list, output_path: Path, topic_filter: str = None):
    """Export everything to a single structured JSON file, grouped by topic."""
    # Build article lookup by cluster_id (str key for consistent UUID matching)
    articles_by_cluster = {}
    for a in articles:
        cid = str(a["cluster_id"]) if a["cluster_id"] else "__unclustered__"
        if cid not in articles_by_cluster:
            articles_by_cluster[cid] = []
        articles_by_cluster[cid].append({
            "id": str(a["id"]),
            "title": a["title"] or "",
            "url": a["url"] or "",
            "language": a["language"] or "en",
            "content_length": a["content_length"] or 0,
            "quality_score": round(a["quality_score"] or 0.0, 4),
            "extracted_at": a["extracted_at"].isoformat() if a["extracted_at"] else "",
            "published_at": a["published_at"].isoformat() if a["published_at"] else "",
        })

    # Group clusters by topic (use str(id) as lookup key)
    by_topic = {}
    for c in clusters:
        topic = classify_topic(c["label"] or "")
        if topic_filter and topic.lower() != topic_filter.lower():
            continue
        if topic not in by_topic:
            by_topic[topic] = []
        cluster_id_str = str(c["id"])
        by_topic[topic].append({
            "id": cluster_id_str,
            "label": c["label"] or "",
            "article_count": c["article_count"] or 0,
            "status": c["status"] or "active",
            "top_sources": (c["top_sources"] or "").split("|"),
            "created_at": c["created_at"].isoformat() if c["created_at"] else "",
            "last_activity": c["last_activity"].isoformat() if c["last_activity"] else "",
            "articles": articles_by_cluster.get(cluster_id_str, []),
        })

    output = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "topic_filter": topic_filter or "all",
        "topic_count": len(by_topic),
        "total_clusters": sum(len(v) for v in by_topic.values()),
        "total_articles": sum(
            len(cl["articles"]) for clusters in by_topic.values() for cl in clusters
        ),
        "topics": by_topic,
    }

    path = output_path / "vyomacast_export.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    total_clusters = output["total_clusters"]
    total_articles = output["total_articles"]
    print(f"[PASS] vyomacast_export.json -- {total_clusters} clusters, {total_articles} articles -> {path}")
    return path


def print_topic_summary(clusters: list):
    """Print a breakdown of how many clusters fall into each topic."""
    counts = {}
    for c in clusters:
        topic = classify_topic(c["label"] or "")
        counts[topic] = counts.get(topic, 0) + 1

    print("\n[INFO] Topic distribution (from cluster titles):")
    print(f"  {'Topic':<25} {'Clusters':>10}")
    print(f"  {'-'*25} {'-'*10}")
    for topic, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {topic:<25} {count:>10}")
    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Export VyomaCast data to CSV or JSON."
    )
    parser.add_argument(
        "--output",
        default="exports",
        help="Output directory (default: exports/)",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help=(
            "Filter by topic. Options: Technology, Business & Finance, "
            "Politics, World News, Science, Health & Medicine, "
            "Sports, Entertainment, Environment, General"
        ),
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print topic distribution summary and exit without exporting",
    )
    args = parser.parse_args()

    # Load DATABASE_URL from environment or .env file
    database_url = os.environ.get("VYOMACAST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("VYOMACAST_DATABASE_URL=") or line.startswith("DATABASE_URL="):
                    database_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break


    if not database_url:
        print("[FAIL] DATABASE_URL not found in environment or .env file.")
        sys.exit(1)

    clusters, articles = await fetch_all_data(database_url)

    if args.summary:
        print_topic_summary(clusters)
        return

    print_topic_summary(clusters)

    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] Writing exports to: {output_path.resolve()}\n")

    if args.format in ("csv", "both"):
        export_clusters_csv(clusters, output_path, topic_filter=args.topic)
        export_articles_csv(articles, clusters, output_path, topic_filter=args.topic)

    if args.format in ("json", "both"):
        export_json(clusters, articles, output_path, topic_filter=args.topic)

    print("\n[PASS] Export complete.")


if __name__ == "__main__":
    asyncio.run(main())
