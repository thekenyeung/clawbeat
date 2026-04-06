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
-- GitHub Projects — Rubric scoring columns (OpenClaw Eval Rubric v1.3)
-- Run this block on an existing DB to add the new columns.
-- rubric_score: 0–100 integer computed by forge.py at ingest time
-- rubric_tier: 'featured' | 'listed' | 'watchlist' | 'skip'
-- pushed_at: ISO timestamp of last GitHub push (activity signal)
-- open_issues_count: raw open issue count from GitHub API
-- =============================================================
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS rubric_score     INTEGER DEFAULT NULL;
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS rubric_tier      TEXT    DEFAULT NULL;
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS pushed_at        TEXT    DEFAULT '';
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS open_issues_count INTEGER DEFAULT 0;

-- Index on rubric_tier for fast filtered queries from forge.html
CREATE INDEX IF NOT EXISTS idx_github_projects_rubric_tier
  ON github_projects (rubric_tier, rubric_score DESC NULLS LAST);

-- =============================================================
-- Add is_fork and size to github_projects (scoring signal fields)
-- is_fork: true if GitHub marks this as a fork of another repo
-- size: repo disk size in KB from GitHub API (0 = empty repo)
-- Run just this block on an existing DB
-- =============================================================
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS is_fork BOOLEAN DEFAULT false;
ALTER TABLE github_projects ADD COLUMN IF NOT EXISTS size    INTEGER DEFAULT 0;

