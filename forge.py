import urllib.parse
import feedparser
import requests
import json
import re
import os
import time
import numpy as np
import sys
import yt_dlp
from supabase import create_client, Client as SupabaseClient
from dotenv import load_dotenv, find_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from datetime import datetime, timedelta
from urllib.parse import urlparse
from newspaper import Article
try:
    from langdetect import detect as _langdetect
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False

# --- 1. COMPACT ENCODER ---
class CompactJSONEncoder(json.JSONEncoder):
    def iterencode(self, o, _one_shot=False):
        if isinstance(o, list) and not any(isinstance(i, (list, dict)) for i in o):
            return "[" + ", ".join(json.dumps(i) for i in o) + "]"
        return super().iterencode(o, _one_shot)

# --- 2. SETUP & CONFIGURATION ---
load_dotenv(find_dotenv(), override=True)
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip().replace('"', '').replace("'", "")

if not GEMINI_KEY:
    print("❌ ERROR: GEMINI_API_KEY not found.")
    exit(1)

client = genai.Client(api_key=GEMINI_KEY)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
_supabase: "SupabaseClient | None" = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("⚠️  SUPABASE_URL / SUPABASE_SERVICE_KEY not set — DB writes disabled.")

CORE_BRANDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "claudbot", "peter steinberger", "steinberger"]
KEYWORDS = CORE_BRANDS

WHITELIST_PATH = "./src/whitelist.json"
OUTPUT_PATH = "./public/data.json"

MAX_BATCH_SIZE = 50
SLEEP_BETWEEN_REQUESTS = 6.5

# Generic newsletter/blog platforms that host whitelisted Creator sources
PRIORITY_SITES = ['substack.com', 'beehiiv.com']

# Press release wires and spam/PR aggregators — never anchor headlines from these
DELIST_SITES = [
    'prnewswire.com', 'businesswire.com', 'globenewswire.com',
    'accessnewswire.com', 'einpresswire.com', 'prlog.org',
    '24-7pressrelease.com', 'newswire.com', 'prweb.com',
    'issuewire.com', 'openpr.com', 'releasewire.com', 'send2press.com',
    'marketwired.com', 'webwire.com', 'pressrelease.com',
]
BANNED_SOURCES = [
    "access newswire", "globenewswire", "prnewswire", "business wire",
    "pr newswire", "einpresswire", "prweb", "newswire", "press release",
    "marketwired", "webwire",
]

# --- Dynamically load whitelist domain authority sets ---
def _load_whitelist_domains():
    publisher_domains, creator_domains = set(), set()
    try:
        with open(WHITELIST_PATH, 'r') as f:
            entries = json.load(f)
        for entry in entries:
            url = entry.get("Website URL", "")
            if not url:
                continue
            try:
                parsed = urlparse(url if url.startswith('http') else 'https://' + url)
                domain = parsed.netloc.lower().lstrip('www.')
            except Exception:
                domain = url.lower().lstrip('www.').split('/')[0]
            if not domain:
                continue
            cat = entry.get("Category", "")
            if cat == "Publisher":
                publisher_domains.add(domain)
            elif cat == "Creator":
                creator_domains.add(domain)
    except Exception:
        pass
    return publisher_domains, creator_domains

WHITELIST_PUBLISHER_DOMAINS, WHITELIST_CREATOR_DOMAINS = _load_whitelist_domains()

# --- 3. HELPER FUNCTIONS ---

def strip_html(text):
    """Strip HTML tags and return clean plain text."""
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)

def is_english(text):
    """Return True if text is predominantly English (or too short to detect)."""
    if not _LANGDETECT_AVAILABLE or not text or len(text.strip()) < 30:
        return True
    try:
        return _langdetect(text[:500]) == 'en'
    except Exception:
        return True  # allow through on detection failure

def cosine_similarity(v1, v2):
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def get_source_type(url, source_name=""):
    url_lower = url.lower()
    source_lower = source_name.lower()
    if any(k in url_lower for k in DELIST_SITES) or any(k in source_lower for k in BANNED_SOURCES):
        return "delist"
    if any(domain in url_lower for domain in WHITELIST_PUBLISHER_DOMAINS):
        return "priority"
    if any(k in url_lower for k in PRIORITY_SITES):
        return "priority"
    return "standard"

def get_source_authority(url, source_name=""):
    """Numeric authority for anchor selection: 3=whitelist Publisher, 2=whitelist Creator, 1=standard, 0=delist."""
    url_lower = url.lower()
    source_lower = source_name.lower()
    if any(k in url_lower for k in DELIST_SITES) or any(k in source_lower for k in BANNED_SOURCES):
        return 0
    if any(domain in url_lower for domain in WHITELIST_PUBLISHER_DOMAINS):
        return 3
    if any(k in url_lower for k in PRIORITY_SITES):
        return 3
    if any(domain in url_lower for domain in WHITELIST_CREATOR_DOMAINS):
        return 2
    return 1

# Helper for robust date sorting
def try_parse_date(date_str):
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime(2000, 1, 1)

# --- 4. DATA FETCHING & FILTERING ---

def get_ai_summary(title, current_summary):
    prompt = f"Rewrite this as a professional 1-sentence tech intel brief. Impact focus. Title: {title}. Context: {current_summary}. Output ONLY the sentence."
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except: return "Summary pending."

# Lazy-loaded spaCy model — loaded once per process, never reloaded.
_spacy_nlp = None

def _get_spacy():
    global _spacy_nlp
    if _spacy_nlp is None:
        import spacy
        _spacy_nlp = spacy.load("en_core_web_sm")
    return _spacy_nlp

