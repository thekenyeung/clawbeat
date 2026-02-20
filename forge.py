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

def get_embeddings_batch(texts):
    if not texts: return []
    try:
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
                source = extract_real_source(entry, site["Source Name"])
                
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
                        "source": source,
                        "date": datetime.now().strftime("%m-%d-%Y"),
                        "summary": clean_summary[:200] + "..."
                    })
        except Exception as e:
            continue
            
    return found_articles

def cluster_articles_semantic(all_articles):
    if not all_articles: return []
    
    vectors = get_embeddings_batch([a['title'] for a in all_articles])
    for i, art in enumerate(all_articles): 
        art['vec'] = vectors[i]
    
    clusters = []
    for art in all_articles:
        if art['vec'] is None: continue
        matched = False
        for cluster in clusters:
            if cosine_similarity(art['vec'], cluster[0]['vec']) > 0.85:
                cluster.append(art)
                matched = True
                break
        if not matched: 
            clusters.append([art])

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
        for a in cluster: 
            a.pop('vec', None)
        final_topics.append(anchor)
        
    return final_topics

def fetch_github_projects():
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

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
                "updated_at": repo['updated_at']
            })
        return projects
    except Exception as e:
        print(f"⚠️ GitHub Fetch Failed: {e}")
        return []

def scan_google_news(query="OpenClaw OR 'Moltbot' OR 'Clawdbot' OR 'Moltbook' OR 'Steinberger'"):
    import urllib.parse
    encoded_query = urllib.parse.quote(query)
    gn_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    try:
        resp = requests.get(gn_url, timeout=10)
        feed = feedparser.parse(resp.content)
        wild_articles = []
        
        for entry in feed.entries[:15]:
            # 1. REMOVE PUBLICATION FROM HEADLINE
            # Google News usually ends headlines with " - Source Name"
            raw_title = entry.title
            if " - " in raw_title:
                # We split at the last " - " to remove the source suffix
                clean_title = " - ".join(raw_title.split(" - ")[:-1])
            else:
                clean_title = raw_title

            # 2. EXTRACT REAL SUMMARY
            # Google News hides the text snippet inside an HTML table in the 'summary' field
            raw_summary = entry.get('summary', '')
            soup = BeautifulSoup(raw_summary, "html.parser")
            
            # The actual snippet is usually the first few sentences in the HTML description
            # We strip the "all coverage" and "source" text that Google adds
            text_blocks = soup.find_all('li')
            if text_blocks:
                # Often the first <li> contains the snippet
                snippet = text_blocks[0].get_text(strip=True)
            else:
                snippet = soup.get_text(strip=True)
            
            # Clean up the snippet (remove 'view full coverage', etc)
            clean_summary = snippet.split("View Full Coverage")[0].strip()

            wild_articles.append({
                "title": clean_title,
                "url": entry.link.split("&url=")[-1] if "&url=" in entry.link else entry.link,
                "source": entry.source.get('title', 'web search').lower(),
                "summary": clean_summary[:200] + "..." if clean_summary else "ecosystem update.",
                "date": datetime.now().strftime("%m-%d-%Y")
            })
        return wild_articles
    except Exception as e:
        print(f"⚠️ Google News Search failed: {e}")
        return []

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("🛠️ Forging Intel Feed...")
    
    # 1. Load History
    existing_news = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
                existing_news = json.load(f).get('items', [])
        except: pass

    # 2. Scrape Whitelist + Google News
    whitelist_articles = scan_rss()
    wild_articles = scan_google_news()
    
    # 3. Merge, Deduplicate, and Cluster
    all_found = wild_articles + whitelist_articles + existing_news

    # 4. Deduplicate by URL
    seen_urls = set()
    unique_news = []
    for art in all_found:
        if art['url'] not in seen_urls:
            unique_news.append(art)
            seen_urls.add(art['url'])

    # 5. Cluster and Deepen the River (Increased to 1000)
    clustered_news = cluster_articles_semantic(unique_news[:1000])

    # 6. YouTube
    all_videos = []
    try:
        if os.path.exists(WHITELIST_PATH):
            with open(WHITELIST_PATH, 'r') as f:
                whitelist_data = json.load(f)
            for entry in whitelist_data:
                yt_id = entry.get("YouTube Channel ID")
                if yt_id: all_videos.extend(fetch_youtube_videos(yt_id))
    except Exception as e:
        print(f"⚠️ YouTube Logic Failed: {e}")

    # 7. GitHub
    github_projects = fetch_github_projects()

    # 8. Safety Check
    if not clustered_news and existing_news:
        clustered_news = existing_news

    # 9. Save
    final_data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": clustered_news,
        "videos": all_videos,
        "githubProjects": github_projects
    }
    
    if not os.path.exists("./public"): os.makedirs("./public")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)
        
    print(f"✅ Success. River updated. Total items: {len(clustered_news)}")