-- =============================================================
-- ecosystem_family_stats table — tracks GitHub total_count per claw family
-- Updated daily by forge.py via GitHub Search API total_count field.
-- Rows: 'openclaw' | 'nanobot' | 'picoclaw' | 'nanoclaw' | 'zeroclaw'
-- Run just this block on an existing DB
-- =============================================================
CREATE TABLE IF NOT EXISTS ecosystem_family_stats (
  family        TEXT PRIMARY KEY,   -- slug: 'openclaw', 'nanobot', etc.
  display_name  TEXT DEFAULT '',    -- 'OpenClaw', 'Nanobot', etc.
  search_query  TEXT DEFAULT '',    -- GitHub search query used
  total_count   INTEGER DEFAULT 0,  -- total repos on GitHub matching the query
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE ecosystem_family_stats ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON ecosystem_family_stats
  FOR SELECT TO anon, authenticated USING (true);

-- Service role writes (forge.py uses service key — no auth policy needed)

-- =============================================================
-- api_usage table — tracks daily Gemini API call counts for rate-limit monitoring
-- forge.py loads today's row at startup, increments in-memory counters during the
-- run, and upserts the final counts at the end. GitHub Actions ::warning:: /
-- ::error:: annotations fire when counts cross 80% / 95% of the free-tier limit.
-- =============================================================
CREATE TABLE IF NOT EXISTS api_usage (
  usage_date          DATE PRIMARY KEY,
  gemini_text_calls   INTEGER DEFAULT 0,   -- generate_content RPD (limit: 1500)
  gemini_embed_calls  INTEGER DEFAULT 0,   -- embed_content RPD   (limit: 1500)
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- channel_vetted: cache of YouTube channel vetting results
-- Channels are checked once for: age > 30 days, > 1 non-Shorts video,
-- tech/business keyword presence. Results persist to avoid re-vetting on each run.
CREATE TABLE IF NOT EXISTS channel_vetted (
  channel_id    TEXT PRIMARY KEY,
  channel_name  TEXT,
  is_vetted     BOOLEAN NOT NULL DEFAULT FALSE,
  fail_reason   TEXT,
  checked_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE channel_vetted ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON channel_vetted
  USING (TRUE) WITH CHECK (TRUE);

-- No public reads needed — accessed only by forge.py via the service key.

-- =============================================================
-- needs_reprocess flag on news_items
-- Set to true when an anchor article is deleted via the admin panel and its
-- more_coverage sublinks are orphaned for possible reclustering on the next
-- forge run. forge.py strips these rows from existing_urls so they can be
-- re-discovered from live feeds, then deletes any that weren't re-found.
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS needs_reprocess BOOLEAN DEFAULT false;

-- =============================================================
-- cluster_locked flag on news_items
-- When true: forge.py will never overwrite more_coverage for this article,
-- and the duplicate-cleanup pass will not strip manually-curated coverage links.
-- Cross-batch clustering also skips absorbing new articles into a locked headline.
-- Set this via the admin Dispatch Editor lock checkbox.
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS cluster_locked BOOLEAN DEFAULT false;

-- =============================================================
-- spotlight_excluded table — tracks articles manually displaced from spotlight
-- When the admin replaces article X in a spotlight slot, X is excluded from
-- all algo-filled spotlight slots for that date (but stays in the news feed).
-- X can be manually re-added to spotlight via the override UI at any time.
-- =============================================================
CREATE TABLE IF NOT EXISTS spotlight_excluded (
  dispatch_date  TEXT NOT NULL,
  url            TEXT NOT NULL,
  PRIMARY KEY (dispatch_date, url)
);
ALTER TABLE spotlight_excluded ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public reads" ON spotlight_excluded
  FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "Admin writes" ON spotlight_excluded
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- news_permalinks table — stores per-article landing page data
-- Populated by POST /api/news_permalink (called by post_to_bsky.py and admin).
-- Served by GET /news/:date/:slug via api/news_permalink.py.
-- =============================================================
CREATE TABLE IF NOT EXISTS news_permalinks (
  date          TEXT NOT NULL,               -- YYYY-MM-DD
  slug          TEXT NOT NULL,               -- slugified headline, max 60 chars
  article_url   TEXT NOT NULL,               -- source article URL
  headline      TEXT DEFAULT '',
  pub_name      TEXT DEFAULT '',
  pub_date      TEXT DEFAULT '',
  og_image_url  TEXT DEFAULT '',
  ai_summary    TEXT DEFAULT '',
  more_coverage JSONB DEFAULT '[]'::jsonb,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (date, slug)
);

ALTER TABLE news_permalinks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON news_permalinks
  FOR SELECT TO anon, authenticated USING (true);

-- =============================================================
-- Bluesky publishing columns on news_items
-- bsky_post_uri:      AT Protocol post URI; null = not yet posted
-- bsky_short_code:    8-char MD5 hash of article URL → /n/:code redirect
-- bsky_permalink_url: full ClawBeat permalink URL (with UTM params)
-- =============================================================
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS bsky_post_uri     TEXT DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS bsky_short_code   TEXT DEFAULT NULL;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS bsky_permalink_url TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_news_items_bsky_short_code
  ON news_items (bsky_short_code)
  WHERE bsky_short_code IS NOT NULL;

-- =============================================================
-- blocked_urls table — permanently excludes articles from algo re-discovery
-- When an admin deletes an article from the feed, its URL is written here.
-- forge.py loads this list at startup and adds all URLs to existing_urls,
-- preventing them from ever being re-inserted by the scraper/algo.
-- A URL can only be removed from this list manually (Supabase dashboard or future UI).
-- =============================================================
CREATE TABLE IF NOT EXISTS blocked_urls (
  url         TEXT PRIMARY KEY,
  blocked_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE blocked_urls ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON blocked_urls
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Admin writes" ON blocked_urls
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- article_feedback table — admin rejection signals for score recalibration
-- Data collection only. Does NOT modify scoring pipeline.
-- article_id references news_items(url) (text PK).
-- signal: 'reject' | 'approve' | 'boost' — only 'reject' has UI currently.
-- reason: one of six predefined enum values.
-- =============================================================
-- article_id is intentionally NOT a FK — the feedback row must outlive the
-- news_items row so rejection signals persist after the article is deleted.
CREATE TABLE IF NOT EXISTS article_feedback (
  id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  article_id  TEXT NOT NULL,
  signal      TEXT NOT NULL CHECK (signal IN ('reject', 'approve', 'boost')),
  reason      TEXT NOT NULL CHECK (reason IN (
                 'too_elementary', 'off_topic',
                 'low_quality_source', 'clickbait', 'duplicate', 'marketing_pr'
               )),
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE article_feedback ENABLE ROW LEVEL SECURITY;

-- Admin-only: only the admin account can read or write feedback
CREATE POLICY "Admin writes" ON article_feedback
  FOR ALL TO authenticated
  USING     (auth.email() = 'ADMIN_EMAIL_HERE')
  WITH CHECK (auth.email() = 'ADMIN_EMAIL_HERE');

-- =============================================================
-- article_feedback — make reason nullable for approve/boost signals
-- Run this block on an existing DB after the table was created above.
-- approve/boost (e.g. Slackbot ingest) don't require a reason.
-- The new check constraint ensures reject always includes a reason.
-- Note: PostgreSQL CHECK passes NULL values (UNKNOWN ≠ FALSE),
-- so dropping NOT NULL is sufficient to allow nulls past the existing
-- reason IN (...) check. The explicit constraint below makes intent clear.
-- =============================================================
ALTER TABLE article_feedback ALTER COLUMN reason DROP NOT NULL;

ALTER TABLE article_feedback
  ADD CONSTRAINT article_feedback_reject_reason_required
  CHECK (signal != 'reject' OR reason IS NOT NULL);

-- Allow the Slackbot service key to insert feedback rows
-- (slack_ingest.py uses SUPABASE_SERVICE_KEY which bypasses RLS,
--  so no extra policy is needed — this comment is for documentation only.)

-- =============================================================
-- article_feedback — add marketing_pr to reason check constraint
-- The inline CHECK on CREATE TABLE only covers new installs.
-- For an existing DB, drop the auto-generated constraint and recreate it.
-- =============================================================
ALTER TABLE article_feedback DROP CONSTRAINT IF EXISTS article_feedback_reason_check;
ALTER TABLE article_feedback
  ADD CONSTRAINT article_feedback_reason_check
  CHECK (reason IN (
    'too_elementary', 'off_topic',
    'low_quality_source', 'clickbait', 'duplicate', 'marketing_pr'
  ));

-- =============================================================
-- article_feedback — drop FK to news_items (ON DELETE CASCADE was silently
-- deleting rejection signals whenever the article was removed, allowing
-- rejected URLs to be re-ingested and re-sent to Slack on the next run).
-- Feedback rows must outlive news_items rows — no FK needed.
-- =============================================================
ALTER TABLE article_feedback DROP CONSTRAINT IF EXISTS article_feedback_article_id_fkey;

-- =============================================================
-- article_feedback — allow reason=NULL for approve/boost signals.
-- The reason_check added above requires a non-null enum value, but
-- approve/boost actions have no rejection reason. Relax the check so
-- NULL is valid, while reject still requires a reason (enforced by
-- article_feedback_reject_reason_required).
-- =============================================================
ALTER TABLE article_feedback DROP CONSTRAINT IF EXISTS article_feedback_reason_check;
ALTER TABLE article_feedback
  ADD CONSTRAINT article_feedback_reason_check
  CHECK (reason IS NULL OR reason IN (
    'too_elementary', 'off_topic',
    'low_quality_source', 'clickbait', 'duplicate', 'marketing_pr'
  ));

-- =============================================================
-- github_releases — nightly-scraped release announcements per repo/family
-- Populated by scrape_github_meta.py via GitHub Releases API.
-- Unique on (repo_full_name, tag_name) so upserts are idempotent.
-- =============================================================
CREATE TABLE IF NOT EXISTS github_releases (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repo_full_name   TEXT NOT NULL,
  family           TEXT,
  tag_name         TEXT NOT NULL,
  release_name     TEXT,
  body_preview     TEXT,
  html_url         TEXT NOT NULL,
  published_at     TIMESTAMPTZ NOT NULL,
  author_login     TEXT,
  is_prerelease    BOOLEAN DEFAULT FALSE,
  scraped_at       TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (repo_full_name, tag_name)
);

ALTER TABLE github_releases ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON github_releases
  FOR SELECT TO anon, authenticated USING (true);

-- =============================================================
-- repo_contributors — top contributors per repo, updated nightly
-- Populated by scrape_github_meta.py via GitHub contributors API.
-- contributions = commit count per contributor per repo.
-- Unique on (repo_full_name, contributor_login).
-- =============================================================
CREATE TABLE IF NOT EXISTS repo_contributors (
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repo_full_name         TEXT NOT NULL,
  family                 TEXT,
  contributor_login      TEXT NOT NULL,
  contributor_avatar_url TEXT,
  contributions          INT DEFAULT 0,
  scraped_at             TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (repo_full_name, contributor_login)
);

ALTER TABLE repo_contributors ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public reads" ON repo_contributors
  FOR SELECT TO anon, authenticated USING (true);

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

-- Add pending_review column for Slack approval gate.
-- Items scoring 10–45 are held here until manually accepted or auto-expired after 24h.
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS pending_review BOOLEAN DEFAULT false;
