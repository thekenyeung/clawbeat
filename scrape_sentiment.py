"""
ClawBeat Sentiment Tracker
Scrapes HackerNews, Bluesky, Reddit, GitHub, YouTube, and Mastodon for
mentions of OpenClaw and related ecosystem terms. Scores each mention with
VADER, then calls Gemini once per run for topic clustering and narrative.
Writes results to Supabase: sentiment_mentions, sentiment_snapshots,
sentiment_articles, sentiment_ecosystem.

Run: python scrape_sentiment.py
Schedule: GitHub Actions cron — 3x/day (morning, afternoon, evening PT)
"""

import os
import re
import json
import time
import hashlib
import datetime
import requests
from zoneinfo import ZoneInfo
from dotenv import load_dotenv, find_dotenv

# VADER — pip install vaderSentiment
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Supabase — pip install supabase
from supabase import create_client, Client as SupabaseClient

# Gemini — pip install google-genai
from google import genai
from google.genai import types

# ──────────────────────────────────────────────────────────────
# 1. CONFIG
# ──────────────────────────────────────────────────────────────
load_dotenv(find_dotenv(), override=True)

SUPABASE_URL         = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "").strip()

REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "clawbeat-sentiment/1.0 by u/clawbeat").strip()

BLUESKY_HANDLE       = os.getenv("BLUESKY_HANDLE", "clawbeat.bsky.social").strip()
BLUESKY_APP_PASSWORD = os.getenv("BLUESKY_APP_PASSWORD", "").strip()

YOUTUBE_API_KEY      = os.getenv("YOUTUBE_API_KEY", "").strip()

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Search terms — case-insensitive applied at collection time
SEARCH_TERMS = [
    "openclaw", "open claw", "nanoclaw", "picoclaw",
    "moltbot", "clawdbot", "clawhub",
]

# Competitor co-mention tracking
COMPETITORS = ["langchain", "crewai", "autogen", "llamaindex", "langgraph"]

# Mastodon instances to query
MASTODON_INSTANCES = [
    "mastodon.social",
    "hachyderm.io",
    "fosstodon.org",
    "sigmoid.social",
    "infosec.exchange",
    "discuss.systems",
]

# Reddit subreddits to search
SUBREDDITS = [
    "MachineLearning", "LocalLLaMA", "artificial",
    "agentframework", "learnmachinelearning", "programming",
]

# How many items per source to keep (soft cap before dedup)
MAX_PER_SOURCE = 100

# Gemini model
GEMINI_MODEL = "gemini-2.5-flash"

# ──────────────────────────────────────────────────────────────
# 2. HELPERS
# ──────────────────────────────────────────────────────────────

def _period() -> str:
    """Return morning / afternoon / evening based on current PT hour."""
    hour = datetime.datetime.now(_PACIFIC).hour
    if hour < 12:
        return "morning"
    elif hour < 18:
        return "afternoon"
    else:
        return "evening"


def _today() -> datetime.date:
    return datetime.datetime.now(_PACIFIC).date()


def _contains_term(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in SEARCH_TERMS)


def _anonymize(text: str) -> str:
    """Strip @handles, u/usernames, display names from text."""
    text = re.sub(r'@[\w.\-:]+', '', text)       # @handle or @handle.bsky.social
    text = re.sub(r'u/[\w\-]+', '', text)         # Reddit u/username
    text = re.sub(r'r/[\w\-]+', 'r/[sub]', text)  # keep subreddit as context, strip name
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def _vader(analyzer: SentimentIntensityAnalyzer, text: str) -> tuple[float, str]:
    scores = analyzer.polarity_scores(text)
    compound = round(scores["compound"], 4)
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return compound, label


def _url_fingerprint(url: str) -> str:
    """Canonical URL fingerprint for dedup."""
    url = re.sub(r'\?.*$', '', url.rstrip('/'))
    return hashlib.md5(url.encode()).hexdigest()


def _safe_get(url: str, headers: dict = None, params: dict = None, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] GET {url} failed: {e}")
        return None

# ──────────────────────────────────────────────────────────────
# 3. SCRAPERS
# ──────────────────────────────────────────────────────────────

