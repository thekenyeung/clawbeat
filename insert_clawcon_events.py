"""
One-time script: insert ClawCon tour events into Supabase events table.
Run once, then delete.
"""
import os
from supabase import create_client
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

SUPABASE_URL         = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise SystemExit("❌ SUPABASE_URL / SUPABASE_SERVICE_KEY not set")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

DESCRIPTION = (
    "An open, social-first gathering from the OpenClaw community — "
    "personal AI tool demos, Q&A, and unstructured networking. "
    "Free and inclusive; no LinkedIn or GitHub screening, no paywall."
)

EVENTS = [
    {
        "url":              "https://luma.com/clawcondfw",
        "title":            "ClawCon DFW",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Arlington",
        "location_state":   "Texas",
        "location_country": "United States",
        "start_date":       "03/24/2026",
        "end_date":         "03/24/2026",
        "description":      DESCRIPTION,
    },
    {
        "url":              "https://luma.com/clawconmiami",
        "title":            "ClawCon Miami presented by Kilo Code",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Miami",
        "location_state":   "Florida",
        "location_country": "United States",
        "start_date":       "03/25/2026",
        "end_date":         "03/25/2026",
        "description":      (
            "Demos and Dancehall — personal AI tool demos, Q&A, and networking. "
            "Co-hosted with #MiamiTech Happy Hour, Beats + Bytes, and Kilo Code. "
            "Free and inclusive; no LinkedIn or GitHub screening."
        ),
    },
    {
        "url":              "https://luma.com/clawcontokyo",
        "title":            "ClawCon Tokyo",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Tokyo",
        "location_state":   "",
        "location_country": "Japan",
        "start_date":       "03/30/2026",
        "end_date":         "03/30/2026",
        "description":      DESCRIPTION,
    },
    {
        "url":              "https://luma.com/clawconlondon",
        "title":            "ClawCon London",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "London",
        "location_state":   "England",
        "location_country": "United Kingdom",
        "start_date":       "04/08/2026",
        "end_date":         "04/08/2026",
        "description":      (
            "An open, social-first gathering at Encode Hub — "
            "personal AI tool demos, Q&A, and informal networking. "
            "Free to attend with no screening required."
        ),
    },
    {
        "url":              "https://luma.com/clawconguad",
        "title":            "ClawCon Guadalajara",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Guadalajara",
        "location_state":   "Jalisco",
        "location_country": "Mexico",
        "start_date":       "04/25/2026",
        "end_date":         "04/25/2026",
        "description":      DESCRIPTION,
    },
    {
        "url":              "https://luma.com/clawconrio",
        "title":            "ClawCon Rio de Janeiro",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Rio de Janeiro",
        "location_state":   "Rio de Janeiro",
        "location_country": "Brazil",
        "start_date":       "04/29/2026",
        "end_date":         "04/29/2026",
        "description":      DESCRIPTION,
    },
    {
        "url":              "https://luma.com/clawconseoul",
        "title":            "ClawCon Seoul",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Seoul",
        "location_state":   "",
        "location_country": "South Korea",
        "start_date":       "05/02/2026",
        "end_date":         "05/02/2026",
        "description":      DESCRIPTION,
    },
    {
        "url":              "https://luma.com/clawconcdmx",
        "title":            "ClawCon CDMX",
        "organizer":        "OpenClaw",
        "event_type":       "in-person",
        "location_city":    "Mexico City",
        "location_state":   "",
        "location_country": "Mexico",
        "start_date":       "05/09/2026",
        "end_date":         "05/09/2026",
        "description":      DESCRIPTION,
    },
]

resp = sb.table("events").upsert(EVENTS).execute()
print(f"✅ Upserted {len(EVENTS)} ClawCon events.")
