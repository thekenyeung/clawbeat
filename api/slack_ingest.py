"""
ClawBeat Slack Ingest — Vercel Python Serverless Function
Route: POST /api/slack_ingest

Listens for DMs sent to the ClawBeat Slack bot.
When an authorized user sends a URL, it:
  1. Fetches OG tags (title, source, description)
  2. Fetches a summary via Jina Reader
  3. Upserts into Supabase news_items
  4. Replies in the DM with a short confirmation

Required environment variables (set in Vercel dashboard):
  SUPABASE_URL          — e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — service role key (bypasses RLS)
  SLACK_BOT_TOKEN       — xoxb-...
  SLACK_SIGNING_SECRET  — from Slack app Basic Information
  SLACK_ALLOWED_USER_ID — your Slack user ID (e.g. U012AB3CD)
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests

# ── env ──────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID", "")


# ── helpers ───────────────────────────────────────────────────────────────────


def verify_signature(body: bytes, timestamp: str, sig: str) -> bool:
    """Verify HMAC-SHA256 Slack signing secret."""
    if not timestamp or not sig:
        return False
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def extract_url(text: str) -> str | None:
    """Pull the first HTTP(S) URL out of a Slack message.

    Slack wraps linked URLs as <https://...> or <https://...|label>.
    """
    m = re.search(r"<(https?://[^|>\s]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None


def fetch_og(url: str) -> dict:
    """Fetch OG/meta tags from the article page."""
    try:
        r = requests.get(
            url,
            timeout=4,
            headers={"User-Agent": "ClawBeat/1.0 (compatible; Mozilla/5.0)"},
            allow_redirects=True,
        )
        html = r.text[:60_000]

        def og(prop: str) -> str:
            # Try property="og:…" (both attribute orders)
            for pat in [
                rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\'](.*?)["\']',
                rf'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:{prop}["\']',
            ]:
                m = re.search(pat, html, re.I)
                if m:
                    return m.group(1).strip()
            return ""

        title = og("title")
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = m.group(1).strip() if m else ""

        site_name = og("site_name")
        if not site_name:
            site_name = urlparse(url).netloc.lstrip("www.")

        description = og("description")
        if not description:
            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                html,
                re.I,
            )
            description = m.group(1).strip() if m else ""

        return {
            "title": title[:500],
            "source": site_name[:200],
            "description": description[:1000],
        }
    except Exception:
        domain = urlparse(url).netloc.lstrip("www.")
        return {"title": "", "source": domain, "description": ""}


def fetch_jina(url: str) -> str:
    """Pull cleaned article text via Jina Reader; return first 1 500 chars."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            timeout=5,
            headers={"Accept": "text/plain", "User-Agent": "ClawBeat/1.0"},
        )
        return r.text[:1500].strip()
    except Exception:
        return ""


def supabase_upsert(url: str, title: str, source: str, summary: str) -> bool:
    """Upsert article into news_items (merge on duplicate URL)."""
    today = datetime.now().strftime("%m-%d-%Y")
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/news_items",
            json={
                "url": url,
                "title": title,
                "source": source,
                "date": today,
                "summary": summary,
                "date_is_manual": True,
                "source_type": "priority",
            },
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            timeout=4,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def slack_reply(channel: str, text: str) -> None:
    """Post a message back to the DM channel."""
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": text},
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=4,
        )
    except Exception:
        pass


# ── Vercel handler ────────────────────────────────────────────────────────────


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # ── 1. Verify Slack signature ─────────────────────────────────────
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")
        if not verify_signature(body, timestamp, signature):
            self._respond(403, b"Forbidden")
            return

        data = json.loads(body)

        # ── 2. URL-verification handshake (one-time setup step) ───────────
        if data.get("type") == "url_verification":
            self._json(200, {"challenge": data["challenge"]})
            return

        # ── 3. Ignore Slack retries to avoid duplicate saves ──────────────
        if self.headers.get("X-Slack-Retry-Num"):
            self._json(200, {"ok": True})
            return

        # ── 4. Acknowledge immediately (Slack requires < 3s response) ─────
        self._json(200, {"ok": True})

        # ── 5. Process the event ──────────────────────────────────────────
        event = data.get("event", {})

        # Only handle plain DMs from the authorized user
        if (
            event.get("type") != "message"
            or event.get("subtype")  # skip edits, deletes, bot_message
            or event.get("channel_type") != "im"
            or event.get("user") != SLACK_ALLOWED_USER_ID
        ):
            return

        channel = event.get("channel", "")
        url = extract_url(event.get("text", ""))

        if not url:
            slack_reply(channel, "No URL found in that message.")
            return

        # Fetch metadata
        og = fetch_og(url)
        title = og["title"] or url
        source = og["source"]
        summary = fetch_jina(url) or og["description"]

        # Save
        ok = supabase_upsert(url, title, source, summary)
        if ok:
            slack_reply(channel, f"✓ Saved to ClawBeat\n*{title}*\n_{source}_")
        else:
            slack_reply(channel, "✗ Failed to save — check Supabase logs.")

    # ── helpers ───────────────────────────────────────────────────────────

    def _json(self, code: int, data: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default stderr logging
