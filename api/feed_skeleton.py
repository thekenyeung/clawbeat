"""
ClawBeat Feed Generator — getFeedSkeleton XRPC endpoint

Called by Bluesky to populate the "ClawBeat Intel" custom feed.

GET /xrpc/app.bsky.feed.getFeedSkeleton
  ?feed=at://...   — feed AT URI (accepted but ignored; only one feed)
  ?limit=30        — posts to return (default 30, max 100)
  ?cursor=...      — pagination cursor (ISO timestamp of last seen item)

Returns:
  { "feed": [{"post": "at://did:plc:.../app.bsky.feed.post/..."}], "cursor": "..." }

Required env vars:
  ADMIN_SUPABASE_URL   — Supabase project URL
  SUPABASE_SERVICE_KEY — service role key
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

SUPABASE_URL = os.environ.get("ADMIN_SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

DEFAULT_LIMIT = 30
MAX_LIMIT     = 100


def fetch_feed(cursor: str | None, limit: int) -> tuple[list[str], str | None]:
    """Return (list of AT-URI strings, next_cursor).

    Filters out NULL and 'backfilled' sentinel values so only real
    Bluesky post URIs are returned. Orders newest-first.
    Fetches limit+1 rows to detect whether a next page exists.
    """
    params: dict = {
        "select":        "bsky_post_uri,inserted_at",
        # neq excludes both NULLs and the 'backfilled' sentinel
        # (PostgreSQL: NULL != 'backfilled' → NULL → falsy)
        "bsky_post_uri": "neq.backfilled",
        "order":         "inserted_at.desc",
        "limit":         str(limit + 1),
    }
    if cursor:
        params["inserted_at"] = f"lt.{cursor}"

    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/news_items",
            params=params,
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=8,
        )
        rows = r.json() if r.ok and isinstance(r.json(), list) else []
    except Exception:
        rows = []

    next_cursor = None
    if len(rows) > limit:
        rows        = rows[:limit]
        next_cursor = rows[-1]["inserted_at"]

    uris = [row["bsky_post_uri"] for row in rows if row.get("bsky_post_uri")]
    return uris, next_cursor


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        try:
            limit = min(int((qs.get("limit") or ["30"])[0]), MAX_LIMIT)
        except (ValueError, IndexError):
            limit = DEFAULT_LIMIT

        cursor = (qs.get("cursor") or [None])[0]

        uris, next_cursor = fetch_feed(cursor, limit)

        payload: dict = {"feed": [{"post": uri} for uri in uris]}
        if next_cursor:
            payload["cursor"] = next_cursor

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type",                "application/json")
        self.send_header("Content-Length",              str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
