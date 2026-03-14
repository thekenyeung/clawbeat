#!/usr/bin/env python3
"""
scripts/migrate_daily_urls.py
──────────────────────────────
Migrates existing Daily Edition HTML files from the flat structure:
  public/daily/YYYY-MM-DD.html

To the new nested structure:
  public/daily/YYYY-MM-DD/[seo-slug].html

The original file is replaced with a lightweight redirect stub so that
any previously indexed or shared URLs continue to work.

Usage:
  python scripts/migrate_daily_urls.py [--dry-run]

Flags:
  --dry-run   Print what would happen without writing any files.
"""

import re
import sys
from pathlib import Path

DAILY_DIR = Path(__file__).parent.parent / "public" / "daily"
DRY_RUN = "--dry-run" in sys.argv


# ─── Helpers ─────────────────────────────────────────────────────────────────

def slugify(text: str, fallback: str = "edition") -> str:
    """Convert a headline into a URL-safe ASCII slug (max 60 chars)."""
    text = text.encode("ascii", "ignore").decode("ascii")  # strip non-ASCII
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or fallback


def extract_headline(html: str) -> str:
    """
    Extract story 1 headline from the baked <title> tag.
    Title format: "The Daily Edition: [HEADLINE] — ClawBeat · MM-DD-YYYY"
    """
    match = re.search(
        r"<title>The Daily Edition:\s*(.+?)\s*[—\-–]+\s*ClawBeat",
        html,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else "edition"


REDIRECT_STUB = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0; url={new_url}">
  <link rel="canonical" href="{new_url}">
  <title>Redirecting…</title>
  <script>window.location.replace("{new_url}");</script>
</head>
<body></body>
</html>
"""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    html_files = sorted(f for f in DAILY_DIR.glob("*.html") if re.match(r"\d{4}-\d{2}-\d{2}", f.stem))

    if not html_files:
        print("No flat daily HTML files found — nothing to migrate.")
        return

    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Migrating {len(html_files)} files…\n")

    for f in html_files:
        date_str = f.stem  # YYYY-MM-DD
        html = f.read_text(encoding="utf-8")

        headline = extract_headline(html)
        slug = slugify(headline, fallback=date_str)
        new_url = f"https://clawbeat.co/daily/{date_str}/{slug}.html"

        new_dir = DAILY_DIR / date_str
        new_file = new_dir / f"{slug}.html"

        print(f"  {date_str}")
        print(f"    headline : {headline[:70]}")
        print(f"    slug     : {slug}")
        print(f"    new path : public/daily/{date_str}/{slug}.html")

        if DRY_RUN:
            print()
            continue

        # Create subdirectory and write content to new location,
        # updating the canonical / og:url to point to the new path.
        new_dir.mkdir(exist_ok=True)
        old_url = f"https://clawbeat.co/daily/{date_str}.html"
        updated_html = html.replace(old_url, new_url)
        new_file.write_text(updated_html, encoding="utf-8")

        # Replace original flat file with a redirect stub
        stub = REDIRECT_STUB.format(new_url=new_url)
        f.write_text(stub, encoding="utf-8")

        print(f"    ✓ written\n")

    if not DRY_RUN:
        print("Done. Stage and commit the changes:")
        print("  git add public/daily/")
        print('  git commit -m "daily: migrate URLs to /daily/[date]/[slug].html"')


if __name__ == "__main__":
    main()
