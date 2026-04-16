#!/usr/bin/env python3
"""
Update the podcast RSS feed with a new episode.

Usage:
    python update_feed.py --date 2026-04-15 --mp3 digests/digest-2026-04-15.mp3
    python update_feed.py --date 2026-04-15  # auto-finds MP3 in digests/
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FEED_PATH = SCRIPT_DIR / "docs" / "feed.xml"
REPO = "yleng/weekly-research-digest"
PAGES_URL = f"https://yleng.github.io/weekly-research-digest"


def get_mp3_size(path: str) -> int:
    return os.path.getsize(path)


def get_mp3_duration_approx(path: str) -> int:
    """Approximate MP3 duration in seconds from file size (128kbps estimate)."""
    size = os.path.getsize(path)
    return max(60, size // 16000)  # 128kbps = 16KB/s


def create_release_and_upload(date: str, mp3_path: str) -> str:
    """Create a GitHub release and upload the MP3. Returns the download URL."""
    tag = f"v{date}"
    title = f"Weekly Digest {date}"

    # Read script file for release notes if available
    script_path = Path(mp3_path).with_suffix("").with_name(f"digest-{date}-script.txt")
    notes = f"Audio digest for week of {date}"
    if script_path.exists():
        text = script_path.read_text()
        # First few lines as notes
        notes = "\n".join(text.split("\n")[:5])

    # Create release
    result = subprocess.run(
        ["gh", "release", "create", tag,
         "--title", title,
         "--notes", notes,
         mp3_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Release might already exist — try uploading to it
        subprocess.run(
            ["gh", "release", "upload", tag, mp3_path, "--clobber"],
            capture_output=True, text=True
        )

    return f"https://github.com/{REPO}/releases/download/{tag}/digest-{date}.mp3"


def format_pub_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to RFC 2822 date format."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%a, %d %b %Y 09:00:00 -0500")


def read_summary(date: str) -> str:
    """Read the speech script to build episode summary."""
    script_path = SCRIPT_DIR / "digests" / f"digest-{date}-script.txt"
    if script_path.exists():
        text = script_path.read_text()
        # Extract first paragraph as summary
        paragraphs = text.split("\n\n")
        summary_parts = paragraphs[:3]
        return " ".join(line.strip() for p in summary_parts for line in p.split("\n") if line.strip())[:500]
    return f"Weekly research digest for {date}."


def update_feed(date: str, mp3_url: str, mp3_path: str):
    """Insert a new <item> into the RSS feed."""
    feed = FEED_PATH.read_text()

    size = get_mp3_size(mp3_path)
    duration = get_mp3_duration_approx(mp3_path)
    pub_date = format_pub_date(date)
    summary = read_summary(date)

    # Build week range
    dt = datetime.strptime(date, "%Y-%m-%d")
    from datetime import timedelta
    start = (dt - timedelta(days=7)).strftime("%b %d")
    end = dt.strftime("%b %d, %Y")

    new_item = f"""
    <item>
      <title>Week of {start}–{end}</title>
      <description>{summary}</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{mp3_url}" length="{size}" type="audio/mpeg"/>
      <guid isPermaLink="false">digest-{date}</guid>
      <itunes:duration>{duration}</itunes:duration>
      <itunes:summary>{summary}</itunes:summary>
    </item>
"""

    # Insert after the opening <channel> items but before existing <item>s
    # Find the first <item> and insert before it
    if f"<guid isPermaLink=\"false\">digest-{date}</guid>" in feed:
        print(f"  Episode for {date} already in feed, skipping.")
        return

    insert_pos = feed.find("<item>")
    if insert_pos == -1:
        # No items yet — insert before </channel>
        insert_pos = feed.find("</channel>")

    feed = feed[:insert_pos] + new_item + feed[insert_pos:]
    FEED_PATH.write_text(feed)
    print(f"  Feed updated with episode for {date}")


def main():
    parser = argparse.ArgumentParser(description="Update podcast feed with new episode")
    parser.add_argument("--date", required=True, help="Episode date (YYYY-MM-DD)")
    parser.add_argument("--mp3", help="Path to MP3 file (auto-detected if omitted)")
    args = parser.parse_args()

    mp3_path = args.mp3 or str(SCRIPT_DIR / "digests" / f"digest-{args.date}.mp3")
    if not Path(mp3_path).exists():
        print(f"Error: MP3 not found at {mp3_path}")
        sys.exit(1)

    print(f"Uploading MP3 as GitHub Release...")
    mp3_url = create_release_and_upload(args.date, mp3_path)
    print(f"  URL: {mp3_url}")

    print(f"Updating feed...")
    update_feed(args.date, mp3_url, mp3_path)

    print("Done! Don't forget to commit and push docs/feed.xml")


if __name__ == "__main__":
    main()
