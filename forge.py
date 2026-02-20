import feedparser
import requests
import json
import re
import os
import time
import numpy as np
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types 
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv, find_dotenv
from googleapiclient.discovery import build
from urllib.parse import urlparse

# 1. SETUP & KEY LOADING
load_dotenv(find_dotenv(), override=True)
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip().replace('"', '').replace("'", "")

if not GEMINI_KEY:
    print("❌ ERROR: GEMINI_API_KEY not found in .env file.")
    exit(1)

# 2. INITIALIZE CLIENTS
youtube = build('youtube', 'v3', developerKey=GEMINI_KEY)
client = genai.Client(api_key=GEMINI_KEY)

# CONFIGURATION
KEYWORDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "steinberger", "claudbot", "openclaw foundation"]
WHITELIST_PATH = "./src/whitelist.json"
OUTPUT_PATH = "./public/data.json"

# --- RATE LIMIT CONFIGURATION ---
RPM_LIMIT = 10 
SLEEP_BETWEEN_REQUESTS = 6.5 # Slightly over 6s to be safe for 10 RPM
MAX_BATCH_SIZE = 50 # Limit how many new summaries we generate per run to save daily quota

# --- NEW PRIORITY CONFIGURATION ---
PRIORITY_SITES = [
    'substack.com', 'beehiiv.com', 'ghost.io', 'medium.com', 'tldr.tech', 
    'stratechery.com', 'newcomer.co', 'theinformation.com', 'platformer.news',
    'theverge.com', 'wired.com', 'techcrunch.com', 'venturebeat.com', 
    'arstechnica.com', 'engadget.com', 'gizmodo.com', 'thenextweb.com',
    'mashable.com', 'recode.net', 'zdnet.com', 'cnet.com', 'pcmag.com',
    'technologyreview.com', 'spectrum.ieee.org', 'theintercept.com', 
    'restofworld.org', 'theregister.com', 'quantamagazine.org',
    'wsj.com', 'nytimes.com', 'bloomberg.com', 'ft.com', 'forbes.com', 
    'fastcompany.com', 'businessinsider.com', 'economist.com'
]

DELIST_SITES = [
    'prnewswire.com', 'businesswire.com', 'globenewswire.com', 
    'accesswire.com', 'einpresswire.com', 'prweb.com', 'newswire.com',
    'prlog.org', 'prowly.com', 'issiswire.com', 'send2press.com',
    '24-7pressrelease.com', 'pressat.co.uk', 'marketwired.com'
]

# --- FUNCTIONS ---

def get_source_type(url):
    """Determines if a source is high-priority, standard, or should be delisted."""
    url_lower = url.lower()
    if any(k in url_lower for k in DELIST_SITES):
        return "delist"
    if any(k in url_lower for k in PRIORITY_SITES):
        return "priority"
    return "standard"

def fetch_youtube_videos(channel_id):
    try:
        ch_resp = youtube.channels().list(id=channel_id, part='contentDetails').execute()
        uploads_id = ch_resp['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        pl_resp = youtube.playlistItems().list(playlistId=uploads_id, part='snippet', maxResults=10).execute()

        videos = []
        for item in pl_resp.get('items', []):
            snippet = item['snippet']
            title = snippet.get('title', '')
            description = snippet.get('description', '')
            video_id = snippet['resourceId']['videoId']
            
            if any(kw in (title + description).lower() for kw in KEYWORDS):
                videos.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "thumbnail": snippet['thumbnails']['high']['url'] if 'thumbnails' in snippet else None,
                    "channel": snippet['channelTitle'],
                    "description": description[:150] + "...", 
                    "publishedAt": snippet['publishedAt'],
                    "isPriority": False
                })
        return videos[:3]
    except Exception as e:
        print(f"⚠️ YouTube Fetch Failed for {channel_id}: {e}")
        return []

def get_embeddings_batch(texts, batch_size=5):
    if not texts: return []
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            print(f"📡 Embedding {len(batch)} items (Progress: {i}/{len(texts)})...")
            result = client.models.embed_content(
                model="models/gemini-embedding-001", 
                contents=batch,
                config=types.EmbedContentConfig(task_type="CLUSTERING")
            )
            all_embeddings.extend([e.values for e in result.embeddings])
            if i + batch_size < len(texts):
                time.sleep(12) 
        except Exception as e:
            print(f"❌ Batch failed: {e}")
            all_embeddings.extend([None] * len(batch))
    return all_embeddings

