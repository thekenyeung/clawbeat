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
    url = story_page_url(ym, story["slug"])
    image_url = story.get("image_url", "")
    bg_style = f"background-image:url('{esc(image_url)}')" if image_url else ""
    category = esc(story.get("category", ""))
    pub_name  = esc(story.get("pub_name", ""))
    pub_url   = esc(story.get("pub_url", ""))
    headline  = esc(story.get("headline", ""))
    deck      = esc(story.get("deck", ""))
    author    = esc(story.get("author", ""))
    pub_date  = esc(story.get("pub_date", ""))
    byline = f"By {author} · {pub_date}" if author else pub_date

    return f"""<!-- ── COVER STORY: Signal 01 ── -->
<section class="cover-section">
  <a href="{esc(url)}" class="cover-hero" style="display:flex">
    <div class="cover-bg" style="{bg_style}"></div>
    <div class="cover-gradient"></div>
    <div class="cover-top-badges">
      <span class="cover-story-badge">Cover Story · Signal 01</span>
      <span class="cover-category-badge">{category}</span>
    </div>
    <div class="cover-content">
      <div class="cover-source-line">{pub_name} · <a href="{pub_url}" target="_blank" rel="noopener">{pub_url.replace("https://","").replace("http://","")}</a></div>
      <h2 class="cover-headline">{headline}</h2>
      <p class="cover-deck">{deck}</p>
      <div class="cover-footer">
        <span class="cover-read-link">Read Full Analysis <i data-lucide="arrow-right"></i></span>
        <span class="cover-byline">{byline}</span>
      </div>
    </div>
  </a>
</section>"""

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
        <div class="card-source">{pub_name} · <a href="{esc(pub_url)}" target="_blank" rel="noopener">{pub_url.replace("https://","").replace("http://","")}</a></div>
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
        <i data-lucide="arrow-right" class="compact-arrow" style="width:12px;height:12px"></i>
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
    """Build all story grid sections + daily/repo sections for the cover page."""
    parts = []

    # Story 1: cover hero (full-bleed)
    if len(stories) >= 1:
        parts.append(build_cover_hero(stories[0], ym))

    parts.append('<div class="wrap">')

    # Stories 2–3: grid-two
    if len(stories) >= 3:
        parts.append(section_rule("Top Signals", "2–3"))
        parts.append('<div class="grid-two">')
        for i in range(1, 3):
            if i < len(stories):
                parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')

    # Stories 4–5: asymmetric
    if len(stories) >= 5:
        parts.append(section_rule("Signal Spotlight", "4–5"))
        parts.append('<div class="grid-asym-inner">')
        # Story 4 — wide card
        parts.append(build_card(stories[3], ym, 4))
        # Story 5 — narrow card
        s5 = stories[4]
        s5_html = build_card(s5, ym, 5)
        # Swap class to card-small
        s5_html = s5_html.replace('class="card"', 'class="card card-small"', 1)
        parts.append(s5_html)
        parts.append('</div>')

    # Stories 6–8: grid-three
    if len(stories) >= 8:
        parts.append(section_rule("Signals", "6–8"))
        parts.append('<div class="grid-three">')
        for i in range(5, 8):
            if i < len(stories):
                parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')

    # Story 9: wide feature break
    if len(stories) >= 9:
        parts.append(section_rule("Deep Signal"))
        parts.append(build_wide_feature(stories[8], ym, 9))

    # Stories 10–13: 2x2
    if len(stories) >= 13:
        parts.append(section_rule("Intel Roundup", "10–13"))
        parts.append('<div class="grid-two-two">')
        for i in range(9, 13):
            if i < len(stories):
                parts.append(build_card(stories[i], ym, i + 1, show_summary=False))
        parts.append('</div>')

    # Stories 14–17: compact digest (no images)
    if len(stories) >= 17:
        parts.append(section_rule("Quick Signals", "14–17"))
        parts.append('<div class="grid-compact">')
        for i in range(13, 17):
            if i < len(stories):
                parts.append(build_compact_card(stories[i], ym, i + 1))
        parts.append('</div>')

    # Stories 18–20: closing three
    if len(stories) >= 20:
        parts.append(section_rule("Closing Signals", "18–20"))
        parts.append('<div class="grid-final">')
        for i in range(17, 20):
            if i < len(stories):
                parts.append(build_card(stories[i], ym, i + 1))
        parts.append('</div>')

    parts.append('</div><!-- /wrap -->')
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
      <div class="section-rule-label"><span class="sl">//</span> Daily Transmissions</div>
      <div class="section-rule-line"></div>
    </div>
    <div class="daily-grid">
      {"".join(cards)}
    </div>
  </div>"""

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

    # 8. Bake Supabase credentials into the archive index
    archive_path = OUTPUT_BASE / "index.html"
    if archive_path.exists():
        archive_html = archive_path.read_text(encoding="utf-8")
        archive_html = archive_html \
            .replace("{{SUPABASE_URL}}", SUPABASE_URL) \
            .replace("{{SUPABASE_ANON_KEY}}", SUPABASE_ANON_KEY)
        archive_path.write_text(archive_html, encoding="utf-8")
        print("[trace] Archive index credentials baked.")

    # 9. Upsert to trace_issues Supabase table
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

    print(f"[trace] Done. Issue No. {issue_number} — {month_lbl} — {len(stories)} stories.")


if __name__ == "__main__":
    main()
