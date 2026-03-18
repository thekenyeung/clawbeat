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


def fetch_og_image(url: str) -> str:
    """Fetch og:image from article HTML. Returns absolute URL string or empty string."""
    try:
        r = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "ClawBeat/1.0 (compatible; Mozilla/5.0)"},
            allow_redirects=True,
        )
        html = r.text[:60_000]
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                img = m.group(1).strip()
                # Absolutize relative URLs
                if img.startswith("//"):
                    img = "https:" + img
                elif img.startswith("/"):
                    parsed = urlparse(r.url)
                    img = f"{parsed.scheme}://{parsed.netloc}{img}"
                return img
    except Exception:
        pass
    return ""


def fetch_jina(url: str) -> str:
    """Pull cleaned article text via Jina Reader."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            timeout=8,
            headers={"Accept": "text/plain", "User-Agent": "ClawBeat/1.0"},
        )
        text = r.text
        marker = "Markdown Content:"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
        return text.strip()[:6000]
    except Exception:
        return ""


def gemini_summarize(headline: str, article_text: str, fallback_summary: str = "") -> str:
    """Generate a 3-4 sentence AI summary grounded in the article content.
    Falls back to fallback_summary (existing DB summary) if Jina returns nothing."""
    if not GEMINI_API_KEY:
        return fallback_summary
    source_text = article_text or fallback_summary
    if not source_text:
        return ""
    try:
        prompt = (
            "You are an AI analyst for ClawBeat, an agentic AI news feed. "
            "Write a 3-4 sentence summary of the following article. "
            "Be factual, specific, and grounded in the article content. "
            "Do not start with 'This article'. Do not editorialize.\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{source_text[:5000]}"
        )
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 180, "temperature": 0.2},
            },
            timeout=15,
        )
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return fallback_summary


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
        summary_block = f"""
    <section class="analysis">
      <div class="section-hdr"><span class="hdr-slash">// </span><span class="hdr-label">signal_analysis</span></div>
      <p class="summary-text">{ai_summary}</p>
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
      line-height: 1.7;
      color: var(--text-1);
      margin-bottom: 0.85rem;
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
        """Serve the permalink landing page."""
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

        # ── Idempotency: return cached result only if fields are populated ────
        existing = supabase_get(date_str, slug)
        if existing and existing.get("ai_summary") and existing.get("og_image_url"):
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
