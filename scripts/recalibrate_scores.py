#!/usr/bin/env python3
"""
recalibrate_scores.py — Query article_feedback rejection patterns and log
weight adjustment suggestions.

Does NOT modify any scoring data. Output only — data collection phase.

Usage:
    python scripts/recalibrate_scores.py

Required env vars (or .env file):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not required; set env vars directly

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required.")


def supabase_get(table, params=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    print(f"\n{'=' * 60}")
    print("  ClawBeat Score Recalibration Report")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 60}\n")

    # ── Fetch all rejection signals ───────────────────────────────────────────
    rejections = supabase_get(
        "article_feedback",
        "signal=eq.reject&select=article_id,reason,created_at",
    )

    if not rejections:
        print("No rejection signals found yet. Keep curating!")
        return

    print(f"Total rejections: {len(rejections)}\n")

    # ── Fetch article metadata for rejected URLs ──────────────────────────────
    rejected_ids = list({r["article_id"] for r in rejections})
    # Supabase REST `in` filter: url=in.(val1,val2,...)
    ids_csv = ",".join(f'"{i}"' for i in rejected_ids[:200])
    articles = supabase_get(
        "news_items",
        f"url=in.({ids_csv})&select=url,source,source_type,total_score,d1_tier",
    )
    article_map = {a["url"]: a for a in articles}

    # ── Merge rejection + article metadata ────────────────────────────────────
    records = []
    for r in rejections:
        art = article_map.get(r["article_id"], {})
        records.append(
            {
                "url": r["article_id"],
                "reason": r["reason"],
                "source": art.get("source") or "unknown",
                "source_type": art.get("source_type") or "standard",
                "total_score": art.get("total_score"),
                "d1_tier": art.get("d1_tier"),
            }
        )

    # ── Analysis 1: Rejection reason breakdown ────────────────────────────────
    reason_counts: dict[str, int] = defaultdict(int)
    for r in records:
        reason_counts[r["reason"]] += 1

    print("── Rejection reasons ──")
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        pct = count / len(records) * 100
        print(f"  {reason:<26}  {count:>3}  ({pct:.0f}%)")
    print()

    # ── Analysis 2: Sources with repeated rejections ──────────────────────────
    source_data: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "reasons": defaultdict(int), "source_type": "standard"}
    )
    for r in records:
        s = r["source"]
        source_data[s]["count"] += 1
        source_data[s]["reasons"][r["reason"]] += 1
        source_data[s]["source_type"] = r["source_type"]

    repeat_sources = {s: v for s, v in source_data.items() if v["count"] >= 2}
    if repeat_sources:
        print("── Sources with ≥2 rejections ──")
        for source, data in sorted(repeat_sources.items(), key=lambda x: -x[1]["count"]):
            top_reason = max(data["reasons"], key=data["reasons"].get)
            flag = " ← already delisted" if data["source_type"] == "delist" else ""
            print(
                f"  {source:<30}  {data['count']:>3} rejection(s)  "
                f"top: {top_reason}{flag}"
            )
        print()

    # ── Suggestions (not applied — review manually) ───────────────────────────
    print("── Weight adjustment suggestions (NOT applied — review manually) ──")
    suggestions = []

    # Sources with ≥3 rejections not yet delisted → suggest delisting
    for source, data in source_data.items():
        if data["count"] >= 3 and data["source_type"] != "delist":
            suggestions.append(
                f"  SUGGEST DELIST: '{source}' ({data['count']} rejections) — "
                f"set source_type='delist' in news_items or whitelist_sources"
            )

    # High off_topic count among tier ≥2 articles → d1 threshold too loose
    off_topic = [r for r in records if r["reason"] == "off_topic"]
    if len(off_topic) >= 3:
        tier_loose = [r for r in off_topic if r["d1_tier"] and r["d1_tier"] >= 2]
        if tier_loose:
            suggestions.append(
                f"  SUGGEST D1 REVIEW: {len(tier_loose)} off_topic rejections are "
                f"d1_tier ≥2 — consider raising the minimum d1_tier threshold "
                f"for feed inclusion (currently includes tier 1–3)"
            )

    # High too_elementary count → d3 (technical depth) weight may be too low
    elementary = [r for r in records if r["reason"] == "too_elementary"]
    if len(elementary) >= 3:
        suggestions.append(
            f"  SUGGEST D3 WEIGHT: {len(elementary)} too_elementary rejections — "
            f"consider increasing d3_score weight to surface more technical content"
        )

    # High clickbait count → d2 (source quality) may need a tighter ceiling
    clickbait = [r for r in records if r["reason"] == "clickbait"]
    if len(clickbait) >= 3:
        suggestions.append(
            f"  SUGGEST D2 CEILING: {len(clickbait)} clickbait rejections — "
            f"consider reducing max d2_score for sources with repeated clickbait flags"
        )

    if suggestions:
        for s in suggestions:
            print(s)
    else:
        print("  No actionable patterns yet — keep collecting signals.")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
