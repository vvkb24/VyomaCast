"""Quick verification that the schema was applied correctly."""
import asyncio
import asyncpg


async def verify():
    conn = await asyncpg.connect(
        "postgresql://vyomacast:vyomacast@localhost:5433/vyomacast"
    )
    try:
        # Check tables
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        print("Tables in database:")
        for t in tables:
            if t["tablename"] != "alembic_version":
                cols = await conn.fetch(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = $1 ORDER BY ordinal_position",
                    t["tablename"],
                )
                print(f"  {t['tablename']} ({len(cols)} columns)")

        # Check indexes
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'ix_%' ORDER BY indexname"
        )
        print(f"\nCustom indexes ({len(indexes)}):")
        for i in indexes:
            print(f"  {i['indexname']}")

        # Check extensions
        exts = await conn.fetch(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'uuid-ossp')"
        )
        print(f"\nExtensions: {[e['extname'] for e in exts]}")

        # Check alembic version
        ver = await conn.fetchval("SELECT version_num FROM alembic_version")
        print(f"\nAlembic version: {ver}")
    finally:
        await conn.close()


asyncio.run(verify())