def scrape_hackernews() -> list[dict]:
    """Query Algolia HN Search API — no auth required."""
    mentions = []
    for term in SEARCH_TERMS:
        data = _safe_get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": term, "tags": "story,comment", "hitsPerPage": 50},
        )
        if not data:
            continue
        for hit in data.get("hits", []):
            text = hit.get("comment_text") or hit.get("title") or ""
            if not _contains_term(text):
                continue
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}"
            ts_raw = hit.get("created_at")
            try:
                pub = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
            except Exception:
                pub = None
            mentions.append({
                "source": "hackernews",
                "content_text": _anonymize(text),
                "url": url,
                "title": _anonymize(hit.get("title") or ""),
                "published_at": pub.isoformat() if pub else None,
                "points": hit.get("points", 0) or 0,
                "num_comments": hit.get("num_comments", 0) or 0,
            })
    print(f"  HN: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_bluesky() -> list[dict]:
    """Search Bluesky public AppView — uses App Password if available for higher limits."""
    headers = {"Content-Type": "application/json"}
    token = None

    if BLUESKY_HANDLE and BLUESKY_APP_PASSWORD:
        auth = _safe_get.__module__  # just a ref check — use requests directly
        try:
            r = requests.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD},
                timeout=10,
            )
            if r.ok:
                token = r.json().get("accessJwt")
                headers["Authorization"] = f"Bearer {token}"
        except Exception as e:
            print(f"  [WARN] Bluesky auth failed: {e}")

    mentions = []
    for term in SEARCH_TERMS:
        params = {"q": term, "limit": 50, "sort": "latest"}
        try:
            r = requests.get(
                "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                headers=headers,
                params=params,
                timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()
        except Exception as e:
            print(f"  [WARN] Bluesky search '{term}' failed: {e}")
            continue

        for post in data.get("posts", []):
            text = post.get("record", {}).get("text", "")
            if not _contains_term(text):
                continue
            uri = post.get("uri", "")
            # Convert at:// URI to bsky.app URL
            url = ""
            if uri.startswith("at://"):
                parts = uri.replace("at://", "").split("/")
                if len(parts) == 3:
                    url = f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
            ts_raw = post.get("indexedAt") or post.get("record", {}).get("createdAt")
            try:
                pub = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
            except Exception:
                pub = None
            like_count = post.get("likeCount", 0) or 0
            reply_count = post.get("replyCount", 0) or 0
            mentions.append({
                "source": "bluesky",
                "content_text": _anonymize(text),
                "url": url,
                "title": "",
                "published_at": pub.isoformat() if pub else None,
                "points": like_count,
                "num_comments": reply_count,
            })

    print(f"  Bluesky: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_reddit() -> list[dict]:
    """Search Reddit using OAuth read-only app credentials."""
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        print("  Reddit: skipped (no credentials)")
        return []

    # Get OAuth token
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        reddit_token = r.json().get("access_token")
    except Exception as e:
        print(f"  [WARN] Reddit OAuth failed: {e}")
        return []

    headers = {
        "Authorization": f"bearer {reddit_token}",
        "User-Agent": REDDIT_USER_AGENT,
    }

    mentions = []
    for term in SEARCH_TERMS:
        for sub in SUBREDDITS:
            try:
                r = requests.get(
                    f"https://oauth.reddit.com/r/{sub}/search",
                    headers=headers,
                    params={"q": term, "restrict_sr": 1, "sort": "new", "limit": 25, "t": "week"},
                    timeout=10,
                )
                if not r.ok:
                    continue
                posts = r.json().get("data", {}).get("children", [])
            except Exception as e:
                print(f"  [WARN] Reddit r/{sub} search failed: {e}")
                continue

            for child in posts:
                post = child.get("data", {})
                title = post.get("title", "")
                selftext = post.get("selftext", "")
                text = f"{title} {selftext}".strip()
                if not _contains_term(text):
                    continue
                ts = post.get("created_utc")
                pub = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc) if ts else None
                url = f"https://reddit.com{post.get('permalink', '')}"
                mentions.append({
                    "source": "reddit",
                    "content_text": _anonymize(text[:2000]),
                    "url": url,
                    "title": _anonymize(title),
                    "published_at": pub.isoformat() if pub else None,
                    "points": post.get("score", 0) or 0,
                    "num_comments": post.get("num_comments", 0) or 0,
                })
            time.sleep(0.5)  # Reddit rate limit courtesy delay

    print(f"  Reddit: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_github() -> list[dict]:
    """Search GitHub issues and discussions for OpenClaw mentions."""
    mentions = []
    gh_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    if gh_token:
        gh_headers["Authorization"] = f"Bearer {gh_token}"

    for term in ["openclaw", "nanoclaw"]:
        data = _safe_get(
            "https://api.github.com/search/issues",
            headers=gh_headers,
            params={"q": f"{term} is:issue", "sort": "created", "order": "desc", "per_page": 30},
        )
        if not data:
            continue
        for item in data.get("items", []):
            text = f"{item.get('title', '')} {item.get('body', '') or ''}"
            if not _contains_term(text):
                continue
            ts_raw = item.get("created_at")
            try:
                pub = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
            except Exception:
                pub = None
            mentions.append({
                "source": "github",
                "content_text": _anonymize(text[:2000]),
                "url": item.get("html_url", ""),
                "title": _anonymize(item.get("title", "")),
                "published_at": pub.isoformat() if pub else None,
                "points": item.get("reactions", {}).get("+1", 0) or 0,
                "num_comments": item.get("comments", 0) or 0,
            })
        time.sleep(1)

    print(f"  GitHub: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_youtube() -> list[dict]:
    """Search YouTube Data API v3 for OpenClaw videos."""
    if not YOUTUBE_API_KEY:
        print("  YouTube: skipped (no API key)")
        return []

    mentions = []
    for term in ["openclaw", "open claw agent framework"]:
        data = _safe_get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": term,
                "type": "video",
                "order": "date",
                "maxResults": 20,
                "key": YOUTUBE_API_KEY,
            },
        )
        if not data:
            continue
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            text = f"{title} {description}"
            if not _contains_term(text):
                continue
            video_id = item.get("id", {}).get("videoId", "")
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
            ts_raw = snippet.get("publishedAt")
            try:
                pub = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
            except Exception:
                pub = None
            mentions.append({
                "source": "youtube",
                "content_text": _anonymize(text[:1000]),
                "url": url,
                "title": _anonymize(title),
                "published_at": pub.isoformat() if pub else None,
                "points": 0,
                "num_comments": 0,
            })

    print(f"  YouTube: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_mastodon() -> list[dict]:
    """Search public Mastodon instances — no auth required."""
    mentions = []
    for instance in MASTODON_INSTANCES:
        for term in SEARCH_TERMS[:3]:  # limit to top 3 terms per instance to stay polite
            data = _safe_get(
                f"https://{instance}/api/v2/search",
                params={"q": term, "type": "statuses", "limit": 20, "resolve": "false"},
                timeout=8,
            )
            if not data:
                continue
            for status in data.get("statuses", []):
                # Strip HTML tags from content
                raw = status.get("content", "")
                text = re.sub(r'<[^>]+>', ' ', raw)
                text = re.sub(r'\s{2,}', ' ', text).strip()
                if not _contains_term(text):
                    continue
                url = status.get("url", "")
                ts_raw = status.get("created_at")
                try:
                    pub = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
                except Exception:
                    pub = None
                mentions.append({
                    "source": "mastodon",
                    "content_text": _anonymize(text[:1500]),
                    "url": url,
                    "title": "",
                    "published_at": pub.isoformat() if pub else None,
                    "points": status.get("favourites_count", 0) or 0,
                    "num_comments": status.get("replies_count", 0) or 0,
                })
            time.sleep(0.3)

    print(f"  Mastodon: {len(mentions)} mentions")
    return mentions[:MAX_PER_SOURCE]


def scrape_news_feed(supabase: SupabaseClient) -> list[dict]:
    """Pull today's articles from existing news_items table."""
    today = _today().isoformat()
    try:
        rows = (
            supabase.table("news_items")
            .select("url, title, summary, date, inserted_at")
            .gte("inserted_at", f"{today}T00:00:00+00:00")
            .execute()
            .data
        )
    except Exception as e:
        print(f"  [WARN] news_items fetch failed: {e}")
        return []
    mentions = []
    for row in rows:
        text = f"{row.get('title', '')} {row.get('summary', '') or ''}"
        if not _contains_term(text):
            continue
        mentions.append({
            "source": "news",
            "content_text": _anonymize(text[:2000]),
            "url": row.get("url", ""),
            "title": row.get("title", ""),
            "published_at": row.get("inserted_at"),
            "points": 0,
            "num_comments": 0,
        })
    print(f"  News feed: {len(mentions)} mentions")
    return mentions


# ──────────────────────────────────────────────────────────────
# 4. ECOSYSTEM METRICS (GitHub hard signals)
# ──────────────────────────────────────────────────────────────

def collect_ecosystem_metrics(supabase: SupabaseClient):
    """Pull GitHub star counts and issue stats for tracked families."""
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    gh_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if gh_token:
        gh_headers["Authorization"] = f"Bearer {gh_token}"

    families = {
        "openclaw":  ("openclaw", "openclaw"),
        "nanoclaw":  ("nanoclaw", "nanoclaw"),
        "picoclaw":  ("picoclaw", "picoclaw"),
    }

    for family_key, (owner, repo) in families.items():
        data = _safe_get(f"https://api.github.com/repos/{owner}/{repo}", headers=gh_headers)
        if not data:
            continue

        # Closed issue count requires a separate search call
        closed_data = _safe_get(
            "https://api.github.com/search/issues",
            headers=gh_headers,
            params={"q": f"repo:{owner}/{repo} is:issue is:closed", "per_page": 1},
        )
        closed_count = closed_data.get("total_count", 0) if closed_data else 0
        open_count = data.get("open_issues_count", 0)
        total_issues = open_count + closed_count
        close_ratio = round(closed_count / total_issues, 3) if total_issues > 0 else 0

        # PyPI downloads
        pypi_data = _safe_get(f"https://pypistats.org/api/packages/{repo}/recent", timeout=8)
        pypi_week = 0
        if pypi_data and "data" in pypi_data:
            pypi_week = pypi_data["data"].get("last_week", 0) or 0

        # Fetch prior row for star delta
        prior = None
        try:
            r = supabase.table("sentiment_ecosystem").select("github_stars").eq("family", family_key).execute()
            if r.data:
                prior = r.data[0].get("github_stars", 0)
        except Exception:
            pass

        stars = data.get("stargazers_count", 0) or 0
        star_delta = (stars - prior) if prior is not None else 0

        try:
            supabase.table("sentiment_ecosystem").upsert({
                "family": family_key,
                "display_name": data.get("name", family_key),
                "github_stars": stars,
                "github_stars_delta": star_delta,
                "github_forks": data.get("forks_count", 0) or 0,
                "open_issues": open_count,
                "issue_close_ratio": close_ratio,
                "pypi_downloads_week": pypi_week,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }, on_conflict="family").execute()
        except Exception as e:
            print(f"  [WARN] ecosystem upsert failed for {family_key}: {e}")

        time.sleep(1)

    print("  Ecosystem metrics updated.")


# ──────────────────────────────────────────────────────────────
# 5. SCORING
# ──────────────────────────────────────────────────────────────

def compute_composite_scores(
    mentions: list[dict],
    prior_count: int,
    gemini_confidence: float = 1.0,
) -> dict:
    """
    Returns four composite scores (0–100) for the snapshot:
    - momentum: velocity vs prior baseline
    - sentiment: VADER compound average scaled to 0–100
    - trust: proxy for DX/docs/community health (positive HN + GH engagement)
    - buzz: cross-platform echo + thread depth
    """
    n = len(mentions)
    if n == 0:
        return {"momentum": 0, "sentiment": 50, "trust": 50, "buzz": 0}

    # Momentum: compare current run count to prior
    baseline = max(prior_count, 1)
    velocity_ratio = n / baseline
    momentum = min(round(velocity_ratio * 50, 1), 100)

    # Sentiment: VADER avg scaled from [-1,1] to [0,100]
    scores = [m["sentiment_score"] for m in mentions if m.get("sentiment_score") is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    sentiment_scaled = round((avg_score + 1) * 50, 1)

    # Trust: ratio of positive mentions from authoritative sources (HN, GitHub)
    trust_sources = [m for m in mentions if m["source"] in ("hackernews", "github", "news")]
    pos_trust = sum(1 for m in trust_sources if m.get("sentiment_label") == "positive")
    trust = round((pos_trust / max(len(trust_sources), 1)) * 100, 1)

    # Buzz: cross-platform presence + weighted engagement
    unique_sources = len(set(m["source"] for m in mentions))
    total_engagement = sum((m.get("points", 0) or 0) + (m.get("num_comments", 0) or 0) for m in mentions)
    source_factor = min(unique_sources / 6, 1.0)  # max 6 sources
    engagement_factor = min(total_engagement / 500, 1.0)
    buzz = round(((source_factor * 0.6) + (engagement_factor * 0.4)) * 100, 1)

    # Apply Gemini confidence multiplier to all scores
    def _apply(val):
        return min(round(val * gemini_confidence, 1), 100)

    return {
        "momentum": _apply(momentum),
        "sentiment": _apply(sentiment_scaled),
        "trust": _apply(trust),
        "buzz": _apply(buzz),
    }


# ──────────────────────────────────────────────────────────────
# 6. GEMINI ANALYSIS
# ──────────────────────────────────────────────────────────────

def run_gemini_analysis(mentions: list[dict], prior_snapshot: dict | None) -> dict:
    """
    One Gemini call per run. Feeds anonymized mention batch for:
    - Narrative summary
    - Topic clusters with sentiment, tension, novelty, momentum
    - Emerging story flag
    - Competitive framing
    Returns structured dict.
    """
    if not GEMINI_API_KEY:
        print("  Gemini: skipped (no API key)")
        return _empty_gemini()

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build compact mention list for the prompt (keep it under token budget)
    sample = mentions[:60]
    mention_lines = []
    for m in sample:
        src = m["source"]
        label = m.get("sentiment_label", "neutral")
        score = m.get("sentiment_score", 0)
        # Strip quotes and control characters that could break JSON output
        text = m["content_text"][:300]
        text = text.replace('"', "'").replace('\\', ' ').replace('\n', ' ').replace('\r', ' ')
        mention_lines.append(f"[{src}][{label} {score:+.2f}] {text}")

    prior_summary = ""
    if prior_snapshot:
        prior_summary = f"""
Previous snapshot narrative (for comparison):
{prior_snapshot.get('gemini_narrative', 'N/A')}
Previous emerging story: {prior_snapshot.get('emerging_story', 'N/A')}
"""

    prompt = f"""You are an AI analyst for ClawBeat, a news intelligence platform covering the OpenClaw agentic AI framework ecosystem.

Below are {len(sample)} anonymized social mentions collected in the past few hours. Each line shows [source][sentiment score] text.

{chr(10).join(mention_lines)}

{prior_summary}

Competitors to watch for co-mentions: {', '.join(COMPETITORS)}

Analyze and return ONLY valid JSON in this exact structure (no markdown, no explanation):
{{
  "narrative": "2-3 sentence plain-English summary of the current conversation for developers",
  "emerging_story": "One sentence describing the most notable new or fast-moving story, or empty string if none",
  "competitive_framing": "One sentence on OpenClaw vs competitors if present, or empty string if none",
  "confidence": 1.0,
  "topics": [
    {{
      "name": "topic name (2-4 words)",
      "sentiment": 0.0,
      "tension": "one sentence: what people love vs what frustrates them, or empty",
      "novelty": "new|recurring|fading",
      "momentum": "accelerating|stable|declining"
    }}
  ]
}}

Rules:
- topics: 3–6 items, ordered by significance
- sentiment: float -1.0 to 1.0
- confidence: float 0.8–1.2 (higher if you see a clear strong signal)
- narrative must be actionable for a developer deciding whether to adopt OpenClaw
- Do NOT include any usernames, handles, or personally identifying information
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )
        raw = response.text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        # Remove trailing commas before } or ] (common Gemini JSON quirk)
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        # Extract the first complete JSON object if Gemini added trailing text
        brace_start = raw.find('{')
        if brace_start != -1:
            depth = 0
            for i, ch in enumerate(raw[brace_start:], brace_start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        raw = raw[brace_start:i+1]
                        break
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            try:
                import json5
                result = json5.loads(raw)
            except Exception:
                # Last resort: ast.literal_eval with bool/null substitution
                import ast
                repaired = raw.replace(': true', ': True').replace(':true', ':True')
                repaired = repaired.replace(': false', ': False').replace(':false', ':False')
                repaired = repaired.replace(': null', ': None').replace(':null', ':None')
                result = ast.literal_eval(repaired)
        print(f"  Gemini: {len(result.get('topics', []))} topics, confidence={result.get('confidence', 1.0)}")
        return result
    except Exception as e:
        print(f"  [WARN] Gemini analysis failed: {e}")
        return _empty_gemini()


def _empty_gemini() -> dict:
    return {
        "narrative": "",
        "emerging_story": "",
        "competitive_framing": "",
        "confidence": 1.0,
        "topics": [],
    }


# ──────────────────────────────────────────────────────────────
# 7. ARTICLE CROSS-PLATFORM TRACKING
# ──────────────────────────────────────────────────────────────

def track_articles(mentions: list[dict], supabase: SupabaseClient):
    """Upsert articles mentioned across platforms into sentiment_articles."""
    # Collect unique URLs with their mention context
    url_map: dict[str, dict] = {}
    for m in mentions:
        url = (m.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        fp = _url_fingerprint(url)
        if fp not in url_map:
            url_map[fp] = {
                "url": url,
                "title": m.get("title") or "",
                "sources": set(),
                "sentiments": [],
                "topic_tags": m.get("topic_tags") or [],
            }
        url_map[fp]["sources"].add(m["source"])
        if m.get("sentiment_score") is not None:
            url_map[fp]["sentiments"].append(m["sentiment_score"])

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for fp, art in url_map.items():
        sources = sorted(art["sources"])
        avg_sent = round(sum(art["sentiments"]) / len(art["sentiments"]), 4) if art["sentiments"] else 0
        try:
            # Try update first (increment share_count)
            existing = supabase.table("sentiment_articles").select("share_count, sources").eq("url", art["url"]).execute()
            if existing.data:
                ex = existing.data[0]
                ex_sources = set(ex.get("sources") or [])
                merged_sources = sorted(ex_sources | set(sources))
                new_count = ex.get("share_count", 1) + len(set(sources) - ex_sources)
                supabase.table("sentiment_articles").update({
                    "last_seen_at": now,
                    "share_count": new_count,
                    "sources": merged_sources,
                    "avg_sentiment": avg_sent,
                }).eq("url", art["url"]).execute()
            else:
                supabase.table("sentiment_articles").insert({
                    "url": art["url"],
                    "title": art["title"],
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "share_count": len(sources),
                    "sources": sources,
                    "avg_sentiment": avg_sent,
                    "topic_tags": art["topic_tags"],
                }).execute()
        except Exception as e:
            print(f"  [WARN] article upsert failed: {e}")


# ──────────────────────────────────────────────────────────────
# 8. DEDUPLICATION
# ──────────────────────────────────────────────────────────────

def dedup(mentions: list[dict]) -> list[dict]:
    """Remove duplicate URLs within this run."""
    seen = set()
    out = []
    for m in mentions:
        url = m.get("url", "")
        fp = _url_fingerprint(url) if url else None
        text_fp = hashlib.md5(m["content_text"][:100].encode()).hexdigest()
        key = fp or text_fp
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


# ──────────────────────────────────────────────────────────────
# 9. MAIN
# ──────────────────────────────────────────────────────────────

def main():
    print(f"\n=== ClawBeat Sentiment Tracker — {_period().upper()} run ===")
    print(f"    {datetime.datetime.now(_PACIFIC).strftime('%Y-%m-%d %H:%M PT')}\n")

    # Init clients
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("❌ SUPABASE_URL / SUPABASE_SERVICE_KEY not set.")
        return
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    analyzer = SentimentIntensityAnalyzer()
    period = _period()
    today = _today()

    # ── Scrape all sources
    print("── Scraping sources...")
    raw_mentions: list[dict] = []
    raw_mentions += scrape_hackernews()
    raw_mentions += scrape_bluesky()
    raw_mentions += scrape_reddit()
    raw_mentions += scrape_github()
    raw_mentions += scrape_youtube()
    raw_mentions += scrape_mastodon()
    raw_mentions += scrape_news_feed(supabase)

    # ── Dedup
    raw_mentions = dedup(raw_mentions)
    print(f"\n  Total unique mentions: {len(raw_mentions)}")

    if not raw_mentions:
        print("  No mentions found this run. Exiting.")
        return

    # ── VADER scoring
    print("── Running VADER sentiment scoring...")
    for m in raw_mentions:
        score, label = _vader(analyzer, m["content_text"])
        m["sentiment_score"] = score
        m["sentiment_label"] = label
        m["run_period"] = period

    # ── Insert mentions into Supabase
    print("── Writing mentions to Supabase...")
    rows_to_insert = []
    for m in raw_mentions:
        rows_to_insert.append({
            "source": m["source"],
            "content_text": m["content_text"],
            "url": m.get("url", ""),
            "title": m.get("title", ""),
            "sentiment_score": m.get("sentiment_score"),
            "sentiment_label": m.get("sentiment_label", "neutral"),
            "published_at": m.get("published_at"),
            "run_period": m.get("run_period", period),
            "topic_tags": m.get("topic_tags", []),
        })
    try:
        supabase.table("sentiment_mentions").insert(rows_to_insert).execute()
        print(f"  Inserted {len(rows_to_insert)} mention rows.")
    except Exception as e:
        print(f"  [WARN] mentions insert failed: {e}")

    # ── Track cross-platform articles
    print("── Tracking article shares...")
    track_articles(raw_mentions, supabase)

    # ── Ecosystem metrics
    print("── Collecting ecosystem metrics...")
    collect_ecosystem_metrics(supabase)

    # ── Fetch prior snapshot for Gemini comparison
    prior_snapshot = None
    try:
        r = supabase.table("sentiment_snapshots").select("*").order("snapshot_at", desc=True).limit(1).execute()
        if r.data:
            prior_snapshot = r.data[0]
    except Exception:
        pass

    # ── Prior mention count for momentum scoring
    prior_count = 0
    if prior_snapshot:
        prior_count = prior_snapshot.get("total_mentions", 0) or 0

    # ── Gemini topic analysis
    print("── Running Gemini analysis...")
    gemini = run_gemini_analysis(raw_mentions, prior_snapshot)
    gemini_confidence = float(gemini.get("confidence", 1.0))

    # Attach Gemini topic_tags back to mentions (best-effort)
    topic_names = [t["name"] for t in gemini.get("topics", [])]
    for m in raw_mentions:
        # Simple keyword match to assign tags
        assigned = []
        for topic in topic_names:
            if any(word in m["content_text"].lower() for word in topic.lower().split()):
                assigned.append(topic)
        m["topic_tags"] = assigned[:3]

    # ── Compute aggregate scores
    scores = compute_composite_scores(raw_mentions, prior_count, gemini_confidence)

    # ── Source breakdown
    source_breakdown: dict[str, int] = {}
    for m in raw_mentions:
        source_breakdown[m["source"]] = source_breakdown.get(m["source"], 0) + 1

    # ── Sentiment counts
    pos = sum(1 for m in raw_mentions if m.get("sentiment_label") == "positive")
    neg = sum(1 for m in raw_mentions if m.get("sentiment_label") == "negative")
    neu = len(raw_mentions) - pos - neg
    n = len(raw_mentions)

    # ── Top articles for snapshot
    try:
        top_art_rows = (
            supabase.table("sentiment_articles")
            .select("url, title, share_count, sources, avg_sentiment")
            .order("share_count", desc=True)
            .limit(10)
            .execute()
            .data
        )
    except Exception:
        top_art_rows = []

    # ── Write snapshot
    print("── Writing snapshot to Supabase...")
    snapshot = {
        "snapshot_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "period": period,
        "snapshot_date": today.isoformat(),
        "score_momentum": scores["momentum"],
        "score_sentiment": scores["sentiment"],
        "score_trust": scores["trust"],
        "score_buzz": scores["buzz"],
        "total_mentions": n,
        "positive_count": pos,
        "negative_count": neg,
        "neutral_count": neu,
        "positive_pct": round(pos / n * 100, 1) if n else 0,
        "negative_pct": round(neg / n * 100, 1) if n else 0,
        "neutral_pct": round(neu / n * 100, 1) if n else 0,
        "source_breakdown": source_breakdown,
        "gemini_narrative": gemini.get("narrative", ""),
        "emerging_story": gemini.get("emerging_story", ""),
        "competitive_framing": gemini.get("competitive_framing", ""),
        "gemini_confidence": gemini_confidence,
        "topics": gemini.get("topics", []),
        "top_articles": top_art_rows,
    }
    try:
        supabase.table("sentiment_snapshots").insert(snapshot).execute()
        print("  Snapshot written.")
    except Exception as e:
        print(f"  [WARN] snapshot insert failed: {e}")

    print(f"\n=== Done. {n} mentions | {pos} pos / {neg} neg / {neu} neu ===")
    print(f"    Momentum={scores['momentum']} Sentiment={scores['sentiment']} Trust={scores['trust']} Buzz={scores['buzz']}")
    if gemini.get("narrative"):
        print(f"\n  Gemini: {gemini['narrative']}")
    if gemini.get("emerging_story"):
        print(f"  Emerging: {gemini['emerging_story']}")


if __name__ == "__main__":
    main()
