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
import base64
import hashlib
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


# ── OPENCLAW CLONE DETECTION ──────────────────────────────────────────────────

_OPENCLAW_OFFICIAL_META: "dict | None" = None


def _get_openclaw_official_meta() -> dict:
    """Fetch and cache HEAD SHA + size_kb of the official openclaw/openclaw repo."""
    global _OPENCLAW_OFFICIAL_META
    if _OPENCLAW_OFFICIAL_META is not None:
        return _OPENCLAW_OFFICIAL_META
    try:
        meta = requests.get(
            "https://api.github.com/repos/openclaw/openclaw",
            headers=GH_HEADERS, timeout=10,
        ).json()
        branch  = meta.get('default_branch', 'main')
        size_kb = meta.get('size', 0) or 0
        sha = requests.get(
            f"https://api.github.com/repos/openclaw/openclaw/branches/{branch}",
            headers=GH_HEADERS, timeout=10,
        ).json().get('commit', {}).get('sha', '')
        # Also fetch the official README for verbatim content comparison
        readme_hash = ''
        try:
            readme_resp = requests.get(
                "https://api.github.com/repos/openclaw/openclaw/readme",
                headers=GH_HEADERS, timeout=10,
            ).json()
            b64 = readme_resp.get('content', '')
            if b64:
                readme_text = base64.b64decode(b64).decode('utf-8', errors='ignore')
                readme_hash = hashlib.sha256(readme_text.encode()).hexdigest()
        except Exception:
            pass
        _OPENCLAW_OFFICIAL_META = {'sha': sha, 'size_kb': size_kb, 'readme_hash': readme_hash}
        print(f"  📌 openclaw/openclaw: size={size_kb:,} KB  sha={sha[:12]}…  readme_hash={readme_hash[:12]}…")
    except Exception as e:
        print(f"  ⚠️  Could not fetch openclaw/openclaw metadata: {e}")
        _OPENCLAW_OFFICIAL_META = {'sha': '', 'size_kb': 0, 'readme_hash': ''}
    return _OPENCLAW_OFFICIAL_META


def _is_openclaw_clone(owner: str, repo: str, size_kb: int, official: dict) -> bool:
    """Return True if a non-official 'openclaw'-named repo is a clone of the original.

    Signal 1 — size within 15% of the official repo (no extra API call).
    Signal 2 — HEAD commit SHA matches (1–2 extra API calls).
    Either alone is sufficient.
    """
    off_sha     = official.get('sha', '')
    off_size_kb = official.get('size_kb', 0)

    if off_size_kb > 0 and size_kb > 0:
        if abs(size_kb - off_size_kb) / off_size_kb <= 0.15:
            return True

    if off_sha:
        try:
            branch = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=GH_HEADERS, timeout=10,
            ).json().get('default_branch', 'main')
            sha = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}",
                headers=GH_HEADERS, timeout=10,
            ).json().get('commit', {}).get('sha', '')
            if sha and sha == off_sha:
                return True
        except Exception:
            pass

    # Signal 3: README verbatim match — catches clones where a single commit
    # was added on top of the original (different HEAD SHA, same README content)
    off_readme_hash = official.get('readme_hash', '')
    if off_readme_hash:
        try:
            readme_resp = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                headers=GH_HEADERS, timeout=10,
            ).json()
            b64 = readme_resp.get('content', '')
            if b64:
                readme_text = base64.b64decode(b64).decode('utf-8', errors='ignore')
                if hashlib.sha256(readme_text.encode()).hexdigest() == off_readme_hash:
                    return True
        except Exception:
            pass

    return False


