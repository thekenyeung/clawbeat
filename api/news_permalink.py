"""
ClawBeat News Permalink — Vercel Python Serverless Function

POST /api/news_permalink
  Generates a permalink page for an anchor headline.
  Requires: Authorization: Bearer <PERMALINK_SECRET>
  Body: { article_url, headline, pub_name, pub_date, date (YYYY-MM-DD), more_coverage }
  Returns: { permalink_url }

GET /news/:date/:slug  (rewritten by Vercel to /api/news_permalink?date=...&slug=...)
  Serves the landing page HTML for the given permalink.

Required env vars:
  ADMIN_SUPABASE_URL    — Supabase project URL
  SUPABASE_SERVICE_KEY  — service role key
  GEMINI_API_KEY        — Gemini API key
  PERMALINK_SECRET      — static bearer token for POST auth
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

# ── env ───────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ.get("ADMIN_SUPABASE_URL", "").strip()
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "").strip()
PERMALINK_SECRET  = os.environ.get("PERMALINK_SECRET", "").strip()

SITE_HOST = "https://clawbeat.co"
TABLE     = "news_permalinks"


# ── helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str, fallback: str = "article") -> str:
    """Convert a headline into a URL-safe ASCII slug (max 60 chars)."""
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or fallback


def _absolutize(img: str, base_url: str) -> str:
    """Make a potentially relative image URL absolute."""
    if img.startswith("//"):
        return "https:" + img
    if img.startswith("/"):
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{img}"
    return img


def fetch_og_image(url: str) -> str:
    """Fetch the best available image from an article page.

    Priority:
      1. og:image meta tag
      2. twitter:image meta tag
      3. First <img> whose src looks like a real content image (skips icons/avatars)
    Returns absolute URL string or empty string on failure.
    """
    try:
        r = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "ClawBeat/1.0 (compatible; Mozilla/5.0)"},
            allow_redirects=True,
        )
        html = r.text[:60_000]
        base = r.url

        # 1. og:image
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                img = m.group(1).strip()
                if img:
                    return _absolutize(img, base)

        # 2. twitter:image
        for pat in [
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']twitter:image["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                img = m.group(1).strip()
                if img:
                    return _absolutize(img, base)

        # 3. First plausible content <img> — skip tiny icons, avatars, tracking pixels
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
            src = m.group(1).strip()
            if not src or src.startswith("data:"):
                continue
            src_lower = src.lower()
            # Skip known noise patterns
            if any(x in src_lower for x in ("avatar", "icon", "logo", "pixel", "1x1", "badge", "emoji")):
                continue
            # Prefer known Medium CDN domains; also accept any https image > likely content
            is_medium_cdn = any(d in src_lower for d in ("miro.medium.com", "cdn-images-1.medium.com"))
            is_likely_content = src_lower.startswith("https://") and any(
                src_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
            )
            if is_medium_cdn or is_likely_content:
                return _absolutize(src, base)

    except Exception:
        pass
    return ""


# Regex patterns to strip paywall/nav boilerplate — applied before sending to Gemini.
# We strip the phrase and everything after it on the same line, since it's always a tail.
_PAYWALL_STRIP_RES = [
    re.compile(r'[Cc]ontinue\s+reading\s+on\s+\w[\w.]*\s*\.?', re.I),
    re.compile(r'[Rr]ead\s+(the\s+)?(full|more|rest)(\s+(article|story|post))?\s+on\s+\w[\w.]*\s*\.?', re.I),
    re.compile(r'[Ss]ign[\s\-]up(\s+for\s+free)?\s+to\s+(read|continue|unlock)\s*\.?', re.I),
    re.compile(r'[Cc]reate\s+a(\s+free)?\s+account\s+to\s+(read|continue)\s*\.?', re.I),
    re.compile(r'[Mm]ember[\s\-]only\s+(content|story|article)\s*\.?', re.I),
    re.compile(r'[Ss]ubscribe\s+to\s+(read|continue|unlock)\s*\.?', re.I),
    re.compile(r'[Ll]og\s+in\s+to\s+(read|continue)\s*\.?', re.I),
]


def _clean_text(text: str) -> str:
    """Strip paywall phrases and normalize whitespace. Returns whatever real content remains."""
    if not text:
        return ""
    for pat in _PAYWALL_STRIP_RES:
        text = pat.sub("", text)
    # Collapse runs of whitespace/blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# Jina header fields that appear before the article body (in the top metadata block)
_JINA_META_RE = re.compile(
    r'^(Title|URL Source|Published Time|Author|Source|Byline|Date|By)\s*:', re.I | re.M
)
# Lines that are just the article headline repeated as an H1/H2, a byline, or a datestamp
_ARTICLE_HEADER_RE = re.compile(
    r'^\s*(#{1,3}\s.{0,200}|By\s+\S.{0,100}|\d{1,2}\s+\w+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})\s*$',
    re.M,
)


def _strip_article_header(text: str, headline: str) -> str:
    """Remove the leading metadata block Jina prepends to article markdown.

    Jina often starts with: the article title as H1, a byline (By Author · Date),
    and the publish date — all before the actual body content begins. We skip lines
    at the top that duplicate the headline or match byline/date patterns, stopping
    once we hit a substantive paragraph.
    """
    lines = text.splitlines()
    headline_lower = headline.lower().strip()
    body_lines: list[str] = []
    found_body = False

    for line in lines:
        stripped = line.strip()
        if not found_body:
            # Skip blank lines at the top
            if not stripped:
                continue
            # Skip H1/H2/H3 that closely match the headline
            clean_line = re.sub(r'^#+\s*', '', stripped).lower()
            if clean_line and (clean_line in headline_lower or headline_lower in clean_line):
                continue
            # Skip short lines that look like bylines or dates (< 80 chars, no sentence structure)
            if len(stripped) < 80 and _ARTICLE_HEADER_RE.match(stripped):
                continue
            # Skip "By Author Name" / "Author · Date" patterns
            if re.match(r'^[Bb]y\s+\S', stripped) and len(stripped) < 100:
                continue
            # This looks like body content — start collecting
            found_body = True

        body_lines.append(line)

    return "\n".join(body_lines).strip()


def fetch_jina(url: str) -> str:
    """Pull cleaned article body text via Jina Reader. Returns clean content or '' on error."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            timeout=8,
            headers={"Accept": "text/plain", "User-Agent": "ClawBeat/1.0"},
        )
        text = r.text
        # Jina wraps content after this marker
        marker = "Markdown Content:"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
        # Some responses include a second metadata header block before the body
        # (Title:, URL Source:, Published Time:) — strip those lines too
        text = _JINA_META_RE.sub("", text)
        text = _clean_text(text)
        return text[:8000]
    except Exception:
        return ""


