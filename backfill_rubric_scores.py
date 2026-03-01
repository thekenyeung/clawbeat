#!/usr/bin/env python3
"""
Backfill rubric scores for all github_projects rows in Supabase.

Phase 1: Fetch pushed_at for all repos from GitHub Search API (paginated,
         up to 1,000 results) so Activity scores are accurate.
Phase 2: Score every Supabase row using enriched data; upsert rubric_score,
         rubric_tier, and pushed_at back.

Usage:
  export SUPABASE_URL=https://...supabase.co
  export SUPABASE_SERVICE_KEY=eyJ...
  export GITHUB_TOKEN=ghp_...   # optional but strongly recommended (5,000 req/hr vs 10/min)
  python backfill_rubric_scores.py
"""
import os
import sys
import time
import requests
from datetime import datetime

try:
    from supabase import create_client
except ImportError:
    print("Run: pip install supabase requests")
    sys.exit(1)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

GH_HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"
    print("🔑 Using GITHUB_TOKEN (5,000 req/hr limit)")
else:
    print("⚠️  No GITHUB_TOKEN — unauthenticated (10 req/min). Pausing between pages.")


def fetch_pushed_at_lookup() -> dict:
    """Fetch up to 1,000 results from GitHub Search and return url→pushed_at map."""
    lookup = {}
    for page in range(1, 11):   # GitHub Search caps at 10 pages × 100 = 1,000
        try:
            resp = requests.get(
                f"https://api.github.com/search/repositories"
                f"?q=openclaw&sort=updated&order=desc&per_page=100&page={page}",
                headers=GH_HEADERS, timeout=15,
            )
            if resp.status_code == 422:   # page beyond total results
                break
            if resp.status_code == 403:
                print(f"  ⚠️  Rate limited on page {page}. Waiting 60s…")
                time.sleep(60)
                resp = requests.get(resp.url, headers=GH_HEADERS, timeout=15)
            resp.raise_for_status()
            data  = resp.json()
            items = data.get('items', [])
            if not items:
                break
            for r in items:
                lookup[r['html_url']] = r.get('pushed_at', '')
            total = data.get('total_count', '?')
            print(f"  Page {page:2d}: {len(items)} repos (total on GitHub: {total:,})")
            if len(items) < 100:
                break
            # Be polite: short pause between pages (mandatory without token)
            time.sleep(1 if GITHUB_TOKEN else 7)
        except Exception as e:
            print(f"  ⚠️  Error on page {page}: {e}")
            break
    print(f"  Built pushed_at lookup for {len(lookup)} repos")
    return lookup


