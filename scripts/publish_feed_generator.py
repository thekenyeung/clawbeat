"""
ClawBeat Feed Generator — one-time publish script

Creates the app.bsky.feed.generator record on the clawbeat.bsky.social account,
making the "ClawBeat Intel" feed discoverable and subscribable in Bluesky.

Run AFTER deploying to Vercel (api/feed_skeleton.py and public/.well-known/did.json
must be live so Bluesky can resolve did:web:clawbeat.co).

Usage:
  BSKY_IDENTIFIER=clawbeat.bsky.social \\
  BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \\
  python scripts/publish_feed_generator.py

Re-running is safe — putRecord is idempotent (upserts the record).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

BSKY_IDENTIFIER   = os.environ.get("BSKY_IDENTIFIER", "").strip()
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD", "").strip()

FEED_RKEY         = "intel"
FEED_DID          = "did:web:clawbeat.co"
FEED_DISPLAY_NAME = "ClawBeat Intel"
FEED_DESCRIPTION  = (
    "OpenClaw ecosystem news, releases, and community signals — "
    "curated by ClawBeat. Agentic AI frameworks, tooling, and research."
)


def main() -> None:
    if not BSKY_IDENTIFIER or not BSKY_APP_PASSWORD:
        print(
            "Error: set BSKY_IDENTIFIER and BSKY_APP_PASSWORD env vars.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── 1. Authenticate ───────────────────────────────────────────────────────
    print(f"Logging in as {BSKY_IDENTIFIER}...")
    login = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": BSKY_IDENTIFIER, "password": BSKY_APP_PASSWORD},
        timeout=10,
    )
    login.raise_for_status()
    session    = login.json()
    access_jwt = session["accessJwt"]
    did        = session["did"]
    print(f"Authenticated — DID: {did}")

    # ── 2. Publish feed generator record ─────────────────────────────────────
    print("Publishing app.bsky.feed.generator record...")
    resp = requests.post(
        "https://bsky.social/xrpc/com.atproto.repo.putRecord",
        json={
            "repo":       did,
            "collection": "app.bsky.feed.generator",
            "rkey":       FEED_RKEY,
            "record": {
                "$type":       "app.bsky.feed.generator",
                "did":         FEED_DID,
                "displayName": FEED_DISPLAY_NAME,
                "description": FEED_DESCRIPTION,
                "createdAt":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=10,
    )
    resp.raise_for_status()

    feed_uri = f"at://{did}/app.bsky.feed.generator/{FEED_RKEY}"
    print("✓ Feed generator published.")
    print(f"  Display name : {FEED_DISPLAY_NAME}")
    print(f"  Feed URI     : {feed_uri}")
    print(f"  Server DID   : {FEED_DID}")
    print()
    print("Users can now find and subscribe to 'ClawBeat Intel' in Bluesky.")


if __name__ == "__main__":
    main()
