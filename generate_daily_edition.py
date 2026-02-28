#!/usr/bin/env python3
"""
generate_daily_edition.py
─────────────────────────
Generates a Daily Edition HTML page for ClawBeat.

Usage:
  python generate_daily_edition.py

Environment variables (all required):
  SUPABASE_URL           Supabase project URL
  SUPABASE_SERVICE_KEY   Supabase service-role key (bypasses RLS)
  GEMINI_API_KEY         Google AI Studio API key

Optional:
  EDITION_DATE           Override date in YYYY-MM-DD format (defaults to today PT)

Output:
  public/daily/YYYY-MM-DD.html
  Supabase daily_editions table row updated
"""

import os
import re
import sys
import json
import time
import datetime
import textwrap
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from google import genai

# ─── Config ──────────────────────────────────────────────────────────────────

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
EDITION_DATE_OVERRIDE = os.environ.get("EDITION_DATE", "").strip()  # YYYY-MM-DD

TEMPLATE_PATH = Path(__file__).parent / "public" / "daily-edition.html"
OUTPUT_DIR    = Path(__file__).parent / "public" / "daily"
COMPILED_TIME = "17:00 PT"

# Gemini model
GEMINI_MODEL = "gemini-1.5-flash"

# Max characters for article text sent to Gemini (to stay within token limits)
MAX_ARTICLE_CHARS = 8000

# ─── Date helpers ────────────────────────────────────────────────────────────

