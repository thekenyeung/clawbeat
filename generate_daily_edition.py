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
  public/daily/YYYY-MM-DD/[seo-slug].html
  Supabase daily_editions table row updated
"""

import os
import re
import sys
import json
import time
import random
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
SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")   # public read key for client-side JS
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
EDITION_DATE_OVERRIDE = os.environ.get("EDITION_DATE", "").strip()  # YYYY-MM-DD

TEMPLATE_PATH  = Path(__file__).parent / "public" / "daily-edition.html"
OUTPUT_DIR     = Path(__file__).parent / "public" / "daily"
_WHITELIST_PATH = Path(__file__).parent / "src" / "whitelist.json"
_WHITELIST: list[dict] = json.loads(_WHITELIST_PATH.read_text()) if _WHITELIST_PATH.exists() else []
COMPILED_TIME = "17:00 PT"

# Fallback hero images (rotated randomly) used when no og:image can be scraped
FALLBACK_IMAGES = [
    "https://clawbeat.co/images/lobster-adobe-firefly-paper-1500x571.jpg",
    "https://clawbeat.co/images/lobster-two-computer-screen-adobe-firefly-1500x571.jpg",
    "https://clawbeat.co/images/lobster-ipad-screen-adobe-firefly-1500x571.jpg",
]

# Gemini model
GEMINI_MODEL = "gemini-2.5-flash"

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

def _is_whitelisted(url: str, source: str) -> bool:
    """Return True if the article's URL or source name matches a whitelist entry."""
    url_lower    = (url    or "").lower()
    source_lower = (source or "").lower().strip()
    for w in _WHITELIST:
        wname = str(w.get("Source Name", "") or "").lower().strip()
        if wname and wname == source_lower:
            return True
        wurl = (str(w.get("Website URL", "") or "")
                .lower()
                .replace("https://", "")
                .replace("http://", "")
                .replace("www.", ""))
        if wurl and wurl in url_lower:
            return True
    return False


def score_article(item: dict) -> int:
    """Replicate frontend scoring for algorithmic spotlight selection."""
    score = len(item.get("more_coverage") or []) * 3

    # Priority publisher boost (Substack, Beehiiv newsletters)
    if item.get("source_type") == "priority":
        score += 2

    # Whitelist source boost — open-access publishers rank higher for Daily Edition fetchability.
    # medium.com excluded (paywall/fetch failures); other whitelisted sources get a strong boost.
    url = (item.get("url") or "").lower()
    if _is_whitelisted(item.get("url", ""), item.get("source", "")) and "medium.com" not in url:
        score += 6

    # OpenClaw-specific boost — news_items are already news articles; only exclude "Show HN" posts
    title = (item.get("title") or "").lower()
    if not title.startswith("show hn:"):
        d1_tier = item.get("d1_tier")
        if d1_tier == 1:
            score += 5   # Direct OpenClaw/Moltbot/Clawdbot coverage
        elif d1_tier == 2:
            score += 3   # Moltbook coverage

    return score

def get_spotlight_articles(sb: Client, dispatch_date_mdy: str) -> list[dict]:
    """
    Returns 4 story dicts for the given dispatch date, applying spotlight_overrides
    (same logic as the React frontend).
    Each dict has keys: url, title, source, summary, date
    """
    # Load all articles for this date — exclude items pending review or suppressed
    articles_res = sb.table("news_items") \
        .select("url,title,source,summary,date,more_coverage,tags,d1_tier,source_type,total_score") \
        .eq("date", dispatch_date_mdy) \
        .eq("pending_review", False) \
        .execute()
    # Also exclude suppressed items (total_score < 10), matching frontend behaviour
    articles = [
        a for a in (articles_res.data or [])
        if a.get("total_score") is None or (a.get("total_score") or 0) >= 10
    ]
    print(f"[daily-edition] Raw news_items count for {dispatch_date_mdy}: {len(articles)}")

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

def slugify(text: str, fallback: str = "edition") -> str:
    """Convert a headline into a URL-safe ASCII slug (max 60 chars)."""
    text = text.encode("ascii", "ignore").decode("ascii")  # strip non-ASCII
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or fallback


