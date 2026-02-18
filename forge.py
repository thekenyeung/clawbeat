import feedparser
import requests
import json
import re
import os
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

# --- FUNCTIONS ---

def fetch_youtube_videos(channel_id):
    try:
        ch_resp = youtube.channels().list(id=channel_id, part='contentDetails').execute()
        uploads_id = ch_resp['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        pl_resp = youtube.playlistItems().list(playlistId=uploads_id, part='snippet', maxResults=10).execute()

        MEDIA_KEYWORDS = ["openclaw", "moltbot", "clawdbot", "moltbook", "steinberger", "claudbot", "openclaw foundation"]
        videos = []
        for item in pl_resp.get('items', []):
            snippet = item['snippet']
            title = snippet.get('title', '')
            description = snippet.get('description', '')
            video_id = snippet['resourceId']['videoId']
            
            if any(kw in (title + description).lower() for kw in MEDIA_KEYWORDS):
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

def get_embeddings_batch(texts):
    if not texts: return []
    try:
        # Use verified stable model path for Feb 2026
        selected_model = "models/gemini-embedding-001"
        result = client.models.embed_content(
            model=selected_model,
            contents=texts,
            config=types.EmbedContentConfig(task_type="CLUSTERING")
        )
        return [e.values for e in result.embeddings]
    except Exception as e:
        print(f"❌ Embedding failed: {e}")
        return [None] * len(texts)

def cosine_similarity(v1, v2):
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def extract_real_source(entry, default_source):
    # If the whitelist name doesn't mention Flipboard, trust the whitelist
    if "flipboard" not in default_source.lower():
        return default_source

    # Strategy 1: Look at the URL domain (The most reliable way)
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
        # Fallback: turn "engadget.com" into "Engadget"
        return domain.split('.')[0].capitalize()

    # Strategy 2: Check the Title for a colon (e.g., "The Verge: Story Title")
    title = entry.get('title', '')
    if ":" in title:
        return title.split(":")[0].strip()

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
                
                # Determine real source and clean the title
                source = extract_real_source(entry, site["Source Name"])
                
                # If title is "Source: Headline", remove the "Source:" part
                if ":" in raw_title and "flipboard" in site["Source Name"].lower():
                    display_title = raw_title.split(":", 1)[1].strip()
                else:
                    display_title = raw_title

                summary = entry.get('summary', '') or entry.get('description', '')
                clean_summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)

                if any(kw in (display_title + clean_summary).lower() for kw in KEYWORDS):
                    found_articles.append({
                        "title": display_title,
                        "url": entry.link,
                        "source": source, # Uses the cleaned source name
                        "date": datetime.now().strftime("%m-%d-%Y"),
                        "summary": clean_summary[:200] + "..."
                    })
        except Exception as e:
            print(f"Error scanning {rss_url}: {e}")
            continue
            
    return found_articles

def cluster_articles_semantic(all_articles):
    if not all_articles: return []
    
    # 1. Get embeddings for all headlines
    vectors = get_embeddings_batch([a['title'] for a in all_articles])
    for i, art in enumerate(all_articles): 
        art['vec'] = vectors[i]
    
    clusters = []
    for art in all_articles:
        if art['vec'] is None: continue
        matched = False
        for cluster in clusters:
            # If similarity is high, add to existing cluster
            if cosine_similarity(art['vec'], cluster[0]['vec']) > 0.85:
                cluster.append(art)
                matched = True
                break
        if not matched: 
            clusters.append([art])

    final_topics = []
    for cluster in clusters:
        # The first article in the cluster becomes the main "Anchor"
        anchor = cluster[0]
        
        # 2. Deduplicate "More Coverage"
        unique_coverage = []
        seen_urls = {anchor['url']} # Pre-populate with the main article's URL to exclude it
        
        for a in cluster[1:]:
            # Only add if the URL is new AND not the same as the main article
            if a['url'] not in seen_urls:
                unique_coverage.append({"source": a['source'], "url": a['url']})
                seen_urls.add(a['url'])
        
        anchor['moreCoverage'] = unique_coverage
        
        # Cleanup: Remove vectors before saving to JSON
        for a in cluster: 
            a.pop('vec', None)
            
        final_topics.append(anchor)
        
    return final_topics

def fetch_github_projects():
    """Searches GitHub for OpenClaw-related repositories."""
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    # Change per_page from 10 to 100
    query = "OpenClaw"
    url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=100"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        
        projects = []
        for repo in data.get('items', []):
            projects.append({
                "name": repo['name'],
                "owner": repo['owner']['login'],
                "description": repo['description'] or "No description provided.",
                "url": repo['html_url'],
                "stars": repo['stargazers_count'],
                "created_at": repo['created_at'],
                "updated_at": repo['updated_at'] # Added this for better sorting
            })
        return projects
    except Exception as e:
        print(f"⚠️ GitHub Fetch Failed: {e}")
        return []

# --- SINGLE UNIFIED EXECUTION ---
if __name__ == "__main__":
    print("🛠️ Forging Intel Feed...")
    
    # 1. News - Scrape the latest
    new_articles = scan_rss()

    # 2. Load Existing History for the "River"
    existing_news = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                existing_news = old_data.get('items', [])
        except Exception as e:
            print(f"⚠️ Could not load history: {e}")

    # 3. Merge & Deduplicate (URLs must be unique)
    # Combine lists, prioritizing new articles at the top
    combined_news = new_articles + existing_news
    
    seen_urls = set()
    unique_news = []
    for art in combined_news:
        if art['url'] not in seen_urls:
            unique_news.append(art)
            seen_urls.add(art['url'])

    # 4. Clustering (Run on the unique combined list)
    # We re-cluster everything to ensure "More Coverage" includes old and new links
    clustered_news = cluster_articles_semantic(unique_news[:200]) # Keep a rolling 200 items

    # 5. YouTube & GitHub (Keep these fresh/overwritten)
    all_videos = []
    try:
        with open(WHITELIST_PATH, 'r') as f:
            whitelist_data = json.load(f)
        for entry in whitelist_data:
            yt_id = entry.get("YouTube Channel ID")
            if yt_id:
                all_videos.extend(fetch_youtube_videos(yt_id))
    except Exception as e:
        print(f"⚠️ YouTube Logic Failed: {e}")

    github_projects = fetch_github_projects()

    # 6. Save Final Package
    final_data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": clustered_news,
        "videos": all_videos,
        "githubProjects": github_projects
    }
    
    if not os.path.exists("./public"): os.makedirs("./public")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)
        
    print(f"✅ Success: River updated. Total articles in history: {len(clustered_news)}")