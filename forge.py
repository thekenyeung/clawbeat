import feedparser
import requests
import json
import re
import os
import time
import numpy as np
from dotenv import load_dotenv, find_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types 
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from urllib.parse import urlparse

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

KEYWORDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "steinberger", "claudbot", "openclaw foundation"]
WHITELIST_PATH = "./src/whitelist.json"
OUTPUT_PATH = "./public/data.json"

MAX_BATCH_SIZE = 50
SLEEP_BETWEEN_REQUESTS = 6.5

PRIORITY_SITES = ['substack.com', 'beehiiv.com', 'techcrunch.com', 'wired.com', 'theverge.com', 'venturebeat.com']
DELIST_SITES = ['prnewswire.com', 'businesswire.com', 'globenewswire.com']
BANNED_SOURCES = ["access newswire", "globenewswire", "prnewswire", "business wire"]

# --- 3. HELPER FUNCTIONS ---

def normalize_source_name(name):
    return name.lower().replace('the ', '').replace('.com', '').replace('.net', '').strip()

def normalize_title(title):
    title = re.sub(r'[^\w\s]', '', title.lower())
    stop_words = {'a', 'the', 'is', 'at', 'on', 'by', 'for', 'in', 'of', 'and'}
    return " ".join([word for word in title.split() if word not in stop_words])

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

# --- 4. DATA FETCHING ---

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

def scan_rss():
    if not os.path.exists(WHITELIST_PATH): return []
    with open(WHITELIST_PATH, 'r') as f: whitelist = json.load(f)
    found = []
    for site in whitelist:
        rss_url = site.get("Website RSS")
        if not rss_url or rss_url == "N/A": continue
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:15]:
                title = entry.get('title', '')
                summary = BeautifulSoup(entry.get('summary', ''), "html.parser").get_text(strip=True)
                if any(kw in (title + summary).lower() for kw in KEYWORDS):
                    found.append({
                        "title": title, "url": entry.link, "source": site["Source Name"],
                        "date": datetime.now().strftime("%m-%d-%Y"), "summary": summary[:200] + "...", "vec": None
                    })
        except: continue
    return found

def scan_google_news():
    query = "OpenClaw OR Moltbot OR Clawdbot"
    gn_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        feed = feedparser.parse(gn_url)
        return [{"title": e.title, "url": e.link, "source": "Web Search", "summary": "Ecosystem update.", "date": datetime.now().strftime("%m-%d-%Y"), "vec": None} for e in feed.entries[:50]]
    except: return []

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
                    "title": snip['title'], "url": f"https://www.youtube.com/watch?{snip['resourceId']['videoId']}",
                    "thumbnail": snip['thumbnails']['high']['url'], "channel": snip['channelTitle'],
                    "description": snip['description'][:150], "publishedAt": snip['publishedAt']
                })
        return videos
    except: return []

def cluster_articles_semantic(all_articles):
    if not all_articles: return []
    needs_embedding = [a for a in all_articles if a.get('vec') is None]
    if needs_embedding:
        new_vectors = get_embeddings_batch([a['title'] for a in needs_embedding])
        for i, art in enumerate(needs_embedding): art['vec'] = new_vectors[i]
    
    valid = [a for a in all_articles if a.get('vec') is not None]
    clusters = []
    for art in valid:
        matched = False
        for cluster in clusters:
            sim = cosine_similarity(np.array(art['vec']), np.array(cluster[0]['vec']))
            if sim > 0.85:
                cluster.append(art)
                matched = True
                break
        if not matched: clusters.append([art])
    
    final_topics = []
    for cluster in clusters:
        anchor = cluster[0]
        anchor['moreCoverage'] = [{"source": a['source'], "url": a['url']} for a in cluster[1:]]
        final_topics.append(anchor)
    return final_topics

# --- 5. MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed...")
    
    # LOAD EXISTING DATABASE
    try:
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                db = json.load(f)
            # EMERGENCY CHECK: Ensure all expected keys exist even in old JSON files
            for key in ["items", "videos", "githubProjects", "research"]:
                if key not in db:
                    db[key] = []
        else:
            db = {"items": [], "videos": [], "githubProjects": [], "research": []}
    except:
        db = {"items": [], "videos": [], "githubProjects": [], "research": []}

    # FETCH NEW NEWS
    new_found_news = scan_rss() + scan_google_news()
    existing_urls = {item['url'] for item in db.get('items', [])}
    unique_new_news = []
    new_summaries_count = 0

    for art in new_found_news:
        if art['url'] not in existing_urls:
            art['source_type'] = get_source_type(art['url'], art.get('source', ''))
            if art['source_type'] == "delist": continue
            
            # AI Brief Generation
            if art['source_type'] == "priority" and new_summaries_count < MAX_BATCH_SIZE:
                art['summary'] = get_ai_summary(art['title'], art['summary'])
                new_summaries_count += 1
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            unique_new_news.append(art)

    # MERGE & FILTER NEWS
    combined_news = db.get('items', []) + unique_new_news
    now = datetime.now()
    threshold = now - timedelta(hours=48)
    priority_keywords = ['openclaw', 'moltbot', 'clawdbot', 'moltbook', 'claudbot']

    final_news = []
    for item in combined_news:
        is_priority = any(k in item['title'].lower() or k in item.get('summary', '').lower() for k in priority_keywords)
        try:
            item_date = datetime.strptime(item['date'], "%m-%d-%Y")
        except:
            item_date = now

        if is_priority or item_date > threshold:
            final_news.append(item)

    # RESEARCH ADDITIVE LOGIC
    if os.getenv("RUN_RESEARCH") == "true":
        print("🔍 Scanning Research...")
        new_papers = fetch_arxiv_research()
        
        # SAFETY: Only update if we actually found something
        if new_papers:
            res_urls = {p['url'] for p in db.get('research', [])}
            db['research'] = db.get('research', []) + [p for p in new_papers if p['url'] not in res_urls]
            print(f"📈 Added {len(new_papers)} new papers.")
        else:
            print("⚠️ ArXiv returned 0 results. Keeping existing research data.")

    # UPDATE OTHER CATEGORIES
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH, 'r') as f:
            for entry in json.load(f):
                yt_id = entry.get("YouTube Channel ID")
                if yt_id: db['videos'] = fetch_youtube_videos(yt_id)

    db['items'] = cluster_articles_semantic(final_news)[:1000]
    db['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    # SAVE WITH COMPACT ENCODER
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False, cls=CompactJSONEncoder)
        
    print(f"✅ Success. Total news: {len(db['items'])}. Research: {len(db['research'])}")