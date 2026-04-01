#!/usr/bin/env python3
"""
generate_trace.py
─────────────────
Generates a monthly Trace issue for ClawBeat.

Usage:
  python generate_trace.py

Environment variables (all required):
  SUPABASE_URL           Supabase project URL
  SUPABASE_SERVICE_KEY   Supabase service-role key (bypasses RLS)
  GEMINI_API_KEY         Google AI Studio API key

Optional:
  TRACE_MONTH            Override month in YYYY-MM format (defaults to previous month)

Output:
  public/trace/YYYY-MM/index.html          ← cover page
  public/trace/YYYY-MM/[slug]/index.html   ← 20 story pages
  Supabase trace_issues table row upserted
"""

import os
import re
import sys
import json
import time
import datetime
import calendar
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from google import genai

# ─── Config ──────────────────────────────────────────────────────────────────

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
TRACE_MONTH_OVERRIDE = os.environ.get("TRACE_MONTH", "").strip()  # YYYY-MM

COVER_TEMPLATE_PATH   = Path(__file__).parent / "public" / "trace-issue.html"
ARTICLE_TEMPLATE_PATH = Path(__file__).parent / "public" / "trace-article.html"
OUTPUT_BASE           = Path(__file__).parent / "public" / "trace"
COMPILED_TIME         = "17:00 PT"

# Issue numbering: February 2026 = No. 001
LAUNCH_YEAR  = 2026
LAUNCH_MONTH = 2

# Fallback images (category → URL)
FALLBACK_BASE = "https://clawbeat.co/images/trace-fallbacks"
FALLBACK_CATEGORIES = {
    "release":      f"{FALLBACK_BASE}/release.jpg",
    "tutorial":     f"{FALLBACK_BASE}/tutorial.jpg",
    "community":    f"{FALLBACK_BASE}/community.jpg",
    "research":     f"{FALLBACK_BASE}/research.jpg",
    "tool":         f"{FALLBACK_BASE}/tool.jpg",
    "announcement": f"{FALLBACK_BASE}/announcement.jpg",
    "general":      f"{FALLBACK_BASE}/general.jpg",
}

GEMINI_MODEL    = "gemini-2.5-flash"
MAX_ARTICLE_CHARS = 8000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClawBeatBot/1.0; +https://clawbeat.co)"
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def prev_month_ym() -> str:
    """Return YYYY-MM for the previous calendar month."""
    today = datetime.date.today()
    first = today.replace(day=1)
    last_month = first - datetime.timedelta(days=1)
    return last_month.strftime("%Y-%m")

def compute_issue_number(ym: str) -> str:
    """Return zero-padded issue number based on months since launch."""
    year, month = int(ym[:4]), int(ym[5:7])
    delta = (year - LAUNCH_YEAR) * 12 + (month - LAUNCH_MONTH) + 1
    return str(max(1, delta)).zfill(3)

def month_label(ym: str) -> str:
    """'2026-02' → 'February 2026'"""
    year, month = int(ym[:4]), int(ym[5:7])
    return f"{calendar.month_name[month]} {year}"

def month_date_range(ym: str):
    """Return (start_iso, end_iso) for a YYYY-MM month."""
    year, month = int(ym[:4]), int(ym[5:7])
    start = datetime.date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime.date(year, month, last_day)
    return start.isoformat(), end.isoformat()

def slugify(text: str, fallback: str = "story") -> str:
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or fallback

def esc(s: str) -> str:
    """HTML-escape a string."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def render_template(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value) if value is not None else "")
    return template

# ─── Supabase ─────────────────────────────────────────────────────────────────

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def fetch_news_items(sb: Client, ym: str, limit: int = 60) -> list[dict]:
    """
    Fetch news_items for the given month, sorted by total_score DESC.
    Uses inserted_at for date filtering (proper timestamp).
    """
    year, month = int(ym[:4]), int(ym[5:7])
    last_day = calendar.monthrange(year, month)[1]
    start_ts = f"{ym}-01T00:00:00+00:00"
    end_ts   = f"{ym}-{last_day:02d}T23:59:59+00:00"

    res = sb.table("news_items") \
        .select("url,title,source,summary,tags,total_score,inserted_at") \
        .gte("inserted_at", start_ts) \
        .lte("inserted_at", end_ts) \
        .order("total_score", desc=True) \
        .limit(limit) \
        .execute()
    return res.data or []

def fetch_daily_editions(sb: Client, ym: str) -> list[dict]:
    """Fetch daily_editions rows for the given month, sorted by date."""
    year, month = int(ym[:4]), int(ym[5:7])
    last_day = calendar.monthrange(year, month)[1]
    start = f"{ym}-01"
    end   = f"{ym}-{last_day:02d}"
    res = sb.table("daily_editions") \
        .select("edition_date,permalink,stories") \
        .gte("edition_date", start) \
        .lte("edition_date", end) \
        .order("edition_date") \
        .execute()
    return res.data or []

def fetch_github_repos(sb: Client, ym: str, limit: int = 3) -> list[dict]:
    """Fetch top github_projects updated this month by rubric_score."""
    year, month = int(ym[:4]), int(ym[5:7])
    last_day = calendar.monthrange(year, month)[1]
    start_ts = f"{ym}-01T00:00:00+00:00"
    end_ts   = f"{ym}-{last_day:02d}T23:59:59+00:00"
    res = sb.table("github_projects") \
        .select("name,owner,url,description,language,stars,rubric_score") \
        .gte("inserted_at", start_ts) \
        .lte("inserted_at", end_ts) \
        .order("rubric_score", desc=True) \
        .limit(limit) \
        .execute()
    return res.data or []

# ─── Image extraction ─────────────────────────────────────────────────────────

def infer_fallback_category(item: dict) -> str:
    tags = item.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            t = str(tag).lower()
            for cat in FALLBACK_CATEGORIES:
                if cat in t:
                    return cat
    title = (item.get("title") or "").lower()
    for cat in FALLBACK_CATEGORIES:
        if cat in title:
            return cat
    return "general"

def get_fallback_image(item: dict) -> str:
    cat = infer_fallback_category(item)
    return FALLBACK_CATEGORIES.get(cat, FALLBACK_CATEGORIES["general"])

def is_icon_url(url: str) -> bool:
    """Filter out likely icon/avatar/logo URLs."""
    low = url.lower()
    return any(x in low for x in [
        "favicon", "icon", "logo", "avatar", "badge",
        "pixel", "1x1", "placeholder", "blank", "spacer"
    ])

def extract_image(url: str, item: dict) -> tuple[str, str]:
    """
    4-layer image extraction. Returns (image_url, credit_text).
    Layer 5 (fallback) applied by caller if this returns empty string.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return (tag.get("content") or "").strip() if tag else ""

        # Layer 1: og:image / twitter:image
        img = og("og:image") or og("twitter:image")
        if img and not is_icon_url(img):
            pub_name = og("og:site_name") or ""
            return img, pub_name

        # Layer 2: twitter:image:src
        img = og("twitter:image:src")
        if img and not is_icon_url(img):
            return img, og("og:site_name") or ""

        # Layer 3: first <img> in article body
        body_tags = soup.find("article") or soup.find("main") or soup.find("body")
        if body_tags:
            for img_tag in body_tags.find_all("img", src=True):
                src = img_tag.get("src", "").strip()
                if not src:
                    continue
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    parsed = urlparse(url)
                    src = f"{parsed.scheme}://{parsed.netloc}{src}"
                if src.startswith("http") and not is_icon_url(src):
                    alt = img_tag.get("alt", "") or ""
                    return src, ""

    except Exception as e:
        print(f"  [image] Warning: could not scrape {url}: {e}", file=sys.stderr)

    # Layer 4: parse Jina Reader response for image references (done in caller)
    return "", ""

