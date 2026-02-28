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

-- =============================================================
-- Admin write policies for existing tables
-- Replace 'ADMIN_EMAIL_HERE' with your Google account email
-- Run this AFTER enabling Google OAuth in Supabase Auth
-- =============================================================

-- Allow admin to insert/update/delete news_items
CREATE POLICY "Admin writes" ON news_items
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- Allow admin to insert/update/delete events
CREATE POLICY "Admin writes" ON events
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- spotlight_overrides table (new — manual admin control over spotlight slots)
-- Run just this block on an existing DB
-- =============================================================
CREATE TABLE IF NOT EXISTS spotlight_overrides (
  dispatch_date  TEXT NOT NULL,                  -- MM-DD-YYYY, matches news_items.date
  slot           INTEGER NOT NULL                -- 1=Lead Signal, 2-4=Also Today
                 CHECK (slot BETWEEN 1 AND 4),
  url            TEXT NOT NULL,
  title          TEXT DEFAULT '',
  source         TEXT DEFAULT '',
  summary        TEXT DEFAULT '',
  tags           JSONB DEFAULT '[]'::jsonb,
  updated_at     TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (dispatch_date, slot)
);

ALTER TABLE spotlight_overrides ENABLE ROW LEVEL SECURITY;

-- Public reads
CREATE POLICY "Public reads" ON spotlight_overrides
  FOR SELECT TO anon, authenticated USING (true);

-- Admin writes only
CREATE POLICY "Admin writes" ON spotlight_overrides
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- whitelist_sources table (new — for admin whitelist manager)
-- =============================================================
CREATE TABLE IF NOT EXISTS whitelist_sources (
  id                 TEXT PRIMARY KEY,        -- numeric string, e.g. "1", "42"
  source_name        TEXT NOT NULL,
  category           TEXT DEFAULT 'Publisher', -- 'Publisher' | 'Creator' | 'YouTube'
  website_url        TEXT DEFAULT '',
  website_rss        TEXT DEFAULT '',
  youtube_channel_id TEXT DEFAULT '',
  priority           TEXT DEFAULT '1',
  inserted_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE whitelist_sources ENABLE ROW LEVEL SECURITY;

-- Public reads (forge.py + main app can query this)
CREATE POLICY "Public reads" ON whitelist_sources
  FOR SELECT TO anon, authenticated USING (true);

-- Admin writes only
CREATE POLICY "Admin writes" ON whitelist_sources
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- Add date_is_manual flag to news_items
-- Prevents forge.py from overwriting admin-set dates on the next scrape run.
-- Run this block on an existing DB.
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS date_is_manual BOOLEAN DEFAULT false;

-- =============================================================
-- Add language/topics/forks/license to github_projects
-- Run this block in Supabase SQL Editor if migrating existing DB
-- =============================================================
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS language TEXT    DEFAULT '';
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS topics   JSONB   DEFAULT '[]'::jsonb;
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS forks    INTEGER DEFAULT 0;
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS license  TEXT    DEFAULT '';

-- =============================================================
-- daily_editions table — stores Daily Edition story data per date
-- One row per edition date; stories is a JSONB array of up to 4 objects
-- Run just this block on an existing DB
-- =============================================================
CREATE TABLE IF NOT EXISTS daily_editions (
  edition_date  TEXT PRIMARY KEY,               -- YYYY-MM-DD
  generated_at  TIMESTAMPTZ DEFAULT NOW(),
  stories       JSONB DEFAULT '[]'::jsonb       -- array of story objects (see below)
  -- Each story object:
  -- { slot, url, headline, author, pub_name, pub_url, pub_date, category,
  --   image_url, image_alt, credit_name, credit_url, summary_html, why_it_matters }
);

ALTER TABLE daily_editions ENABLE ROW LEVEL SECURITY;

-- Public reads (main app can query this)
CREATE POLICY "Public reads" ON daily_editions
  FOR SELECT TO anon, authenticated USING (true);

-- Admin writes only
CREATE POLICY "Admin writes" ON daily_editions
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- OpenClaw Feed Scoring Methodology — score columns
-- Run this block on an existing DB to add the new columns.
-- Scores are computed server-side by forge.py and stored here.
-- total_score = d1_score + d2_score + d3_score + d4_score (max 100)
-- d1_tier: 1=OpenClaw/Moltbot/Clawdbot, 2=Moltbook, 3=Tangential
-- stage_tags: array of tags (legacy-name, whitelisted, high-engagement, etc.)
-- source_type: 'priority' | 'standard' | 'delist'
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS total_score  FLOAT   DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d1_score     FLOAT   DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d2_score     FLOAT   DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d3_score     FLOAT   DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d4_score     FLOAT   DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d1_tier      INTEGER DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS stage_tags   JSONB   DEFAULT '[]'::jsonb;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS source_type  TEXT    DEFAULT 'standard';
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS hn_points    INTEGER DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS hn_comments  INTEGER DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS d5_score     FLOAT   DEFAULT NULL;

-- =============================================================
-- Supabase Storage bucket for Daily Edition hero images
-- This cannot be created via SQL — do it in the Supabase Dashboard:
--   Storage → New bucket → Name: "daily-edition-images" → Public: ON
-- Then add this RLS policy so the admin can upload:
--   Storage → daily-edition-images → Policies → New policy (INSERT)
--   Role: authenticated, USING: auth.email() = 'ADMIN_EMAIL_HERE'
-- Uploaded images are stored at: {edition_date}/slot-{N}-{timestamp}.{ext}
-- Public URL format: {SUPABASE_URL}/storage/v1/object/public/daily-edition-images/{path}
-- =============================================================
