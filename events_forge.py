"""
events_forge.py — Daily event discovery for the ClawBeat events feed.

Discovery (RSS-first, per editorial plan):
  Layer 1 — RSS / API (low-friction, no scraping instability):
    • Google News RSS  — searches "openclaw" + "event"
    • Reddit RSS       — searches "openclaw event"
    • HN Algolia API   — searches "openclaw event"
    Event-platform URLs found in feed content are extracted and validated
    without fetching the source articles.

  Layer 2 — Platform scrapers (HTML keyword search):
    • Eventbrite       — keyword search pages for "openclaw"
    • Luma (search)    — keyword search page for "openclaw"
    • Luma (community) — lu.ma/claw calendar directly (trusted, no keyword filter)
    • Meetup           — keyword search pages for "openclaw"
    • AI Tinkerers     — aitinkerers.org/p/events (keyword filter applied)
    • Eventship        — eventship.com search for "openclaw"
    • Circle.so        — scans configured community event spaces directly

Validation (editorial plan keyword density rule):
  Every candidate event page is checked before saving:
    • PASS if "openclaw" appears in the event title
    • PASS if "openclaw" appears 2+ times on the event registration page
    • REJECT otherwise

Note: LinkedIn Events and Facebook Events are auth-walled and cannot be
scraped directly. They are discovered indirectly when their URLs appear in
RSS feed content (Google News, Reddit).
"""

import feedparser
import requests
import re
import os
import json
import time
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse, urljoin
from supabase import create_client, Client as SupabaseClient
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

SUPABASE_URL         = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
_supabase: "SupabaseClient | None" = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("⚠️  SUPABASE credentials not set — DB writes disabled.")

KEYWORD = "openclaw"

# Browser-like headers to reduce bot-detection blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Layer 1 — RSS / API feeds
# ---------------------------------------------------------------------------

RSS_FEEDS = {
    "Google News": (
        "https://news.google.com/rss/search"
        "?q=%22openclaw%22+%22event%22&hl=en-US&gl=US&ceid=US:en"
    ),
    "Reddit": (
        "https://www.reddit.com/search.rss"
        "?q=openclaw+event&sort=new&limit=25"
    ),
}

HN_API_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?query=openclaw+event&tags=story&hitsPerPage=20"
)

# ---------------------------------------------------------------------------
# Layer 2 — Platform searches
# ---------------------------------------------------------------------------

EVENTBRITE_SEARCHES = [
    "https://www.eventbrite.com/d/online/events/?q=openclaw",
    "https://www.eventbrite.com/d/united-states/events/?q=openclaw",
    "https://www.eventbrite.com/d/canada/events/?q=openclaw",
    "https://www.eventbrite.com/d/united-kingdom/events/?q=openclaw",
]

LUMA_SEARCHES = [
    "https://lu.ma/search?q=openclaw",
]

# Trusted first-party community calendars on Luma.
# ALL events here are on-topic — keyword density filter is skipped.
LUMA_COMMUNITY_CALENDARS = [
    "https://lu.ma/claw",  # OpenClaw community calendar
]

# Hand-curated OpenClaw event URLs (seed list).
# Add specific event pages here to guarantee they are ingested on the next run.
# Keyword density filter is skipped — these are manually verified as on-topic.
LUMA_SEED_EVENTS = [
    "https://luma.com/poiq9yzx",  # Claw-a-rado — OpenClaw Denver meetup
]

AITINKERERS_URL = "https://aitinkerers.org/p/events"

EVENTSHIP_SEARCHES = [
    "https://eventship.com/search?q=openclaw",
]

MEETUP_SEARCHES = [
    "https://www.meetup.com/find/?q=openclaw&source=EVENTS",
    "https://www.meetup.com/find/?q=openclaw&source=EVENTS&eventType=online",
]

# Circle.so communities to scan directly.
# Add more communities here as needed.
CIRCLE_COMMUNITIES = [
    {
        "name":         "MindStudio Academy",
        "base_url":     "https://mindstudio-academy.circle.so",
        "events_space": "events-bootcamps",
    },
]