def cosine_similarity(v1, v2):
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def extract_real_source(entry, default_source):
    if "flipboard" not in default_source.lower():
        return default_source
    link = entry.get('link', '')
    if link:
        domain = urlparse(link).netloc.replace('www.', '')
        domain_map = {
            "theverge.com": "The Verge",
            "techcrunch.com": "TechCrunch",
            "venturebeat.com": "VentureBeat",
            "wired.com": "Wired",
            "nytimes.com": "NY Times",
            "arstechnica.com": "Ars Technica",
            "bloomberg.com": "Bloomberg",
            "wsj.com": "WSJ",
            "reuters.com": "Reuters"
        }
        if domain in domain_map:
            return domain_map[domain]
        return domain.split('.')[0].capitalize()
    return default_source

def scan_rss():
    if not os.path.exists(WHITELIST_PATH): return []
    with open(WHITELIST_PATH, 'r') as f:
        whitelist = json.load(f)
    headers = {'User-Agent': 'Mozilla/5.0'}
    found_articles = []
    for site in whitelist:
        rss_url = site.get("Website RSS")
        if not rss_url or rss_url == "N/A": continue
        try:
            resp = requests.get(rss_url, headers=headers, timeout=10)
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:20]:
                raw_title = entry.get('title', '')
                source = extract_real_source(entry, site["Source Name"])
                display_title = raw_title.split(":", 1)[1].strip() if ":" in raw_title and "flipboard" in site["Source Name"].lower() else raw_title
                summary = entry.get('summary', '') or entry.get('description', '')
                clean_summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)
                url = entry.link
                
                if any(kw in (display_title + clean_summary).lower() for kw in KEYWORDS):
                    found_articles.append({
                        "title": display_title,
                        "url": url,
                        "source": source,
                        "date": datetime.now().strftime("%m-%d-%Y"),
                        "summary": clean_summary[:200] + "...",
                        "vec": None,
                        "source_type": get_source_type(url)
                    })
        except: continue
    return found_articles

def scan_google_news(query="OpenClaw OR 'Moltbot' OR 'Clawdbot' OR 'Moltbook' OR 'Steinberger'"):
    import urllib.parse
    encoded_query = urllib.parse.quote(query)
    gn_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(gn_url, timeout=10)
        feed = feedparser.parse(resp.content)
        wild_articles = []
        for entry in feed.entries[:250]:
            raw_title = entry.title
            clean_title = " - ".join(raw_title.split(" - ")[:-1]) if " - " in raw_title else raw_title
            soup = BeautifulSoup(entry.get('summary', ''), "html.parser")
            clean_summary = soup.get_text(separator=' ', strip=True).split("View Full Coverage")[0].strip()
            url = entry.link.split("&url=")[-1] if "&url=" in entry.link else entry.link
            
            wild_articles.append({
                "title": clean_title,
                "url": url,
                "source": entry.source.get('title', 'web search').lower(),
                "summary": clean_summary[:250] + "..." if len(clean_summary) > 20 else "Ecosystem update.",
                "date": datetime.now().strftime("%m-%d-%Y"),
                "vec": None,
                "source_type": get_source_type(url)
            })
        return wild_articles
    except: return []

def get_ai_summary(title, current_summary):
    """
    Uses Gemini with retries and backoff to handle 503 errors.
    """
    # Skip if we already have a long, professional summary
    if current_summary and len(current_summary) > 100 and "Summary pending" not in current_summary:
        return current_summary

    prompt = (
        f"Rewrite this as a professional 1-sentence tech intel brief. "
        f"Focus on the impact. Title: {title}. Raw Context: {current_summary}. "
        f"Output ONLY the sentence."
    )
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
            return response.text.strip()
        except Exception as e:
            wait_time = (attempt + 1) * 10
            print(f"⚠️ Limit hit. Waiting {wait_time}s... (Error: {e})")
            time.sleep(wait_time)
            
    return "Summary pending update." # Fallback for local storage

