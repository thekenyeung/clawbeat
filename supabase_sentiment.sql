-- =============================================================
-- ClawBeat Sentiment Tracker — Supabase Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- =============================================================

-- sentiment_mentions: one anonymized row per scraped post/comment
-- Usernames, handles, and display names are stripped before insert.
CREATE TABLE IF NOT EXISTS sentiment_mentions (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source          TEXT NOT NULL,             -- 'hackernews' | 'bluesky' | 'reddit' | 'github' | 'youtube' | 'mastodon' | 'news'
  content_text    TEXT NOT NULL,             -- anonymized text (no handles/usernames)
  url             TEXT DEFAULT '',           -- link to original post (public URL only)
  title           TEXT DEFAULT '',           -- post/thread title if available
  sentiment_score FLOAT,                     -- VADER compound score (-1.0 to +1.0)
  sentiment_label TEXT DEFAULT 'neutral',    -- 'positive' | 'negative' | 'neutral'
  published_at    TIMESTAMPTZ,               -- original post timestamp
  collected_at    TIMESTAMPTZ DEFAULT NOW(),
  run_period      TEXT DEFAULT '',           -- 'morning' | 'afternoon' | 'evening'
  topic_tags      JSONB DEFAULT '[]'::jsonb  -- Gemini-assigned topic labels e.g. ["memory management", "routing"]
);

ALTER TABLE sentiment_mentions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public reads" ON sentiment_mentions
  FOR SELECT TO anon, authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_sentiment_mentions_collected_at
  ON sentiment_mentions (collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_sentiment_mentions_source
  ON sentiment_mentions (source, collected_at DESC);

-- -------------------------------------------------------------
-- sentiment_snapshots: aggregated per-run summary
-- One row per scraper run (up to 3/day: morning, afternoon, evening)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentiment_snapshots (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_at         TIMESTAMPTZ DEFAULT NOW(),
  period              TEXT NOT NULL,           -- 'morning' | 'afternoon' | 'evening'
  snapshot_date       DATE NOT NULL,           -- date portion for easy daily grouping

  -- Overall scores (0–100 composite)
  score_momentum      FLOAT DEFAULT 0,         -- mention velocity vs prior baseline
  score_sentiment     FLOAT DEFAULT 0,         -- VADER compound average, scaled to 0–100
  score_trust         FLOAT DEFAULT 0,         -- DX + docs + community health proxy
  score_buzz          FLOAT DEFAULT 0,         -- cross-platform echo + thread depth

  -- Raw counts
  total_mentions      INTEGER DEFAULT 0,
  positive_count      INTEGER DEFAULT 0,
  negative_count      INTEGER DEFAULT 0,
  neutral_count       INTEGER DEFAULT 0,
  positive_pct        FLOAT DEFAULT 0,
  negative_pct        FLOAT DEFAULT 0,
  neutral_pct         FLOAT DEFAULT 0,

  -- Per-source breakdown  { source: mention_count }
  source_breakdown    JSONB DEFAULT '{}'::jsonb,

  -- Gemini-produced fields
  gemini_narrative    TEXT DEFAULT '',         -- 1-paragraph plain-English summary
  emerging_story      TEXT DEFAULT '',         -- notable new story Gemini flagged
  competitive_framing TEXT DEFAULT '',         -- "OpenClaw vs X" narrative if present
  gemini_confidence   FLOAT DEFAULT 1.0,       -- multiplier 0.8–1.2 from Gemini

  -- Top topics from this run (array of topic objects)
  -- Each: { name, sentiment, confidence, tension, novelty, momentum }
  topics              JSONB DEFAULT '[]'::jsonb,

  -- Top shared articles across platforms
  -- Each: { url, title, share_count, sources, avg_sentiment }
  top_articles        JSONB DEFAULT '[]'::jsonb
);

ALTER TABLE sentiment_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public reads" ON sentiment_snapshots
  FOR SELECT TO anon, authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_sentiment_snapshots_date
  ON sentiment_snapshots (snapshot_date DESC, period);

-- -------------------------------------------------------------
-- sentiment_articles: cross-platform article share tracking
-- Upserted each run — share_count increments as more sources pick it up
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentiment_articles (
  url             TEXT PRIMARY KEY,
  title           TEXT DEFAULT '',
  first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
  share_count     INTEGER DEFAULT 1,           -- how many times linked across all sources
  sources         JSONB DEFAULT '[]'::jsonb,   -- ["hackernews", "bluesky", ...]
  avg_sentiment   FLOAT DEFAULT 0,
  topic_tags      JSONB DEFAULT '[]'::jsonb
);

ALTER TABLE sentiment_articles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public reads" ON sentiment_articles
  FOR SELECT TO anon, authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_sentiment_articles_share_count
  ON sentiment_articles (share_count DESC, last_seen_at DESC);

-- -------------------------------------------------------------
-- sentiment_ecosystem: GitHub hard metrics, updated each run
-- One row per tracked family/repo — upserted, not appended
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentiment_ecosystem (
  family              TEXT PRIMARY KEY,        -- 'openclaw' | 'nanoclaw' | 'picoclaw' etc.
  display_name        TEXT DEFAULT '',
  github_stars        INTEGER DEFAULT 0,
  github_stars_delta  INTEGER DEFAULT 0,       -- change since last run
  github_forks        INTEGER DEFAULT 0,
  open_issues         INTEGER DEFAULT 0,
  issue_close_ratio   FLOAT DEFAULT 0,         -- closed / (open + closed), 0–1
  dependent_repos     INTEGER DEFAULT 0,       -- repos depending on this package
  pypi_downloads_week INTEGER DEFAULT 0,       -- PyPI weekly downloads (0 if unavailable)
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE sentiment_ecosystem ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public reads" ON sentiment_ecosystem
  FOR SELECT TO anon, authenticated USING (true);