EVENT_SCHEMA_TYPES = {
    "Event", "MusicEvent", "EducationEvent", "SocialEvent",
    "BusinessEvent", "Hackathon", "ExhibitionEvent", "CourseInstance",
}

# Regex to find event-platform URLs in arbitrary text.
# Covers Eventbrite, Luma, Meetup, LinkedIn Events, Facebook Events, Circle.so.
_EVENT_URL_RE = re.compile(
    r'https?://(?:'
    r'(?:www\.)?eventbrite\.com/e/[^\s\'"<>)\]]+|'
    r'lu\.ma/[^\s\'"<>)\]]+|'
    r'(?:www\.)?luma\.com/[^\s\'"<>)\]]+|'
    r'(?:www\.)?meetup\.com/[^/\s\'"<>)\]]+/events/[^\s\'"<>)\]]+|'
    r'(?:www\.)?linkedin\.com/events/[^\s\'"<>)\]]+|'
    r'(?:www\.)?facebook\.com/events/[^\s\'"<>)\]]+|'
    r'[a-z0-9-]+\.circle\.so/c/[^\s\'"<>)\]]+'
    r')',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTML fetch
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 12) -> tuple["BeautifulSoup | None", str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser"), resp.text
        print(f"  ⚠️  HTTP {resp.status_code} for {url}")
    except Exception as ex:
        print(f"  ⚠️  Fetch error for {url}: {ex}")
    return None, ""


# ---------------------------------------------------------------------------
# Keyword density validation
# ---------------------------------------------------------------------------

def passes_keyword_filter(title: str, page_text: str) -> bool:
    """
    PASS if "openclaw" is in the event title.
    PASS if "openclaw" appears 2+ times on the event page.
    REJECT otherwise.
    """
    kw = KEYWORD.lower()
    if kw in title.lower():
        return True
    return page_text.lower().count(kw) >= 2


# ---------------------------------------------------------------------------
# Event URL extraction from arbitrary text (for RSS layer)
# ---------------------------------------------------------------------------

def extract_event_urls(text: str) -> list[str]:
    """Find event-platform URLs embedded in article/post text."""
    raw = re.findall(_EVENT_URL_RE, text)
    cleaned: list[str] = []
    for u in raw:
        u = u.rstrip(".,;:!?)")
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def extract_json_ld(soup: BeautifulSoup) -> list:
    blocks = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            blocks.append(json.loads(script.string or ""))
        except Exception:
            pass
    return blocks


def find_event_schemas(blocks: list) -> list:
    """Recursively pull out schema.org Event objects from JSON-LD."""
    events = []
    for block in blocks:
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and item.get("@type") in EVENT_SCHEMA_TYPES:
                    events.append(item)
        elif isinstance(block, dict):
            if block.get("@type") in EVENT_SCHEMA_TYPES:
                events.append(block)
            for elem in block.get("itemListElement", []):
                if isinstance(elem, dict):
                    inner = elem.get("item", elem)
                    if isinstance(inner, dict) and inner.get("@type") in EVENT_SCHEMA_TYPES:
                        events.append(inner)
            for node in block.get("@graph", []):
                if isinstance(node, dict) and node.get("@type") in EVENT_SCHEMA_TYPES:
                    events.append(node)
    return events


# ---------------------------------------------------------------------------
# Structured data parsing
# ---------------------------------------------------------------------------

def parse_iso_date(raw: str) -> str:
    """ISO 8601 → MM/DD/YYYY."""
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt[:len(fmt)]).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


def detect_event_type(schema: dict) -> str:
    mode = str(schema.get("eventAttendanceMode", "")).lower()
    if "online" in mode:
        return "virtual"
    if "offline" in mode or "inperson" in mode:
        return "in-person"
    loc = schema.get("location", {})
    if isinstance(loc, dict):
        if loc.get("@type") == "VirtualLocation":
            return "virtual"
        if loc.get("@type") == "Place":
            return "in-person"
    return "unknown"


