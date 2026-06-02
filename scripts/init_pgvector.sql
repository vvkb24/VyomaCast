-- ============================================================
-- VyomaCast — PostgreSQL Initialization Script
-- ============================================================
-- Mounted into /docker-entrypoint-initdb.d/ so it runs
-- automatically on first container start.
--
-- Creates the required extensions for:
--   * pgvector  — 384-dim cosine similarity search
--   * uuid-ossp — UUID generation functions (uuid_generate_v4)
-- ============================================================

-- Enable the pgvector extension for embedding storage and similarity search
CREATE EXTENSION IF NOT EXISTS vector;

-- Enable uuid-ossp for UUID generation (used by Article/Cluster/Feed PKs)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Verify extensions are installed
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector extension failed to install';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'uuid-ossp') THEN
        RAISE EXCEPTION 'uuid-ossp extension failed to install';
    END IF;
    RAISE NOTICE 'VyomaCast extensions installed successfully: vector, uuid-ossp';
END $$;
