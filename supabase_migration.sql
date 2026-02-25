-- =============================================================
-- ClawBeat Supabase Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- =============================================================

-- news_items: one row per article headline
CREATE TABLE IF NOT EXISTS news_items (
  url          TEXT PRIMARY KEY,
  title        TEXT,
  source       TEXT,
  date         TEXT,          -- MM-DD-YYYY display date
  summary      TEXT,
  density      INTEGER DEFAULT 0,
  is_minor     BOOLEAN DEFAULT false,
  more_coverage JSONB DEFAULT '[]'::jsonb,  -- [{source, url}, ...]
  inserted_at  TIMESTAMPTZ DEFAULT NOW()
);

-- videos: YouTube / media items
CREATE TABLE IF NOT EXISTS videos (
  url          TEXT PRIMARY KEY,
  title        TEXT,
  thumbnail    TEXT,
  channel      TEXT,
  description  TEXT,
  published_at TEXT,          -- MM-DD-YYYY display date
  inserted_at  TIMESTAMPTZ DEFAULT NOW()
);

-- github_projects: GitHub repos from search
CREATE TABLE IF NOT EXISTS github_projects (
  url          TEXT PRIMARY KEY,
  name         TEXT,
  owner        TEXT,
  description  TEXT,
  stars        INTEGER DEFAULT 0,
  created_at   TEXT,          -- ISO date string from GitHub API
  inserted_at  TIMESTAMPTZ DEFAULT NOW()
);

-- research_papers: ArXiv papers
CREATE TABLE IF NOT EXISTS research_papers (
  url          TEXT PRIMARY KEY,
  title        TEXT,
  authors      JSONB DEFAULT '[]'::jsonb,  -- ["Author One", "Author Two", ...]
  date         TEXT,          -- ISO date string from ArXiv
  summary      TEXT,
  inserted_at  TIMESTAMPTZ DEFAULT NOW()
);

-- feed_metadata: single-row table tracking last forge run time
CREATE TABLE IF NOT EXISTS feed_metadata (
  id           INTEGER PRIMARY KEY,
  last_updated TEXT,
  CONSTRAINT single_row CHECK (id = 1)
);

-- =============================================================
-- Row Level Security — public reads, service-role-only writes
-- =============================================================
ALTER TABLE news_items      ENABLE ROW LEVEL SECURITY;
ALTER TABLE videos          ENABLE ROW LEVEL SECURITY;
ALTER TABLE github_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_papers ENABLE ROW LEVEL SECURITY;
ALTER TABLE feed_metadata   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON news_items
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Public reads" ON videos
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Public reads" ON github_projects
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Public reads" ON research_papers
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Public reads" ON feed_metadata
  FOR SELECT TO anon, authenticated USING (true);

-- =============================================================
-- Add tags column to news_items (run this if migrating an existing DB)
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb;

-- =============================================================
-- Events table (new — run just this block on an existing DB)
-- =============================================================
CREATE TABLE IF NOT EXISTS events (
  url              TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  organizer        TEXT DEFAULT '',
  event_type       TEXT DEFAULT 'unknown',  -- 'virtual' | 'in-person' | 'unknown'
  location_city    TEXT DEFAULT '',
  location_state   TEXT DEFAULT '',
  location_country TEXT DEFAULT '',
  start_date       TEXT DEFAULT '',         -- MM/DD/YYYY
  end_date         TEXT DEFAULT '',         -- MM/DD/YYYY
  description      TEXT DEFAULT '',
  inserted_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON events
  FOR SELECT TO anon, authenticated USING (true);