def extract_location(schema: dict) -> tuple[str, str, str]:
    loc = schema.get("location", {})
    if isinstance(loc, dict) and loc.get("@type") == "Place":
        addr = loc.get("address", {})
        if isinstance(addr, dict):
            return (
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("addressCountry", ""),
            )
        if isinstance(addr, str) and addr:
            parts = [p.strip() for p in addr.split(",")]
            return (
                parts[0] if len(parts) > 0 else "",
                parts[1] if len(parts) > 1 else "",
                parts[2] if len(parts) > 2 else "",
            )
    return "", "", ""


def clean_text(raw: str, max_sentences: int = 3) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(separator=" ", strip=True)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:max_sentences]).strip()


def schema_to_event(schema: dict, fallback_url: str) -> "dict | None":
    title = schema.get("name", "").replace("\n", " ").strip()
    if not title:
        return None

    url = schema.get("url", fallback_url) or fallback_url
    if not url:
        return None

    start_date = parse_iso_date(schema.get("startDate", ""))
    end_date   = parse_iso_date(schema.get("endDate", "")) or start_date

    event_type = detect_event_type(schema)
    city, state, country = ("", "", "") if event_type == "virtual" else extract_location(schema)

    org = schema.get("organizer", {})
    if isinstance(org, dict):
        organizer = org.get("name", "")
    elif isinstance(org, str):
        organizer = org
    else:
        organizer = ""
    if not organizer:
        try:
            organizer = urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()
        except Exception:
            organizer = ""

    description = clean_text(schema.get("description", ""))

    return {
        "url":              url,
        "title":            title,
        "organizer":        organizer,
        "event_type":       event_type,
        "location_city":    city,
        "location_state":   state,
        "location_country": country,
        "start_date":       start_date,
        "end_date":         end_date,
        "description":      description,
    }


# ---------------------------------------------------------------------------
# Generic event-page extractor (JSON-LD → og: meta fallback)
# Used by the RSS layer and wherever per-page fetching is needed.
# ---------------------------------------------------------------------------

def _extract_date_from_text(text: str) -> str:
    months = (
        "january|february|march|april|may|june|july|august|"
        "september|october|november|december|"
        "jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
    )
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }
    dm = re.search(
        rf'({months})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})',
        text, re.IGNORECASE,
    )
    if dm:
        mon = month_map.get(dm.group(1).lower(), 0)
        day = int(dm.group(2))
        yr  = int(dm.group(3))
        if mon:
            return f"{mon:02d}/{day:02d}/{yr}"
    return ""


def extract_event_from_page(url: str, org_name: str = "") -> "dict | None":
    """
    Fetch url, validate keyword density, extract event data.
    Returns None if page is inaccessible or fails the keyword filter.
    """
    soup, _ = fetch_html(url)
    if not soup:
        return None

    page_text = soup.get_text(separator=" ", strip=True)

    # Try JSON-LD first
    schemas = find_event_schemas(extract_json_ld(soup))
    for s in schemas:
        e = schema_to_event(s, url)
        if e:
            if not passes_keyword_filter(e["title"], page_text):
                print(f"     ⛔ Filtered (low density): {e['title'][:60]}")
                return None
            return e

    # Fallback: og: meta tags
    def og(prop: str) -> str:
        tag = soup.find("meta", {"property": f"og:{prop}"}) or \
              soup.find("meta", {"name": prop})
        return str(tag["content"]).strip() if tag and tag.get("content") else ""

    title = og("title") or (soup.title.string.strip() if soup.title else "")
    if not title:
        return None

    if not passes_keyword_filter(title, page_text):
        print(f"     ⛔ Filtered (low density): {title[:60]}")
        return None

    description = clean_text(og("description"))
    start_date  = _extract_date_from_text(page_text)

    combined = (title + " " + description + " " + page_text[:500]).lower()
    if any(w in combined for w in ("virtual", "online", "zoom", "webinar", "livestream")):
        event_type = "virtual"
    else:
        event_type = "unknown"

    if not org_name:
        try:
            org_name = urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()
        except Exception:
            org_name = ""

    return {
        "url":              url,
        "title":            title,
        "organizer":        org_name,
        "event_type":       event_type,
        "location_city":    "",
        "location_state":   "",
        "location_country": "",
        "start_date":       start_date,
        "end_date":         start_date,
        "description":      description,
    }