def _score_github_project(r: dict) -> tuple:
    """Compute rubric score and tier (OpenClaw Eval Rubric v1.3).
    Returns (score: int, tier: str).
    """
    stars         = r.get('stars', 0) or 0
    forks         = r.get('forks', 0) or 0
    lic           = r.get('license', '') or ''
    topics        = r.get('topics', []) or []
    desc          = (r.get('description', '') or '').lower()
    name          = (r.get('name', '') or '').lower()
    owner         = (r.get('owner', '') or '').lower()
    pushed_at     = r.get('pushed_at', '') or ''
    created_at    = r.get('created_at', '') or ''
    open_issues   = r.get('open_issues_count', 0) or 0
    archived      = r.get('archived', False) or False
    fork_ratio    = forks / max(stars, 1)
    today         = datetime.today().date()

    def _days_since(iso):
        if not iso: return 9999
        try: return (today - datetime.fromisoformat(iso[:10]).date()).days
        except: return 9999

    days_created     = _days_since(created_at)
    last_commit_days = _days_since(pushed_at)

    # ── AUTO-DISQUALIFIERS
    if lic in ('NOASSERTION', 'SSPL-1.0'):
        return 0, 'skip'
    for word in ('test', 'demo', 'temp', 'wip', 'todo', 'untitled'):
        if word in name:
            return 0, 'skip'
    if last_commit_days >= 548 and open_issues > 5:
        return 0, 'skip'

    # ── 1. ACTIVITY (0–30)
    if   last_commit_days <= 60:  act = 24
    elif last_commit_days <= 180: act = 17
    elif last_commit_days <= 365: act = 9
    else:                         act = 2
    if days_created <= 30: act = min(act, 15)

    # ── 2. QUALITY (0–25)
    qual = 12
    if   lic in ('MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause'): qual += 2
    elif not lic:                                                         qual -= 5
    elif lic in ('GPL-3.0', 'AGPL-3.0'):                                qual -= 2
    if stars > 5000 and lic in ('MIT', 'Apache-2.0'):                   qual += 2
    qual = max(0, min(25, qual))

    # ── 3. RELEVANCE (0–25)
    openclaw_kw = {'openclaw', 'clawdbot', 'moltbot', 'moltis', 'clawd',
                   'skills', 'skill', 'openclaw-skills', 'clawdbot-skill', 'crustacean'}
    topic_str = ' '.join(topics).lower()
    kw_hits   = sum(1 for k in openclaw_kw if k in topic_str)

    if   owner == 'openclaw' or name == 'openclaw':                          rel = 23
    elif any(k in name for k in ('awesome-openclaw', 'openclaw-skills',
                                  'openclaw-usecases')):                      rel = 20
    elif 'openclaw' in name or 'moltis' in name:                             rel = 18
    elif any(k in name for k in ('skill', 'awesome', 'usecases')):          rel = 16
    elif any(k in name for k in ('claw', 'molty', 'clawdbot', 'clawd')):    rel = 16
    elif kw_hits >= 3:                                                        rel = 15
    elif kw_hits >= 1:                                                        rel = 12
    elif 'openclaw' in desc or 'clawdbot' in desc or 'moltbot' in desc:     rel = 10
    else:                                                                     rel =  6
    if fork_ratio > 0.20: rel = min(25, rel + 2)

    # ── 4. TRACTION (0–15)
    if   stars >= 20000 and forks >= 2000:    trac = 13
    elif stars >= 5000  and forks >= 300:     trac = 10
    elif stars >= 1000  and forks >= 50:      trac = 7
    elif days_created <= 90 and stars >= 200: trac = 4
    else:                                      trac = 2
    if fork_ratio > 0.20:                     trac = min(15, trac + 2)
    if forks == 0 and stars > 500:            trac = max(0, trac - 3)

    # ── 5. NOVELTY (0–5)
    novelty_words = {'memory', 'mem', 'router', 'proxy', 'studio', 'lancedb',
                     'security', 'translation', 'guide', 'usecases', 'free'}
    if   owner == 'openclaw' or name == 'openclaw' or stars > 20000: novelty = 4
    elif any(k in name for k in novelty_words):                       novelty = 4
    elif stars > 5000 or 'awesome' in name:                           novelty = 3
    else:                                                              novelty = 2

    total = act + qual + rel + trac + novelty
    if archived and total >= 75: total = 74

    if   total >= 75: tier = 'featured'
    elif total >= 50: tier = 'listed'
    elif total >= 25: tier = 'watchlist'
    else:             tier = 'skip'
    return total, tier


def main():
    # ── Phase 1: enrich pushed_at from GitHub Search API ──────────────
    print("\n📡 Phase 1: Fetching pushed_at from GitHub Search API…")
    pushed_at_lookup = fetch_pushed_at_lookup()

    enriched = sum(1 for v in pushed_at_lookup.values() if v)
    print(f"  pushed_at enriched for {enriched} repos")

    # ── Phase 2: fetch all Supabase rows ──────────────────────────────
    print("\n🔍 Phase 2: Fetching all github_projects from Supabase…")
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = sb.table('github_projects').select('*').range(offset, offset + page_size - 1).execute()
        rows = resp.data or []
        all_rows.extend(rows)
        print(f"  Fetched {len(all_rows)} rows total")
        if len(rows) < page_size:
            break
        offset += page_size

    # ── Phase 3: score with enriched pushed_at ────────────────────────
    print(f"\n📊 Phase 3: Scoring {len(all_rows)} repos…")
    updates = []
    enriched_count = 0
    for r in all_rows:
        # Prefer GitHub API pushed_at; fall back to whatever is stored
        gh_pushed = pushed_at_lookup.get(r.get('url', ''), '')
        if gh_pushed and gh_pushed != r.get('pushed_at', ''):
            r['pushed_at'] = gh_pushed
            enriched_count += 1
        score, tier = _score_github_project(r)
        updates.append({
            'url':          r['url'],
            'rubric_score': score,
            'rubric_tier':  tier,
            'pushed_at':    r.get('pushed_at', ''),
        })
    print(f"  pushed_at updated for {enriched_count} rows via GitHub lookup")

    # ── Phase 4: upsert ───────────────────────────────────────────────
    print("\n💾 Phase 4: Upserting scores in batches of 200…")
    batch_size = 200
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        sb.table('github_projects').upsert(batch).execute()
        print(f"  Batch {i // batch_size + 1}: {len(batch)} rows written")

    tiers: dict = {}
    for u in updates:
        t = u['rubric_tier']
        tiers[t] = tiers.get(t, 0) + 1
    print("\n✅ Done. Tier breakdown:", tiers)
    featured = tiers.get('featured', 0)
    listed   = tiers.get('listed', 0)
    print(f"   → featured_repo_index will show {featured + listed} repos ({featured} featured + {listed} listed)")


if __name__ == '__main__':
    main()