def get_nlp_tags(title, summary):
    """Extract up to 4 named-entity tags using spaCy NER (local, no API calls).

    Only retains ORG, PRODUCT, PERSON, GPE, and WORK_OF_ART entities, which
    map directly to what readers want to filter on in a tech-news feed.
    Requires: pip install spacy && python -m spacy download en_core_web_sm
    """
    try:
        nlp = _get_spacy()
        # Title carries the highest signal; append a short summary window for context.
        text = title + (" " + summary[:200] if summary else "")
        doc = nlp(text)
        brand_lower = {b.lower() for b in CORE_BRANDS}
        seen, tags = set(), []
        KEEP = {"ORG", "PRODUCT", "PERSON", "GPE", "WORK_OF_ART"}
        for ent in doc.ents:
            if ent.label_ not in KEEP:
                continue
            tag = ent.text.strip()
            if len(tag) < 3 or tag.lower() in brand_lower:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
            if len(tags) == 4:
                break
        return tags
    except Exception:
        return []

def get_embeddings_batch(texts, batch_size=5):
    if not texts: return []
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            result = client.models.embed_content(
                model="models/gemini-embedding-001", 
                contents=batch,
                config=types.EmbedContentConfig(task_type="CLUSTERING")
            )
            all_embeddings.extend([e.values for e in result.embeddings])
            if i + batch_size < len(texts): time.sleep(2)
        except: all_embeddings.extend([None] * len(batch))
    return all_embeddings

def process_article_intel(url):
    try:
        article = Article(url)
        article.download()
        article.parse()
        # Explicit non-English meta tag → reject immediately
        if article.meta_lang and article.meta_lang != 'en':
            return False, 0, ""
        # When meta_lang is absent, verify with langdetect on the article body
        if not article.meta_lang and not is_english(article.text[:500]):
            return False, 0, ""
        is_recent = True
        if article.publish_date:
            now = datetime.now(article.publish_date.tzinfo) if article.publish_date.tzinfo else datetime.now()
            if (now - article.publish_date).total_seconds() > 172800:
                is_recent = False
        else:
            path = urlparse(url).path
            date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', path)
            if date_match:
                year, month, day = map(int, date_match.groups())
                if (datetime.now() - datetime(year, month, day)).days > 2:
                    is_recent = False
            else:
                is_recent = False 
        if not is_recent: return False, 0, ""
        full_text = (article.title + " " + article.text).lower()
        brand_bonus = 10 if any(b in full_text for b in CORE_BRANDS) else 0
        keyword_matches = sum(1 for kw in KEYWORDS if kw.lower() in full_text)
        density_score = keyword_matches + brand_bonus
        return True, density_score, article.text[:300]
    except: return False, 0, ""

def scan_rss():
    if not os.path.exists(WHITELIST_PATH): return []
    with open(WHITELIST_PATH, 'r') as f: whitelist = json.load(f)
    found = []
    now = datetime.now()
    for site in whitelist:
        rss_url = site.get("Website RSS")
        if not rss_url or rss_url == "N/A": continue
        # Skip YouTube-only entries — they have no RSS feed for articles
        if site.get("Category") == "YouTube": continue
        source_name = site["Source Name"]
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:25]:
                title = entry.get('title', '')
                url = getattr(entry, 'link', None) or entry.get('link')
                if not url: continue

                # Delist check — reject PR wires even if they somehow appear in a whitelist feed
                if get_source_type(url, source_name) == "delist":
                    continue

                # Parse RSS-level publication date as a recency fallback
                rss_date = None
                for date_field in ('published_parsed', 'updated_parsed'):
                    raw = entry.get(date_field)
                    if raw:
                        try:
                            rss_date = datetime(*raw[:6])
                            break
                        except Exception:
                            pass

                passes, density, clean_text = process_article_intel(url)

                # RSS-only fallback: if full download fails but RSS signals a recent, brand-relevant article
                if not passes and rss_date and (now - rss_date).total_seconds() <= 172800:
                    raw_summary = strip_html(entry.get('summary', ''))
                    rss_text = (title + " " + raw_summary).lower()
                    if not is_english(title + " " + raw_summary):
                        continue
                    brand_bonus = 10 if any(b in rss_text for b in CORE_BRANDS) else 0
                    kw_matches = sum(1 for kw in KEYWORDS if kw.lower() in rss_text)
                    if brand_bonus > 0 or kw_matches >= 1:
                        passes = True
                        density = kw_matches + brand_bonus
                        clean_text = raw_summary[:300]

                # Brand mention in title always qualifies; otherwise require density >= 1
                is_brand_title = any(brand.lower() in title.lower() for brand in CORE_BRANDS)
                if not passes or (not is_brand_title and density < 1):
                    continue

                # Use actual publication date when available, fall back to today
                if rss_date:
                    article_date = rss_date.strftime("%m-%d-%Y")
                else:
                    article_date = now.strftime("%m-%d-%Y")

                display_source = source_name
                if display_source == "Medium":
                    author_name = (entry.get('author') or
                                   entry.get('author_detail', {}).get('name') or
                                   entry.get('dc_creator'))
                    if author_name:
                        display_source = f"{author_name}, Medium"

                found.append({
                    "title": title, "url": url, "source": display_source,
                    "date": article_date,
                    "summary": clean_text[:250] + "..." if clean_text else "",
                    "density": density, "vec": None
                })
        except: continue
    return found