# ---------------------------------------------------------------------------
# Layer 1: RSS / API scanners
# ---------------------------------------------------------------------------

def scan_rss_feeds() -> list[dict]:
    """
    Scan Google News and Reddit RSS feeds for event-platform URLs.
    Extracts event URLs directly from feed item content (title + link + summary)
    without fetching source articles — minimises request volume.
    LinkedIn Events and Facebook Events are discovered here when their URLs
    appear in articles or posts (auth-walled platforms cannot be scraped directly).
    """
    found = []
    for name, feed_url in RSS_FEEDS.items():
        print(f"  📡 RSS [{name}]...")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as ex:
            print(f"     ⚠️  feedparser error: {ex}")
            continue

        entries = feed.entries[:25]
        print(f"     {len(entries)} item(s) in feed.")

        candidate_urls: set[str] = set()
        for entry in entries:
            blob = " ".join([
                getattr(entry, "title", ""),
                getattr(entry, "link", ""),
                getattr(entry, "summary", ""),
            ])
            for url in extract_event_urls(blob):
                candidate_urls.add(url)

        print(f"     {len(candidate_urls)} candidate event URL(s) extracted.")
        for url in candidate_urls:
            time.sleep(1)
            e = extract_event_from_page(url)
            if e:
                found.append(e)
                print(f"     ✅ {e['title'][:60]}")

        time.sleep(2)

    return found


def scan_hn_api() -> list[dict]:
    """
    Query the Hacker News Algolia API for OpenClaw event stories.
    Checks story URLs directly for event-platform matches.
    """
    print(f"  📡 HN Algolia API...")
    found = []
    try:
        resp = requests.get(HN_API_URL, timeout=12)
        if resp.status_code != 200:
            print(f"     ⚠️  HTTP {resp.status_code}")
            return found
        hits = resp.json().get("hits", [])
        print(f"     {len(hits)} hit(s) from HN.")
    except Exception as ex:
        print(f"     ⚠️  HN API error: {ex}")
        return found

    candidate_urls: set[str] = set()
    for hit in hits:
        blob = f"{hit.get('title', '')} {hit.get('url', '')}"
        for url in extract_event_urls(blob):
            candidate_urls.add(url)

    print(f"     {len(candidate_urls)} candidate event URL(s).")
    for url in candidate_urls:
        time.sleep(1)
        e = extract_event_from_page(url)
        if e:
            found.append(e)
            print(f"     ✅ {e['title'][:60]}")

    time.sleep(2)
    return found


# ---------------------------------------------------------------------------
# Layer 2: Platform scrapers
# ---------------------------------------------------------------------------

def scan_eventbrite() -> list[dict]:
    """
    Fetch Eventbrite keyword search pages for "openclaw".
    Primary: extract Event schemas from search-page JSON-LD.
    Fallback: visit individual /e/ event links.
    Validation: keyword density filter applied on every event.
    """
    found = []
    for search_url in EVENTBRITE_SEARCHES:
        print(f"  📅 Eventbrite: {search_url}")
        soup, _ = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        page_text = soup.get_text(separator=" ", strip=True)
        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) on search page.")
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e and passes_keyword_filter(e["title"], page_text):
                    found.append(e)
                elif e:
                    print(f"     ⛔ Filtered: {e['title'][:60]}")
        else:
            event_links: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if re.search(r"eventbrite\.com/e/", href):
                    clean = href.split("?")[0].split("#")[0]
                    if not clean.startswith("http"):
                        clean = urljoin("https://www.eventbrite.com", clean)
                    event_links.add(clean)
            print(f"     No JSON-LD; visiting {len(event_links)} event link(s).")
            for link in list(event_links)[:10]:
                time.sleep(1.5)
                e = extract_event_from_page(link)
                if e:
                    found.append(e)

        time.sleep(2)
    return found