def fetch_github_enrichment() -> dict:
    """Fetch up to 1,000 results from GitHub Search.
    Returns url → dict with pushed_at, forks, stars, open_issues_count,
    license, topics, archived — all the fields the scoring function needs.
    """
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
                lookup[r['html_url']] = {
                    'pushed_at':         r.get('pushed_at', ''),
                    'forks':             r.get('forks_count', 0),
                    'stars':             r.get('stargazers_count', 0),
                    'open_issues_count': r.get('open_issues_count', 0),
                    'license':           (r.get('license') or {}).get('spdx_id') or '',
                    'topics':            r.get('topics') or [],
                    'archived':          r.get('archived', False),
                    'size':              r.get('size', 0),
                    'fork':              r.get('fork', False),
                }
            total = data.get('total_count', '?')
            print(f"  Page {page:2d}: {len(items)} repos (total on GitHub: {total:,})")
            if len(items) < 100:
                break
            time.sleep(1 if GITHUB_TOKEN else 7)
        except Exception as e:
            print(f"  ⚠️  Error on page {page}: {e}")
            break
    print(f"  Built enrichment lookup for {len(lookup)} repos")
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
    size          = r.get('size', 0) or 0
    is_fork       = r.get('fork', False) or False
    has_no_desc   = not desc or desc == 'no description.'
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
    if size < 5:
        return 0, 'skip'                                   # essentially empty — git init or placeholder only
    if is_fork and stars < 10:
        return 0, 'skip'                                   # fork with zero community traction
    if stars == 0 and forks == 0 and has_no_desc:
        return 0, 'skip'                                   # no description and no community signal
    if days_created <= 14 and open_issues > 5 and stars == 0:
        return 0, 'skip'                                   # new dump: issues imported but zero organic traction

    # ── ENGLISH-ONLY FILTER
    # Reject repos whose description contains non-Latin-script characters.
    # Covers CJK, Hiragana/Katakana, Hangul, Arabic, Hebrew, Cyrillic.
    if desc and len(desc) >= 20:
        for _c in desc:
            _cp = ord(_c)
            if (0x4E00 <= _cp <= 0x9FFF or  # CJK Unified Ideographs
                    0x3040 <= _cp <= 0x30FF or  # Hiragana / Katakana
                    0xAC00 <= _cp <= 0xD7AF or  # Hangul
                    0x0600 <= _cp <= 0x06FF or  # Arabic
                    0x0590 <= _cp <= 0x05FF or  # Hebrew
                    0x0400 <= _cp <= 0x04FF):   # Cyrillic
                return 0, 'skip'
        if len(desc) >= 40 and sum(1 for _c in desc if ord(_c) > 127) / len(desc) > 0.25:
            return 0, 'skip'                               # high non-ASCII ratio → non-Latin script

    # ── NON-OFFICIAL "openclaw" REPO GUARDS
    # Applies to any repo named exactly "openclaw" not owned by the official org.
    if name == 'openclaw' and owner != 'openclaw':
        if is_fork:
            return 0, 'skip'                               # fork of the official repo — always skip
        if size < 50:
            return 0, 'skip'                               # near-empty — placeholder or bare clone
        if stars < 50:
            return 0, 'skip'                               # insufficient organic traction
        # Either official phrase alone is enough to confirm a verbatim clone
        if ('your own personal ai assistant' in desc
                or 'any os. any platform. the lobster way' in desc):
            return 0, 'skip'                               # description copied from openclaw/openclaw

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
    # Topics alone are not sufficient proof of relevance — tag spam is common.
    # Require that the description also mentions an ecosystem keyword to award
    # topic-based relevance credit; otherwise fall through to the base score.
    desc_confirms_oc = any(k in desc for k in ('openclaw', 'clawdbot', 'moltbot', 'moltis', 'clawd'))

    # Non-official repos must confirm ecosystem relevance in their description
    # to earn name-based relevance credit; name alone is not sufficient.
    if   owner == 'openclaw':                                                  rel = 23
    elif any(k in name for k in ('awesome-openclaw', 'openclaw-skills',
                                  'openclaw-usecases')):
        rel = 20 if desc_confirms_oc else 8
    elif 'openclaw' in name or 'moltis' in name:
        rel = 18 if desc_confirms_oc else 8
    elif any(k in name for k in ('skill', 'awesome', 'usecases')):
        rel = 16 if desc_confirms_oc else 6
    elif any(k in name for k in ('claw', 'molty', 'clawdbot', 'clawd')):
        rel = 16 if desc_confirms_oc else 6
    elif kw_hits >= 3 and desc_confirms_oc:                                  rel = 15
    elif kw_hits >= 1 and desc_confirms_oc:                                  rel = 12
    elif 'openclaw' in desc or 'clawdbot' in desc or 'moltbot' in desc:     rel = 10
    else:                                                                     rel =  2   # topic-only / no match — near-zero relevance
    if fork_ratio > 0.20: rel = min(25, rel + 2)

    # ── 4. TRACTION (0–15)
    if   stars >= 20000 and forks >= 2000:    trac = 13
    elif stars >= 5000  and forks >= 300:     trac = 10
    elif stars >= 1000  and forks >= 50:      trac = 7
    elif days_created <= 90 and stars >= 200: trac = 4
    elif stars >= 1:                           trac = 2
    else:                                      trac = 0   # zero organic signal — no community validation
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
    # ── Phase 1: enrich all scoring fields from GitHub Search API ─────
    print("\n📡 Phase 1: Fetching live data from GitHub Search API…")
    enrichment = fetch_github_enrichment()
    print(f"  Live data available for {len(enrichment)} repos")

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

    # ── Phase 3: merge live data and score ────────────────────────────
    print(f"\n📊 Phase 3: Scoring {len(all_rows)} repos…")
    updates = []
    enriched_count = 0
    # 'archived' and 'fork' use explicit override so False values aren't dropped
    # by the "not in (None, '', [], 0)" guard (False == 0 in Python).
    BOOL_FIELDS   = ('archived', 'fork')
    ENRICH_FIELDS = ('pushed_at', 'forks', 'stars', 'open_issues_count',
                     'license', 'topics', 'size') + BOOL_FIELDS
    for r in all_rows:
        gh = enrichment.get(r.get('url', ''))
        if gh:
            for field in ENRICH_FIELDS:
                if field in BOOL_FIELDS or gh.get(field) not in (None, '', [], 0):
                    r[field] = gh[field]
            enriched_count += 1
        score, tier = _score_github_project(r)
        updates.append({
            'url':               r['url'],
            'rubric_score':      score,
            'rubric_tier':       tier,
            'pushed_at':         r.get('pushed_at', ''),
            'forks':             r.get('forks', 0),
            'stars':             r.get('stars', 0),
            'open_issues_count': r.get('open_issues_count', 0),
            'size':              r.get('size', 0),
            'is_fork':           r.get('fork', False),
        })
    print(f"  Live data applied for {enriched_count} rows")

    # ── Phase 3b: clone detection for non-official 'openclaw' repos ───
    # Any repo named exactly "openclaw" by a non-openclaw owner that passed
    # the scorer's stars≥20 guard is checked against the official repo's
    # size and HEAD commit SHA. Confirmed clones are forced to 'skip'.
    clone_candidates = [
        u for u in updates
        if u.get('rubric_tier') != 'skip'
        and next((r for r in all_rows if r['url'] == u['url']), {}).get('name', '').lower() == 'openclaw'
        and next((r for r in all_rows if r['url'] == u['url']), {}).get('owner', '').lower() != 'openclaw'
    ]
    if clone_candidates:
        print(f"\n🔍 Phase 3b: Clone detection for {len(clone_candidates)} non-official 'openclaw' repo(s)…")
        official = _get_openclaw_official_meta()
        url_to_row = {r['url']: r for r in all_rows}
        for u in clone_candidates:
            row = url_to_row.get(u['url'], {})
            if _is_openclaw_clone(row.get('owner', ''), row.get('name', ''),
                                  row.get('size', 0), official):
                print(f"  🚫 Clone: {row.get('owner')}/{row.get('name')} → forced skip")
                u['rubric_score'] = 0
                u['rubric_tier']  = 'skip'

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