def gemini_summarize(headline: str, article_text: str, fallback_summary: str = "") -> str:
    """Generate a deep multi-paragraph AI analysis.

    Source priority: Jina article text > cleaned fallback_summary > headline-only.
    Never returns paywall boilerplate.
    """
    if not GEMINI_API_KEY:
        return _clean_text(fallback_summary)

    # Build the best source text we can; strip any article header the caller didn't remove
    source_text = _strip_article_header(article_text, headline) if article_text else ""
    if not source_text:
        source_text = _clean_text(fallback_summary)
    headline_only = len(source_text) < 120  # effectively nothing useful

    if headline_only:
        # We have no article content — generate based on headline + topic knowledge
        content_section = (
            "No article text is available (the source is paywalled or inaccessible). "
            "Use your knowledge of this topic to write the analysis, but note at the end "
            "of the final paragraph that this analysis is based on the headline and topic context, "
            "not the full article text."
        )
    else:
        content_section = f"Article excerpt:\n{source_text[:7000]}"

    try:
        prompt = (
            "You are a senior analyst for ClawBeat, an agentic AI intelligence feed covering the OpenClaw "
            "ecosystem, AI agents, and related tooling. Write a detailed, substantive signal analysis. "
            "Structure your response as 3-4 distinct paragraphs:\n\n"
            "1. **What happened**: The core event, announcement, or finding — be specific with names, "
            "technical details, and context.\n"
            "2. **Key details**: Notable technical specifics, architecture choices, benchmarks, or background "
            "that a practitioner would care about.\n"
            "3. **OpenClaw ecosystem implications**: How this connects to or affects agentic AI frameworks, "
            "multi-agent systems, or the broader developer ecosystem.\n"
            "4. **Signal strength**: Who should pay attention and why — developers, researchers, or operators.\n\n"
            "Rules: Be factual. Do not repeat the headline, author name, publication name, or date — "
            "those are already shown on the page. Do not start with 'This article'. Do not use bullet "
            "points — write in flowing prose. Each paragraph should be 3-5 sentences. Separate paragraphs "
            "with a blank line. Never include phrases like 'Continue reading on Medium' or similar paywall "
            "text.\n\n"
            f"Headline: {headline}\n\n"
            f"{content_section}"
        )
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 700, "temperature": 0.25},
            },
            timeout=20,
        )
        result = _clean_text(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        return result or _clean_text(fallback_summary)
    except Exception:
        return _clean_text(fallback_summary)


def supabase_get(date: str, slug: str) -> dict | None:
    """Fetch a permalink row by date+slug. Returns row dict or None."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"date": f"eq.{date}", "slug": f"eq.{slug}", "limit": "1"},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=5,
        )
        rows = r.json()
        return rows[0] if rows else None
    except Exception:
        return None


def supabase_upsert(row: dict) -> bool:
    """Upsert a permalink row."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            json=row,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            timeout=5,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def format_display_date(iso_date: str) -> str:
    """YYYY-MM-DD → Mon DD, YYYY"""
    try:
        from datetime import date
        d = date.fromisoformat(iso_date)
        return d.strftime("%b %d, %Y")
    except Exception:
        return iso_date


# ── HTML template ─────────────────────────────────────────────────────────────

def render_landing_page(row: dict) -> str:
    headline     = html_mod.escape(row.get("headline", ""))
    pub_name     = html_mod.escape(row.get("pub_name", ""))
    display_date = html_mod.escape(format_display_date(row.get("date", "")))
    ai_summary   = html_mod.escape(row.get("ai_summary", ""))
    og_image     = html_mod.escape(row.get("og_image_url", ""))
    article_url  = html_mod.escape(row.get("article_url", "#"))
    slug         = row.get("slug", "")
    date_str     = row.get("date", "")
    more_cov     = row.get("more_coverage") or []

    permalink    = f"{SITE_HOST}/news/{date_str}/{slug}"
    og_desc      = ai_summary[:200] + ("…" if len(ai_summary) > 200 else "")

    image_block = ""
    if og_image:
        image_block = f"""
    <a href="{article_url}" target="_blank" rel="noopener noreferrer" class="hero-img-link">
      <img src="{og_image}" alt="{headline}" class="hero-img" onerror="this.parentElement.style.display='none'">
    </a>"""

    more_cov_block = ""
    if more_cov:
        links = "".join(
            f'<a href="{html_mod.escape(m.get("url","#"))}" target="_blank" rel="noopener noreferrer" class="more-link">'
            f'{html_mod.escape(m.get("source",""))} ↗</a>'
            for m in more_cov
            if not m.get("source", "").lower().startswith("facebook")
        )
        if links:
            more_cov_block = f"""
    <section class="more-cov">
      <div class="section-hdr"><span class="hdr-slash">// </span><span class="hdr-label">more_coverage</span></div>
      <div class="more-links">{links}</div>
    </section>"""

    summary_block = ""
    if ai_summary:
        # Split into paragraphs and render each as a <p>
        raw_paras = [p.strip() for p in ai_summary.split("\n\n") if p.strip()]
        # Strip leading markdown bold markers (e.g. "**What happened**: ...")
        import re as _re
        cleaned_paras = [_re.sub(r"^\*\*[^*]+\*\*:?\s*", "", p) for p in raw_paras]
        paras_html = "".join(f"<p>{p}</p>" for p in cleaned_paras if p)
        summary_block = f"""
    <section class="analysis">
      <div class="section-hdr"><span class="hdr-slash">// </span><span class="hdr-label">signal_analysis</span></div>
      <div class="summary-text">{paras_html}</div>
      <span class="ai-badge">AI-generated · Grounded in source article</span>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{headline} · ClawBeat</title>

  <!-- Social / OG -->
  <meta property="og:type"        content="article">
  <meta property="og:title"       content="{headline}">
  <meta property="og:description" content="{og_desc}">
  <meta property="og:url"         content="{permalink}">
  <meta property="og:image"       content="{og_image}">
  <meta property="og:site_name"   content="ClawBeat">
  <meta name="twitter:card"       content="summary_large_image">
  <meta name="twitter:title"      content="{headline}">
  <meta name="twitter:description" content="{og_desc}">
  <meta name="twitter:image"      content="{og_image}">
  <link rel="canonical"           href="{permalink}">

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;900&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">

  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --sans:   'Space Grotesk', sans-serif;
      --mono:   'JetBrains Mono', monospace;
      --bg:     #08090b;
      --bg-2:   #0d0f12;
      --bg-3:   #131619;
      --border: rgba(255,255,255,0.05);
      --border-2: rgba(255,255,255,0.09);
      --text-1: #e2e4e9;
      --text-2: #9097a3;
      --text-3: #525866;
      --orange: #f97316;
      --green:  #22c55e;
    }}

    body {{
      font-family: var(--sans);
      background: var(--bg);
      color: var(--text-1);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    /* ── Header ── */
    .site-header {{
      padding: 1rem 2rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .brand {{
      font-family: var(--sans);
      font-weight: 900;
      font-style: italic;
      font-size: 1.1rem;
      letter-spacing: -0.04em;
      color: #fff;
      text-decoration: none;
    }}
    .brand span {{ color: var(--orange); }}
    .nav-link {{
      font-family: var(--mono);
      font-size: 0.65rem;
      color: var(--text-2);
      text-decoration: none;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .nav-link:hover {{ color: var(--orange); }}

    /* ── Main ── */
    main {{
      flex: 1;
      max-width: 760px;
      width: 100%;
      margin: 0 auto;
      padding: 2.5rem 1.5rem 4rem;
    }}

    /* ── Hero image ── */
    .hero-img-link {{ display: block; margin-bottom: 2rem; border-radius: 6px; overflow: hidden; }}
    .hero-img {{
      width: 100%;
      max-height: 420px;
      object-fit: cover;
      display: block;
      border-radius: 6px;
      border: 1px solid var(--border-2);
    }}

    /* ── Meta row ── */
    .meta-row {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 1rem;
      font-family: var(--mono);
      font-size: 0.65rem;
      color: var(--text-2);
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .meta-date {{ color: var(--text-3); }}
    .meta-sep  {{ color: var(--text-3); }}
    .meta-source a {{
      color: var(--orange);
      text-decoration: none;
      font-weight: 700;
    }}
    .meta-source a:hover {{ text-decoration: underline; }}

    /* ── Headline ── */
    .article-headline {{
      font-size: clamp(1.5rem, 4vw, 2.25rem);
      font-weight: 900;
      line-height: 1.15;
      letter-spacing: -0.02em;
      color: #fff;
      margin-bottom: 2rem;
    }}

    /* ── Section header ── */
    .section-hdr {{
      font-family: var(--mono);
      font-size: 0.6rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 0.85rem;
      display: flex;
      align-items: center;
      gap: 0;
    }}
    .hdr-slash {{ color: var(--text-3); }}
    .hdr-label {{ color: var(--orange); font-weight: 700; }}

    /* ── Analysis / Summary ── */
    .analysis {{
      background: var(--bg-2);
      border: 1px solid var(--border-2);
      border-radius: 6px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .summary-text {{
      font-size: 1rem;
      line-height: 1.75;
      color: var(--text-1);
      margin-bottom: 0.85rem;
    }}
    .summary-text p {{
      margin-bottom: 1rem;
    }}
    .summary-text p:last-child {{
      margin-bottom: 0;
    }}
    .ai-badge {{
      font-family: var(--mono);
      font-size: 0.55rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text-3);
    }}

    /* ── More coverage ── */
    .more-cov {{
      margin-bottom: 1.5rem;
    }}
    .more-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}
    .more-link {{
      font-family: var(--mono);
      font-size: 0.65rem;
      color: var(--text-2);
      text-decoration: none;
      border: 1px solid var(--border-2);
      border-radius: 3px;
      padding: 0.3rem 0.6rem;
      letter-spacing: 0.04em;
      transition: color 0.15s, border-color 0.15s;
    }}
    .more-link:hover {{ color: var(--orange); border-color: rgba(249,115,22,0.4); }}

    /* ── CTA ── */
    .cta-row {{
      margin-top: 2rem;
    }}
    .cta-btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: var(--orange);
      color: #fff;
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      text-decoration: none;
      padding: 0.75rem 1.5rem;
      border-radius: 4px;
      transition: opacity 0.15s;
    }}
    .cta-btn:hover {{ opacity: 0.88; }}

    /* ── Footer ── */
    .footer {{
      background: var(--bg-2);
      border-top: 1px solid var(--border-2);
      padding: 1.25rem 2rem;
    }}
    .footer-inner {{
      max-width: 760px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .footer-brand {{
      font-family: var(--sans);
      font-weight: 900;
      font-style: italic;
      font-size: 0.85rem;
      letter-spacing: -0.04em;
      color: #fff;
    }}
    .footer-brand span {{ color: var(--orange); }}
    .clawhub-badge {{
      font-family: var(--mono);
      font-size: 0.6rem;
      color: var(--text-3);
      text-decoration: none;
      letter-spacing: 0.06em;
    }}
    .clawhub-badge:hover {{ color: var(--orange); }}

    @media (max-width: 600px) {{
      main {{ padding: 1.5rem 1rem 3rem; }}
      .site-header {{ padding: 0.85rem 1rem; }}
    }}
  </style>
</head>
<body>

  <header class="site-header">
    <a href="/" class="brand">CLAWBEAT<span>.co</span></a>
    <a href="/news.html" class="nav-link">← Intel Feed</a>
  </header>

  <main>
    {image_block}

    <div class="meta-row">
      <span class="meta-date">{display_date}</span>
      <span class="meta-sep">·</span>
      <span class="meta-source"><a href="{article_url}" target="_blank" rel="noopener noreferrer">{pub_name}</a></span>
    </div>

    <h1 class="article-headline">{headline}</h1>

    {summary_block}

    {more_cov_block}

    <div class="cta-row">
      <a href="{article_url}" target="_blank" rel="noopener noreferrer" class="cta-btn">
        Read Full Story →
      </a>
    </div>
  </main>

  <footer class="footer">
    <div class="footer-inner">
      <div class="footer-brand">CLAWBEAT<span>.co</span></div>
      <a class="clawhub-badge" href="https://clawhub.ai/thekenyeung/clawbeat" target="_blank" rel="noopener">// openclaw_skill ↗</a>
    </div>
  </footer>

</body>
</html>"""


def render_404() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Not Found · ClawBeat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { background: #08090b; color: #e2e4e9; font-family: 'JetBrains Mono', monospace;
           display: flex; align-items: center; justify-content: center; min-height: 100vh;
           text-align: center; }
    h1 { font-size: 1rem; color: #f97316; margin-bottom: 0.5rem; }
    p  { font-size: 0.75rem; color: #525866; }
    a  { color: #f97316; }
  </style>
</head>
<body>
  <div>
    <h1>// signal_not_found</h1>
    <p>This permalink does not exist. <a href="/news.html">Back to intel feed →</a></p>
  </div>
</body>
</html>"""


# ── Vercel handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Serve the permalink landing page. Auto-heals cached summaries containing paywall text."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        date_str = (qs.get("date") or [""])[0]
        slug     = (qs.get("slug") or [""])[0].removesuffix(".html")

        if not date_str or not slug:
            self._html(400, render_404())
            return

        row = supabase_get(date_str, slug)
        if not row:
            self._html(404, render_404())
            return

        # Heal rows where the cached ai_summary contains paywall boilerplate
        cached_summary = row.get("ai_summary", "") or ""
        if _clean_text(cached_summary) != cached_summary.strip() or (
            cached_summary and len(_clean_text(cached_summary)) < 120
        ):
            article_text = fetch_jina(row.get("article_url", ""))
            fresh = gemini_summarize(row.get("headline", ""), article_text, "")
            if fresh:
                row["ai_summary"] = fresh
                supabase_upsert({**row, "ai_summary": fresh})

        self._html(200, render_landing_page(row))

    def do_POST(self):
        """Create (or return cached) permalink for an anchor headline."""
        # ── Auth ──────────────────────────────────────────────────────────────
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not PERMALINK_SECRET or token != PERMALINK_SECRET:
            self._json(401, {"error": "unauthorized"})
            return

        # ── Parse body ────────────────────────────────────────────────────────
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        article_url      = body.get("article_url", "").strip()
        headline         = body.get("headline", "").strip()
        pub_name         = body.get("pub_name", "").strip()
        pub_date         = body.get("pub_date", "").strip()
        date_str         = body.get("date", "").strip()  # YYYY-MM-DD
        fallback_summary = body.get("summary", "").strip()
        more_cov         = body.get("more_coverage", [])

        if not article_url or not headline or not date_str:
            self._json(400, {"error": "missing required fields"})
            return

        slug = slugify(headline, fallback=date_str)

        # ── Idempotency: return cached result only if summary is clean and populated ────
        existing = supabase_get(date_str, slug)
        cached_ok = (
            existing
            and existing.get("og_image_url")
            and existing.get("ai_summary")
            and _clean_text(existing["ai_summary"]) == existing["ai_summary"].strip()
            and len(_clean_text(existing["ai_summary"])) >= 120
        )
        if cached_ok:
            permalink_url = (
                f"{SITE_HOST}/news/{date_str}/{slug}"
                "?utm_source=clawbeat&utm_medium=share&utm_campaign=permalink"
            )
            self._json(200, {"permalink_url": permalink_url})
            return

        # ── Fetch content + generate summary ─────────────────────────────────
        article_text = fetch_jina(article_url)
        ai_summary   = gemini_summarize(headline, article_text, fallback_summary)
        # Reuse og_image_url from existing row if already fetched
        og_image_url = (existing or {}).get("og_image_url") or fetch_og_image(article_url)

        # ── Persist ───────────────────────────────────────────────────────────
        row = {
            "date":         date_str,
            "slug":         slug,
            "article_url":  article_url,
            "headline":     headline,
            "pub_name":     pub_name,
            "pub_date":     pub_date,
            "og_image_url": og_image_url,
            "ai_summary":   ai_summary,
            "more_coverage": more_cov,
        }
        supabase_upsert(row)

        permalink_url = (
            f"{SITE_HOST}/news/{date_str}/{slug}"
            "?utm_source=clawbeat&utm_medium=share&utm_campaign=permalink"
        )
        self._json(200, {"permalink_url": permalink_url})

    # ── helpers ───────────────────────────────────────────────────────────────

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default stderr logging