def today_pt() -> datetime.date:
    """Return today's date in Pacific Time."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    return datetime.datetime.now(tz).date()

def iso_to_mdy(iso: str) -> str:
    """YYYY-MM-DD → MM-DD-YYYY"""
    y, m, d = iso.split("-")
    return f"{m}-{d}-{y}"

def mdy_to_iso(mdy: str) -> str:
    """MM-DD-YYYY → YYYY-MM-DD"""
    parts = mdy.split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        m, d, y = parts
        return f"{y}-{m}-{d}"
    return mdy  # already ISO or unexpected format

def fmt_display_date(iso: str) -> str:
    """YYYY-MM-DD → MM-DD-YYYY (display format matching template {{DATE}})"""
    return iso_to_mdy(iso)

# ─── Supabase helpers ────────────────────────────────────────────────────────

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def score_article(item: dict) -> int:
    """Replicate frontend scoring for algorithmic spotlight selection."""
    return len(item.get("more_coverage") or []) * 3

def get_spotlight_articles(sb: Client, dispatch_date_mdy: str) -> list[dict]:
    """
    Returns 4 story dicts for the given dispatch date, applying spotlight_overrides
    (same logic as the React frontend).
    Each dict has keys: url, title, source, summary, date
    """
    # Load all articles for this date
    articles_res = sb.table("news_items") \
        .select("url,title,source,summary,date,more_coverage,tags") \
        .eq("date", dispatch_date_mdy) \
        .execute()
    articles = articles_res.data or []

    # Load overrides for this date
    overrides_res = sb.table("spotlight_overrides") \
        .select("*") \
        .eq("dispatch_date", dispatch_date_mdy) \
        .execute()
    overrides_by_slot = {ov["slot"]: ov for ov in (overrides_res.data or [])}

    # Algorithmic queue: sort by score, exclude overridden URLs
    overridden_urls = {ov["url"] for ov in overrides_by_slot.values()}
    queue = sorted(
        [a for a in articles if a["url"] not in overridden_urls],
        key=lambda a: score_article(a),
        reverse=True
    )

    slots = []
    for slot in [1, 2, 3, 4]:
        if slot in overrides_by_slot:
            ov = overrides_by_slot[slot]
            slots.append({
                "url":     ov["url"],
                "title":   ov.get("title") or "",
                "source":  ov.get("source") or "",
                "summary": ov.get("summary") or "",
                "date":    dispatch_date_mdy,
                "tags":    ov.get("tags") or [],
            })
        elif queue:
            a = queue.pop(0)
            slots.append({
                "url":     a["url"],
                "title":   a.get("title") or "",
                "source":  a.get("source") or "",
                "summary": a.get("summary") or "",
                "date":    dispatch_date_mdy,
                "tags":    a.get("tags") or [],
            })
        # If queue is empty and no override, slot is omitted

    return slots

# ─── Article metadata & image scraping ───────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ClawBeatBot/1.0; "
        "+https://clawbeat.co)"
    )
}

def fetch_article_meta(url: str) -> dict:
    """
    Fetches article URL and extracts Open Graph metadata.
    Returns dict with keys: image_url, image_alt, author, pub_name, pub_url, pub_date, description
    """
    meta = {
        "image_url":  "",
        "image_alt":  "",
        "author":     "",
        "pub_name":   "",
        "pub_url":    "",
        "pub_date":   "",
        "description": "",
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return (tag.get("content") or "") if tag else ""

        meta["image_url"]   = og("og:image") or og("twitter:image")
        meta["image_alt"]   = og("og:image:alt") or og("twitter:title") or og("og:title")
        meta["description"] = og("og:description") or og("description")

        # Author — try article:author, then various meta tags
        meta["author"] = (
            og("article:author")
            or og("author")
            or og("twitter:creator")
            or ""
        )
        # Strip URL-style author (some sites put a URL here)
        if meta["author"].startswith("http"):
            meta["author"] = ""

        # Publisher name
        meta["pub_name"] = og("og:site_name") or ""

        # Publisher URL — derive from article URL origin
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            meta["pub_url"] = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        # Publication date
        pub_date_raw = (
            og("article:published_time")
            or og("date")
            or og("pubdate")
            or ""
        )
        if pub_date_raw:
            # Convert ISO 8601 → MM-DD-YYYY
            try:
                dt = datetime.datetime.fromisoformat(pub_date_raw[:10])
                meta["pub_date"] = dt.strftime("%m-%d-%Y")
            except Exception:
                meta["pub_date"] = pub_date_raw[:10]

    except Exception as e:
        print(f"  [meta] Warning: could not fetch {url}: {e}", file=sys.stderr)

    return meta


def fetch_article_text(url: str) -> str:
    """
    Fetch clean article text via Jina Reader (https://r.jina.ai/{url}).
    Falls back to empty string on failure.
    """
    jina_url = f"https://r.jina.ai/{url}"
    try:
        r = requests.get(jina_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        text = r.text.strip()
        # Jina returns markdown; truncate to avoid token bloat
        return text[:MAX_ARTICLE_CHARS]
    except Exception as e:
        print(f"  [jina] Warning: could not fetch {url}: {e}", file=sys.stderr)
        return ""

# ─── Gemini helpers ───────────────────────────────────────────────────────────

def setup_gemini():
    return genai.Client(api_key=GEMINI_API_KEY)

def call_gemini(client, prompt: str, retries: int = 5) -> str:
    """Call Gemini with retry on rate-limit errors."""
    for attempt in range(retries):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text.strip()
        except Exception as e:
            err = str(e).lower()
            is_rate_limit = (
                "429" in err or "quota" in err or "rate" in err
                or "resource_exhausted" in err or "resourceexhausted" in err
                or "too many requests" in err
            )
            if is_rate_limit:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s, 240s, 480s
                print(f"  [gemini] Rate limited (attempt {attempt+1}), waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  [gemini] Error: {e}", file=sys.stderr)
                return ""
    return ""

def generate_ai_content(client, article_text: str, fallback: str = "") -> tuple:
    """
    Generate both summary and analysis in a SINGLE Gemini call per story.
    Returns (summary_html, why_it_matters) tuple.
    Halves API usage vs two separate calls.
    """
    context = article_text or fallback
    if not context:
        return '<p class="story-summary">Summary unavailable.</p>', "Analysis unavailable."

    prompt = textwrap.dedent(f"""
        You are a veteran technology reporter and senior industry analyst.
        Read the article below and produce TWO sections, separated by exactly the line: ---ANALYSIS---

        SECTION 1 — Journalist Summary (~700 characters):
        - Lead with the single most newsworthy development — not background, not context
        - State the real-world impact concisely: who is affected and how
        - Cut all hype, marketing language, and superlatives; use precise, concrete language
        - Avoid passive voice where possible
        - Tell a story: there should be a clear subject doing something with a consequence
        - Do not start with "The article" or restate the headline
        - Write one flowing paragraph — no bullets, no headers
        - Return as: <p class="story-summary">Your summary here.</p>

        ---ANALYSIS---

        SECTION 2 — Why It Matters (~500 characters, plain text):
        As a senior industry analyst covering AI and OpenClaw: explain the strategic significance —
        why this matters beyond the headline, who benefits, what this signals about where the
        market is heading, and what's missing or underplayed in the coverage.
        No bullets. One plain paragraph.

        Article:
        {context[:8000]}
    """).strip()

    result = call_gemini(client, prompt)

    if "---ANALYSIS---" in result:
        parts = result.split("---ANALYSIS---", 1)
        summary_raw = parts[0].strip()
        analysis   = re.sub(r"<[^>]+>", "", parts[1]).strip()
    else:
        summary_raw = result.strip()
        analysis   = ""

    # Ensure summary is wrapped in the correct HTML tag
    match = re.search(r'<p class="story-summary">.*?</p>', summary_raw, re.DOTALL)
    if match:
        summary_html = match.group(0)
    else:
        text = re.sub(r"<[^>]+>", "", summary_raw).strip()
        summary_html = f'<p class="story-summary">{text or fallback[:700]}</p>'

    return summary_html, analysis or "Analysis unavailable."

# ─── Infer category from tags / source ───────────────────────────────────────

def infer_category(story: dict) -> str:
    tags = story.get("tags") or []
    if isinstance(tags, list) and tags:
        return " · ".join(str(t).title() for t in tags[:2])
    source = story.get("source") or ""
    return source or "AI"

# ─── Template rendering ───────────────────────────────────────────────────────

def render_template(template: str, variables: dict) -> str:
    """Replace all {{KEY}} placeholders in template with values from variables dict."""
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value) if value is not None else "")
    return template

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Determine edition date
    if EDITION_DATE_OVERRIDE:
        edition_iso = EDITION_DATE_OVERRIDE  # YYYY-MM-DD
    else:
        edition_iso = today_pt().isoformat()  # YYYY-MM-DD

    dispatch_mdy = iso_to_mdy(edition_iso)   # MM-DD-YYYY (matches DB date format)
    display_date = fmt_display_date(edition_iso)  # MM-DD-YYYY for template

    print(f"[daily-edition] Generating edition for {edition_iso} (dispatch date: {dispatch_mdy})")

    # Connect to Supabase
    sb = get_supabase()

    # Check for existing daily_editions row (may have admin overrides)
    existing_res = sb.table("daily_editions") \
        .select("stories") \
        .eq("edition_date", edition_iso) \
        .execute()
    existing_stories: list[dict] = []
    if existing_res.data:
        existing_stories = existing_res.data[0].get("stories") or []
    existing_by_slot = {s["slot"]: s for s in existing_stories if "slot" in s}

    # Get spotlight articles for this dispatch
    spotlight = get_spotlight_articles(sb, dispatch_mdy)
    if not spotlight:
        print(f"[daily-edition] No articles found for {dispatch_mdy}. Exiting.", file=sys.stderr)
        sys.exit(1)

    print(f"[daily-edition] Found {len(spotlight)} spotlight articles")

    # Setup Gemini
    model = setup_gemini()

    # Build story data
    final_stories: list[dict] = []

    for idx, article in enumerate(spotlight):
        slot = idx + 1
        url   = article["url"]
        print(f"[daily-edition] Processing slot {slot}: {url}")

        # Start with any existing admin-saved data for this slot
        saved = existing_by_slot.get(slot, {})

        # --- Article metadata & image ---
        if saved.get("image_url"):
            # Admin already set image — use it
            image_url   = saved["image_url"]
            image_alt   = saved.get("image_alt") or article["title"]
            credit_name = saved.get("credit_name") or ""
            credit_url  = saved.get("credit_url") or ""
            author      = saved.get("author") or ""
            pub_name    = saved.get("pub_name") or article["source"]
            pub_url     = saved.get("pub_url") or ""
            pub_date    = saved.get("pub_date") or ""
        else:
            meta        = fetch_article_meta(url)
            image_url   = meta["image_url"]
            image_alt   = meta["image_alt"] or article["title"]
            author      = meta["author"]
            pub_name    = meta["pub_name"] or article["source"] or ""
            pub_url     = meta["pub_url"]
            pub_date    = meta["pub_date"] or dispatch_mdy
            # Credit defaults to publication name/url if no specific photographer
            credit_name = saved.get("credit_name") or pub_name
            credit_url  = saved.get("credit_url") or pub_url

        category = saved.get("category") or infer_category(article)

        # --- AI content ---
        # If admin already saved summary/analysis, use those verbatim
        if saved.get("summary_html") and saved.get("why_it_matters"):
            summary_html   = saved["summary_html"]
            why_it_matters = saved["why_it_matters"]
            print(f"  Slot {slot}: Using admin-saved AI content")
        else:
            # Fetch article text for Gemini
            article_text = fetch_article_text(url)
            fallback_text = article.get("summary") or article.get("title") or ""

            need_summary  = not saved.get("summary_html")
            need_analysis = not saved.get("why_it_matters")

            if need_summary or need_analysis:
                print(f"  Slot {slot}: Generating AI content…")
                gen_summary, gen_analysis = generate_ai_content(model, article_text, fallback_text)
                time.sleep(10)  # One call per story; 10s keeps us safely under 15 RPM
            else:
                gen_summary = gen_analysis = None

            summary_html   = saved.get("summary_html")   or gen_summary  or f'<p class="story-summary">{fallback_text[:700]}</p>'
            why_it_matters = saved.get("why_it_matters") or gen_analysis or "Analysis unavailable."

        story = {
            "slot":           slot,
            "url":            url,
            "headline":       article["title"],
            "author":         author,
            "pub_name":       pub_name,
            "pub_url":        pub_url,
            "pub_date":       pub_date,
            "category":       category,
            "image_url":      image_url,
            "image_alt":      image_alt,
            "credit_name":    credit_name,
            "credit_url":     credit_url,
            "summary_html":   summary_html,
            "why_it_matters": why_it_matters,
        }
        final_stories.append(story)

    # --- Save to Supabase ---
    print("[daily-edition] Saving to Supabase daily_editions…")
    sb.table("daily_editions").upsert({
        "edition_date": edition_iso,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "stories":      final_stories,
    }, on_conflict="edition_date").execute()

    # --- Build template variables ---
    template_vars: dict[str, str] = {
        "DATE":          display_date,
        "COMPILED_TIME": COMPILED_TIME,
    }

    for story in final_stories:
        n = story["slot"]
        template_vars[f"STORY_{n}_URL"]               = story["url"]
        template_vars[f"STORY_{n}_HEADLINE"]          = story["headline"]
        template_vars[f"STORY_{n}_AUTHOR"]            = story["author"]
        template_vars[f"STORY_{n}_PUBLICATION_NAME"]  = story["pub_name"]
        template_vars[f"STORY_{n}_PUBLICATION_URL"]   = story["pub_url"]
        template_vars[f"STORY_{n}_PUB_DATE"]          = story["pub_date"]
        template_vars[f"STORY_{n}_CATEGORY"]          = story["category"]
        template_vars[f"STORY_{n}_IMAGE_URL"]         = story["image_url"]
        template_vars[f"STORY_{n}_IMAGE_ALT"]         = story["image_alt"]
        template_vars[f"STORY_{n}_PHOTO_CREDIT_NAME"] = story["credit_name"]
        template_vars[f"STORY_{n}_PHOTO_CREDIT_URL"]  = story["credit_url"]
        template_vars[f"STORY_{n}_SUMMARY_HTML"]      = story["summary_html"]
        template_vars[f"STORY_{n}_WHY_IT_MATTERS"]    = story["why_it_matters"]

    # Fill empty slots (stories 2-4 may be missing if dispatch had fewer articles)
    for n in range(len(final_stories) + 1, 5):
        template_vars[f"STORY_{n}_URL"]               = "#"
        template_vars[f"STORY_{n}_HEADLINE"]          = "—"
        template_vars[f"STORY_{n}_AUTHOR"]            = ""
        template_vars[f"STORY_{n}_PUBLICATION_NAME"]  = ""
        template_vars[f"STORY_{n}_PUBLICATION_URL"]   = "#"
        template_vars[f"STORY_{n}_PUB_DATE"]          = ""
        template_vars[f"STORY_{n}_CATEGORY"]          = ""
        template_vars[f"STORY_{n}_IMAGE_URL"]         = ""
        template_vars[f"STORY_{n}_IMAGE_ALT"]         = ""
        template_vars[f"STORY_{n}_PHOTO_CREDIT_NAME"] = ""
        template_vars[f"STORY_{n}_PHOTO_CREDIT_URL"]  = "#"
        template_vars[f"STORY_{n}_SUMMARY_HTML"]      = '<p class="story-summary">No story available for this slot.</p>'
        template_vars[f"STORY_{n}_WHY_IT_MATTERS"]    = ""

    # --- Render & write HTML ---
    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")
    output_html   = render_template(template_html, template_vars)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{edition_iso}.html"
    output_path.write_text(output_html, encoding="utf-8")
    print(f"[daily-edition] Written to {output_path}")
    print(f"[daily-edition] Done. Stories: {len(final_stories)}")


if __name__ == "__main__":
    main()