def scan_luma() -> list[dict]:
    """
    Fetch Luma search page for "openclaw".
    Luma is often JS-rendered; falls back to Next.js __NEXT_DATA__ and link crawl.
    Validation: keyword density filter applied on every event.
    """
    found = []
    for search_url in LUMA_SEARCHES:
        print(f"  📅 Luma: {search_url}")
        soup, raw = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) found.")
            page_text = soup.get_text(separator=" ", strip=True)
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e and passes_keyword_filter(e["title"], page_text):
                    found.append(e)
                elif e:
                    print(f"     ⛔ Filtered: {e['title'][:60]}")
        else:
            event_urls: set[str] = set()
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
            if m:
                try:
                    next_data = json.loads(m.group(1))
                    for match in re.finditer(r'"url"\s*:\s*"(https://lu\.ma/[^"]+)"', json.dumps(next_data)):
                        event_urls.add(match.group(1))
                except Exception as ex:
                    print(f"     Could not parse Next.js data: {ex}")

            # Also collect Luma links from anchor tags
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if re.match(r'https?://lu\.ma/[^/?#\s]+$', href):
                    event_urls.add(href.split("?")[0])

            print(f"     Visiting {len(event_urls)} Luma event link(s).")
            for link in list(event_urls)[:10]:
                time.sleep(1.5)
                e = extract_event_from_page(link)
                if e:
                    found.append(e)

        time.sleep(2)
    return found


def scan_luma_communities() -> list[dict]:
    """
    Scrape trusted Luma community calendars (e.g. lu.ma/claw) directly.
    These are first-party OpenClaw event pages — keyword density filter is skipped
    because every event on the calendar is by definition on-topic.
    """
    found = []
    for cal_url in LUMA_COMMUNITY_CALENDARS:
        print(f"  📅 Luma community: {cal_url}")
        soup, raw = fetch_html(cal_url)
        if not soup:
            time.sleep(2)
            continue

        event_urls: set[str] = set()

        # lu.ma is a Next.js app — parse the __NEXT_DATA__ blob first.
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
        if m:
            try:
                data_str = json.dumps(json.loads(m.group(1)))
                # Collect fully-qualified lu.ma event URLs
                for match in re.finditer(r'"url"\s*:\s*"(https://lu\.ma/[^"]+)"', data_str):
                    u = match.group(1).split("?")[0]
                    if not any(x in u for x in ["/claw", "/search", "/calendar", "/user"]):
                        event_urls.add(u)
                # Also collect short api_id slugs (evt-XXXXX)
                for match in re.finditer(r'"api_id"\s*:\s*"(evt-[^"]+)"', data_str):
                    event_urls.add(f"https://lu.ma/{match.group(1)}")
            except Exception as ex:
                print(f"     Could not parse Next.js data: {ex}")

        # Anchor-tag fallback
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if not href.startswith("http"):
                href = urljoin("https://lu.ma", href)
            href = href.split("?")[0]
            if (re.match(r"https://(lu\.ma|luma\.com)/[a-z0-9_-]{3,}$", href, re.IGNORECASE)
                    and href not in (cal_url, "https://lu.ma", "https://luma.com")):
                event_urls.add(href)

        print(f"     Found {len(event_urls)} event link(s) to visit.")
        for link in list(event_urls)[:20]:
            time.sleep(1.5)
            soup2, _ = fetch_html(link)
            if not soup2:
                continue

            # Try schema.org JSON-LD
            schemas = find_event_schemas(extract_json_ld(soup2))
            added = False
            for s in schemas:
                e = schema_to_event(s, link)
                if e:
                    found.append(e)
                    print(f"     ✅ {e['title'][:60]}")
                    added = True
                    break

            if not added:
                # og: meta fallback
                def og(prop: str) -> str:
                    tag = soup2.find("meta", {"property": f"og:{prop}"}) or \
                          soup2.find("meta", {"name": prop})
                    return str(tag["content"]).strip() if tag and tag.get("content") else ""

                title = og("title") or (soup2.title.string.strip() if soup2.title else "")
                if title:
                    page_text = soup2.get_text(separator=" ", strip=True)
                    description = clean_text(og("description"))
                    start_date  = _extract_date_from_text(page_text)
                    found.append({
                        "url":              link,
                        "title":            title,
                        "organizer":        "OpenClaw",
                        "event_type":       "unknown",
                        "location_city":    "",
                        "location_state":   "",
                        "location_country": "",
                        "start_date":       start_date,
                        "end_date":         start_date,
                        "description":      description,
                    })
                    print(f"     ✅ {title[:60]} (og: fallback)")

        time.sleep(2)
    return found


