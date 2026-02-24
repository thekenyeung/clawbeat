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
KEYWORDS = CORE_BRANDS + ["openclaw foundation", "ai safety", "ai agent", "autonomous agents", "large language models"]

WHITELIST_PATH = "./src/whitelist.json"
OUTPUT_PATH = "./public/data.json"

MAX_BATCH_SIZE = 50
SLEEP_BETWEEN_REQUESTS = 6.5

PRIORITY_SITES = ['substack.com', 'beehiiv.com', 'techcrunch.com', 'wired.com', 'theverge.com', 'venturebeat.com', '404media.co', 'pcgamer.com']
DELIST_SITES = ['prnewswire.com', 'businesswire.com', 'globenewswire.com']
BANNED_SOURCES = ["access newswire", "globenewswire", "prnewswire", "business wire"]

# --- 3. HELPER FUNCTIONS ---

def normalize_source_name(name):
    return name.lower().replace('the ', '').replace('.com', '').replace('.net', '').strip()

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

def process_article_intel(url):
    """
    Downloads article once to check both Recency AND Density.
    Returns (is_recent, density_score, full_text_summary)
    """
    try:
        article = Article(url)
        article.download()
        article.parse()
        
        # 1. Recency Gate (48 Hours)
        is_recent = True
        if article.publish_date:
            now = datetime.now(article.publish_date.tzinfo) if article.publish_date.tzinfo else datetime.now()
            if (now - article.publish_date).total_seconds() > 172800:
                is_recent = False
        
        # 2. Density Scoring
        full_text = (article.title + " " + article.text).lower()
        brand_bonus = 10 if any(b in full_text for b in CORE_BRANDS) else 0
        keyword_matches = sum(1 for kw in KEYWORDS if kw.lower() in full_text)
        density_score = keyword_matches + brand_bonus
        
        return is_recent, density_score, article.text[:300]
    except:
        return False, 0, ""

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
                # Quick preliminary check before heavy download
                title_summary = (entry.get('title', '') + entry.get('summary', '')).lower()
                is_likely_match = any(kw in title_summary for kw in KEYWORDS)
                
                # The Gatekeeper: Verify Recency and Density via Deep Scan
                is_recent, density, full_text = process_article_intel(entry.link)
                
                if is_recent and (is_likely_match or density >= 3):
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
            is_recent, density, _ = process_article_intel(e.link)
            if is_recent and density >= 2: # Lower bar for search discovery
                found.append({
                    "title": e.title, "url": e.link, "source": "Web Search", 
                    "summary": "Ecosystem update.", "date": datetime.now().strftime("%m-%d-%Y"), 
                    "density": density, "vec": None
                })
    except: pass
    return found

# --- 5. CLUSTERING & ARCHIVING ---

def cluster_articles_temporal(new_articles, existing_items):
    """
    Operates in 'Isolated World Mode'. 
    Clusters ONLY new items by date and then merges with history.
    """
    if not new_articles: return existing_items

    # 1. Get embeddings for new items
    needs_embedding = [a for a in new_articles if a.get('vec') is None]
    if needs_embedding:
        texts = [f"{a['title']}: {a['summary'][:100]}" for a in needs_embedding]
        new_vectors = get_embeddings_batch(texts)
        for i, art in enumerate(needs_embedding): art['vec'] = new_vectors[i]
    
    # 2. Cluster by Date (Ensures articles don't jump dispatches)
    date_buckets = {}
    for art in new_articles:
        d = art['date']
        if d not in date_buckets: date_buckets[d] = []
        date_buckets[d].append(art)
    
    current_batch_clustered = []
    for date_key in date_buckets:
        day_articles = date_buckets[date_key]
        # Sort by density so highest-intel story becomes the 'Anchor' (Headline)
        day_articles.sort(key=lambda x: x.get('density', 0), reverse=True)
        
        daily_clusters = []
        for art in day_articles:
            if art['vec'] is None: continue
            matched = False
            for cluster in daily_clusters:
                sim = cosine_similarity(np.array(art['vec']), np.array(cluster[0]['vec']))
                if sim > 0.75: # Strict threshold to keep headlines distinct
                    cluster.append(art)
                    matched = True
                    break
            if not matched: daily_clusters.append([art])
        
        for cluster in daily_clusters:
            anchor = cluster[0]
            anchor['moreCoverage'] = [{"source": a['source'], "url": a['url']} for a in cluster[1:]]
            current_batch_clustered.append(anchor)

    # 3. MERGE (Additive: New on top of Old)
    # Filter to prevent URL duplicates in the history
    existing_urls = {item['url'] for item in existing_items}
    final_news = [a for a in current_batch_clustered if a['url'] not in existing_urls] + existing_items
    
    # Final sort by date descending
    final_news.sort(key=lambda x: datetime.strptime(x['date'], "%m-%d-%Y"), reverse=True)
    return final_news[:1000] # Maintain a deep rolling window

# --- 6. MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed (Isolated Dispatch Mode)...")
    
    # Load Existing Data (Additive Database)
    try:
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                db = json.load(f)
        else:
            db = {"items": [], "videos": [], "githubProjects": [], "research": []}
    except:
        db = {"items": [], "videos": [], "githubProjects": [], "research": []}

    # Step 1: Discover New Content (Recency Gate applied inside scan functions)
    new_discovered = scan_rss() + scan_google_news()
    
    # Step 2: AI Summaries for Priority Sources
    new_summaries_count = 0
    for art in new_discovered:
        if get_source_type(art['url'], art['source']) == "priority" and new_summaries_count < MAX_BATCH_SIZE:
            print(f"✍️ Drafting brief: {art['title']}")
            art['summary'] = get_ai_summary(art['title'], art['summary'])
            new_summaries_count += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    # Step 3: Cluster New Items & Append to History (The Temporal Wall)
    db['items'] = cluster_articles_temporal(new_discovered, db.get('items', []))

    # Step 4: Social & Research Backfills
    if os.getenv("RUN_RESEARCH") == "true":
        db['research'] = fetch_arxiv_research() # Research is usually small enough to refresh

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

    # Save
    db['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False, cls=CompactJSONEncoder)
        
    print(f"✅ Success. Items in Feed: {len(db['items'])}")