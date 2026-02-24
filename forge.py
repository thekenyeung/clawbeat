import feedparser
import requests
import json
import re
import os
import time
import numpy as np
import sys
from dotenv import load_dotenv, find_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from urllib.parse import urlparse
from newspaper import Article

# --- 1. COMPACT ENCODER ---
class CompactJSONEncoder(json.JSONEncoder):
    """A JSON Encoder that puts small lists (like vectors) on single lines."""
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

youtube = build('youtube', 'v3', developerKey=GEMINI_KEY)
client = genai.Client(api_key=GEMINI_KEY)

# Core brand keywords for high-priority matching and density bonuses
CORE_BRANDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "claudbot", "steinberger"]
KEYWORDS = CORE_BRANDS + ["openclaw foundation", "openclaw safety", "openclaw agent", "moltbot capabilities", "openclaw ecosystem", "clawdbot updates", "openclaw updates", "openclaw openai"]

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

def is_article_recent(url):
    """THE GATEKEEPER: Applies the 48-hour discovery window."""
    try:
        article = Article(url)
        article.download()
        article.parse()
        if article.publish_date:
            now = datetime.now(article.publish_date.tzinfo) if article.publish_date.tzinfo else datetime.now()
            diff = now - article.publish_date
            return diff.total_seconds() <= 172800 # 48 hours
        return True 
    except: return False

def process_article_intel(url):
    """Extracts text and calculates density score for relevance filtering."""
    try:
        article = Article(url)
        article.download()
        article.parse()
        full_text = (article.title + " " + article.text).lower()
        
        brand_bonus = 10 if any(b in full_text for b in CORE_BRANDS) else 0
        keyword_matches = sum(1 for kw in KEYWORDS if kw.lower() in full_text)
        
        return keyword_matches + brand_bonus, article.text[:300]
    except: return 0, ""

def scan_rss():
    if not os.path.exists(WHITELIST_PATH): return []
    with open(WHITELIST_PATH, 'r') as f: whitelist = json.load(f)
    found = []
    for site in whitelist:
        rss_url = site.get("Website RSS")
        if not rss_url or rss_url == "N/A": continue
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:20]:
                density, _ = process_article_intel(entry.link)
                if density >= 3:
                    display_source = site["Source Name"]
                    if display_source == "Medium":
                        author_name = entry.get('author') or entry.get('author_detail', {}).get('name') or entry.get('dc_creator')
                        if author_name: display_source = f"{author_name}, Medium"

                    found.append({
                        "title": entry.get('title', ''), "url": entry.link, "source": display_source,
                        "date": datetime.now().strftime("%m-%d-%Y"), 
                        "summary": entry.get('summary', '')[:200], "density": density, "vec": None
                    })
        except: continue
    return found

def scan_google_news():
    query = "OpenClaw OR Moltbot OR Clawdbot"
    gn_url = f"https://news.google.com/rss/search?q={query}+when:48h&hl=en-US&gl=US&ceid=US:en"
    found = []
    try:
        feed = feedparser.parse(gn_url)
        for e in feed.entries[:30]:
            density, _ = process_article_intel(e.link)
            if density >= 2:
                found.append({
                    "title": e.title, "url": e.link, "source": "Web Search", 
                    "summary": "Ecosystem update.", "date": datetime.now().strftime("%m-%d-%Y"), 
                    "density": density, "vec": None
                })
    except: pass
    return found

# --- 5. BACKFILL FETCHERS ---

def fetch_arxiv_research():
    query = '(OpenClaw OR MoltBot OR Clawdbot)'
    arxiv_url = f"http://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=10"
    try:
        feed = feedparser.parse(arxiv_url)
        papers = []
        for entry in feed.entries:
            arxiv_id = entry.id.split('/abs/')[-1]
            ss_url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{arxiv_id}?fields=tldr,abstract"
            summary = "Research analysis in progress."
            try:
                ss_resp = requests.get(ss_url, timeout=5).json()
                if ss_resp.get('tldr'): summary = ss_resp['tldr']['text']
                elif ss_resp.get('abstract'):
                    abstract = ss_resp['abstract'].replace('\n', ' ')
                    summary = '. '.join(abstract.split('. ')[:2]) + '.'
            except: pass
            papers.append({
                "title": entry.title.replace('\n', ' ').strip(),
                "authors": [a.name for a in entry.authors],
                "date": entry.published, "url": entry.link, "summary": summary
            })
        return papers
    except: return []