def scan_seed_events() -> list[dict]:
    """
    Directly fetch hand-curated OpenClaw event URLs from LUMA_SEED_EVENTS.
    Keyword density filter is skipped — all seed events are manually verified.
    Uses the same JSON-LD / og: meta extraction as the community calendar scanner.
    """
    found = []
    for url in LUMA_SEED_EVENTS:
        print(f"  📌 Seed event: {url}")
        time.sleep(1)
        soup, _ = fetch_html(url)
        if not soup:
            continue

        schemas = find_event_schemas(extract_json_ld(soup))
        added = False
        for s in schemas:
            e = schema_to_event(s, url)
            if e:
                found.append(e)
                print(f"     ✅ {e['title'][:60]}")
                added = True
                break

        if not added:
            def og(prop: str) -> str:
                tag = soup.find("meta", {"property": f"og:{prop}"}) or \
                      soup.find("meta", {"name": prop})
                return str(tag["content"]).strip() if tag and tag.get("content") else ""

            title = og("title") or (soup.title.string.strip() if soup.title else "")
            if title:
                page_text = soup.get_text(separator=" ", strip=True)
                description = clean_text(og("description"))
                start_date  = _extract_date_from_text(page_text)
                found.append({
                    "url":              url,
                    "title":            title,
                    "organizer":        "OpenClaw",
                    "event_type":       "in-person",
                    "location_city":    "",
                    "location_state":   "",
                    "location_country": "",
                    "start_date":       start_date,
                    "end_date":         start_date,
                    "description":      description,
                })
                print(f"     ✅ {title[:60]} (og: fallback)")

    return found


def scan_aitinkerers() -> list[dict]:
    """
    Scrape AI Tinkerers events page for OpenClaw-related events.
    AI Tinkerers is a global AI engineering community (87k+ members, 203 cities).
    Keyword density filter applied — not all events are OpenClaw-specific.
    """
    found = []
    print(f"  📅 AI Tinkerers: {AITINKERERS_URL}")
    soup, raw = fetch_html(AITINKERERS_URL)
    if not soup:
        return found

    # JSON-LD first
    schemas = find_event_schemas(extract_json_ld(soup))
    if schemas:
        page_text = soup.get_text(separator=" ", strip=True)
        print(f"     {len(schemas)} event schema(s) found.")
        for s in schemas:
            e = schema_to_event(s, AITINKERERS_URL)
            if e and passes_keyword_filter(e["title"], page_text):
                found.append(e)
                print(f"     ✅ {e['title'][:60]}")
            elif e:
                print(f"     ⛔ Filtered: {e['title'][:60]}")
        time.sleep(2)
        return found

    # Collect event page links from anchor tags
    event_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if not href.startswith("http"):
            href = urljoin("https://aitinkerers.org", href)
        if re.search(r"aitinkerers\.org/(p|events)/[^/?#\s]+", href):
            event_links.add(href.split("?")[0])

    print(f"     No JSON-LD; visiting {len(event_links)} event link(s).")
    for link in list(event_links)[:10]:
        time.sleep(1.5)
        e = extract_event_from_page(link, "AI Tinkerers")
        if e:
            found.append(e)
            print(f"     ✅ {e['title'][:60]}")

    time.sleep(2)
    return found


