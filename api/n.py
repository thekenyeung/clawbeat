"""
ClawBeat Short URL Redirect — Vercel Python Serverless Function

GET /n/:code  (rewritten by Vercel to /api/n?code=:code)

Looks up the 8-char short code in news_items.bsky_short_code and issues
a 302 redirect to the stored bsky_permalink_url (ClawBeat permalink + UTM).
Returns 404 if the code is not found.

Required env vars:
  ADMIN_SUPABASE_URL   — Supabase project URL
  SUPABASE_SERVICE_KEY — service role key
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

SUPABASE_URL = os.environ.get("ADMIN_SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


def lookup_short_code(code: str) -> str | None:
    """Return bsky_permalink_url for the given short code, or None."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/news_items",
            params={
                "bsky_short_code": f"eq.{code}",
                "select":          "bsky_permalink_url",
                "limit":           "1",
            },
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=5,
        )
        rows = r.json()
        if rows and rows[0].get("bsky_permalink_url"):
            return rows[0]["bsky_permalink_url"]
    except Exception:
        pass
    return None


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        code   = (qs.get("code") or [""])[0].strip()

        if not code:
            self._not_found()
            return

        permalink_url = lookup_short_code(code)
        if not permalink_url:
            self._not_found()
            return

        self.send_response(302)
        self.send_header("Location", permalink_url)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()

    def _not_found(self):
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
