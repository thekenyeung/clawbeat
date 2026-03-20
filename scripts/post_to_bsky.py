"""
ClawBeat → Bluesky Publisher

Runs after forge.py in the news_forge GitHub Actions workflow.

Queries news_items for unposted headline articles (bsky_post_uri IS NULL,
source_type != 'delist') and publishes each as a Bluesky post with an
external-embed card (og:image + ClawBeat permalink).

Post format:
  [trimmed summary, ≤200 chars]
  [card embed: ClawBeat permalink, headline title, og:image from source article]

Required env vars:
  SUPABASE_URL          — Supabase project URL
  SUPABASE_SERVICE_KEY  — service role key (bypasses RLS)
  BSKY_IDENTIFIER       — clawbeat.bsky.social
  BSKY_APP_PASSWORD     — Bluesky app password
  PERMALINK_SECRET      — bearer token for POST /api/news_permalink (optional)
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from atproto import Client, models

# ── env ───────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
BSKY_IDENTIFIER   = os.environ["BSKY_IDENTIFIER"]    # clawbeat.bsky.social
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]
PERMALINK_SECRET  = os.environ.get("PERMALINK_SECRET", "")

SITE_HOST     = "https://clawbeat.co"
PERMALINK_API = f"{SITE_HOST}/api/news_permalink"
MAX_POSTS     = 20   # safety cap per forge run
POST_DELAY    = 1.5  # seconds between posts (rate limit courtesy)


# ── helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str, fallback: str = "article") -> str:
    """Convert a headline into a URL-safe ASCII slug (max 60 chars).
    Mirrors the same function in api/news_permalink.py."""
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or fallback


def short_code(url: str) -> str:
    """8-char MD5 hash of the article URL — used for /n/:code redirects."""
    return hashlib.md5(url.encode()).hexdigest()[:8]


def date_mdy_to_iso(date_mdy: str) -> str:
    """MM-DD-YYYY → YYYY-MM-DD"""
    try:
        return datetime.strptime(date_mdy, "%m-%d-%Y").strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def trim_summary(text: str, max_chars: int = 200) -> str:
    """Trim to ≤max_chars at a word boundary, appending ellipsis if cut."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars].rsplit(" ", 1)[0]
    return trimmed.rstrip(".,;:") + "\u2026"


def fetch_og_image(url: str) -> str:
    """Extract og:image URL from article HTML. Returns '' on failure."""
    try:
        r = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "ClawBeat/1.0 (compatible; Mozilla/5.0)"},
            allow_redirects=True,
        )
        html = r.text[:60_000]
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                img = m.group(1).strip()
                if img.startswith("//"):
                    img = "https:" + img
                elif img.startswith("/"):
                    parsed = urlparse(r.url)
                    img = f"{parsed.scheme}://{parsed.netloc}{img}"
                return img
    except Exception:
        pass
    return ""


def download_image(url: str) -> tuple[bytes, str]:
    """Download image bytes and MIME type. Returns (b'', '') on failure."""
    try:
        r = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "ClawBeat/1.0"},
        )
        if not r.ok:
            return b"", ""
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return r.content, mime
    except Exception:
        return b"", ""


def ensure_permalink(
    article_url: str,
    headline: str,
    pub_name: str,
    date_iso: str,
    summary: str,
    more_coverage: list,
) -> None:
    """Call POST /api/news_permalink so the landing page exists before we post.
    Fire-and-forget — failures are non-blocking."""
    if not PERMALINK_SECRET:
        return
    try:
        requests.post(
            PERMALINK_API,
            json={
                "article_url":   article_url,
                "headline":      headline,
                "pub_name":      pub_name,
                "date":          date_iso,
                "summary":       summary,
                "more_coverage": more_coverage or [],
            },
            headers={"Authorization": f"Bearer {PERMALINK_SECRET}"},
            timeout=25,
        )
    except Exception:
        pass


# ── Supabase helpers ──────────────────────────────────────────────────────────

def fetch_unposted() -> list[dict]:
    """Return unposted headline articles, oldest first, capped at MAX_POSTS."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/news_items",
        params={
            "bsky_post_uri": "is.null",
            "source_type":   "neq.delist",
            "select":        "url,title,source,date,summary,more_coverage",
            "order":         "inserted_at.asc",
            "limit":         str(MAX_POSTS),
        },
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        timeout=10,
    )
    if not r.ok:
        print(f"Supabase fetch failed: {r.status_code} {r.text}", file=sys.stderr)
        return []
    return r.json()


def mark_posted(article_url: str, post_uri: str, code: str, permalink_url: str) -> None:
    """Write bsky_post_uri, bsky_short_code, and bsky_permalink_url back to news_items."""
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/news_items",
        params={"url": f"eq.{article_url}"},
        json={
            "bsky_post_uri":     post_uri,
            "bsky_short_code":   code,
            "bsky_permalink_url": permalink_url,
        },
        headers={
            "apikey":           SUPABASE_KEY,
            "Authorization":    f"Bearer {SUPABASE_KEY}",
            "Content-Type":     "application/json",
        },
        timeout=5,
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    articles = fetch_unposted()
    if not articles:
        print("No unposted articles.")
        return

    print(f"Found {len(articles)} unposted article(s).")

    client = Client()
    client.login(BSKY_IDENTIFIER, BSKY_APP_PASSWORD)

    posted = 0
    for art in articles:
        url      = art.get("url", "")
        headline = art.get("title") or ""
        pub_name = art.get("source") or ""
        date_mdy = art.get("date") or datetime.utcnow().strftime("%m-%d-%Y")
        summary  = art.get("summary") or ""
        more_cov = art.get("more_coverage") or []

        try:
            date_iso     = date_mdy_to_iso(date_mdy)
            slug         = slugify(headline, fallback=date_iso)
            code         = short_code(url)
            permalink_url = (
                f"{SITE_HOST}/news/{date_iso}/{slug}"
                "?utm_source=clawbeat&utm_medium=share&utm_campaign=permalink"
            )
            short_url = f"{SITE_HOST}/n/{code}"

            post_text = trim_summary(summary, 200)
            if not post_text:
                post_text = headline[:200]

            # Ensure landing page exists before the post goes live
            ensure_permalink(url, headline, pub_name, date_iso, summary, more_cov)

            # Fetch og:image from source article
            og_image_url = fetch_og_image(url)

            # Upload image to Bluesky CDN
            thumb_blob = None
            if og_image_url:
                img_bytes, mime = download_image(og_image_url)
                if img_bytes:
                    try:
                        upload    = client.upload_blob(img_bytes)
                        thumb_blob = upload.blob
                    except Exception as e:
                        print(f"  Image upload failed, posting without thumb: {e}", file=sys.stderr)

            # Build external embed card
            embed = models.AppBskyEmbedExternal.Main(
                external=models.AppBskyEmbedExternal.External(
                    uri=short_url,
                    title=headline,
                    description=post_text,
                    thumb=thumb_blob,
                )
            )

            # Publish
            resp     = client.send_post(text=post_text, embed=embed)
            post_uri = resp.uri

            mark_posted(url, post_uri, code, permalink_url)
            posted += 1
            print(f"  ✓ {headline[:70]}")

        except Exception as e:
            print(f"  ✗ Skipped [{url[:60]}]: {e}", file=sys.stderr)
            continue

        time.sleep(POST_DELAY)

    print(f"Done — {posted}/{len(articles)} posted to Bluesky.")


if __name__ == "__main__":
    main()