def fetch_youtube_videos(channel_id):
    try:
        ch_resp = youtube.channels().list(id=channel_id, part='contentDetails').execute()
        uploads_id = ch_resp['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        pl_resp = youtube.playlistItems().list(playlistId=uploads_id, part='snippet', maxResults=5).execute()
        videos = []
        for item in pl_resp.get('items', []):
            snip = item['snippet']
            if any(kw in (snip['title'] + snip['description']).lower() for kw in KEYWORDS):
                videos.append({
                    "title": snip['title'], "url": f"https://www.youtube.com/watch?v={snip['resourceId']['videoId']}",
                    "thumbnail": snip['thumbnails']['high']['url'], "channel": snip['channelTitle'],
                    "description": snip['description'][:150], "publishedAt": snip['publishedAt']
                })
        return videos
    except: return []

def fetch_github_projects():
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token: headers["Authorization"] = f"token {token}"
    try:
        resp = requests.get("https://api.github.com/search/repositories?q=OpenClaw&sort=updated&order=desc", headers=headers, timeout=10)
        items = resp.json().get('items', [])
        return [{"name": repo['name'], "owner": repo['owner']['login'], "description": repo['description'] or "No description.", "url": repo['html_url'], "stars": repo['stargazers_count'], "created_at": repo['created_at']} for repo in items]
    except Exception as e:
        print(f"⚠️ GitHub Fetch Failed: {e}")
        return []

# --- 6. CLUSTERING & ARCHIVING ---

def cluster_articles_temporal(new_articles, existing_items):
    if not new_articles: return existing_items
    
    # 1. Standard Embedding logic
    needs_embedding = [a for a in new_articles if a.get('vec') is None]
    if needs_embedding:
        texts = [f"{a['title']}: {a['summary'][:100]}" for a in needs_embedding]
        new_vectors = get_embeddings_batch(texts)
        for i, art in enumerate(needs_embedding): art['vec'] = new_vectors[i]
    
    date_buckets = {}
    for art in new_articles:
        d = art['date']
        if d not in date_buckets: date_buckets[d] = []
        date_buckets[d].append(art)
    
    current_batch_clustered = []
    HEADLINE_THRESH = 8  # Articles below this score won't become "Anchors"

    for date_key in date_buckets:
        day_articles = date_buckets[date_key]
        day_articles.sort(key=lambda x: x.get('density', 0), reverse=True)
        
        daily_clusters = []
        for art in day_articles:
            if art['vec'] is None: continue
            matched = False
            for cluster in daily_clusters:
                sim = cosine_similarity(np.array(art['vec']), np.array(cluster[0]['vec']))
                if sim > 0.75:
                    cluster.append(art)
                    matched = True
                    break
            if not matched: daily_clusters.append([art])
            
        for cluster in daily_clusters:
            anchor = cluster[0]
            
            # --- TIGHTENED LOGIC ---
            if anchor.get('density', 0) < HEADLINE_THRESH:
                anchor['is_minor'] = True 
            else:
                anchor['is_minor'] = False

            anchor['moreCoverage'] = [{"source": a['source'], "url": a['url']} for a in cluster[1:]]
            current_batch_clustered.append(anchor)

    # Merge and archive logic
    existing_urls = {item['url'] for item in existing_items}
    final_news = [a for a in current_batch_clustered if a['url'] not in existing_urls] + existing_items
    final_news.sort(key=lambda x: datetime.strptime(x['date'], "%m-%d-%Y"), reverse=True)
    return final_news[:1000]

# --- 7. MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed (Recency Discovery + Additive Archiving)...")
    
    try:
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                db = json.load(f)
            for key in ["items", "videos", "githubProjects", "research"]:
                if key not in db: db[key] = []
        else:
            db = {"items": [], "videos": [], "githubProjects": [], "research": []}
    except:
        db = {"items": [], "videos": [], "githubProjects": [], "research": []}
        
    existing_news_urls = {item['url'] for item in db.get('items', [])}

    # 2. FIND: Only look for NEW news published < 48h ago
    raw_news = scan_rss() + scan_google_news()
    newly_discovered = []
    new_summaries_count = 0

    for art in raw_news:
        if art['url'] in existing_news_urls: 
            continue
        
        # APPLY THE RECENCY FILTER HERE
        if not is_article_recent(art['url']): 
            continue

        # --- THE NOISE KILLER LOGIC ---
        # 1. Is this a core brand story? (High Priority)
        is_priority = any(brand.lower() in art['title'].lower() for brand in CORE_BRANDS)
        
        # 2. Does it pass the tightened gate? (Density 5 for general news)
        if art.get('density', 0) >= 5 or is_priority:
            
            if get_source_type(art['url'], art['source']) == "priority" and new_summaries_count < MAX_BATCH_SIZE:
                print(f"✍️ Drafting brief: {art['title']}")
                art['summary'] = get_ai_summary(art['title'], art['summary'])
                new_summaries_count += 1
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            
            newly_discovered.append(art)

    # 3. FORGE: Cluster only the NEW items (Passing existing db items for context)
    db['items'] = cluster_articles_temporal(newly_discovered, db.get('items', []))

    # 4. BACKFILLS (Research, YouTube, GitHub)
    if os.getenv("RUN_RESEARCH") == "true":
        print("🔍 Scanning Research...")
        db['research'] = fetch_arxiv_research()

    print("📺 Scanning Videos...")
    scanned_videos = []
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH, 'r') as f:
            for entry in json.load(f):
                yt_id = entry.get("YouTube Channel ID")
                if yt_id: scanned_videos.extend(fetch_youtube_videos(yt_id))
    
    vid_urls = {v['url'] for v in db.get('videos', [])}
    db['videos'] = db.get('videos', []) + [v for v in scanned_videos if v['url'] not in vid_urls]

    print("💻 Scanning GitHub...")
    new_repos = fetch_github_projects()
    repo_urls = {r['url'] for r in db.get('githubProjects', [])}
    db['githubProjects'] = db.get('githubProjects', []) + [r for r in new_repos if r['url'] not in repo_urls]

    # 5. SAVE: Permanent update
    db['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False, cls=CompactJSONEncoder)
        
    print(f"✅ Success. Items in Feed: {len(db['items'])}")