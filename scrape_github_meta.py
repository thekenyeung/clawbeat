#!/usr/bin/env python3
"""
Nightly scraper — GitHub repo contributors + release announcements.

Reads all repos from github_projects, then for each repo:
  - Fetches top-10 contributors  → upserts repo_contributors
  - Fetches latest-5 releases    → upserts github_releases

Family is detected client-side via the same keyword rules used in homepage.html.
Uses GITHUB_TOKEN for 5,000 req/hr; falls back to 60 req/hr unauthenticated.

Usage:
  export SUPABASE_URL=https://...supabase.co
  export SUPABASE_SERVICE_KEY=eyJ...
  export GITHUB_TOKEN=ghp_...        # provided by GitHub Actions automatically
  python scrape_github_meta.py
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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌  Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

GH_HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"
    print("🔑  Using GITHUB_TOKEN (5,000 req/hr)")
else:
    print("⚠️   No GITHUB_TOKEN — unauthenticated (60 req/hr). Will throttle.")

# Mirrors FAMILY_KW in public/homepage.html
FAMILY_KW = {
    "openclaw": ["openclaw", "open-claw"],
    "nanoclaw": ["nanoclaw", "nano-claw"],
    "nemoclaw": ["nemoclaw", "nemo-claw", "nemobot"],
    "picoclaw": ["picoclaw", "pico-claw"],
    "nanobot":  ["nanobot", "nano-bot"],
    "zeroclaw": ["zeroclaw", "zero-claw"],
}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def detect_family(repo: dict) -> str | None:
    text = " ".join([
        repo.get("name", "") or "",
        repo.get("description", "") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()
    for family, keywords in FAMILY_KW.items():
        if any(kw in text for kw in keywords):
            return family
    return None


def gh_get(path: str, retries: int = 3):
    url = f"https://api.github.com/{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=GH_HEADERS, timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                reset = int(r.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset - int(time.time()), 0) + 5
                print(f"    ⏳  Rate limited — sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"    ⚠️   GH error {path}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CONTRIBUTORS ──────────────────────────────────────────────────────────────

def scrape_contributors(repo: dict, family: str | None) -> int:
    full_name = repo.get("full_name") or f"{repo.get('owner','')}/{repo.get('name','')}"
    data = gh_get(f"repos/{full_name}/contributors?per_page=10&anon=false")
    if not data or not isinstance(data, list):
        return 0

    rows = []
    for c in data[:10]:
        if c.get("type") != "User":
            continue
        rows.append({
            "repo_full_name":         full_name,
            "family":                 family,
            "contributor_login":      c["login"],
            "contributor_avatar_url": c.get("avatar_url"),
            "contributions":          c.get("contributions", 0),
            "scraped_at":             now_iso(),
        })

    if rows:
        sb.table("repo_contributors") \
          .upsert(rows, on_conflict="repo_full_name,contributor_login") \
          .execute()
    return len(rows)


# ── RELEASES ──────────────────────────────────────────────────────────────────

def scrape_releases(repo: dict, family: str | None) -> int:
    full_name = repo.get("full_name") or f"{repo.get('owner','')}/{repo.get('name','')}"
    data = gh_get(f"repos/{full_name}/releases?per_page=5")
    if not data or not isinstance(data, list):
        return 0

    rows = []
    for rel in data[:5]:
        if not rel.get("tag_name") or not rel.get("published_at"):
            continue
        body = (rel.get("body") or "").strip()
        rows.append({
            "repo_full_name": full_name,
            "family":         family,
            "tag_name":       rel["tag_name"],
            "release_name":   rel.get("name") or rel["tag_name"],
            "body_preview":   body[:500] if body else None,
            "html_url":       rel["html_url"],
            "published_at":   rel["published_at"],
            "author_login":   (rel.get("author") or {}).get("login"),
            "is_prerelease":  rel.get("prerelease", False),
        })

    if rows:
        sb.table("github_releases") \
          .upsert(rows, on_conflict="repo_full_name,tag_name") \
          .execute()
    return len(rows)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("📡  Fetching repos from Supabase …")
    repos = sb.table("github_projects").select("*").limit(500).execute().data or []
    print(f"    Found {len(repos)} repos\n")

    total_c = total_r = 0
    delay = 0.5 if GITHUB_TOKEN else 6  # respect rate limits

    for i, repo in enumerate(repos):
        family    = detect_family(repo)
        full_name = repo.get("full_name") or f"{repo.get('owner','')}/{repo.get('name','')}"
        print(f"  [{i+1:>3}/{len(repos)}] {full_name} ({family or 'unknown'})")

        c = scrape_contributors(repo, family)
        r = scrape_releases(repo, family)
        total_c += c
        total_r += r
        time.sleep(delay)

    print(f"\n✅  Done — {total_c} contributor rows, {total_r} release rows upserted")


if __name__ == "__main__":
    main()
