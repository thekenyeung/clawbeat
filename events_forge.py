"""
events_forge.py — Daily OpenClaw event discovery (zero-cost, no LLM).

Discovery pipeline:
  1. Google News RSS  — keyword searches for openclaw + event terms
  2. Reddit RSS        — keyword search for openclaw events
  3. HN Algolia API    — free search, no auth required

Qualification rules (per the spec):
  • Title contains "openclaw"  →  immediate pass
  • OR fetched page contains "openclaw" ≥ 2 times

Extraction uses:
  • schema.org Event JSON-LD (primary — covers Eventbrite, Luma, Meetup, etc.)
  • Open Graph / meta description tags (fallback)
  • Simple regex for dates and location (last-resort fallback)
"""

import feedparser
import requests
import re
import os
import json
import time
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse
from supabase import create_client, Client as SupabaseClient
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

SUPABASE_URL        = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
_supabase: "SupabaseClient | None" = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("⚠️  SUPABASE credentials not set — DB writes disabled.")

KEYWORD = "openclaw"

EVENT_QUERIES = [
    "openclaw event",
    "openclaw meetup",
    "openclaw hackathon",
    "openclaw conference",
    "openclaw workshop",
    "openclaw livestream",
    "openclaw demo day",
]

VIRTUAL_SIGNALS = {
    'virtual', 'online', 'zoom', 'webinar', 'livestream',
    'remote', 'stream', 'broadcast', 'digital', 'attendancemodeononline',
}

HEADERS = {'User-Agent': 'ClawBeatEventBot/1.0 (events discovery)'}


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

def is_candidate(title: str, summary: str) -> bool:
    """Quick pre-filter before fetching the page."""
    combined = (title + " " + summary).lower()
    return KEYWORD in combined


def qualifies(url: str, title: str) -> tuple[bool, str]:
    """
    Returns (passes, page_description).
    Title match → instant pass without a network request.
    Otherwise fetch the page; passes if openclaw appears ≥ 2 times.
    """
    if KEYWORD in title.lower():
        return True, ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return False, ""
        soup = BeautifulSoup(resp.text, "html.parser")
        plain = soup.get_text(separator=" ", strip=True).lower()
        if plain.count(KEYWORD) >= 2:
            og  = soup.find("meta", {"property": "og:description"})
            meta = soup.find("meta", {"name": "description"})
            desc = (og and og.get("content")) or (meta and meta.get("content")) or ""
            return True, str(desc)
        return False, ""
    except Exception:
        return False, ""


# ---------------------------------------------------------------------------
# Structured extraction helpers
# ---------------------------------------------------------------------------

def extract_schema_event(soup: BeautifulSoup) -> dict:
    """Parse schema.org/Event from JSON-LD — the richest free source."""
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict)), {})
            if not isinstance(data, dict):
                continue
            event_types = {
                "Event", "MusicEvent", "EducationEvent", "SocialEvent",
                "BusinessEvent", "Hackathon", "ExhibitionEvent", "CourseInstance",
            }
            if data.get("@type") in event_types:
                return data
            # Sometimes wrapped in @graph
            graph = data.get("@graph", [])
            for node in graph:
                if isinstance(node, dict) and node.get("@type") in event_types:
                    return node
        except Exception:
            continue
    return {}


def parse_iso_date(raw: str) -> str:
    """Convert ISO 8601 date string to MM/DD/YYYY, or return empty string."""
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt[:len(fmt)]).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