def scan_hackernews(hours_back: int = 48) -> list:
    """Search Hacker News for OpenClaw-related content via the Algolia HN Search API.

    Uses the search_by_date endpoint (date-sorted) with a recency window, then
    fetches full article content via process_article_intel for density scoring.
    Stories and Show HN posts are both included.

    Returns articles in the same dict format as scan_rss() / scan_google_news(),
    augmented with 'hn_points' and 'hn_comments' for D3 engagement scoring.
    """
    import time as _time
    cutoff_ts = int(_time.time()) - (hours_back * 3600)

    # Search each brand separately to maximise recall; deduplicate by URL.
    # Algolia HN Search API: https://hn.algolia.com/api/v1/
    HN_SEARCH_URL = 'https://hn.algolia.com/api/v1/search_by_date'
    HN_HEADERS    = {'User-Agent': 'OpenClawIntelBot/1.0'}

    found    = []
    seen_urls: set = set()

    for brand in ["OpenClaw", "Moltbot", "Clawdbot", "Moltbook"]:
        try:
            resp = requests.get(
                HN_SEARCH_URL,
                params={
                    'query':          brand,
                    'tags':           '(story,show_hn)',
                    'numericFilters': f'created_at_i>{cutoff_ts}',
                    'hitsPerPage':    20,
                },
                headers=HN_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            hits = resp.json().get('hits', [])
            print(f"  🔶 HN '{brand}': {len(hits)} hits")

            for hit in hits:
                story_url = hit.get('url')
                # Skip self-posts (Ask/Show HN without an external URL) and dupes
                if not story_url or story_url in seen_urls:
                    continue
                if get_source_type(story_url) == 'delist':
                    continue

                seen_urls.add(story_url)

                hn_points   = hit.get('points', 0) or 0
                hn_comments = hit.get('num_comments', 0) or 0

                # Publication date from HN Unix timestamp
                created_at_i = hit.get('created_at_i', 0)
                if created_at_i:
                    article_date = datetime.fromtimestamp(created_at_i).strftime('%m-%d-%Y')
                else:
                    article_date = datetime.now().strftime('%m-%d-%Y')

                # Source name: derive from URL domain (whitelist-aware via get_source_type)
                try:
                    domain = urlparse(story_url).netloc.lower().replace('www.', '')
                except Exception:
                    domain = 'hacker-news.com'

                # Full article fetch for density scoring; tolerate failures gracefully
                passes, density, clean_text = process_article_intel(story_url)

                if not passes:
                    title_lower = (hit.get('title') or '').lower()
                    is_brand_title = any(b in title_lower for b in CORE_BRANDS)
                    # Allow through only if brand is in the title or HN score signals relevance
                    if not is_brand_title and hn_points < 10:
                        continue
                    # Estimate density from HN score when article fetch failed
                    density = max(density, hn_points // 15)
                    clean_text = ''

                found.append({
                    'title':       hit.get('title', ''),
                    'url':         story_url,
                    'source':      domain,
                    'date':        article_date,
                    'summary':     clean_text[:250] + '...' if clean_text else '',
                    'density':     density,
                    'hn_points':   hn_points,
                    'hn_comments': hn_comments,
                    'vec':         None,
                })

            time.sleep(1)   # courtesy pause between brand queries
        except Exception as e:
            print(f"⚠️ HN scan failed for '{brand}': {e}")

    print(f"📡 HN: {len(found)} new candidate articles.")
    return found


def scan_google_news():
    query = "OpenClaw OR Moltbot OR Clawdbot OR Claudbot OR Moltbook OR \"Peter Steinberger\""
    gn_url = f"https://news.google.com/rss/search?q={query}+when:48h&hl=en-US&gl=US&ceid=US:en"
    found = []
    try:
        feed = feedparser.parse(gn_url)
        for e in feed.entries[:30]:
            passes, density, clean_text = process_article_intel(e.link)
            if passes and density >= 2:
                found.append({
                    "title": e.title, "url": e.link, "source": "Web Search", 
                    "summary": clean_text[:250] + "...", "date": datetime.now().strftime("%m-%d-%Y"), 
                    "density": density, "vec": None
                })
    except: pass
    return found

# --- 5. BACKFILL FETCHERS ---

def fetch_arxiv_research():
    search_query = 'all:OpenClaw+OR+all:MoltBot+OR+all:Clawdbot'
    arxiv_url = f"http://export.arxiv.org/api/query?search_query={search_query}&sortBy=submittedDate&sortOrder=descending&max_results=10"
    print(f"📡 Scanning ArXiv: {arxiv_url}")
    try:
        headers = {'User-Agent': 'OpenClawIntelBot/1.0'}
        response = requests.get(arxiv_url, headers=headers, timeout=10)
        feed = feedparser.parse(response.text)
        print(f"  🔍 API matched {len(feed.entries)} papers.")
        if not feed.entries: return []
        papers = []
        for entry in feed.entries:
            arxiv_id = entry.id.split('/abs/')[-1]
            ss_url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{arxiv_id}?fields=tldr,abstract"
            raw_abstract = entry.summary.replace('\n', ' ')
            summary = '. '.join(raw_abstract.split('. ')[:2]) + '.'
            try:
                time.sleep(1)
                ss_resp = requests.get(ss_url, timeout=5).json()
                if ss_resp.get('tldr') and ss_resp['tldr'].get('text'):
                    summary = ss_resp['tldr']['text']
                elif ss_resp.get('abstract'):
                    ss_abstract = ss_resp['abstract'].replace('\n', ' ')
                    summary = '. '.join(ss_abstract.split('. ')[:2]) + '.'
            except: pass
            papers.append({
                "title": entry.title.replace('\n', ' ').strip(),
                "authors": [a.name for a in entry.authors],
                "date": entry.published, 
                "url": entry.link, 
                "summary": summary
            })
        return papers
    except Exception as e:
        print(f"⚠️ ArXiv fetch failed: {e}")
        return []

def _format_yt_date(raw_date):
    """Convert yt-dlp YYYYMMDD string to MM-DD-YYYY, or return None."""
    if raw_date and len(raw_date) == 8:
        return f"{raw_date[4:6]}-{raw_date[6:]}-{raw_date[:4]}"
    return None

def get_video_upload_date(video_id):
    """Fetch the actual upload date for a single YouTube video ID."""
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            return _format_yt_date(info.get('upload_date'))
    except Exception:
        return None

def fetch_youtube_videos_ytdlp(channel_url):
    if '/channel/' in channel_url and '@' in channel_url:
        channel_url = channel_url.split('/channel/')[0] + '/' + channel_url.split('/channel/')[1]
    ydl_opts = {'quiet': True, 'extract_flat': 'in_playlist', 'playlistend': 50}
    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if 'entries' in info:
                for entry in info['entries']:
                    if not entry: continue
                    full_text = (str(entry.get('title', '')) + " " + str(entry.get('description', ''))).lower()
                    if any(b.lower() in full_text or b.lower().replace(" ","") in full_text.replace(" ","") for b in CORE_BRANDS):
                        formatted_date = (
                            _format_yt_date(entry.get('upload_date'))
                            or get_video_upload_date(entry['id'])
                            or datetime.now().strftime("%m-%d-%Y")
                        )
                        videos.append({
                            "title": entry.get('title'),
                            "url": f"https://www.youtube.com/watch?v={entry['id']}",
                            "thumbnail": f"https://img.youtube.com/vi/{entry['id']}/hqdefault.jpg",
                            "channel": info.get('uploader', 'Unknown'),
                            "description": str(entry.get('description', ''))[:150],
                            "publishedAt": formatted_date
                        })
        return videos
    except Exception as e:
        print(f"⚠️ Error scanning {channel_url}: {e}")
        return []

def fetch_global_openclaw_videos(query="OpenClaw OR Moltbot OR Clawdbot", limit=30):
    search_target = f"ytsearch{limit}:{query}"
    ydl_opts = {'quiet': True, 'extract_flat': 'in_playlist', 'skip_download': True}
    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_target, download=False)
            if info and 'entries' in info:
                for entry in info['entries']:
                    if not entry: continue
                    formatted_date = (
                        _format_yt_date(entry.get('upload_date'))
                        or get_video_upload_date(entry.get('id'))
                        or datetime.now().strftime("%m-%d-%Y")
                    )
                    videos.append({
                        "title": entry.get('title') or "Untitled Video",
                        "url": f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "thumbnail": f"https://img.youtube.com/vi/{entry.get('id')}/hqdefault.jpg",
                        "channel": entry.get('uploader', 'Community'),
                        "description": (entry.get('description') or "")[:150],
                        "publishedAt": formatted_date
                    })
        return videos
    except Exception as e:
        print(f"⚠️ Global search failed: {e}")
        return []

def fetch_github_projects():
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token: headers["Authorization"] = f"token {token}"
    try:
        resp = requests.get("https://api.github.com/search/repositories?q=OpenClaw&sort=updated&order=desc&per_page=100", headers=headers, timeout=10)
        items = resp.json().get('items', [])
        return [{
            "name":        r['name'],
            "owner":       r['owner']['login'],
            "description": r['description'] or "No description.",
            "url":         r['html_url'],
            "stars":       r['stargazers_count'],
            "created_at":  r['created_at'],
            "language":    r.get('language') or '',
            "topics":      r.get('topics') or [],
            "forks":       r.get('forks_count', 0),
            "license":     (r.get('license') or {}).get('spdx_id') or '',
        } for r in items]
    except: return []

# --- 6. OPENCLAW FEED SCORING (Methodology v1.2) ---

# Tier classification keywords
_TIER1_BRANDS = ['openclaw', 'moltbot', 'clawdbot', 'claudbot']
_TIER2_BRANDS = ['moltbook']
_COMPETITOR_SIGNALS = ['vs ', ' versus ', 'compared to', 'alternative to', 'competitor']

def _get_centrality(density: int, is_brand_title: bool, has_brand_in_text: bool) -> int:
    """Map density and title signal to a 0–10 centrality score (D1 sub-dimension)."""
    if is_brand_title and density >= 10:
        return 10
    elif is_brand_title and density >= 5:
        return 8
    elif is_brand_title:
        return 6
    elif has_brand_in_text and density >= 5:
        return 6
    elif has_brand_in_text:
        return 4
    elif density >= 3:
        return 3
    else:
        return 2

def _compute_d5(item: dict, tier: int, centrality: int, authority: int) -> float:
    """Heuristic approximation of D5 Reader Value (0–20 pts, v1.3 methodology).

    Answers the 12 checklist questions via structural signals since full
    content analysis is unavailable at ingest time.

    Categories:
      A  Practical Utility         0–8 pts
      B  Community Relevance       0–6 pts
      C  Technology Directness     0–4 pts  (–2 penalty if generic)
      D  Timeliness & Accuracy     0–4 pts
    """
    title   = (item.get('title', '') or '').lower()
    text    = (title + ' ' + (item.get('summary', '') or '')).lower()
    density = item.get('density', 0)
    mc      = len(item.get('moreCoverage', []) or [])
    hn_pts  = item.get('hn_points', 0) or 0
    hn_cmt  = item.get('hn_comments', 0) or 0

    d5 = 0.0

    # ── Category A: Practical Utility (0–8 pts) ──────────────────────────────

    # A1 (+3): Helps a developer build / configure / debug with OpenClaw directly?
    # Proxy: step-by-step / process keywords in title/summary AND Tier 1 or 2
    BUILD_TERMS = {'tutorial', 'guide', 'how to', 'how-to', 'walkthrough',
                   'setup', 'configure', 'debug', 'debugging', 'install',
                   'getting started', 'quickstart', 'migration'}
    if any(t in text for t in BUILD_TERMS) and tier <= 2:
        d5 += 3

    # A2 (+2): Introduces or explains a feature, API, or capability?
    # Proxy: introduction/feature keywords AND Tier 1 (primary ecosystem)
    FEATURE_TERMS = {'introduces', 'new feature', "what's new", 'new in',
                     'announcing', 'new api', 'new sdk', 'new plugin',
                     'new integration', 'new endpoint'}
    if any(t in text for t in FEATURE_TERMS) and tier == 1:
        d5 += 2

    # A3 (+2): Includes working code, commands, or implementation guidance?
    # Proxy: code/artifact keywords AND Tier 1 or 2
    CODE_TERMS = {'code snippet', 'code sample', 'implementation', 'example code',
                  'runnable', 'demo', 'playground', 'repository', 'github.com'}
    if any(t in text for t in CODE_TERMS) and tier <= 2:
        d5 += 2

    # A4 (+1): Addresses a known pain point or FAQ?
    # Proxy: high multi-source or HN discussion → many people care about this
    if mc >= 2 or hn_cmt >= 20:
        d5 += 1

    # ── Category B: Community & Ecosystem Relevance (0–6 pts) ────────────────

    # B5 (+2): Covers a person, project, or org in the OpenClaw ecosystem?
    # Proxy: whitelist publisher/creator sources are known ecosystem players
    if authority >= 2:
        d5 += 2

    # B6 (+2): Surfaces a community discussion, debate, or decision?
    # Proxy: cross-source coverage or meaningful HN engagement
    if mc >= 1 or hn_cmt >= 10 or hn_pts >= 20:
        d5 += 2

    # B7 (+2): Announces something developers need to act on or be aware of?
    # Proxy: announcement/change keywords AND Tier 1 or 2
    ANNOUNCE_TERMS = {'release', 'launches', 'launch', 'announced', 'deprecat',
                      'end of life', 'eol', 'breaking change', 'roadmap',
                      'beta', 'rc ', 'v2.', 'v3.', 'v4.', 'v5.', '2.0', '3.0'}
    if any(t in text for t in ANNOUNCE_TERMS) and tier <= 2:
        d5 += 2

    # ── Category C: Technology Directness (0–4 pts, –2 penalty) ─────────────

    # C8: Is OpenClaw the primary technology? (+2 primary / +1 supporting)
    if tier == 1 and centrality >= 7:
        d5 += 2   # primary subject
    elif tier <= 2 and centrality >= 4:
        d5 += 1   # supporting role

    # C9: Adjacent tool with OpenClaw-specific impact explained?
    # Proxy: Tier 2/3 articles that still have substantial density signal
    if tier <= 2:
        d5 += 1   # implicit connection via brand mention
    elif density >= 3:
        d5 += 1   # Tier 3 but meaningful brand signal

    # C10: Would this be equally relevant to non-OpenClaw developers? (–2 if yes)
    # Proxy: Tier 3 articles without the brand in the raw title are likely generic
    is_brand_title = any(b in title for b in _TIER1_BRANDS + _TIER2_BRANDS)
    if tier == 3 and not is_brand_title:
        d5 -= 2

    # ── Category D: Timeliness & Accuracy (0–4 pts) ──────────────────────────

    # D11 (+2): Reflects current state of OpenClaw?
    # All ingested articles pass a 48 h recency filter, so presumed current.
    d5 += 2

    # D12 (+2): Responding to something in the current news cycle?
    # Proxy: multi-source coverage or notable HN engagement
    if mc >= 1 or hn_pts >= 10:
        d5 += 2

    return max(0.0, min(20.0, d5))


def compute_scores(item: dict) -> dict:
    """Compute D1–D5 scores, total_score, d1_tier, stage_tags, and source_type
    for a single article dict (post-clustering, so moreCoverage is set).

    Returns a dict with the score keys ready to merge into the DB record.
    Total Score = (D1/40×35)+(D2/25×20)+(D3/20×15)+(D4/15×10)+(D5/20×20)  max=100 (v1.3).
    """
    url          = item.get('url', '')
    source       = item.get('source', '')
    title        = item.get('title', '')
    summary      = item.get('summary', '')
    density      = item.get('density', 0)
    more_cov     = item.get('moreCoverage', []) or []
    hn_points    = item.get('hn_points', 0) or 0
    hn_comments  = item.get('hn_comments', 0) or 0

    url_lower    = url.lower()
    source_lower = source.lower()
    title_lower  = title.lower()
    text_lower   = (title + ' ' + summary).lower()

    # ── D1: Product Relevance (0–40) ─────────────────────────────────────────
    has_tier1 = any(b in text_lower for b in _TIER1_BRANDS)
    has_tier2 = any(b in text_lower for b in _TIER2_BRANDS)

    if has_tier1:
        tier, tier_mult = 1, 1.0
    elif has_tier2:
        tier, tier_mult = 2, 0.65
    else:
        tier, tier_mult = 3, 0.30

    is_brand_title = any(b in title_lower for b in _TIER1_BRANDS + _TIER2_BRANDS)
    has_brand_text = has_tier1 or has_tier2
    centrality = _get_centrality(density, is_brand_title, has_brand_text)

    d1 = min(40.0, centrality * tier_mult * 4)

    # Competitor/comparison penalty: –10 if article is comparison-framed and
    # our brand is not the primary subject of the title.
    if any(sig in title_lower for sig in _COMPETITOR_SIGNALS) and not is_brand_title:
        d1 = max(0.0, d1 - 10)

    # ── D2: Content Depth & Actionability (0–25) ─────────────────────────────
    authority = get_source_authority(url, source)
    has_summary = bool(summary and len(summary.strip()) > 50)

    # Depth (0–15)
    if authority >= 3 and has_summary and len(summary) > 150:
        depth = 12   # whitelist publisher, rich AI-generated brief
    elif authority >= 2 and has_summary:
        depth = 9
    elif has_summary:
        depth = 6
    else:
        depth = 3

    # Actionability (0–10): title keyword signals
    ACT_KEYWORDS = {
        'release': 3, 'launches': 3, 'launch': 3, 'update': 3,
        'tutorial': 2, 'guide': 2, 'how to': 2, 'how-to': 2,
        'api': 2, 'patch': 2, 'changelog': 2,
        'documentation': 1, 'docs': 1, 'example': 1, 'demo': 1,
    }
    actionability = 0
    for kw, pts in ACT_KEYWORDS.items():
        if kw in title_lower:
            actionability += pts
    actionability = min(10, actionability)

    d2 = float(depth + actionability)

    # ── D3: Engagement & Social Signal (0–20) ────────────────────────────────
    d3 = 0.0
    mc_count = len(more_cov)

    # Multi-source coverage proxy
    if mc_count >= 4:
        d3 += 10
    elif mc_count >= 2:
        d3 += 7
    elif mc_count >= 1:
        d3 += 5

    # Source-level signal
    if authority >= 3:
        d3 += 8
    elif any(k in url_lower for k in ('substack.com', 'beehiiv.com')):
        d3 += 4
    elif authority >= 2:
        d3 += 3

    # High keyword density → community interest
    if density >= 15:
        d3 += 3
    elif density >= 8:
        d3 += 1

    # HN direct engagement signal (Stage 3 methodology, social media row)
    # Points and comments are capped together; social cap is still 20 overall.
    if hn_points > 100:
        d3 += 10
    elif hn_points > 50 or hn_comments >= 50:
        d3 += 7
    elif hn_points >= 20 or hn_comments >= 20:
        d3 += 3
    elif hn_points > 0:
        d3 += 1

    d3 = min(20.0, d3)

    # ── D4: Source Credibility (0–15) ────────────────────────────────────────
    is_delist = (
        any(k in url_lower for k in DELIST_SITES)
        or any(k in source_lower for k in BANNED_SOURCES)
    )
    if is_delist:
        d4 = 0.0
    elif authority >= 3:
        d4 = 14.0
    elif authority == 2:
        d4 = 11.0
    elif authority == 1:
        d4 = 6.0
    else:
        d4 = 0.0

    d5 = _compute_d5(item, tier, centrality, authority)
    total = round((d1/40*35) + (d2/25*20) + (d3/20*15) + (d4/15*10) + (d5/20*20), 2)

    # ── Stage 3 Tags ─────────────────────────────────────────────────────────
    stage_tags = []
    if authority >= 3:
        stage_tags.append('official-source')
    if authority >= 2:
        stage_tags.append('whitelisted')
    if d3 >= 15:
        stage_tags.append('high-engagement')
    if tier == 2:
        stage_tags.append('moltbook-only')
    if mc_count >= 1:
        stage_tags.append('cluster-anchor')
    # Legacy name: uses moltbot/clawdbot but not openclaw, within 90 days
    has_legacy_only = (
        any(b in text_lower for b in ['moltbot', 'clawdbot', 'claudbot'])
        and 'openclaw' not in text_lower
    )
    if has_legacy_only:
        stage_tags.append('legacy-name')
    if is_delist:
        stage_tags.append('promotional')

    return {
        'd1_score':   round(d1, 2),
        'd2_score':   round(d2, 2),
        'd3_score':   round(d3, 2),
        'd4_score':   round(d4, 2),
        'd5_score':   round(d5, 2),
        'total_score': total,
        'd1_tier':    tier,
        'stage_tags': stage_tags,
        'source_type': get_source_type(url, source),
    }


# --- 7. SUPABASE I/O ---

def _load_from_supabase() -> dict:
    """Load all existing data from Supabase at forge startup."""
    empty = {"items": [], "videos": [], "githubProjects": [], "research": []}
    if not _supabase:
        return empty
    try:
        news_resp     = _supabase.table('news_items').select('*').order('inserted_at', desc=True).limit(1500).execute()
        videos_resp   = _supabase.table('videos').select('*').limit(300).execute()
        research_resp = _supabase.table('research_papers').select('*').limit(100).execute()

        # Map DB snake_case → forge.py camelCase internals; add vec=None (not stored)
        items = []
        for row in (news_resp.data or []):
            items.append({
                'url':           row['url'],
                'title':         row.get('title', ''),
                'source':        row.get('source', ''),
                'date':          row.get('date', ''),
                'summary':       row.get('summary', ''),
                'density':       row.get('density', 0),
                'is_minor':      row.get('is_minor', False),
                'moreCoverage':  row.get('more_coverage', []) or [],
                'tags':          row.get('tags', []) or [],
                'date_is_manual': row.get('date_is_manual', False),
                'source_type':   row.get('source_type', 'standard'),
                'total_score':   row.get('total_score'),
                'd1_score':      row.get('d1_score'),
                'd2_score':      row.get('d2_score'),
                'd3_score':      row.get('d3_score'),
                'd4_score':      row.get('d4_score'),
                'd1_tier':       row.get('d1_tier'),
                'stage_tags':    row.get('stage_tags', []) or [],
                'hn_points':     row.get('hn_points'),
                'hn_comments':   row.get('hn_comments'),
                'd5_score':      row.get('d5_score'),
                'vec':           None,
            })

        videos = []
        for row in (videos_resp.data or []):
            videos.append({
                'url':         row['url'],
                'title':       row.get('title', ''),
                'thumbnail':   row.get('thumbnail', ''),
                'channel':     row.get('channel', ''),
                'description': row.get('description', ''),
                'publishedAt': row.get('published_at', ''),
            })

        research = []
        for row in (research_resp.data or []):
            research.append({
                'url':     row['url'],
                'title':   row.get('title', ''),
                'authors': row.get('authors', []) or [],
                'date':    row.get('date', ''),
                'summary': row.get('summary', ''),
            })

        print(f"📦 Loaded from Supabase: {len(items)} items, {len(videos)} videos, {len(research)} papers.")
        return {"items": items, "videos": videos, "githubProjects": [], "research": research}
    except Exception as e:
        print(f"⚠️  Supabase load failed: {e}")
        return empty


def _save_to_supabase(db: dict) -> None:
    """Upsert all data to Supabase. Only prunes stale items from the current dispatch date;
    articles from past dispatches are never deleted."""
    if not _supabase:
        print("⚠️  Supabase client not initialized — skipping DB write.")
        return
    try:
        # --- news_items ---
        # Re-fetch admin-locked dates immediately before writing to guard against
        # race conditions where the in-memory snapshot pre-dates an admin edit.
        try:
            manual_resp = _supabase.table('news_items').select('url,date').eq('date_is_manual', True).execute()
            manual_date_map = {r['url']: r['date'] for r in (manual_resp.data or [])}
        except Exception:
            manual_date_map = {}

        news_records = [{
            'url':           item['url'],
            'title':         item.get('title', ''),
            'source':        item.get('source', ''),
            'date':          manual_date_map.get(item['url'], item.get('date', '')),
            'summary':       item.get('summary', ''),
            'density':       item.get('density', 0),
            'is_minor':      item.get('is_minor', False),
            'more_coverage': item.get('moreCoverage', []),
            'tags':          item.get('tags', []),
            'date_is_manual': item.get('date_is_manual', False) or (item['url'] in manual_date_map),
            'source_type':   item.get('source_type', 'standard'),
            'total_score':   item.get('total_score'),
            'd1_score':      item.get('d1_score'),
            'd2_score':      item.get('d2_score'),
            'd3_score':      item.get('d3_score'),
            'd4_score':      item.get('d4_score'),
            'd1_tier':       item.get('d1_tier'),
            'stage_tags':    item.get('stage_tags', []),
            'hn_points':     item.get('hn_points'),
            'hn_comments':   item.get('hn_comments'),
            'd5_score':      item.get('d5_score'),
        } for item in db.get('items', [])]
        if news_records:
            _supabase.table('news_items').upsert(news_records).execute()
            print(f"✅ Upserted {len(news_records)} news items.")

        # Prune stale items from the CURRENT dispatch only.
        # Past dispatches are sealed once a new dispatch has begun — their articles
        # can never be removed, even if a partial load caused them to drop from the
        # in-memory list mid-run.
        if len(news_records) >= 5:
            try:
                keep_urls = {r['url'] for r in news_records}
                # Identify the current dispatch date (most recent date in the batch)
                current_dispatch_date = max(
                    (r['date'] for r in news_records if r.get('date')),
                    key=lambda d: try_parse_date(d),
                    default=None,
                )
                all_rows_resp = _supabase.table('news_items').select('url,date').execute()
                stale = [
                    r['url'] for r in (all_rows_resp.data or [])
                    if r['url'] not in keep_urls
                    and r.get('date') == current_dispatch_date  # only prune current dispatch
                ]
                for i in range(0, len(stale), 50):
                    _supabase.table('news_items').delete().in_('url', stale[i:i+50]).execute()
                if stale:
                    print(f"🗑️  Pruned {len(stale)} stale items from current dispatch ({current_dispatch_date}).")
            except Exception as prune_err:
                print(f"⚠️  Pruning failed (non-fatal): {prune_err}")

        # --- videos ---
        video_records = [{
            'url':          v['url'],
            'title':        v.get('title', ''),
            'thumbnail':    v.get('thumbnail', ''),
            'channel':      v.get('channel', ''),
            'description':  v.get('description', ''),
            'published_at': v.get('publishedAt', ''),
        } for v in db.get('videos', [])]
        if video_records:
            _supabase.table('videos').upsert(video_records).execute()
            print(f"✅ Upserted {len(video_records)} videos.")

        # --- github_projects ---
        project_records = [{
            'url':         p['url'],
            'name':        p.get('name', ''),
            'owner':       p.get('owner', ''),
            'description': p.get('description', ''),
            'stars':       p.get('stars', 0),
            'created_at':  p.get('created_at', ''),
            'language':    p.get('language', ''),
            'topics':      p.get('topics', []),
            'forks':       p.get('forks', 0),
            'license':     p.get('license', ''),
        } for p in db.get('githubProjects', [])]
        if project_records:
            _supabase.table('github_projects').upsert(project_records).execute()
            print(f"✅ Upserted {len(project_records)} GitHub projects.")

        # --- research_papers ---
        research_records = [{
            'url':     p['url'],
            'title':   p.get('title', ''),
            'authors': p.get('authors', []),
            'date':    p.get('date', ''),
            'summary': p.get('summary', ''),
        } for p in db.get('research', [])]
        if research_records:
            _supabase.table('research_papers').upsert(research_records).execute()
            print(f"✅ Upserted {len(research_records)} research papers.")

        # --- feed_metadata ---
        _supabase.table('feed_metadata').upsert({'id': 1, 'last_updated': db.get('last_updated', '')}).execute()

    except Exception as e:
        print(f"❌ Supabase save failed: {e}")


# --- 8. CLUSTERING & ARCHIVING ---

def cluster_articles_temporal(new_articles, existing_items):
    if not new_articles: return existing_items
    needs_embedding = [a for a in new_articles if a.get('vec') is None]
    if needs_embedding:
        texts = [f"{a['title']} {a['summary'][:120]}" for a in needs_embedding]
        new_vectors = get_embeddings_batch(texts)
        for i, art in enumerate(needs_embedding): art['vec'] = new_vectors[i]
    date_buckets = {}
    for art in new_articles:
        d = art['date']
        if d not in date_buckets: date_buckets[d] = []
        date_buckets[d].append(art)
    current_batch_clustered = []
    for date_key in date_buckets:
        day_articles = date_buckets[date_key]
        day_articles.sort(key=lambda x: x.get('density', 0), reverse=True)
        daily_clusters = []
        for art in day_articles:
            if art['vec'] is None: continue
            matched = False
            for cluster in daily_clusters:
                sim = cosine_similarity(np.array(art['vec']), np.array(cluster[0]['vec']))
                if sim > 0.82:
                    cluster.append(art); matched = True; break
            if not matched: daily_clusters.append([art])
        for cluster in daily_clusters:
            # Select the anchor as the highest-authority article; break ties by density.
            # Whitelist Publishers (authority=3) are always preferred over Creators/newsletters (2)
            # or unknown sources (1), ensuring the primary headline comes from a trusted news outlet.
            anchor = max(cluster, key=lambda a: (
                get_source_authority(a['url'], a['source']),
                a.get('density', 0)
            ))
            others = [a for a in cluster if a is not anchor]
            # Sort More Coverage: best-authority sources first, then by density
            others.sort(
                key=lambda a: (get_source_authority(a['url'], a['source']), a.get('density', 0)),
                reverse=True
            )
            anchor['is_minor'] = anchor.get('density', 0) < 8
            anchor['moreCoverage'] = [{"source": a['source'], "url": a['url']} for a in others]
            current_batch_clustered.append(anchor)
    seen_urls = {item['url'] for item in existing_items}
    unique_new = [a for a in current_batch_clustered if a['url'] not in seen_urls]
    final = unique_new + existing_items
    final.sort(key=lambda x: try_parse_date(x.get('date', '01-01-2000')), reverse=True)
    final = final[:1000]

    # Cleanup: if an article has become a top-level headline, remove it from any
    # other headline's moreCoverage list (handles cross-run promotion cases).
    headline_urls = {item['url'] for item in final}
    for item in final:
        if item.get('moreCoverage'):
            item['moreCoverage'] = [
                mc for mc in item['moreCoverage']
                if mc['url'] not in headline_urls
            ]

    return final

# --- 9. MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed...")
    db = _load_from_supabase()

    raw_news = scan_rss() + scan_google_news() + scan_hackernews()
    newly_discovered = []
    new_summaries_count = 0
    existing_urls = {item['url'] for item in db.get('items', [])}
    # Also exclude URLs already featured in moreCoverage — they're grouped under another headline
    for item in db.get('items', []):
        for mc in item.get('moreCoverage', []):
            existing_urls.add(mc['url'])
    for art in raw_news:
        if art['url'] in existing_urls: continue
        # Generate AI briefs for whitelist Publisher articles (authority=3) up to batch limit.
        # This covers all outlets in whitelist.json, not just the old hardcoded PRIORITY_SITES.
        if get_source_authority(art['url'], art['source']) >= 3 and new_summaries_count < MAX_BATCH_SIZE:
            print(f"✍️ Drafting brief: {art['title']}")
            art['summary'] = get_ai_summary(art['title'], art['summary'])
            new_summaries_count += 1; time.sleep(SLEEP_BETWEEN_REQUESTS)
        newly_discovered.append(art)

    # HN enrichment: articles already in the DB (found via RSS) may now appear
    # on HN with engagement data.  Back-fill hn_points/hn_comments so the next
    # score pass can incorporate them.  Only updates items whose HN data was
    # absent or has improved (higher points / more comments).
    hn_by_url = {
        a['url']: a for a in raw_news
        if a.get('hn_points') is not None
    }
    hn_enriched = 0
    for item in db.get('items', []):
        hn_hit = hn_by_url.get(item['url'])
        if not hn_hit:
            continue
        new_pts = hn_hit.get('hn_points', 0) or 0
        new_cmt = hn_hit.get('hn_comments', 0) or 0
        if new_pts > (item.get('hn_points') or 0) or new_cmt > (item.get('hn_comments') or 0):
            item['hn_points']   = new_pts
            item['hn_comments'] = new_cmt
            item['total_score'] = None  # invalidate score so re-scoring runs below
            hn_enriched += 1
    if hn_enriched:
        print(f"🔶 HN enriched {hn_enriched} existing articles.")

    db['items'] = cluster_articles_temporal(newly_discovered, db.get('items', []))

    # Tag backfill: extract named-entity tags for articles that don't have tags yet.
    # Uses spaCy NER (local, no API calls) so there are no rate limits — all
    # untagged articles are processed in a single pass.
    tags_generated = 0
    for item in db['items']:
        if not item.get('tags'):
            item['tags'] = get_nlp_tags(item['title'], item.get('summary', ''))
            tags_generated += 1
    if tags_generated:
        print(f"🏷️  Tagged {tags_generated} articles.")

    # Score pass: compute D1–D4 scores for all items that don't yet have a
    # total_score, or whose moreCoverage changed this run (anchor selection can
    # change D3 engagement score).  Runs fully locally — no API calls.
    scores_computed = 0
    for item in db['items']:
        # Re-score if: new article (no score yet) OR moreCoverage changed this run
        # We detect "changed this run" by checking if the item was in newly_discovered.
        is_new = item.get('total_score') is None
        if is_new:
            scores = compute_scores(item)
            item.update(scores)
            scores_computed += 1
    if scores_computed:
        print(f"📊 Scored {scores_computed} articles.")

    # Retry pass: articles whose Gemini call previously failed and were stored with the
    # fallback string will never be retried by the main loop (URL is already in existing_urls).
    # This sweep fixes them using whatever budget remains.
    if new_summaries_count < MAX_BATCH_SIZE:
        for item in db['items']:
            if new_summaries_count >= MAX_BATCH_SIZE:
                break
            if item.get('summary', '').strip() == 'Summary pending.':
                print(f"♻️ Retrying summary: {item['title']}")
                new_summary = get_ai_summary(item['title'], '')
                if new_summary != 'Summary pending.':
                    item['summary'] = new_summary
                    new_summaries_count += 1
                    time.sleep(SLEEP_BETWEEN_REQUESTS)

    if os.getenv("RUN_RESEARCH") == "true" or True:
        print("🔍 Scanning Research...")
        new_papers = fetch_arxiv_research()
        if new_papers: db['research'] = new_papers

    print("📺 Scanning Videos...")
    scanned_videos = []
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH, 'r') as f:
            for entry in json.load(f):
                yt_target = entry.get("YouTube URL") or entry.get("YouTube Channel ID")
                if yt_target:
                    if not yt_target.startswith('http'): yt_target = f"https://www.youtube.com/channel/{yt_target}"
                    scanned_videos.extend(fetch_youtube_videos_ytdlp(yt_target))

    global_videos = fetch_global_openclaw_videos(limit=30)
    all_new_videos = scanned_videos + global_videos
    vid_urls = {v['url'] for v in db.get('videos', [])}
    combined_vids = db.get('videos', []) + [v for v in all_new_videos if v['url'] not in vid_urls]

    # Flexible sorter fix
    combined_vids.sort(key=lambda x: try_parse_date(x.get('publishedAt', '01-01-2000')), reverse=True)
    db['videos'] = combined_vids[:200]

    db['githubProjects'] = fetch_github_projects()
    db['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    _save_to_supabase(db)
    print(f"✅ Success. Items in Feed: {len(db['items'])}")