def extract_image_from_jina(jina_text: str) -> str:
    """Layer 4: find first markdown image in Jina Reader output."""
    if not jina_text:
        return ""
    # Markdown image: ![alt](url)
    match = re.search(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', jina_text)
    if match:
        url = match.group(1)
        if not is_icon_url(url):
            return url
    return ""

# ─── Jina Reader ──────────────────────────────────────────────────────────────

def fetch_article_text(url: str) -> str:
    jina_url = f"https://r.jina.ai/{url}"
    try:
        r = requests.get(jina_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text.strip()[:MAX_ARTICLE_CHARS]
    except Exception as e:
        print(f"  [jina] Warning: {url}: {e}", file=sys.stderr)
        return ""

# ─── Gemini ───────────────────────────────────────────────────────────────────

def setup_gemini():
    return genai.Client(api_key=GEMINI_API_KEY)

def call_gemini(client, prompt: str, retries: int = 5) -> str:
    for attempt in range(retries):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text.strip()
        except Exception as e:
            err = str(e).lower()
            is_rate = any(x in err for x in ["429", "quota", "rate", "resource_exhausted", "resourceexhausted", "too many requests"])
            if is_rate:
                wait = 30 * (2 ** attempt)
                print(f"  [gemini] Rate limited (attempt {attempt+1}), waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  [gemini] Error: {e}", file=sys.stderr)
                return ""
    return ""

def generate_story_content(client, article_text: str, fallback: str = "") -> tuple[str, str]:
    """
    Single Gemini call → (summary_html, why_it_matters_html).
    Summary: 300–500 word journalist summary.
    Why it matters: 4–8 bullet points.
    """
    context = article_text or fallback
    if not context:
        return '<p class="story-summary">Summary unavailable.</p>', "<p>Analysis unavailable.</p>"

    prompt = textwrap.dedent(f"""
        You are a journalist and analyst with a deep technology background, covering the OpenClaw
        agentic AI ecosystem. Your writing is clear, direct, and editorially grounded — not a
        press release repeater, not a hype machine. You report what happened, what it means, and
        where the limits are. No buzzwords ("game-changing", "revolutionary", "paradigm shift").
        Every claim must be traceable to the article — do not invent or extrapolate facts.

        Read the article below and produce TWO sections, separated by exactly: ---ANALYSIS---

        SECTION 1 — Story Summary (300–500 words):
        - Open with the single most important fact or development — what happened, what was
          released, what was found. No throat-clearing.
        - Extract key highlights: numbers, benchmarks, named techniques, specific capabilities.
          Concrete beats vague.
        - When the article introduces a method or finding, briefly explain what it does and
          why it differs from prior work — in plain terms.
        - If the article surfaces caveats, limitations, or open questions, include them.
          Intellectual honesty is part of the voice.
        - Write in flowing paragraphs — no bullets, no section headers.
        - Do not open with "The article" or restate the headline verbatim.
        - Do not use superlatives without a factual anchor in the text.
        - Return as: <p class="story-summary">…</p>
          (multiple <p> tags are fine for separate paragraphs)

        ---ANALYSIS---

        SECTION 2 — Why It Matters (3–5 bullet points, HTML only):
        Audience: OpenClaw developers, AI practitioners, and builders following the agentic AI
        ecosystem. Each bullet answers: "Why does this story deserve to be in the top 20 this month?"

        Write 3–5 tight, specific, factual bullets. Each bullet must do exactly one of:
        - Identify a direct capability unlock for OpenClaw developers
        - Flag a shift in the ecosystem (new tool, framework, competitor, or standard)
        - Surface a benchmark or result that changes how practitioners should think
        - Note a risk, tradeoff, or limitation builders should factor into decisions
        - Mark a community or organizational signal (funding, open-source release, team move)
          that affects the ecosystem's trajectory

        Rules:
        - One idea per bullet — short and plainly worded
        - No filler ("This exciting development shows…")
        - No bullet that could apply to any AI story — be specific to this article
        - The reader should walk away informed and inspired, not just aware something happened

        Return ONLY:
        <ul class="wim-list">
          <li>…</li>
        </ul>

        Article:
        {context[:8000]}
    """).strip()

    result = call_gemini(client, prompt)

    if "---ANALYSIS---" in result:
        summary_raw, analysis_raw = result.split("---ANALYSIS---", 1)
    else:
        summary_raw = result
        analysis_raw = ""

    # Strip markdown fences
    analysis_raw = re.sub(r'^```(?:html)?\s*', '', analysis_raw.strip(), flags=re.IGNORECASE)
    analysis_raw = re.sub(r'\s*```$', '', analysis_raw).strip()

    # Ensure summary is wrapped correctly
    match = re.search(r'(<p class="story-summary">.*?</p>)', summary_raw, re.DOTALL)
    if match:
        summary_html = match.group(1)
    else:
        text = re.sub(r"<[^>]+>", "", summary_raw).strip()
        summary_html = f'<p class="story-summary">{text or fallback[:700]}</p>'

    why_html = analysis_raw or "<p>Analysis unavailable.</p>"
    return summary_html, why_html

def generate_editorial(client, stories: list[dict]) -> str:
    """Generate a 150–200 word editorial paragraph for the cover page."""
    lines = []
    for s in stories[:10]:
        lines.append(f"- {s.get('headline', '')}")

    prompt = textwrap.dedent(f"""
        You are the editor of ClawBeat, a monthly magazine covering the OpenClaw agentic AI
        ecosystem. You write with the authority of someone who has followed this space closely —
        a journalist and analyst, not a marketer. Your editorial voice is direct, specific, and
        editorially honest: you report what the month meant, including tensions and open questions,
        not just what shipped.

        This month's top stories:
        {chr(10).join(lines)}

        Write a 150–200 word editorial paragraph that:
        - Identifies 2–3 defining threads or themes across the month's stories — not a list of
          what happened, but a read on what the month *meant* for the ecosystem
        - Names specific developments, tools, companies, or findings that exemplify each thread
        - Surfaces any tensions, momentum shifts, or open questions the month's news raises
        - Closes with a forward-looking sentence: what these trends suggest about where things
          are heading
        - Has genuine editorial voice and judgment — authoritative but not arrogant
        - Does NOT use buzzwords ("game-changing", "revolutionary", "paradigm shift")
        - Does NOT use phrases like "this month's edition", "in this issue", or "as we close out"
        - Does NOT use bullet points or headers — flowing prose only
        - Is balanced: not uncritically pro-AI, not excessively negative

        Return only the paragraph text (no HTML tags, no preamble).
    """).strip()

    return call_gemini(client, prompt)

# ─── HTML builders ────────────────────────────────────────────────────────────

def story_page_url(ym: str, slug: str) -> str:
    return f"/trace/{ym}/{slug}/"

def card_image_html(image_url: str, rank: str, credit: str = "") -> str:
    img_tag = f'<img src="{esc(image_url)}" alt="" loading="lazy">' if image_url else ""
    credit_html = f'<span class="card-credit-pill">{esc(credit)}</span>' if credit else ""
    return f"""<div class="card-img-wrap">
        {img_tag}
        <span class="card-rank-ghost">{rank}</span>
        {credit_html}
      </div>"""

def build_cover_hero(story: dict, ym: str) -> str:
    url       = story_page_url(ym, story["slug"])
    image_url = story.get("image_url", "")
    category  = esc(story.get("category", ""))
    pub_name  = esc(story.get("pub_name", ""))
    pub_url   = esc(story.get("pub_url", ""))
    headline  = esc(story.get("headline", ""))
    deck      = esc(story.get("deck", ""))

    img_tag = (
        f'<img src="{esc(image_url)}" alt="{headline}" '
        f'onerror="document.getElementById(\'lead01-wrap\').style.display=\'none\'">'
        if image_url else ""
    )

    return f"""<!-- ── COVER STORY: Signal 01 ── -->
<div class="wrap">
  <div class="lead-card" onclick="window.location='{esc(url)}'">
    <div class="lead-img-wrap" id="lead01-wrap">
      {img_tag}
      <div class="lead-badges">
        <span class="cover-story-badge">Cover Story · Signal 01</span>
        <span class="cover-category-badge">{category}</span>
      </div>
    </div>
    <div class="lead-body">
      <div class="cover-source-line"><a href="{pub_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{pub_name}</a></div>
      <h2 class="cover-headline">{headline}</h2>
      <p class="cover-deck">{deck}</p>
      <div class="cover-footer">
        <a class="cover-read-link" href="{esc(url)}" onclick="event.stopPropagation()">Read Full Analysis <i data-lucide="arrow-right"></i></a>
      </div>
    </div>
  </div>
</div>"""

def build_card(story: dict, ym: str, rank: int, show_summary: bool = True) -> str:
    url      = story_page_url(ym, story["slug"])
    img_url  = story.get("image_url", "")
    cat      = esc(story.get("category", ""))
    headline = esc(story.get("headline", ""))
    pub_name = esc(story.get("pub_name", ""))
    pub_url  = esc(story.get("pub_url", ""))
    img_tag  = f'<img src="{esc(img_url)}" alt="{headline}" loading="lazy">' if img_url else ""
    summary  = esc(story.get("deck", ""))
    summary_html = f'<p class="card-summary">{summary}</p>' if show_summary and summary else ""
    rank_str = str(rank).zfill(2)

    return f"""<a href="{esc(url)}" class="card">
      <div class="card-img-wrap">
        {img_tag}
        <span class="card-rank-ghost">{rank_str}</span>
      </div>
      <div class="card-body">
        <div class="card-meta"><span class="card-rank">Signal {rank_str}</span><span class="card-cat">{cat}</span></div>
        <div class="card-headline">{headline}</div>
        <div class="card-source">{pub_name}</div>
        {summary_html}
        <span class="card-link">Analysis <i data-lucide="arrow-right"></i></span>
      </div>
    </a>"""

def build_compact_card(story: dict, ym: str, rank: int) -> str:
    url      = story_page_url(ym, story["slug"])
    cat      = esc(story.get("category", ""))
    headline = esc(story.get("headline", ""))
    pub_name = esc(story.get("pub_name", ""))
    deck     = esc(story.get("deck", ""))
    rank_str = str(rank).zfill(2)
    summary_html = f'<div class="compact-summary">{deck}</div>' if deck else ""

    return f"""<a href="{esc(url)}" class="compact-card">
      <div class="compact-rank">Signal {rank_str} · {cat}</div>
      <div class="compact-headline">{headline}</div>
      {summary_html}
      <div class="compact-footer">
        <span class="compact-source">{pub_name}</span>
        <span class="compact-cta">Read Analysis <i data-lucide="arrow-right"></i></span>
      </div>
    </a>"""

def build_wide_feature(story: dict, ym: str, rank: int) -> str:
    url      = story_page_url(ym, story["slug"])
    img_url  = story.get("image_url", "")
    cat      = esc(story.get("category", ""))
    headline = esc(story.get("headline", ""))
    pub_name = esc(story.get("pub_name", ""))
    pub_url  = esc(story.get("pub_url", ""))
    deck     = esc(story.get("deck", ""))
    rank_str = str(rank).zfill(2)
    img_tag  = f'<img src="{esc(img_url)}" alt="{headline}" loading="lazy">' if img_url else ""

    return f"""<a href="{esc(url)}" class="wide-feature">
    <div class="card-img-wrap" style="min-height:300px">
      {img_tag}
      <span class="card-rank-ghost" style="font-size:5rem;bottom:1rem;left:1.25rem">{rank_str}</span>
    </div>
    <div class="wide-feature-content">
      <div class="wide-feature-rank">Signal {rank_str} · {cat}</div>
      <h3 class="wide-feature-headline">{headline}</h3>
      <p class="wide-feature-summary">{deck}</p>
      <div class="wide-feature-footer">
        <span class="wide-feature-source">{pub_name} · <a href="{esc(pub_url)}" target="_blank" rel="noopener">{pub_url.replace("https://","").replace("http://","")}</a></span>
        <span class="wide-feature-link">Read Analysis <i data-lucide="arrow-right"></i></span>
      </div>
    </div>
  </a>"""

def section_rule(label: str, badge: str = "") -> str:
    badge_html = f'<span class="badge">{esc(badge)}</span>' if badge else ""
    return f"""<div class="section-rule">
    <div class="section-rule-label"><span class="sl">//</span> {esc(label)} {badge_html}</div>
    <div class="section-rule-line"></div>
  </div>"""

def build_story_sections(stories: list[dict], ym: str) -> str:
    """Build all story grid sections for the cover page — February layout:
    Signal 01 lead card, then grid-two pairs for 2–3, 4–5, 6–7, 8–9,
    then grid-compact for signals 10–20.  No section-rule headers.
    """
    parts = []

    # Story 1: lead card
    if len(stories) >= 1:
        parts.append(build_cover_hero(stories[0], ym))

    # Signals 2–3
    if len(stories) >= 2:
        parts.append('<div class="wrap">')
        parts.append('<div class="grid-two">')
        for i in range(1, min(3, len(stories))):
            parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')
        parts.append('</div>')

    # Signals 4–5
    if len(stories) >= 4:
        parts.append('<div class="wrap" style="margin-top:2.5rem">')
        parts.append('<div class="grid-two">')
        for i in range(3, min(5, len(stories))):
            parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')
        parts.append('</div>')

    # Signals 6–7
    if len(stories) >= 6:
        parts.append('<div class="wrap" style="margin-top:2.5rem">')
        parts.append('<div class="grid-two">')
        for i in range(5, min(7, len(stories))):
            parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')
        parts.append('</div>')

    # Signals 8–9
    if len(stories) >= 8:
        parts.append('<div class="wrap" style="margin-top:2.5rem">')
        parts.append('<div class="grid-two">')
        for i in range(7, min(9, len(stories))):
            parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')
        parts.append('</div>')

    # Signals 10–20: compact digest, no images
    if len(stories) >= 10:
        parts.append('<div class="wrap" style="margin-top:2.5rem">')
        parts.append('<div class="grid-compact">')
        for i in range(9, min(20, len(stories))):
            parts.append(build_compact_card(stories[i], ym, i + 1))
        parts.append('</div>')
        parts.append('</div>')

    return "\n".join(parts)

def build_daily_section(daily_editions: list[dict], ym: str) -> str:
    """Build daily transmissions HTML block."""
    if not daily_editions:
        return ""
    month_short = ym[5:7]  # "02"
    import calendar as _cal
    month_name_short = _cal.month_abbr[int(month_short)]

    cards = []
    for ed in daily_editions:
        date_iso = ed.get("edition_date", "")  # YYYY-MM-DD
        permalink = ed.get("permalink", "")
        stories = ed.get("stories") or []
        lead_headline = stories[0].get("headline", "Daily Edition") if stories else "Daily Edition"
        if not date_iso:
            continue
        day = date_iso[8:10]  # "01"
        url = f"/daily/{date_iso}/{esc(permalink)}.html" if permalink else f"/daily/{date_iso}/"
        cards.append(f"""<a href="{url}" class="daily-card">
      <div class="daily-date-col"><div class="daily-mon">{esc(month_name_short)}</div><div class="daily-day">{day}</div></div>
      <div class="daily-text"><div class="daily-label">Daily Edition</div><div class="daily-headline">{esc(lead_headline)}</div></div>
    </a>""")

    if not cards:
        return ""

    return f"""
  <div class="wrap">
    <div class="section-rule" style="margin-top:3.5rem">
      <div class="section-rule-label"><span class="sl">//</span> Daily Edition Transmissions</div>
      <div class="section-rule-line"></div>
    </div>
    <div class="daily-grid">
      {"".join(cards)}
    </div>
  </div>"""

def render_archive_index(all_issues: list[dict]) -> str:
    """
    Build a fully static /trace/index.html from all trace_issues rows.
    all_issues must be sorted newest-first (by issue_ym desc).
    Returns complete HTML string — no Supabase JS dependency.
    """
    issue_count  = len(all_issues)
    signal_count = sum(i.get("story_count", 20) for i in all_issues)

    # ── Latest issue hero card ──────────────────────────────────────────────
    if all_issues:
        latest  = all_issues[0]
        l_url   = f"/trace/{esc(latest['issue_ym'])}/"
        l_img   = (latest.get("cover_image") or "").strip()
        l_img_html = (
            f'<img class="latest-card-img" src="{esc(l_img)}" '
            f'alt="{esc(latest.get("cover_headline",""))}" onerror="this.style.display=\'none\'">'
            if l_img else ""
        )
        latest_card_html = f"""<a href="{l_url}" class="latest-card">
      <div class="latest-card-content">
        <div class="latest-kicker">Latest Issue</div>
        <div class="latest-issue-num">Trace No. {esc(latest['issue_number'])} · {esc(latest['month_label'])}</div>
        <div class="latest-headline">{esc(latest.get('cover_headline',''))}</div>
        <div class="latest-editorial">{esc(latest.get('editorial',''))}</div>
        <span class="latest-cta">Read Issue <i data-lucide="arrow-right"></i></span>
      </div>
      <div class="latest-card-img-wrap">
        {l_img_html}
        <div class="latest-img-placeholder"></div>
        <div class="latest-img-overlay"></div>
        <div class="latest-img-issue-num">{esc(latest['issue_number'])}</div>
      </div>
    </a>"""
    else:
        latest_card_html = """<div class="empty-state">
      <div class="empty-state-icon"><i data-lucide="layers"></i></div>
      <div class="empty-state-title">First issue coming soon</div>
      <div class="empty-state-body">Trace is compiled monthly. Check back at the end of the month.</div>
    </div>"""

    # ── Archive cards (all but latest) ─────────────────────────────────────
    archive_issues = all_issues[1:]
    if archive_issues:
        cards = []
        for issue in archive_issues:
            a_url = f"/trace/{esc(issue['issue_ym'])}/"
            a_img = (issue.get("cover_image") or "").strip()
            a_img_html = (
                f'<img class="issue-card-img" src="{esc(a_img)}" '
                f'alt="{esc(issue.get("cover_headline",""))}" loading="lazy" onerror="this.style.display=\'none\'">'
                if a_img else ""
            )
            cards.append(f"""<a href="{a_url}" class="issue-card">
      <div class="issue-card-img-wrap">
        {a_img_html}
        <div class="issue-card-img-placeholder"></div>
        <div class="issue-card-img-overlay"></div>
        <span class="issue-card-tag">No. {esc(issue['issue_number'])}</span>
        <span class="issue-card-num-ghost">{esc(issue['issue_number'])}</span>
      </div>
      <div class="issue-card-body">
        <div class="issue-card-meta-row">
          <span class="issue-card-number">Trace No. {esc(issue['issue_number'])}</span>
          <span class="issue-card-month">{esc(issue['month_label'])}</span>
        </div>
        <div class="issue-card-headline">{esc(issue.get('cover_headline',''))}</div>
        <div class="issue-card-editorial">{esc(issue.get('editorial',''))}</div>
        <div class="issue-card-footer">
          <span class="issue-card-story-count">{issue.get('story_count', 20)} Signals</span>
          <span class="issue-card-cta">Read Issue <i data-lucide="arrow-right"></i></span>
        </div>
      </div>
    </a>""")
        archive_grid_html = "\n".join(cards)
    else:
        archive_grid_html = (
            '<div style="grid-column:1/-1">'
            '<p style="font-family:var(--mono);font-size:0.6rem;color:var(--text-4);'
            'padding:1rem 0;letter-spacing:0.1em;text-transform:uppercase;">'
            'No past issues yet — archive builds month by month.</p></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-WHLSLX6VL3"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{ window.dataLayer.push(arguments); }}
  gtag('js', new Date());
  gtag('config', 'G-WHLSLX6VL3');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#08090b">
<link rel="icon" type="image/jpeg" href="/images/clawbeat-icon-claw-logo-512x512.jpg">
<link rel="apple-touch-icon" href="/images/clawbeat-icon-claw-logo-512x512.jpg">
<title>Trace — Signal Intelligence Monthly · ClawBeat</title>
<meta name="description" content="Trace is ClawBeat's monthly magazine curating the top 20 signals from the OpenClaw ecosystem. Deep analysis, curated intelligence, delivered monthly.">
<meta property="og:title" content="Trace — Signal Intelligence Monthly | ClawBeat">
<meta property="og:description" content="The monthly magazine for the OpenClaw ecosystem. Deep-read analysis of the 20 top stories every month.">
<meta property="og:url" content="https://clawbeat.co/trace/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="ClawBeat">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://clawbeat.co/trace/">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,500;0,700;1,400&family=Space+Grotesk:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>

<style>
/* ── RESET ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ -webkit-text-size-adjust: 100%; font-size: 16px; }}

/* ── TOKENS ── */
:root {{
  --bg:          #08090b;
  --bg-2:        #0d0f12;
  --bg-3:        #131619;
  --border:      rgba(255,255,255,0.05);
  --border-2:    rgba(255,255,255,0.09);
  --orange:      #f97316;
  --orange-dim:  rgba(249,115,22,0.10);
  --orange-glow: rgba(249,115,22,0.18);
  --text:        #e2e4e9;
  --text-2:      #9097a3;
  --text-3:      #525866;
  --text-4:      #2e333d;
  --mono:        'JetBrains Mono', monospace;
  --sans:        'Space Grotesk', sans-serif;
}}

body {{ font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; }}
::-webkit-scrollbar {{ width: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: #27272a; border-radius: 3px; }}
a {{ color: inherit; text-decoration: none; }}
img {{ display: block; }}
:focus-visible {{ outline: 2px solid var(--orange); outline-offset: 3px; border-radius: 3px; }}

/* ── HEADER ── */
.header {{ position: sticky; top: 0; z-index: 50; border-bottom: 1px solid var(--border); background: rgba(8,9,11,0.85); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); }}
.header-inner {{ max-width: 72rem; margin: 0 auto; padding: 0 1.5rem; height: 4rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem; }}
.brand {{ display: flex; align-items: center; gap: 0.75rem; text-decoration: none; }}
.brand-img {{ width: 40px; height: 40px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); overflow: hidden; }}
.brand-img img {{ width: 100%; height: 100%; object-fit: cover; }}
.brand-text {{ font-family: var(--sans); font-size: 1.25rem; font-weight: 900; text-transform: uppercase; letter-spacing: -0.05em; font-style: italic; color: #fff; }}
.brand-text span {{ color: var(--orange); }}
.header-nav {{ display: flex; align-items: center; gap: 0.25rem; }}
.nav-item {{ font-family: var(--sans); font-size: 0.625rem; font-weight: 900; letter-spacing: 0.1em; text-transform: uppercase; color: #64748b; padding: 0.375rem 1rem; border-radius: 0.375rem; text-decoration: none; transition: all 0.15s; display: flex; align-items: center; gap: 0.75rem; }}
.nav-item svg {{ width: 16px; height: 16px; }}
.nav-item:hover {{ color: #cbd5e1; background: rgba(255,255,255,0.05); }}
.nav-item.active {{ background: rgba(255,255,255,0.1); color: var(--orange); box-shadow: inset 0 0 10px rgba(249,115,22,0.1); }}
.nav-dropdown {{ position: relative; }}
.nav-dropdown-menu {{ position: absolute; top: calc(100% + 6px); left: 0; background: rgba(10,10,12,0.97); border: 1px solid var(--border-2); border-radius: 0.5rem; min-width: 200px; padding: 0.4rem; opacity: 0; visibility: hidden; transform: translateY(-6px); transition: opacity 0.15s, transform 0.15s, visibility 0.15s; z-index: 100; backdrop-filter: blur(16px); }}
.nav-dropdown:hover .nav-dropdown-menu {{ opacity: 1; visibility: visible; transform: translateY(0); }}
.nav-dropdown-item {{ display: flex; align-items: center; gap: 0.75rem; padding: 0.6rem 0.75rem; border-radius: 0.35rem; transition: background 0.12s; }}
.nav-dropdown-item.active-item {{ background: var(--orange-dim); }}
.nav-dropdown-item:hover {{ background: var(--orange-dim); }}
.nav-dropdown-item svg {{ width: 14px; height: 14px; color: var(--orange); flex-shrink: 0; }}
.nav-dropdown-label {{ font-family: var(--sans); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text); line-height: 1; }}
.nav-dropdown-sub {{ font-family: var(--mono); font-size: 0.55rem; color: var(--text-3); letter-spacing: 0.08em; text-transform: uppercase; margin-top: 0.2rem; }}
.nav-skill-chip {{ font-family: var(--mono); font-size: 0.55rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--orange); text-decoration: none; padding: 0.25rem 0.6rem; border: 1px solid rgba(249,115,22,0.3); border-radius: 0.25rem; background: rgba(249,115,22,0.06); transition: all 0.15s; margin-left: 0.5rem; white-space: nowrap; }}
.nav-skill-chip:hover {{ background: rgba(249,115,22,0.12); border-color: rgba(249,115,22,0.5); }}
.hamburger-btn {{ display: none; padding: 0.5rem; color: #94a3b8; background: transparent; border: none; cursor: pointer; align-items: center; justify-content: center; }}
.hamburger-btn svg {{ width: 24px; height: 24px; }}
.mobile-menu {{ display: none; position: absolute; top: 4rem; left: 0; width: 100%; background: rgba(10,10,12,0.97); backdrop-filter: blur(16px); border-bottom: 1px solid rgba(255,255,255,0.1); padding: 1rem; flex-direction: column; gap: 0.5rem; z-index: 60; }}
.mobile-menu.open {{ display: flex; }}
.mobile-nav-item {{ font-family: var(--sans); font-size: 0.75rem; font-weight: 900; letter-spacing: 0.1em; text-transform: uppercase; color: #64748b; padding: 0.75rem 1rem; border-radius: 0.375rem; text-decoration: none; transition: all 0.15s; display: flex; align-items: center; gap: 0.75rem; width: 100%; }}
.mobile-nav-item:hover {{ color: #cbd5e1; background: rgba(255,255,255,0.05); }}
.mobile-nav-item.active {{ background: rgba(255,255,255,0.1); color: var(--orange); box-shadow: inset 0 0 8px rgba(249,115,22,0.08); }}
.mobile-nav-item svg {{ width: 16px; height: 16px; flex-shrink: 0; }}
.mobile-subnav-item {{ font-family: var(--mono); font-size: 0.6rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--orange); padding: 0.5rem 1rem 0.5rem 2.75rem; border-radius: 0.375rem; text-decoration: none; transition: all 0.15s; display: flex; align-items: center; gap: 0.5rem; width: 100%; background: var(--orange-dim); }}
.mobile-subnav-item svg {{ width: 14px; height: 14px; }}
@media (max-width: 768px) {{ .header-nav {{ display: none; }} .hamburger-btn {{ display: flex; }} }}

/* ── PAGE HERO ── */
.catalog-hero {{
  position: relative; overflow: hidden;
  background: var(--bg);
  border-bottom: 1px solid var(--border-2);
  padding: 5rem 1.5rem 4.5rem;
}}
.catalog-hero::before {{
  content: '';
  position: absolute; inset: 0; pointer-events: none;
  background-image:
    repeating-linear-gradient(0deg, transparent, transparent 39px, rgba(255,255,255,0.014) 39px, rgba(255,255,255,0.014) 40px),
    repeating-linear-gradient(90deg, transparent, transparent 79px, rgba(255,255,255,0.007) 79px, rgba(255,255,255,0.007) 80px);
}}
.catalog-hero::after {{
  content: '';
  position: absolute; top: -60px; left: 50%; transform: translateX(-50%);
  width: 600px; height: 200px;
  background: radial-gradient(ellipse, rgba(249,115,22,0.08) 0%, transparent 70%);
  pointer-events: none;
}}
.catalog-hero-inner {{ max-width: 72rem; margin: 0 auto; position: relative; }}
.catalog-hero-kicker {{
  font-family: var(--mono); font-size: 0.65rem; color: var(--orange);
  letter-spacing: 0.3em; text-transform: uppercase;
  display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.25rem;
}}
.catalog-hero-kicker::before,
.catalog-hero-kicker::after {{ content: '──────'; color: var(--text-4); letter-spacing: -0.1em; }}
.catalog-hero-title {{
  font-family: var(--sans);
  font-size: clamp(4rem, 12vw, 9rem);
  font-weight: 900; letter-spacing: -0.05em; line-height: 0.85;
  color: #fff; text-transform: uppercase; font-style: italic;
  margin-bottom: 1.5rem;
}}
.catalog-hero-title em {{ color: var(--orange); font-style: inherit; }}
.catalog-hero-sub {{
  font-size: 1.05rem; color: var(--text-2); line-height: 1.6;
  max-width: 48ch; margin-bottom: 2rem;
}}
.catalog-stats {{
  display: flex; align-items: center; gap: 2rem; flex-wrap: wrap;
}}
.stat-item {{
  display: flex; align-items: baseline; gap: 0.5rem;
}}
.stat-num {{ font-family: var(--sans); font-size: 2rem; font-weight: 800; color: var(--orange); line-height: 1; }}
.stat-label {{ font-family: var(--mono); font-size: 0.55rem; color: var(--text-3); letter-spacing: 0.15em; text-transform: uppercase; }}
.stat-divider {{ width: 1px; height: 28px; background: var(--border-2); }}

/* ── LATEST ISSUE FEATURE ── */
.latest-section {{ max-width: 72rem; margin: 3.5rem auto 0; padding: 0 1.5rem; }}
.section-rule {{
  display: flex; align-items: center; gap: 1rem; margin-bottom: 1.25rem;
}}
.section-rule-label {{
  font-family: var(--mono); font-size: 0.6rem; color: var(--text-3);
  letter-spacing: 0.2em; text-transform: uppercase; white-space: nowrap;
  display: flex; align-items: center; gap: 0.5rem;
}}
.section-rule-label .sl {{ color: var(--orange); }}
.section-rule-label .badge {{
  background: var(--orange); color: #000;
  border-radius: 2px; padding: 0.15rem 0.5rem;
  font-size: 0.5rem; letter-spacing: 0.12em;
}}
.section-rule-line {{ flex: 1; height: 1px; background: linear-gradient(to right, var(--border-2), transparent); }}

/* Latest issue hero card */
.latest-card {{
  display: grid; grid-template-columns: 1fr 44%; gap: 0;
  border: 1px solid var(--border-2); border-radius: 0.75rem; overflow: hidden;
  background: var(--bg-2); transition: border-color 0.2s;
  min-height: 420px;
}}
.latest-card:hover {{ border-color: rgba(249,115,22,0.2); }}
.latest-card-content {{ padding: 2.5rem 2.75rem; display: flex; flex-direction: column; justify-content: center; gap: 1.25rem; }}
.latest-kicker {{
  font-family: var(--mono); font-size: 0.55rem; color: var(--orange);
  letter-spacing: 0.25em; text-transform: uppercase;
  display: flex; align-items: center; gap: 0.5rem;
}}
.latest-kicker::before {{ content: '//'; opacity: 0.6; }}
.latest-issue-num {{
  font-family: var(--mono); font-size: 0.65rem; color: var(--text-3);
  letter-spacing: 0.15em; text-transform: uppercase;
}}
.latest-headline {{
  font-family: var(--sans); font-size: clamp(1.4rem, 2.5vw, 2rem);
  font-weight: 800; line-height: 1.15; letter-spacing: -0.035em; color: #fff;
}}
.latest-editorial {{
  font-size: 0.9rem; line-height: 1.7; color: var(--text-2);
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}}
.latest-cta {{
  display: inline-flex; align-items: center; gap: 0.6rem;
  font-family: var(--mono); font-size: 0.65rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.12em; color: #000;
  background: var(--orange); border-radius: 0.35rem;
  padding: 0.65rem 1.25rem; transition: background 0.15s; align-self: flex-start;
}}
.latest-cta:hover {{ background: #fb923c; }}
.latest-cta svg {{ width: 14px; height: 14px; }}
.latest-card-img-wrap {{ position: relative; overflow: hidden; background: var(--bg-3); }}
.latest-card-img {{ width: 100%; height: 100%; object-fit: cover; opacity: 0.78; transition: opacity 0.3s, transform 0.3s; }}
.latest-card:hover .latest-card-img {{ opacity: 0.92; transform: scale(1.02); }}
.latest-img-overlay {{
  position: absolute; inset: 0;
  background: linear-gradient(to right, rgba(13,15,18,0.5) 0%, transparent 50%);
}}
.latest-img-issue-num {{
  position: absolute; bottom: 1.5rem; right: 1.75rem;
  font-family: var(--mono); font-size: clamp(3rem, 8vw, 7rem);
  font-weight: 700; font-style: italic;
  color: rgba(255,255,255,0.07); line-height: 1;
  user-select: none; pointer-events: none;
}}
.latest-img-placeholder {{
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
}}
.latest-img-placeholder::after {{ content: '[ COVER ]'; font-family: var(--mono); font-size: 0.55rem; color: var(--text-4); letter-spacing: 0.2em; }}

/* ── BACK ISSUES ── */
.archive-section {{ max-width: 72rem; margin: 3rem auto 0; padding: 0 1.5rem; }}
.archive-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.5rem; }}

/* Issue card */
.issue-card {{
  border: 1px solid var(--border); border-radius: 0.75rem; overflow: hidden;
  background: var(--bg-2); transition: border-color 0.2s, transform 0.2s;
  display: flex; flex-direction: column;
}}
.issue-card:hover {{ border-color: var(--border-2); transform: translateY(-3px); }}
.issue-card-img-wrap {{ position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: var(--bg-3); }}
.issue-card-img {{ width: 100%; height: 100%; object-fit: cover; opacity: 0.72; transition: opacity 0.3s, transform 0.3s; }}
.issue-card:hover .issue-card-img {{ opacity: 0.88; transform: scale(1.03); }}
.issue-card-img-overlay {{
  position: absolute; inset: 0;
  background: linear-gradient(to bottom, transparent 40%, rgba(8,9,11,0.8) 100%);
}}
.issue-card-num-ghost {{
  position: absolute; bottom: 0.75rem; right: 1rem;
  font-family: var(--mono); font-size: clamp(2.5rem, 6vw, 5rem);
  font-weight: 700; font-style: italic;
  color: rgba(255,255,255,0.07); line-height: 1; pointer-events: none; user-select: none;
}}
.issue-card-tag {{
  position: absolute; top: 0.875rem; left: 0.875rem;
  font-family: var(--mono); font-size: 0.5rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.15em;
  background: rgba(8,9,11,0.8); color: var(--text-3);
  backdrop-filter: blur(4px); border: 1px solid var(--border-2);
  padding: 0.25rem 0.6rem; border-radius: 2px;
}}
.issue-card-img-placeholder {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }}
.issue-card-img-placeholder::after {{ content: '[ ISSUE COVER ]'; font-family: var(--mono); font-size: 0.55rem; color: var(--text-4); letter-spacing: 0.2em; }}

.issue-card-body {{ padding: 1.375rem 1.5rem 1.5rem; flex: 1; display: flex; flex-direction: column; gap: 0.75rem; }}
.issue-card-meta-row {{ display: flex; align-items: center; justify-content: space-between; }}
.issue-card-number {{ font-family: var(--mono); font-size: 0.6rem; color: var(--orange); letter-spacing: 0.12em; text-transform: uppercase; }}
.issue-card-month {{ font-family: var(--mono); font-size: 0.55rem; color: var(--text-3); letter-spacing: 0.08em; }}
.issue-card-headline {{
  font-family: var(--sans); font-size: 0.95rem; font-weight: 700;
  line-height: 1.3; letter-spacing: -0.025em; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}}
.issue-card:hover .issue-card-headline {{ color: #fff; }}
.issue-card-editorial {{
  font-size: 0.8rem; line-height: 1.55; color: var(--text-3);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}}
.issue-card-footer {{
  display: flex; align-items: center; justify-content: space-between;
  border-top: 1px solid var(--border); padding-top: 0.875rem; margin-top: auto;
}}
.issue-card-story-count {{ font-family: var(--mono); font-size: 0.5rem; color: var(--text-4); letter-spacing: 0.1em; text-transform: uppercase; }}
.issue-card-cta {{
  display: inline-flex; align-items: center; gap: 0.4rem;
  font-family: var(--mono); font-size: 0.55rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--orange);
  transition: gap 0.15s;
}}
.issue-card-cta:hover {{ gap: 0.65rem; }}
.issue-card-cta svg {{ width: 12px; height: 12px; }}

/* Empty state */
.empty-state {{
  text-align: center; padding: 4rem 1.5rem;
  border: 1px dashed var(--border-2); border-radius: 0.75rem;
}}
.empty-state-icon {{ color: var(--text-4); margin: 0 auto 1rem; }}
.empty-state-icon svg {{ width: 40px; height: 40px; }}
.empty-state-title {{ font-family: var(--mono); font-size: 0.65rem; color: var(--text-3); letter-spacing: 0.2em; text-transform: uppercase; margin-bottom: 0.5rem; }}
.empty-state-body {{ font-size: 0.85rem; color: var(--text-4); }}

/* ── ABOUT TRACE ── */
.about-section {{
  max-width: 72rem; margin: 4rem auto 0; padding: 0 1.5rem;
}}
.about-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--border-2); border: 1px solid var(--border-2); border-radius: 0.75rem; overflow: hidden; }}
.about-cell {{ background: var(--bg-2); padding: 1.5rem; }}
.about-cell-icon {{ color: var(--orange); margin-bottom: 0.875rem; }}
.about-cell-icon svg {{ width: 22px; height: 22px; }}
.about-cell-title {{ font-family: var(--mono); font-size: 0.6rem; color: var(--orange); letter-spacing: 0.2em; text-transform: uppercase; margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.4rem; }}
.about-cell-title::before {{ content: '//'; opacity: 0.5; }}
.about-cell-body {{ font-size: 0.85rem; line-height: 1.6; color: var(--text-3); }}

/* ── FOOTER ── */
.trace-footer {{ max-width: 72rem; margin: 4rem auto 0; padding: 2.5rem 1.5rem; border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
.footer-brand {{ font-family: var(--sans); font-size: 0.75rem; font-weight: 900; text-transform: uppercase; font-style: italic; letter-spacing: -0.03em; color: var(--text-3); }}
.footer-brand span {{ color: var(--orange); }}
.footer-meta {{ font-family: var(--mono); font-size: 0.55rem; color: var(--text-4); letter-spacing: 0.08em; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }}
.footer-meta a {{ color: var(--text-4); }}
.footer-meta a:hover {{ color: var(--text-3); }}

/* ── RESPONSIVE ── */
@media (max-width: 1024px) {{ .latest-card {{ grid-template-columns: 1fr; }} .latest-card-img-wrap {{ aspect-ratio: 16 / 9; }} .about-grid {{ grid-template-columns: 1fr; gap: 1px; }} }}
@media (max-width: 768px) {{ .archive-grid {{ grid-template-columns: 1fr; }} .latest-card-content {{ padding: 1.75rem; }} .catalog-hero {{ padding: 3rem 1rem 3rem; }} }}
@media (max-width: 640px) {{ .latest-section, .archive-section, .about-section {{ padding: 0 1rem; }} .catalog-stats {{ gap: 1.25rem; }} }}
</style>
</head>
<body>

<!-- ── HEADER ── -->
<header class="header" id="header">
  <div class="header-inner">
    <a href="/?tab=news" class="brand">
      <div class="brand-img">
        <img src="/images/clawbeat-icon-claw-logo-512x512.jpg" alt="ClawBeat">
      </div>
      <span class="brand-text">ClawBeat<span>.co</span></span>
    </a>
    <nav class="header-nav">
      <div class="nav-dropdown">
        <a href="/?tab=news" class="nav-item active"><i data-lucide="newspaper"></i>Intel</a>
        <div class="nav-dropdown-menu">
          <a href="/trace/" class="nav-dropdown-item active-item">
            <i data-lucide="layers"></i>
            <div>
              <div class="nav-dropdown-label">Trace</div>
              <div class="nav-dropdown-sub">Monthly magazine</div>
            </div>
          </a>
        </div>
      </div>
      <a href="/research.html" class="nav-item"><i data-lucide="book-open"></i>Research</a>
      <a href="/media.html" class="nav-item"><i data-lucide="video"></i>Media</a>
      <a href="/forge.html" class="nav-item"><i data-lucide="github"></i>Forge</a>
      <a href="/events-calendar.html" class="nav-item"><i data-lucide="calendar"></i>Events</a>
      <a class="nav-skill-chip" href="https://clawhub.ai/thekenyeung/clawbeat" target="_blank" rel="noopener">// skill</a>
    </nav>
    <button class="hamburger-btn" id="hamburger-btn" aria-label="Open menu">
      <span id="icon-menu"><i data-lucide="menu"></i></span>
      <span id="icon-close" style="display:none"><i data-lucide="x"></i></span>
    </button>
  </div>
</header>
<div class="mobile-menu" id="mobile-menu">
  <a href="/?tab=news" class="mobile-nav-item active"><i data-lucide="newspaper"></i>Intel Feed</a>
  <a href="/trace/" class="mobile-subnav-item"><i data-lucide="layers"></i>Trace Magazine</a>
  <a href="/research.html" class="mobile-nav-item"><i data-lucide="book-open"></i>Research</a>
  <a href="/media.html" class="mobile-nav-item"><i data-lucide="video"></i>Media Lab</a>
  <a href="/forge.html" class="mobile-nav-item"><i data-lucide="github"></i>The Forge</a>
  <a href="/events-calendar.html" class="mobile-nav-item"><i data-lucide="calendar"></i>Events</a>
  <a class="nav-skill-chip" href="https://clawhub.ai/thekenyeung/clawbeat" target="_blank" rel="noopener">// skill</a>
</div>

<!-- ── CATALOG HERO ── -->
<section class="catalog-hero">
  <div class="catalog-hero-inner">
    <div class="catalog-hero-kicker">Signal Intelligence · Monthly Edition</div>
    <h1 class="catalog-hero-title">TR<em>A</em>CE</h1>
    <p class="catalog-hero-sub">
      The OpenClaw ecosystem, distilled. Every month, 20 top signals curated from the noise — each with deep AI analysis and a clear-eyed take on why it matters.
    </p>
    <div class="catalog-stats">
      <div class="stat-item">
        <span class="stat-num">{issue_count}</span>
        <span class="stat-label">Issues</span>
      </div>
      <div class="stat-divider"></div>
      <div class="stat-item">
        <span class="stat-num">{signal_count}</span>
        <span class="stat-label">Signals</span>
      </div>
      <div class="stat-divider"></div>
      <div class="stat-item">
        <span class="stat-num">Monthly</span>
        <span class="stat-label">Cadence</span>
      </div>
    </div>
  </div>
</section>

<!-- ── LATEST ISSUE ── -->
<section class="latest-section">
  <div class="section-rule">
    <div class="section-rule-label"><span class="sl">//</span> Latest Issue <span class="badge">New</span></div>
    <div class="section-rule-line"></div>
  </div>
  {latest_card_html}
</section>

<!-- ── ARCHIVE ── -->
<section class="archive-section" style="margin-top:3rem">
  <div class="section-rule">
    <div class="section-rule-label"><span class="sl">//</span> Archive</div>
    <div class="section-rule-line"></div>
  </div>
  <div class="archive-grid">
    {archive_grid_html}
  </div>
</section>

<!-- ── ABOUT TRACE ── -->
<section class="about-section">
  <div class="section-rule" style="margin-bottom:1.25rem">
    <div class="section-rule-label"><span class="sl">//</span> About Trace</div>
    <div class="section-rule-line"></div>
  </div>
  <div class="about-grid">
    <div class="about-cell">
      <div class="about-cell-icon"><i data-lucide="layers"></i></div>
      <div class="about-cell-title">20 Signals</div>
      <div class="about-cell-body">Each issue surfaces the 20 most significant stories from the OpenClaw ecosystem — ranked by signal strength and relevance, not recency.</div>
    </div>
    <div class="about-cell">
      <div class="about-cell-icon"><i data-lucide="brain"></i></div>
      <div class="about-cell-title">AI Analysis</div>
      <div class="about-cell-body">Every story receives a deep AI-generated summary and a rigorous "Why It Matters" breakdown — connecting the dots the original article doesn't.</div>
    </div>
    <div class="about-cell">
      <div class="about-cell-icon"><i data-lucide="calendar"></i></div>
      <div class="about-cell-title">Monthly Cadence</div>
      <div class="about-cell-body">Trace is compiled once per month — giving context and arc to the signal stream that daily editions can't. A record of the ecosystem, month by month.</div>
    </div>
  </div>
</section>

<!-- ── FOOTER ── -->
<footer class="trace-footer">
  <div class="footer-brand">ClawBeat<span>.co</span></div>
  <div class="footer-meta">
    <a href="/">Intel Feed</a>
    <span>·</span>
    <a href="/events-calendar.html">Events</a>
    <span>·</span>
    <a href="/forge.html">Forge</a>
    <span>·</span>
    <a href="/">ClawBeat.co</a>
  </div>
</footer>

<script>
lucide.createIcons();
function toggleMenu() {{
  const menu = document.getElementById('mobile-menu');
  const iconMenu = document.getElementById('icon-menu');
  const iconClose = document.getElementById('icon-close');
  const isOpen = menu.classList.toggle('open');
  iconMenu.style.display = isOpen ? 'none' : 'inline';
  iconClose.style.display = isOpen ? 'inline' : 'none';
}}
document.getElementById('hamburger-btn').addEventListener('click', toggleMenu);
</script>
</body>
</html>"""


def build_repo_section(repos: list[dict]) -> str:
    """Build repo signals HTML block."""
    if not repos:
        return ""
    cards = []
    for repo in repos:
        owner   = repo.get("owner", "")
        rname   = repo.get("name", "")
        name    = esc(f"{owner}/{rname}" if owner else rname)
        url     = esc(repo.get("url", "#"))
        desc    = esc(repo.get("description", ""))
        lang    = esc(repo.get("language", ""))
        stars   = repo.get("stars", 0)
        stars_fmt = f"{stars:,}" if isinstance(stars, int) else str(stars)
        desc_html = f'<div class="repo-desc">{desc}</div>' if desc else ""
        lang_html = f'<span class="repo-lang">{lang}</span>' if lang else ""
        cards.append(f"""<div class="repo-card">
      <div class="repo-header">
        <div class="repo-name"><a href="{url}" target="_blank" rel="noopener">{name}</a></div>
        <div class="repo-stars"><i data-lucide="star" style="width:11px;height:11px"></i> {stars_fmt}</div>
      </div>
      {desc_html}
      {lang_html}
    </div>""")

    return f"""
  <div class="wrap">
    <div class="section-rule" style="margin-top:2.5rem">
      <div class="section-rule-label"><span class="sl">//</span> Repo Signals</div>
      <div class="section-rule-line"></div>
    </div>
    <div class="repo-grid">
      {"".join(cards)}
    </div>
  </div>"""

# ─── Article page builder ─────────────────────────────────────────────────────

def build_article_page(
    template: str,
    story: dict,
    ym: str,
    issue_number: str,
    month_lbl: str,
    prev_story: dict | None,
    next_story: dict | None,
) -> str:
    rank = story["rank"]
    signal_num = str(rank).zfill(2)

    # Image credit
    credit_text = story.get("credit", "")
    credit_html = ""
    if credit_text:
        credit_html = f'<div class="article-hero-credit">Photo: {esc(credit_text)}</div>'

    # Author chip
    author = story.get("author", "")
    author_chip = ""
    if author:
        author_chip = f"""<div class="byline-chip">
      <i data-lucide="user"></i>
      <strong>{esc(author)}</strong>
    </div>"""

    # Meta description from summary
    summary_plain = re.sub(r"<[^>]+>", "", story.get("summary_html", "")).strip()[:155]

    # Prev/next nav buttons
    def nav_btn(s, direction):
        if s is None:
            label = "Previous Signal" if direction == "prev" else "Next Signal"
            return f'<div class="nav-btn{"" if direction=="prev" else " next"} disabled"><div class="nav-btn-dir"><i data-lucide="arrow-{"left" if direction=="prev" else "right"}"></i> {label}</div><div class="nav-btn-rank">—</div><div class="nav-btn-headline">No {"previous" if direction=="prev" else "next"} signal</div></div>'
        surl = story_page_url(ym, s["slug"])
        snum = str(s["rank"]).zfill(2)
        label = "Previous Signal" if direction == "prev" else "Next Signal"
        cls = "nav-btn" if direction == "prev" else "nav-btn next"
        icon = "arrow-left" if direction == "prev" else "arrow-right"
        return f'<a href="{esc(surl)}" class="{cls}"><div class="nav-btn-dir"><i data-lucide="{icon}"></i> {label}</div><div class="nav-btn-rank">Signal {snum}</div><div class="nav-btn-headline">{esc(s["headline"])}</div></a>'

    compiled_note = f"Analyzed {COMPILED_TIME} · Trace No. {issue_number} · {month_lbl}"

    variables = {
        "HEADLINE":            story.get("headline", ""),
        "META_DESCRIPTION":    summary_plain,
        "IMAGE_URL":           story.get("image_url", ""),
        "IMAGE_CREDIT_HTML":   credit_html,
        "ISSUE_NUMBER":        issue_number,
        "MONTH_LABEL":         month_lbl,
        "MONTH_YM":            ym,
        "SIGNAL_NUM":          signal_num,
        "STORY_SLUG":          story["slug"],
        "CATEGORY":            story.get("category", ""),
        "PUB_NAME":            story.get("pub_name", ""),
        "PUB_URL":             story.get("pub_url", ""),
        "AUTHOR":              author,
        "AUTHOR_CHIP_HTML":    author_chip,
        "PUB_DATE":            story.get("pub_date", ""),
        "ARTICLE_URL":         story.get("url", ""),
        "SUMMARY_HTML":        story.get("summary_html", "<p>Summary unavailable.</p>"),
        "WHY_IT_MATTERS_HTML": story.get("why_it_matters", "<p>Analysis unavailable.</p>"),
        "COMPILED_NOTE":       compiled_note,
        "PREV_BTN_HTML":       nav_btn(prev_story, "prev"),
        "NEXT_BTN_HTML":       nav_btn(next_story, "next"),
    }
    return render_template(template, variables)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ym = TRACE_MONTH_OVERRIDE or prev_month_ym()
    print(f"[trace] Generating Trace for {ym}")

    issue_number = compute_issue_number(ym)
    month_lbl    = month_label(ym)
    print(f"[trace] Issue No. {issue_number} — {month_lbl}")

    sb = get_supabase()

    # 1. Fetch raw news items
    raw_items = fetch_news_items(sb, ym, limit=60)
    if not raw_items:
        print(f"[trace] No news_items found for {ym}. Exiting.", file=sys.stderr)
        sys.exit(1)
    print(f"[trace] Found {len(raw_items)} candidates")

    # 2. Extract images — pick top 20 with fallbacks
    gemini = setup_gemini()
    stories = []
    seen_slugs: set[str] = set()

    for item in raw_items:
        if len(stories) >= 20:
            break

        url = item.get("url", "")
        title = item.get("title", "")
        if not url or not title:
            continue

        print(f"[trace] Processing: {title[:60]}")

        # Image extraction (layers 1–3)
        image_url, credit = extract_image(url, item)

        # Layer 4: Jina Reader markdown
        jina_text = ""
        if not image_url:
            jina_text = fetch_article_text(url)
            image_url = extract_image_from_jina(jina_text)

        # Layer 5: category fallback
        if not image_url:
            image_url = get_fallback_image(item)
            credit = "ClawBeat"

        # Article text for AI (reuse jina_text if already fetched)
        if not jina_text:
            jina_text = fetch_article_text(url)

        # AI content
        fallback_text = item.get("summary") or title
        print(f"  Generating AI content…")
        summary_html, why_html = generate_story_content(gemini, jina_text, fallback_text)
        time.sleep(8)  # stay under 15 RPM

        # Deck: first sentence of summary (plain text, truncated)
        deck_plain = re.sub(r"<[^>]+>", "", summary_html).strip()
        deck = deck_plain[:200] if deck_plain else ""

        # Category from tags
        tags = item.get("tags") or []
        category = " · ".join(str(t).title() for t in tags[:2]) if tags else (item.get("source") or "AI")

        # Publisher info
        parsed_url = urlparse(url)
        pub_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        source = item.get("source") or parsed_url.netloc

        # Slug (deduplicated)
        base_slug = slugify(title, fallback=f"signal-{len(stories)+1:02d}")
        slug = base_slug
        counter = 2
        while slug in seen_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        seen_slugs.add(slug)

        stories.append({
            "rank":          len(stories) + 1,
            "url":           url,
            "slug":          slug,
            "headline":      title,
            "deck":          deck,
            "category":      category,
            "image_url":     image_url,
            "credit":        credit,
            "pub_name":      source,
            "pub_url":       pub_url,
            "author":        "",
            "pub_date":      "",
            "summary_html":  summary_html,
            "why_it_matters": why_html,
        })

    if not stories:
        print("[trace] No stories processed. Exiting.", file=sys.stderr)
        sys.exit(1)

    print(f"[trace] Processed {len(stories)} stories")

    # 3. Editorial paragraph
    print("[trace] Generating editorial paragraph…")
    editorial_text = generate_editorial(gemini, stories)
    time.sleep(8)

    # 4. Fetch supporting data
    daily_editions = fetch_daily_editions(sb, ym)
    repos          = fetch_github_repos(sb, ym)
    print(f"[trace] Daily editions: {len(daily_editions)}, Repos: {len(repos)}")

    # 5. Build story sections HTML for cover
    story_sections_html = build_story_sections(stories, ym)
    if daily_editions:
        story_sections_html += build_daily_section(daily_editions, ym)
    if repos:
        story_sections_html += build_repo_section(repos)

    # 6. Render cover page
    cover_template = COVER_TEMPLATE_PATH.read_text(encoding="utf-8")
    og_image = stories[0].get("image_url", "") if stories else ""
    meta_desc = (editorial_text or "")[:155]

    cover_vars = {
        "ISSUE_NUMBER":      issue_number,
        "MONTH_LABEL":       month_lbl,
        "MONTH_YM":          ym,
        "OG_IMAGE":          og_image,
        "META_DESCRIPTION":  meta_desc,
        "COMPILED_TIME":     COMPILED_TIME,
        "EDITORIAL_TEXT":    editorial_text,
        "STORY_SECTIONS_HTML": story_sections_html,
    }
    cover_html = render_template(cover_template, cover_vars)

    # Write cover page
    issue_dir = OUTPUT_BASE / ym
    issue_dir.mkdir(parents=True, exist_ok=True)
    cover_path = issue_dir / "index.html"
    cover_path.write_text(cover_html, encoding="utf-8")
    print(f"[trace] Cover page written: {cover_path}")

    # 7. Render story pages
    article_template = ARTICLE_TEMPLATE_PATH.read_text(encoding="utf-8")

    for i, story in enumerate(stories):
        prev_story = stories[i - 1] if i > 0 else None
        next_story = stories[i + 1] if i < len(stories) - 1 else None
        article_html = build_article_page(
            article_template, story, ym, issue_number, month_lbl, prev_story, next_story
        )
        story_dir = issue_dir / story["slug"]
        story_dir.mkdir(parents=True, exist_ok=True)
        story_path = story_dir / "index.html"
        story_path.write_text(article_html, encoding="utf-8")
        print(f"[trace] Story {story['rank']:02d} written: {story_path}")

    # 8. Upsert to trace_issues Supabase table
    print("[trace] Upserting to trace_issues…")
    sb.table("trace_issues").upsert({
        "issue_ym":       ym,
        "issue_number":   issue_number,
        "month_label":    month_lbl,
        "editorial":      editorial_text,
        "cover_image":    og_image,
        "cover_headline": stories[0]["headline"] if stories else "",
        "cover_slug":     stories[0]["slug"] if stories else "",
        "story_count":    len(stories),
        "published_at":   datetime.datetime.utcnow().isoformat() + "Z",
    }, on_conflict="issue_ym").execute()

    # 9. Render static archive index (no Supabase JS dependency)
    print("[trace] Fetching all issues for archive index…")
    all_issues_res = sb.table("trace_issues") \
        .select("issue_ym,issue_number,month_label,editorial,cover_image,cover_headline,cover_slug,story_count") \
        .order("issue_ym", desc=True) \
        .execute()
    all_issues = all_issues_res.data or []
    archive_html = render_archive_index(all_issues)
    archive_path = OUTPUT_BASE / "index.html"
    archive_path.write_text(archive_html, encoding="utf-8")
    print(f"[trace] Static archive index written: {archive_path} ({len(all_issues)} issues)")

    print(f"[trace] Done. Issue No. {issue_number} — {month_lbl} — {len(stories)} stories.")


if __name__ == "__main__":
    main()