def scan_eventship() -> list[dict]:
    """
    Scan Eventship for OpenClaw-related events.
    Eventship is a platform for in-person communities; keyword filter applied.
    Note: Eventship is a Bubble app (JS-rendered); HTML scraping may yield
    limited results. JSON-LD and link-crawling attempted.
    """
    found = []
    for search_url in EVENTSHIP_SEARCHES:
        print(f"  📅 Eventship: {search_url}")
        soup, _ = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        page_text = soup.get_text(separator=" ", strip=True)
        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) found.")
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e and passes_keyword_filter(e["title"], page_text):
                    found.append(e)
                    print(f"     ✅ {e['title'][:60]}")
                elif e:
                    print(f"     ⛔ Filtered: {e['title'][:60]}")
        else:
            event_links: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if not href.startswith("http"):
                    href = urljoin("https://eventship.com", href)
                if re.search(r"eventship\.com/e/[^/?#\s]+", href):
                    event_links.add(href.split("?")[0])
            print(f"     No JSON-LD; visiting {len(event_links)} event link(s).")
            for link in list(event_links)[:10]:
                time.sleep(1.5)
                e = extract_event_from_page(link, "Eventship")
                if e:
                    found.append(e)
                    print(f"     ✅ {e['title'][:60]}")

        time.sleep(2)
    return found


def scan_meetup() -> list[dict]:
    """
    Fetch Meetup keyword search pages for "openclaw".
    Meetup's API is no longer freely accessible; uses HTML scraping.
    Primary: JSON-LD on search page or event pages.
    Validation: keyword density filter applied on every event.
    """
    found = []
    for search_url in MEETUP_SEARCHES:
        print(f"  📅 Meetup: {search_url}")
        soup, _ = fetch_html(search_url)
        if not soup:
            time.sleep(2)
            continue

        page_text = soup.get_text(separator=" ", strip=True)
        schemas = find_event_schemas(extract_json_ld(soup))
        if schemas:
            print(f"     {len(schemas)} event schema(s) on search page.")
            for s in schemas:
                e = schema_to_event(s, search_url)
                if e and passes_keyword_filter(e["title"], page_text):
                    found.append(e)
                elif e:
                    print(f"     ⛔ Filtered: {e['title'][:60]}")
        else:
            event_links: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                # Meetup event URLs: meetup.com/GroupName/events/EVENTID/
                if re.search(r'meetup\.com/[^/]+/events/\d+', href):
                    clean = href.split("?")[0].split("#")[0]
                    if not clean.startswith("http"):
                        clean = urljoin("https://www.meetup.com", clean)
                    event_links.add(clean)
            print(f"     No JSON-LD; visiting {len(event_links)} event link(s).")
            for link in list(event_links)[:10]:
                time.sleep(1.5)
                e = extract_event_from_page(link)
                if e:
                    found.append(e)

        time.sleep(2)
    return found


def scan_circle() -> list[dict]:
    """
    Scan configured Circle.so community event spaces directly.
    Validation: keyword density filter applied on every event.
    """
    found = []
    for community in CIRCLE_COMMUNITIES:
        base      = community["base_url"].rstrip("/")
        space     = community["events_space"]
        org_name  = community["name"]
        space_url = f"{base}/c/{space}"
        print(f"  📅 Circle.so [{org_name}]: {space_url}")

        soup, raw = fetch_html(space_url)
        if not soup:
            time.sleep(2)
            continue

        event_links: set[str] = set()
        pattern = re.compile(rf"^{re.escape(base)}/c/{re.escape(space)}/[^/?#]+$")
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if not href.startswith("http"):
                href = urljoin(base, href)
            href = href.split("?")[0].split("#")[0]
            if pattern.match(href) and href != space_url:
                event_links.add(href)

        if not event_links:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
            if m:
                try:
                    raw_str = json.loads(m.group(1))
                    for match in re.finditer(
                        rf'"url"\s*:\s*"({re.escape(base)}/c/{re.escape(space)}/[^"]+)"',
                        json.dumps(raw_str),
                    ):
                        event_links.add(match.group(1).split("?")[0])
                except Exception:
                    pass

        print(f"     Found {len(event_links)} event link(s) to visit.")
        for link in list(event_links)[:15]:
            time.sleep(1.5)
            e = extract_event_from_page(link, org_name)
            if e:
                found.append(e)

        time.sleep(2)
    return found


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def cleanup_garbage_events() -> None:
    """
    Delete events already in Supabase that contain no mention of 'openclaw'
    in either their title or description. These were ingested before the
    keyword density filter was introduced (e.g. Eventbrite category-URL bug).
    """
    if not _supabase:
        return
    try:
        resp = _supabase.table("events").select("url,title,description").execute()
        garbage = [
            r["url"] for r in (resp.data or [])
            if KEYWORD not in r.get("title", "").lower()
            and KEYWORD not in r.get("description", "").lower()
        ]
        if not garbage:
            print("✅ No garbage events found — DB is clean.")
            return
        print(f"🗑️  Removing {len(garbage)} garbage event(s) with no 'openclaw' mention...")
        for url in garbage:
            print(f"  🗑️  {url[:80]}")
        _supabase.table("events").delete().in_("url", garbage).execute()
        print(f"✅ Deleted {len(garbage)} garbage event(s).")
    except Exception as ex:
        print(f"⚠️  Cleanup failed: {ex}")


