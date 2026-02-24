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
from dotenv import load_dotenv, find_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from datetime import datetime, timedelta
from urllib.parse import urlparse
from newspaper import Article

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

CORE_BRANDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "claudbot", "steinberger"]
KEYWORDS = CORE_BRANDS 

WHITELIST_PATH = "./src/whitelist.json"
OUTPUT_PATH = "./public/data.json"

MAX_BATCH_SIZE = 50
SLEEP_BETWEEN_REQUESTS = 6.5

PRIORITY_SITES = ['substack.com', 'beehiiv.com', 'techcrunch.com', 'wired.com', 'theverge.com', 'venturebeat.com', '404media.co', 'pcgamer.com']
DELIST_SITES = ['prnewswire.com', 'businesswire.com', 'globenewswire.com']
BANNED_SOURCES = ["access newswire", "globenewswire", "prnewswire", "business wire"]

# --- 3. HELPER FUNCTIONS ---

def cosine_similarity(v1, v2):
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def get_source_type(url, source_name=""):
    url_lower = url.lower()
    source_lower = source_name.lower()
    if any(k in url_lower for k in DELIST_SITES) or any(k in source_lower for k in BANNED_SOURCES):
        return "delist"
    if any(k in url_lower for k in PRIORITY_SITES):
        return "priority"
    return "standard"

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
        response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
        return response.text.strip()
    except: return "Summary pending."

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
        if article.meta_lang != 'en' and article.meta_lang != '':
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
    for site in whitelist:
        rss_url = site.get("Website RSS")
        if not rss_url or rss_url == "N/A": continue
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:25]: # Increased to catch high-volume sites
                title = entry.get('title', '')
                passes, density, clean_text = process_article_intel(entry.link)
                # PRIORITY logic ensures brand in title always passes
                is_priority = any(brand.lower() in title.lower() for brand in CORE_BRANDS)
                if passes and (is_priority or density >= 1):
                    display_source = site["Source Name"]
                    if display_source == "Medium":
                        author_name = entry.get('author') or entry.get('author_detail', {}).get('name') or entry.get('dc_creator')
                        if author_name: display_source = f"{author_name}, Medium"
                    found.append({
                        "title": title, "url": entry.link, "source": display_source,
                        "date": datetime.now().strftime("%m-%d-%Y"), 
                        "summary": clean_text[:250] + "...", 
                        "density": density, "vec": None
                    })
        except: continue
    return found

def scan_google_news():
    query = "OpenClaw OR Moltbot OR Clawdbot OR Steinberger"
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
                        raw_date = entry.get('upload_date')
                        if raw_date and len(raw_date) == 8:
                            formatted_date = f"{raw_date[4:6]}-{raw_date[6:]}-{raw_date[:4]}"
                        else:
                            formatted_date = datetime.now().strftime("%m-%d-%Y")
                        videos.append({
                            "title": entry.get('title'),
                            "url": f"https://www.youtube.com/watch?v={entry['id']}",
                            "thumbnail": entry.get('thumbnails', [{}])[-1].get('url'),
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
                    raw_date = entry.get('upload_date')
                    if raw_date and len(raw_date) == 8:
                        formatted_date = f"{raw_date[4:6]}-{raw_date[6:]}-{raw_date[:4]}"
                    else:
                        formatted_date = datetime.now().strftime("%m-%d-%Y")
                    videos.append({
                        "title": entry.get('title') or "Untitled Video",
                        "url": f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "thumbnail": entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else "",
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
        resp = requests.get("https://api.github.com/search/repositories?q=OpenClaw&sort=updated&order=desc", headers=headers, timeout=10)
        items = resp.json().get('items', [])
        return [{"name": r['name'], "owner": r['owner']['login'], "description": r['description'] or "No description.", "url": r['html_url'], "stars": r['stargazers_count'], "created_at": r['created_at']} for r in items]
    except: return []

# --- 6. CLUSTERING & ARCHIVING ---

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
            anchor = cluster[0]
            anchor['is_minor'] = anchor.get('density', 0) < 8
            anchor['moreCoverage'] = [{"source": a['source'], "url": a['url']} for a in cluster[1:]]
            current_batch_clustered.append(anchor)
    seen_urls = {item['url'] for item in existing_items}
    unique_new = [a for a in current_batch_clustered if a['url'] not in seen_urls]
    final = unique_new + existing_items
    final.sort(key=lambda x: try_parse_date(x.get('date', '01-01-2000')), reverse=True)
    return final[:1000]

# --- 7. MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed...")
    try:
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f: db = json.load(f)
            for k in ["items", "videos", "githubProjects", "research"]:
                if k not in db: db[k] = []
        else: db = {"items": [], "videos": [], "githubProjects": [], "research": []}
    except Exception as e:
        db = {"items": [], "videos": [], "githubProjects": [], "research": []}

    raw_news = scan_rss() + scan_google_news()
    newly_discovered = []
    new_summaries_count = 0
    for art in raw_news:
        url_list = {item['url'] for item in db.get('items', [])}
        if art['url'] in url_list: continue
        if get_source_type(art['url'], art['source']) == "priority" and new_summaries_count < MAX_BATCH_SIZE:
            print(f"✍️ Drafting brief: {art['title']}")
            art['summary'] = get_ai_summary(art['title'], art['summary'])
            new_summaries_count += 1; time.sleep(SLEEP_BETWEEN_REQUESTS)
        newly_discovered.append(art)

    db['items'] = cluster_articles_temporal(newly_discovered, db.get('items', []))

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
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False, cls=CompactJSONEncoder)
    print(f"✅ Success. Items in Feed: {len(db['items'])}")