def cluster_articles_semantic(all_articles):
    if not all_articles: return []
    needs_embedding = [a for a in all_articles if a.get('vec') is None]
    if needs_embedding:
        print(f"🧠 Embedding {len(needs_embedding)} items...")
        new_vectors = get_embeddings_batch([a['title'] for a in needs_embedding])
        for i, art in enumerate(needs_embedding):
            art['vec'] = new_vectors[i]
    valid_articles = [a for a in all_articles if a.get('vec') is not None]
    clusters = []
    for art in valid_articles:
        matched = False
        for cluster in clusters:
            if cosine_similarity(np.array(art['vec']), np.array(cluster[0]['vec'])) > 0.85:
                cluster.append(art)
                matched = True
                break
        if not matched: clusters.append([art])
    final_topics = []
    for cluster in clusters:
        anchor = cluster[0]
        unique_coverage = []
        seen_urls = {anchor['url']}
        for a in cluster[1:]:
            if a['url'] not in seen_urls:
                unique_coverage.append({"source": a['source'], "url": a['url']})
                seen_urls.add(a['url'])
        anchor['moreCoverage'] = unique_coverage
        final_topics.append(anchor)
    return final_topics

def fetch_github_projects():
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token: headers["Authorization"] = f"token {token}"
    query = "OpenClaw"
    url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=100"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        return [{"name": repo['name'], "owner": repo['owner']['login'], "description": repo['description'] or "No description.", "url": repo['html_url'], "stars": repo['stargazers_count'], "created_at": repo['created_at']} for repo in data.get('items', [])]
    except: return []

# --- SINGLE MAIN EXECUTION ---
if __name__ == "__main__":
    print(f"🛠️ Forging Intel Feed (Batch Limit: {MAX_BATCH_SIZE}, Throttle: {RPM_LIMIT} RPM)...")
    
    # 1. LOAD HISTORY
    existing_news = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                existing_news = json.load(f).get('items', [])
            print(f"📂 Loaded {len(existing_news)} historical items.")
        except: pass

    # 2. SCRAPE IN PRIORITY ORDER
    whitelist_articles = scan_rss() 
    discovery_articles = scan_google_news() 
    
    # 3. MERGE, DEDUPLICATE, & FILTER
    all_found = whitelist_articles + discovery_articles + existing_news
    seen_urls = set()
    unique_news = []
    new_summaries_count = 0

    for art in all_found:
        if art['url'] not in seen_urls:
            # Ensure even old items have a source_type
            if 'source_type' not in art:
                art['source_type'] = get_source_type(art['url'])
            
            # DELIST FILTER: Drop press releases immediately
            if art['source_type'] == "delist":
                continue

            # RATE LIMITED SUMMARY GENERATION
            # Check if summary is missing, too short, or a previous failure marker
            is_placeholder = (
                len(art.get('summary', '')) < 65 or 
                "Summary pending" in art.get('summary', '') or
                "upgrade failed" in art.get('summary', '').lower()
            )

            if is_placeholder and new_summaries_count < MAX_BATCH_SIZE:
                print(f"✍️ ({new_summaries_count+1}/{MAX_BATCH_SIZE}) Drafting brief: {art['title']}")
                art['summary'] = get_ai_summary(art['title'], art['summary'])
                new_summaries_count += 1
                
                # Critical Sleep to stay under 10 RPM
                time.sleep(SLEEP_BETWEEN_REQUESTS) 
            
            unique_news.append(art)
            seen_urls.add(art['url'])

    # 4. CLUSTER & DATA FETCHING
    clustered_news = cluster_articles_semantic(unique_news)
    github_projects = fetch_github_projects()
    
    all_videos = []
    if os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH, 'r') as f:
            for entry in json.load(f):
                yt_id = entry.get("YouTube Channel ID")
                if yt_id: all_videos.extend(fetch_youtube_videos(yt_id))

    # 5. SAVE FINAL DATA
    clustered_news.sort(key=lambda x: x.get('date', ''), reverse=True)
    final_items = clustered_news[:1000]

    final_data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
        "items": final_items,
        "videos": all_videos,
        "githubProjects": github_projects
    }
    
    if not os.path.exists("./public"): os.makedirs("./public")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)
        
    print(f"✅ Success. River updated. Total items: {len(final_items)}")