def load_existing_urls() -> set[str]:
    if not _supabase:
        return set()
    try:
        resp = _supabase.table("events").select("url").execute()
        return {r["url"] for r in (resp.data or [])}
    except Exception as ex:
        print(f"  ⚠️  Could not load existing events: {ex}")
        return set()


def save_events(events: list[dict]) -> None:
    if not _supabase or not events:
        return
    try:
        records = [{
            "url":              e["url"],
            "title":            e["title"],
            "organizer":        e.get("organizer", ""),
            "event_type":       e.get("event_type", "unknown"),
            "location_city":    e.get("location_city", ""),
            "location_state":   e.get("location_state", ""),
            "location_country": e.get("location_country", ""),
            "start_date":       e.get("start_date", ""),
            "end_date":         e.get("end_date", ""),
            "description":      e.get("description", ""),
        } for e in events]
        _supabase.table("events").upsert(records).execute()
        print(f"✅ Upserted {len(records)} event(s).")
    except Exception as ex:
        print(f"❌ Event save failed: {ex}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(
        "🗓️  Events Forge — scanning RSS feeds + platforms for OpenClaw events...\n"
        "     Seed events: hand-curated OpenClaw URLs (no keyword filter)\n"
        "     Layer 1 (RSS/API): Google News · Reddit · HN\n"
        "     Layer 2 (scrapers): Eventbrite · Luma search · lu.ma/claw\n"
        "                         AI Tinkerers · Eventship · Meetup · Circle.so\n"
        "     Validation: title match OR 2+ page mentions of 'openclaw'\n"
        "     Note: lu.ma/claw + seed events skip the keyword filter\n"
    )

    print("\n🧹 Step 1: Cleaning up garbage events from previous runs...")
    cleanup_garbage_events()

    print("\n🔍 Step 2: Discovering new events...")
    existing_urls = load_existing_urls()

    raw_events: list[dict] = (
        scan_seed_events()
        + scan_rss_feeds()
        + scan_hn_api()
        + scan_eventbrite()
        + scan_luma()
        + scan_luma_communities()
        + scan_aitinkerers()
        + scan_eventship()
        + scan_meetup()
        + scan_circle()
    )

    # Deduplicate by URL within this run
    seen: set[str] = set()
    unique_events: list[dict] = []
    for e in raw_events:
        if e["url"] not in seen:
            seen.add(e["url"])
            unique_events.append(e)

    new_events = [e for e in unique_events if e["url"] not in existing_urls]
    print(f"\n🔍 {len(unique_events)} unique event(s) found, {len(new_events)} new.")

    if new_events:
        for e in new_events:
            print(f"  ✅ {e['title'][:60]} [{e['event_type']}] {e['start_date']}")
        save_events(new_events)
    else:
        print("ℹ️  No new events found.")

    print("✅ Events forge complete.")
