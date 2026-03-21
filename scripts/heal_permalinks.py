"""
One-off script: heal news_permalinks rows with bad ai_summary values.

Scans every row in the news_permalinks table and re-generates the
ai_summary for any row where the summary is:
  - NULL / empty
  - Very short (< 120 chars after cleaning)
  - Contains paywall boilerplate ("Continue reading on Medium", etc.)

Usage:
  python scripts/heal_permalinks.py            # live run
  python scripts/heal_permalinks.py --dry-run  # preview only, no writes

Reads env vars from .env in the repo root (SUPABASE_URL, SUPABASE_SERVICE_KEY,
GEMINI_API_KEY). Falls back to actual environment if .env is absent.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests

# ── Load .env from repo root ───────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
TABLE        = "news_permalinks"

if not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_KEY:
    sys.exit("ERROR: SUPABASE_URL, SUPABASE_SERVICE_KEY, and GEMINI_API_KEY must all be set.")


# ── Paywall / boilerplate detection ───────────────────────────────────────────

_PAYWALL_STRIP_RES = [
    re.compile(r'[Cc]ontinue\s+reading\s+on\s+\w[\w.]*\s*\.?', re.I),
    re.compile(r'[Rr]ead\s+(the\s+)?(full|more|rest)(\s+(article|story|post))?\s+on\s+\w[\w.]*\s*\.?', re.I),
    re.compile(r'[Ss]ign[\s\-]up(\s+for\s+free)?\s+to\s+(read|continue|unlock)\s*\.?', re.I),
    re.compile(r'[Cc]reate\s+a(\s+free)?\s+account\s+to\s+(read|continue)\s*\.?', re.I),
    re.compile(r'[Mm]ember[\s\-]only\s+(content|story|article)\s*\.?', re.I),
    re.compile(r'[Ss]ubscribe\s+to\s+(read|continue|unlock)\s*\.?', re.I),
    re.compile(r'[Ll]og\s+in\s+to\s+(read|continue)\s*\.?', re.I),
]

_JINA_META_RE = re.compile(
    r'^(Title|URL Source|Published Time|Author|Source|Byline|Date|By)\s*:', re.I | re.M
)
_ARTICLE_HEADER_RE = re.compile(
    r'^\s*(#{1,3}\s.{0,200}|By\s+\S.{0,100}|\d{1,2}\s+\w+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})\s*$',
    re.M,
)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    for pat in _PAYWALL_STRIP_RES:
        text = pat.sub("", text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def _is_bad_summary(summary: str | None) -> bool:
    """Return True if the summary needs to be regenerated."""
    if not summary:
        return True
    cleaned = _clean_text(summary)
    # Changed by cleaning → contained paywall text
    if cleaned != summary.strip():
        return True
    # Too short to be useful
    if len(cleaned) < 120:
        return True
    return False


def _strip_article_header(text: str, headline: str) -> str:
    lines = text.splitlines()
    headline_lower = headline.lower().strip()
    body_lines: list[str] = []
    found_body = False
    for line in lines:
        stripped = line.strip()
        if not found_body:
            if not stripped:
                continue
            clean_line = re.sub(r'^#+\s*', '', stripped).lower()
            if clean_line and (clean_line in headline_lower or headline_lower in clean_line):
                continue
            if len(stripped) < 80 and _ARTICLE_HEADER_RE.match(stripped):
                continue
            if re.match(r'^[Bb]y\s+\S', stripped) and len(stripped) < 100:
                continue
            found_body = True
        body_lines.append(line)
    return "\n".join(body_lines).strip()


# ── Fetch / generate ──────────────────────────────────────────────────────────

def fetch_jina(url: str) -> str:
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            timeout=10,
            headers={"Accept": "text/plain", "User-Agent": "ClawBeat/1.0"},
        )
        text = r.text
        marker = "Markdown Content:"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
        text = _JINA_META_RE.sub("", text)
        return _clean_text(text)[:8000]
    except Exception:
        return ""


def gemini_analyze(headline: str, article_text: str) -> str:
    source_text = _strip_article_header(article_text, headline) if article_text else ""
    headline_only = len(source_text) < 120

    if headline_only:
        content_section = (
            "No article text is available (the source is paywalled or inaccessible). "
            "Use your knowledge of this topic to write the analysis, but note at the end "
            "of the final paragraph that this analysis is based on the headline and topic context, "
            "not the full article text."
        )
    else:
        content_section = f"Article excerpt:\n{source_text[:7000]}"

    prompt = (
        "You are a senior analyst for ClawBeat, an agentic AI intelligence feed covering the OpenClaw "
        "ecosystem, AI agents, and related tooling. Write a detailed, substantive signal analysis. "
        "Structure your response as 3-4 distinct paragraphs:\n\n"
        "1. **What happened**: The core event, announcement, or finding — be specific with names, "
        "technical details, and context.\n"
        "2. **Key details**: Notable technical specifics, architecture choices, benchmarks, or background "
        "that a practitioner would care about.\n"
        "3. **OpenClaw ecosystem implications**: How this connects to or affects agentic AI frameworks, "
        "multi-agent systems, or the broader developer ecosystem.\n"
        "4. **Signal strength**: Who should pay attention and why — developers, researchers, or operators.\n\n"
        "Rules: Be factual. Do not repeat the headline, author name, publication name, or date — "
        "those are already shown on the page. Do not start with 'This article'. Do not use bullet "
        "points — write in flowing prose. Each paragraph should be 3-5 sentences. Separate paragraphs "
        "with a blank line. Never include phrases like 'Continue reading on Medium' or similar paywall "
        "text.\n\n"
        f"Headline: {headline}\n\n"
        f"{content_section}"
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 700, "temperature": 0.25},
            },
            timeout=25,
        )
        result = _clean_text(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        return result
    except Exception as e:
        print(f"    Gemini error: {e}")
        return ""


# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_all_rows() -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"select": "date,slug,headline,article_url,ai_summary", "limit": str(page), "offset": str(offset)},
            headers=sb_headers(),
            timeout=10,
        )
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def patch_summary(date: str, slug: str, ai_summary: str) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        params={"date": f"eq.{date}", "slug": f"eq.{slug}"},
        json={"ai_summary": ai_summary},
        headers={**sb_headers(), "Prefer": "return=minimal"},
        timeout=10,
    )
    return r.status_code in (200, 204)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Heal bad ai_summary rows in news_permalinks.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    dry = args.dry_run
    if dry:
        print("DRY RUN — no writes will be made.\n")

    print("Fetching all rows from news_permalinks…")
    rows = fetch_all_rows()
    print(f"  {len(rows)} total rows\n")

    bad = [r for r in rows if _is_bad_summary(r.get("ai_summary"))]
    print(f"  {len(bad)} rows need healing\n")

    if not bad:
        print("Nothing to do.")
        return

    for i, row in enumerate(bad, 1):
        date    = row["date"]
        slug    = row["slug"]
        headline = row.get("headline", "")
        url     = row.get("article_url", "")
        old     = (row.get("ai_summary") or "")[:80].replace("\n", " ")

        print(f"[{i}/{len(bad)}] {date}/{slug}")
        print(f"  headline : {headline[:80]}")
        print(f"  old      : {old!r}")

        if dry:
            print("  → skipped (dry run)\n")
            continue

        article_text = fetch_jina(url)
        fresh = gemini_analyze(headline, article_text)

        if not fresh:
            print("  → Gemini returned nothing, skipping\n")
            continue

        ok = patch_summary(date, slug, fresh)
        status = "✓ updated" if ok else "✗ patch failed"
        print(f"  new      : {fresh[:120].replace(chr(10), ' ')}…")
        print(f"  → {status}\n")

        # Respect Gemini free-tier rate limit (~60 RPM) — small pause between calls
        time.sleep(1.5)

    print("Done.")


if __name__ == "__main__":
    main()
