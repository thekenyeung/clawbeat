"""
ClawBeat Slack Review Handler — Vercel Python Serverless Function
Route: POST /api/slack_review

Handles interactive button clicks from the #clawbeat-review Slack channel.

Flow:
  1. Article posted to channel with Accept / Reject buttons (by forge.py)
  2. Accept → sets pending_review=False, article enters feed on its publish date
  3. Reject → replaces buttons with five reason options (matching admin panel)
  4. Reason selected → deletes article, logs rejection signal in article_feedback
  5. Any action >24h after insert → responds "Expired", no DB change

Required env vars (Vercel dashboard):
  SUPABASE_URL          — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — service role key (bypasses RLS)
  SLACK_BOT_TOKEN       — xoxb-...
  SLACK_SIGNING_SECRET  — from Slack app Basic Information
  SLACK_ALLOWED_USER_ID — your Slack user ID (gates actions to one reviewer)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

import requests

# ── env ───────────────────────────────────────────────────────────────────────
SUPABASE_URL          = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY          = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SLACK_BOT_TOKEN       = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET  = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID", "")

REVIEW_WINDOW_HOURS = 24

# Rejection reasons — must match article_feedback.reason enum in supabase_migration.sql
REJECT_REASONS = [
    ("off_topic",          "Off Topic"),
    ("too_elementary",     "Too Elementary"),
    ("low_quality_source", "Low Quality Source"),
    ("clickbait",          "Clickbait"),
    ("duplicate",          "Duplicate"),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def verify_signature(body: bytes, timestamp: str, sig: str) -> bool:
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


def update_message(response_url: str, text: str, blocks: list | None = None) -> None:
    """Replace the original Slack message in-place."""
    payload: dict = {"replace_original": True, "text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        requests.post(
            response_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
    except Exception:
        pass


def fetch_inserted_at(article_url: str) -> datetime | None:
    """Return the inserted_at timestamp for the article, or None on error."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/news_items",
            params={"url": f"eq.{article_url}", "select": "inserted_at"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=4,
        )
        rows = r.json()
        if r.status_code == 200 and rows:
            return datetime.fromisoformat(rows[0]["inserted_at"].replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def is_expired(inserted_at: datetime | None) -> bool:
    if inserted_at is None:
        return False  # can't confirm expiry, allow action
    return (datetime.now(timezone.utc) - inserted_at) > timedelta(hours=REVIEW_WINDOW_HOURS)


def supabase_approve(article_url: str) -> tuple[bool, str]:
    """Set pending_review=False so the item becomes visible on its publish date."""
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/news_items",
            params={"url": f"eq.{article_url}"},
            json={"pending_review": False},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=5,
        )
        return r.status_code in (200, 204), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:200]


def supabase_reject(article_url: str, reason: str) -> tuple[bool, str]:
    """Log rejection signal then delete the article.
    The feedback log MUST succeed before deletion — it's the permanent block signal
    that prevents forge from re-ingesting the article on the next run."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/article_feedback",
            json={"article_id": article_url, "signal": "reject", "reason": reason},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=4,
        )
        if r.status_code not in (200, 201):
            return False, f"feedback log failed HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"feedback log error: {str(e)[:200]}"
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/news_items",
            params={"url": f"eq.{article_url}"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        )
        return r.status_code in (200, 204), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:200]


def reason_buttons(article_url: str) -> list:
    """Block Kit actions block with one button per rejection reason."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Why reject?*"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "action_id": f"reject_reason__{reason}",
                    "value": article_url,
                }
                for reason, label in REJECT_REASONS
            ],
        },
    ]


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # 1. Verify Slack signature
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")
        if not verify_signature(body, timestamp, signature):
            self._respond(403, b"Forbidden")
            return

        # 2. Acknowledge immediately (Slack requires < 3s)
        self._respond(200, b"")

        # 3. Parse payload
        try:
            qs      = parse_qs(body.decode("utf-8"))
            payload = json.loads(qs.get("payload", ["{}"])[0])
        except Exception:
            return

        if payload.get("type") != "block_actions":
            return

        # Gate to authorized reviewer
        user_id = payload.get("user", {}).get("id", "")
        if SLACK_ALLOWED_USER_ID and user_id != SLACK_ALLOWED_USER_ID:
            return

        response_url = payload.get("response_url", "")
        actions      = payload.get("actions", [])
        if not actions:
            return

        action     = actions[0]
        action_id  = action.get("action_id", "")
        article_url = action.get("value", "")
        if not article_url:
            return

        # ── Accept ────────────────────────────────────────────────────────────
        if action_id == "accept_article":
            inserted_at = fetch_inserted_at(article_url)
            if is_expired(inserted_at):
                update_message(response_url, f"⏱ Review window closed — this article expired after {REVIEW_WINDOW_HOURS}h.")
                return
            ok, err = supabase_approve(article_url)
            if ok:
                update_message(response_url, f"✅ Accepted — article will appear on its publish date.")
            else:
                update_message(response_url, f"✗ Accept failed: {err}")

        # ── Reject (first tap — show reason buttons) ──────────────────────────
        elif action_id == "reject_article":
            inserted_at = fetch_inserted_at(article_url)
            if is_expired(inserted_at):
                update_message(response_url, f"⏱ Review window closed — this article expired after {REVIEW_WINDOW_HOURS}h.")
                return
            update_message(response_url, "Why reject?", blocks=reason_buttons(article_url))

        # ── Reject reason selected ────────────────────────────────────────────
        elif action_id.startswith("reject_reason__"):
            reason = action_id.split("reject_reason__", 1)[1]
            valid_reasons = {r for r, _ in REJECT_REASONS}
            if reason not in valid_reasons:
                return
            inserted_at = fetch_inserted_at(article_url)
            if is_expired(inserted_at):
                update_message(response_url, f"⏱ Review window closed — this article expired after {REVIEW_WINDOW_HOURS}h.")
                return
            label = next(lbl for r, lbl in REJECT_REASONS if r == reason)
            ok, err = supabase_reject(article_url, reason)
            if ok:
                update_message(response_url, f"✗ Rejected ({label}) — article removed and logged.")
            else:
                update_message(response_url, f"✗ Rejection failed: {err}")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