def generate_edition_summary(client, stories: list[dict]) -> tuple:
    """
    Generate a 2–3 sentence edition overview from all story headlines/summaries.
    Returns (summary_html, summary_plain) tuple.
    summary_html  → <p class="edition-summary">…</p>   (baked into page HTML)
    summary_plain → plain text ≤155 chars              (meta description / JSON-LD)
    """
    if not stories:
        return "", ""

    story_lines = []
    for s in stories:
        headline = s.get("headline", "")
        raw_summary = re.sub(r"<[^>]+>", "", s.get("summary_html", "")).strip()
        story_lines.append(f"- {headline}: {raw_summary[:300]}")

    prompt = textwrap.dedent(f"""
        You are an editor at ClawBeat, an intelligence digest covering the OpenClaw
        agentic AI framework ecosystem.

        Today's edition covers these stories:
        {chr(10).join(story_lines)}

        Write a 2–3 sentence editorial overview paragraph that:
        - Summarizes the major themes across today's signals
        - Is specific — name technologies, companies, or trends where relevant
        - Reads as a tight lead-in to the stories that follow
        - Does NOT use phrases like "today's edition", "in this issue", or "we cover"
        - Uses present tense, authoritative voice
        - Is suitable as both a full display paragraph AND a ~155-char meta description

        Return ONLY two parts separated by exactly the line: ---PLAIN---

        PART 1 (display HTML):
        <p class="edition-summary">Your 2–3 sentence paragraph here.</p>

        ---PLAIN---

        PART 2 (plain text, ≤155 characters, no HTML):
        Your condensed plain-text version here.
    """).strip()

    result = call_gemini(client, prompt)

    if "---PLAIN---" in result:
        html_raw, plain_raw = result.split("---PLAIN---", 1)
        plain = plain_raw.strip()[:155]
    else:
        html_raw = result
        plain = ""

    # Ensure correct HTML wrapper
    match = re.search(r'<p class="edition-summary">.*?</p>', html_raw, re.DOTALL)
    if match:
        summary_html = match.group(0)
    else:
        text = re.sub(r"<[^>]+>", "", html_raw).strip()
        summary_html = f'<p class="edition-summary">{text}</p>'

    if not plain:
        plain = re.sub(r"<[^>]+>", "", summary_html).strip()[:155]

    return summary_html, plain


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

        SECTION 2 — Why It Matters (4–8 bullet points, HTML only):
        You are a senior analyst embedded in the OpenClaw ecosystem — a platform for building
        AI-powered agents and tools. Act like an analyst reviewing this story for the OpenClaw
        developer community. Write 4–8 concise, pointed bullet points covering:
        - How this impacts, influences, or benefits the OpenClaw community and ecosystem
        - What consequences this has for developers actively building OpenClaw agents
        - Risks, trade-offs, or things developers should watch out for
        - Any emerging trends this story signals within the space
        Be critical but also informative and inspiring. Do not generalize to "AI broadly."
        Ground every point in OpenClaw-specific implications.
        Return ONLY the following HTML — no preamble, no markdown, no extra text:
        <ul class="wim-list">
          <li>First bullet point here.</li>
          <li>Second bullet point here.</li>
        </ul>

        Article:
        {context[:8000]}
    """).strip()

    result = call_gemini(client, prompt)

    if "---ANALYSIS---" in result:
        parts = result.split("---ANALYSIS---", 1)
        summary_raw = parts[0].strip()
        analysis   = parts[1].strip()
    else:
        summary_raw = result.strip()
        analysis   = ""

    # Strip markdown code fences Gemini sometimes wraps around HTML output
    analysis = re.sub(r'^```(?:html)?\s*', '', analysis, flags=re.IGNORECASE)
    analysis = re.sub(r'\s*```$', '', analysis)
    analysis = analysis.strip()

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

# ─── Credit HTML builder ─────────────────────────────────────────────────────

def build_hero_credit_html(credit_name: str, credit_url: str, article_url: str) -> str:
    """
    Returns the full <div class="hero-credit"> block, or empty string if no credit.
    - Fallback image (credit_name='Adobe Firefly', credit_url='') → plain text, no link
    - Normal image with credit → linked to article URL
    - No credit name → empty string (div omitted)
    """
    if not credit_name:
        return ""
    if credit_url or (credit_name != "Adobe Firefly" and article_url):
        link_href = credit_url or article_url
        return (
            f'<div class="hero-credit">Photo by '
            f'<a href="{link_href}" target="_blank" rel="noopener">{credit_name}</a>'
            f'</div>'
        )
    # Adobe Firefly fallback or no URL — plain text
    return f'<div class="hero-credit">Photo by {credit_name}</div>'


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
        .select("stories,permalink") \
        .eq("edition_date", edition_iso) \
        .execute()
    existing_stories: list[dict] = []
    existing_permalink: str = ""
    if existing_res.data:
        existing_stories  = existing_res.data[0].get("stories") or []
        existing_permalink = existing_res.data[0].get("permalink") or ""
    # Key by slot AND url so saved data is never applied to a different story
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

        # Use admin-saved data only when it belongs to the same URL (re-runs may
        # produce a different story order; applying saved data across URLs corrupts content)
        _saved_candidate = existing_by_slot.get(slot, {})
        saved = _saved_candidate if _saved_candidate.get("url") == url else {}

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
            image_url   = meta["image_url"] or random.choice(FALLBACK_IMAGES)
            image_alt   = meta["image_alt"] or article["title"]
            author      = meta["author"]
            pub_name    = meta["pub_name"] or article["source"] or ""
            pub_url     = meta["pub_url"]
            pub_date    = meta["pub_date"] or dispatch_mdy
            if image_url in FALLBACK_IMAGES:
                # Fallback image: credit Adobe Firefly, not the publication
                credit_name = "Adobe Firefly"
                credit_url  = ""
            else:
                # Credit defaults to publication name if no specific photographer
                credit_name = saved.get("credit_name") or pub_name
                credit_url  = saved.get("credit_url") or pub_url

        category = saved.get("category") or infer_category(article)

        # --- AI content ---
        # why_it_matters must be HTML (starts with '<') — plain text means old format, regenerate
        saved_analysis = saved.get("why_it_matters") or ""
        analysis_is_html = saved_analysis.strip().startswith("<")

        # If admin already saved both fields in current format, use them verbatim
        if saved.get("summary_html") and analysis_is_html:
            summary_html   = saved["summary_html"]
            why_it_matters = saved_analysis
            print(f"  Slot {slot}: Using admin-saved AI content")
        else:
            # Fetch article text for Gemini
            article_text = fetch_article_text(url)
            fallback_text = article.get("summary") or article.get("title") or ""

            need_summary  = not saved.get("summary_html")
            need_analysis = not analysis_is_html  # regenerate if plain text or missing

            if need_summary or need_analysis:
                print(f"  Slot {slot}: Generating AI content…")
                gen_summary, gen_analysis = generate_ai_content(model, article_text, fallback_text)
                time.sleep(10)  # One call per story; 10s keeps us safely under 15 RPM
            else:
                gen_summary = gen_analysis = None

            summary_html   = saved.get("summary_html")   or gen_summary  or f'<p class="story-summary">{fallback_text[:700]}</p>'
            # When need_analysis is True, prefer fresh gen_analysis — saved value is old plain text
            why_it_matters = (gen_analysis if need_analysis else None) or saved.get("why_it_matters") or gen_analysis or "Analysis unavailable."

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

    # --- Edition summary (one extra Gemini call, baked into HTML for SEO) ---
    print("[daily-edition] Generating edition summary…")
    edition_summary_html, edition_summary_plain = generate_edition_summary(model, final_stories)
    time.sleep(10)

    # --- Build slug from story 1 headline ---
    s1 = final_stories[0] if final_stories else {}
    edition_slug = slugify(s1.get("headline", edition_iso), fallback=edition_iso)

    # --- Save to Supabase ---
    print("[daily-edition] Saving to Supabase daily_editions…")
    sb.table("daily_editions").upsert({
        "edition_date": edition_iso,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "stories":      final_stories,
        "permalink":    edition_slug,
        "summary":      edition_summary_plain,
    }, on_conflict="edition_date").execute()

    # --- Build template variables ---
    # Global vars + story-1 fields (OG/social meta) + edition summary all baked into HTML.
    # Story content is fetched client-side from Supabase on page load.
    template_vars: dict[str, str] = {
        "DATE":                  display_date,
        "EDITION_ISO":           edition_iso,
        "EDITION_SLUG":          edition_slug,
        "COMPILED_TIME":         COMPILED_TIME,
        "SUPABASE_URL":          SUPABASE_URL,
        "SUPABASE_ANON_KEY":     SUPABASE_ANON_KEY,
        # Story 1 fields for OG/social meta tags
        "STORY_1_HEADLINE":      s1.get("headline", "—"),
        "STORY_1_IMAGE_URL":     s1.get("image_url", ""),
        # JSON-encoded values safe for embedding in JSON-LD
        "STORY_1_HEADLINE_JSON": json.dumps(f"The Daily Edition: {s1.get('headline', '')}"),
        # Edition summary — HTML block and plain text
        "EDITION_SUMMARY":       edition_summary_html,
        "EDITION_SUMMARY_PLAIN": edition_summary_plain,
        "EDITION_SUMMARY_JSON":  json.dumps(edition_summary_plain),
    }

    # --- Render & write HTML ---
    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")
    output_html   = render_template(template_html, template_vars)

    edition_dir = OUTPUT_DIR / edition_iso
    edition_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale HTML file if the slug changed on re-run
    if existing_permalink and existing_permalink != edition_slug:
        stale_path = edition_dir / f"{existing_permalink}.html"
        if stale_path.exists():
            stale_path.unlink()
            print(f"[daily-edition] Removed stale file: {stale_path.name}")

    output_path = edition_dir / f"{edition_slug}.html"
    output_path.write_text(output_html, encoding="utf-8")
    print(f"[daily-edition] Written to {output_path}")
    print(f"[daily-edition] Done. Stories: {len(final_stories)}")

    # --- Update sitemaps ---
    update_sitemaps(edition_iso, edition_slug, s1.get("headline", "Daily Edition"))


def update_sitemaps(edition_iso: str, edition_slug: str, headline: str) -> None:
    """Prepend the new daily edition to public/sitemap.xml and public/sitemap.html."""
    import re as _re
    public_dir = Path(__file__).parent / "public"
    page_url = f"https://clawbeat.co/daily/{edition_iso}/{edition_slug}.html"
    page_path = f"/daily/{edition_iso}/{edition_slug}.html"

    # ── sitemap.xml ──
    xml_path = public_dir / "sitemap.xml"
    xml = xml_path.read_text(encoding="utf-8")
    if page_url not in xml:
        xml_entry = (
            f'  <url><loc>{page_url}</loc>'
            f'<lastmod>{edition_iso}</lastmod>'
            f'<changefreq>never</changefreq>'
            f'<priority>0.7</priority></url>\n'
        )
        marker = "  <!-- ── Daily Editions ── -->\n"
        xml = xml.replace(marker, marker + xml_entry)
        xml_path.write_text(xml, encoding="utf-8")
        print(f"[sitemap.xml] Added {edition_iso}")
    else:
        print(f"[sitemap.xml] {edition_iso} already present, skipping")

    # ── sitemap.html ──
    html_path = public_dir / "sitemap.html"
    html = html_path.read_text(encoding="utf-8")
    if page_path not in html:
        html_entry = (
            f'      <a class="daily-row" href="{page_path}">\n'
            f'        <span class="daily-date">{edition_iso}</span>\n'
            f'        <span class="daily-title">{headline}</span>\n'
            f'      </a>\n'
        )
        grid_marker = '    <div class="daily-grid">\n'
        html = _re.sub(
            r'<span class="section-count">(\d+) editions</span>',
            lambda m: f'<span class="section-count">{int(m.group(1)) + 1} editions</span>',
            html,
        )
        html = html.replace(grid_marker, grid_marker + html_entry)
        html_path.write_text(html, encoding="utf-8")
        print(f"[sitemap.html] Added {edition_iso}")
    else:
        print(f"[sitemap.html] {edition_iso} already present, skipping")


if __name__ == "__main__":
    main()
