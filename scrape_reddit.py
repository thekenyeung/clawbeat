#!/usr/bin/env python3
"""
Reddit scraper — top posts from r/openclaw, r/clawdbot, r/LocalLLaMA.

Runs every 4 hours via GitHub Actions. GitHub runner IPs are not blocked
by Reddit, so no OAuth is needed — just a descriptive User-Agent.
Upserts into the `reddit_posts` Supabase table; keeps top 20 by engagement.

Usage:
  export SUPABASE_URL=https://...supabase.co
  export SUPABASE_SERVICE_KEY=eyJ...
  python scrape_reddit.py
"""
import os
import sys
import time
import requests
from datetime import datetime, timezone

try:
    from supabase import create_client
except ImportError:
    print("Run: pip install supabase requests")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌  Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "ClawBeat/1.0 (aggregator for clawbeat.co; contact via GitHub)",
    "Accept": "application/json",
}

CLAW_QUERY = "openclaw OR nanoclaw OR nemoclaw OR nanobot OR zeroclaw OR picoclaw"

SOURCES = [
    "https://www.reddit.com/r/openclaw/hot.json?limit=25",
    "https://www.reddit.com/r/clawdbot/hot.json?limit=25",
    (
        f"https://www.reddit.com/r/LocalLLaMA/search.json"
        f"?q={requests.utils.quote(CLAW_QUERY)}&sort=top&t=all&limit=15&restrict_sr=1"
    ),
]

# ── HELPERS ───────────────────────────────────────────────────────────────────

def reddit_get(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"    ⏳  Rate limited — sleeping {wait}s")
                time.sleep(wait)
                continue
            if not r.ok:
                print(f"    ⚠️   HTTP {r.status_code} for {url}")
                return None
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"    ⚠️   Error: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def engagement(p: dict) -> int:
    return (p.get("score") or 0) + (p.get("num_comments") or 0) * 5


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("📡  Fetching Reddit posts …")

    seen: dict[str, dict] = {}

    for url in SOURCES:
        print(f"  {url[:80]}")
        data = reddit_get(url)
        if not data:
            continue
        children = (data.get("data") or {}).get("children") or []
        added = 0
        for child in children:
            p = child.get("data") or {}
            pid = p.get("id")
            if not pid or p.get("over_18") or p.get("removed_by_category"):
                continue
            if pid not in seen or engagement(p) > engagement(seen[pid]):
                seen[pid] = p
                added += 1
        print(f"    → {added} post(s)")
        time.sleep(1)

    ranked = sorted(seen.values(), key=engagement, reverse=True)[:20]

    if not ranked:
        print("⚠️   No posts found")
        return

    rows = []
    for p in ranked:
        permalink = p.get("permalink") or ""
        if permalink and not permalink.startswith("http"):
            permalink = f"https://reddit.com{permalink}"
        rows.append({
            "id":           p["id"],
            "title":        p.get("title") or "",
            "permalink":    permalink,
            "subreddit":    p.get("subreddit") or "",
            "score":        p.get("score") or 0,
            "num_comments": p.get("num_comments") or 0,
            "author":       p.get("author") or "",
            "created_utc":  int(p.get("created_utc") or 0),
            "scraped_at":   datetime.now(timezone.utc).isoformat(),
        })

    sb.table("reddit_posts").upsert(rows, on_conflict="id").execute()
    print(f"\n✅  Done — {len(rows)} posts upserted")


if __name__ == "__main__":
    main()