def regex_date(text: str) -> str:
    """Last-resort: find the first human-readable date in text → MM/DD/YYYY."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    # "March 15, 2026" or "15 March 2026"
    m = re.search(
        r'(?:(\d{1,2})\s+)?(' + '|'.join(months) + r')\.?\s+(\d{1,2}),?\s+(20\d{2})',
        text.lower()
    )
    if m:
        day   = int(m.group(1) or m.group(3))
        month = months[m.group(2)]
        year  = int(m.group(4))
        return f"{month:02d}/{day:02d}/{year}"
    # "03/15/2026" or "3-15-2026"
    m2 = re.search(r'\b(0?[1-9]|1[0-2])[\/\-](0?[1-9]|[12]\d|3[01])[\/\-](20\d{2})\b', text)
    if m2:
        return f"{int(m2.group(1)):02d}/{int(m2.group(2)):02d}/{m2.group(3)}"
    return ""


def rss_entry_date(entry: dict) -> str:
    for field in ("published_parsed", "updated_parsed"):
        raw = entry.get(field)
        if raw:
            try:
                return datetime(*raw[:6]).strftime("%m/%d/%Y")
            except Exception:
                pass
    return ""


def detect_event_type(schema: dict, title: str, description: str, url: str) -> str:
    """Determine 'virtual' | 'in-person' | 'unknown'."""
    # schema.org attendanceMode
    mode = str(schema.get("eventAttendanceMode", "")).lower()
    if "online" in mode:
        return "virtual"
    if "offline" in mode or "inperson" in mode or "mixed" in mode:
        return "in-person"
    combined = (title + " " + description + " " + url).lower().replace("-", "")
    if any(s in combined for s in VIRTUAL_SIGNALS):
        return "virtual"
    loc = schema.get("location", {})
    if isinstance(loc, dict) and loc.get("@type") == "Place":
        return "in-person"
    return "unknown"


def extract_location(schema: dict, description: str) -> tuple[str, str, str]:
    """Return (city, state, country) — empty strings if not found."""
    loc = schema.get("location", {})
    if isinstance(loc, dict):
        addr = loc.get("address", {})
        if isinstance(addr, dict):
            city    = addr.get("addressLocality", "")
            state   = addr.get("addressRegion", "")
            country = addr.get("addressCountry", "")
            return city, state, country
        if isinstance(addr, str) and addr:
            # crude split on comma: "City, State, Country"
            parts = [p.strip() for p in addr.split(",")]
            return (parts[0] if len(parts) > 0 else "",
                    parts[1] if len(parts) > 1 else "",
                    parts[2] if len(parts) > 2 else "")
    # Regex fallback in description: "in City, State" / "in City, Country"
    m = re.search(
        r'\bin\s+([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2}|[A-Z][a-zA-Z]{3,15})',
        description
    )
    if m:
        city   = m.group(1).strip()
        region = m.group(2).strip()
        state  = region if len(region) == 2 else ""
        country = "" if len(region) == 2 else region
        return city, state, country
    return "", "", ""


def clean_description(raw: str, max_sentences: int = 3) -> str:
    """Strip HTML, truncate to ≤ max_sentences."""
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return " ".join(sentences[:max_sentences]).strip()


def extract_organizer(schema: dict, url: str) -> str:
    org = schema.get("organizer", {})
    if isinstance(org, dict):
        name = org.get("name", "")
        if name:
            return name
    if isinstance(org, str) and org:
        return org
    # Fallback: use the domain name
    try:
        domain = urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()
        return domain
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Discovery scanners
# ---------------------------------------------------------------------------

def scan_google_news() -> list[dict]:
    found = []
    for query in EVENT_QUERIES:
        q = query.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:20]:
                title   = e.get("title", "")
                link    = getattr(e, "link", None) or e.get("link", "")
                summary = e.get("summary", "")
                if link and is_candidate(title, summary):
                    found.append({"title": title, "url": link, "summary": summary, "entry": dict(e)})
            time.sleep(1)
        except Exception as ex:
            print(f"  ⚠️  Google News scan failed ({query}): {ex}")
    return found


def scan_reddit() -> list[dict]:
    found = []
    for term in ["openclaw+event", "openclaw+meetup", "openclaw+hackathon", "openclaw+workshop"]:
        url = f"https://www.reddit.com/search.rss?q={term}&sort=new&limit=25"
        try:
            feed = feedparser.parse(url, agent="ClawBeatEventBot/1.0")
            for e in feed.entries[:25]:
                title   = e.get("title", "")
                link    = getattr(e, "link", None) or e.get("link", "")
                summary = e.get("summary", "")
                if link and is_candidate(title, summary):
                    found.append({"title": title, "url": link, "summary": summary, "entry": dict(e)})
            time.sleep(2)
        except Exception as ex:
            print(f"  ⚠️  Reddit scan failed ({term}): {ex}")
    return found


def scan_hn() -> list[dict]:
    found = []
    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "openclaw event", "tags": "story", "hitsPerPage": 20},
            headers=HEADERS,
            timeout=8,
        )
        for hit in resp.json().get("hits", []):
            title = hit.get("title", "")
            url   = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}"
            if is_candidate(title, ""):
                found.append({"title": title, "url": url, "summary": "", "entry": {}})
    except Exception as ex:
        print(f"  ⚠️  HN scan failed: {ex}")
    return found


def deduplicate(candidates: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in candidates:
        if c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Per-candidate processing
# ---------------------------------------------------------------------------

def process_candidate(candidate: dict) -> dict | None:
    title   = candidate["title"]
    url     = candidate["url"]
    summary = candidate.get("summary", "")
    entry   = candidate.get("entry", {})

    passes, page_desc = qualifies(url, title)
    if not passes:
        return None

    # Try full page fetch to get structured data
    schema: dict = {}
    og_desc = page_desc
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            schema = extract_schema_event(soup)
            if not og_desc:
                og   = soup.find("meta", {"property": "og:description"})
                meta = soup.find("meta", {"name": "description"})
                og_desc = (og and og.get("content")) or (meta and meta.get("content")) or ""
    except Exception:
        pass

    # Title: prefer schema, then RSS
    final_title = schema.get("name", title).replace("\n", " ").strip() or title

    # Description
    raw_desc = schema.get("description", "") or og_desc or summary
    description = clean_description(raw_desc)

    # Dates
    start_raw = schema.get("startDate", "")
    end_raw   = schema.get("endDate", "")
    start_date = parse_iso_date(start_raw) or rss_entry_date(entry) or regex_date(description) or datetime.now().strftime("%m/%d/%Y")
    end_date   = parse_iso_date(end_raw)   or start_date

    # Event type & location
    event_type = detect_event_type(schema, final_title, description, url)
    city, state, country = ("", "", "") if event_type == "virtual" else extract_location(schema, description)

    # Organizer
    organizer = extract_organizer(schema, url)

    return {
        "url":              url,
        "title":            final_title,
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
# Supabase I/O
# ---------------------------------------------------------------------------

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
    print("🗓️  Events Forge — scanning for OpenClaw events...")

    existing_urls = load_existing_urls()

    raw = scan_google_news() + scan_reddit() + scan_hn()
    raw = deduplicate(raw)

    new_only = [c for c in raw if c["url"] not in existing_urls]
    print(f"🔍 {len(raw)} candidate(s) found, {len(new_only)} new.")

    new_events: list[dict] = []
    for c in new_only:
        print(f"  🔎 Checking: {c['title'][:70]}")
        event = process_candidate(c)
        if event:
            new_events.append(event)
            print(f"  ✅ Qualified: {event['title'][:60]} [{event['event_type']}]")
        else:
            print(f"  ❌ Did not qualify")
        time.sleep(0.5)

    if new_events:
        save_events(new_events)
    else:
        print("ℹ️  No new events to add.")

    print(f"✅ Events forge complete. Added: {len(new_events)}")
