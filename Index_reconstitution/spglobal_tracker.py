#!/usr/bin/env python3
"""
S&P Global Press Room monitor.
Polls the press room RSS feed every 30 min (Mon–Fri, market hours) for new
S&P 500 / MidCap 400 / SmallCap 600 constituent change announcements.
Posts to Discord only when a new announcement is found — silent on quiet runs.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from sp500_common import USER_AGENT, get_session, load_state, post_embeds, post_error, save_state, save_to_knowledge_base

STATE_FILE = os.path.join(os.path.dirname(__file__), "spglobal_state.json")
RSS_URL = "https://press.spglobal.com/index.php?s=2429&l=25&pagetemplate=rss"

_STATE_DEFAULT = {"seen_urls": [], "last_run": None}

COLOR_ALERT = 0x4A90D9
STALE_THRESHOLD_DAYS = 60

_INDEX_KEYWORDS = ("S&P 500", "S&P MidCap 400", "S&P SmallCap 600")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_stale(announcement: dict) -> bool:
    """True if the announcement date is older than STALE_THRESHOLD_DAYS."""
    try:
        dt = datetime.strptime(announcement["date"], "%B %d, %Y").replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS)
    except ValueError:
        return False


# ─── RSS ──────────────────────────────────────────────────────────────────────

def fetch_announcements() -> list[dict]:
    resp = get_session().get(RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS feed missing <channel> — feed structure may have changed.")

    announcements = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        url   = (item.findtext("link") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()

        if not any(kw in title for kw in _INDEX_KEYWORDS):
            continue

        try:
            date_str = parsedate_to_datetime(pub).strftime("%B %d, %Y")
        except Exception:
            date_str = pub

        announcements.append({"date": date_str, "title": title, "url": url})

    return announcements


# ─── Discord ──────────────────────────────────────────────────────────────────

def post_announcement(announcement: dict):
    embed = {
        "title":       "📢  S&P Index Change — Official Announcement",
        "description": f"**{announcement['title']}**",
        "url":         announcement["url"],
        "color":       COLOR_ALERT,
        "fields": [
            {"name": "📅  Announced",         "value": announcement["date"],                             "inline": True},
            {"name": "🔗  Full Press Release", "value": f"[Read on S&P Global]({announcement['url']})", "inline": True},
        ],
        "footer":    {"text": "Source: S&P Global Press Room  •  Byzantium Technologies"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_embeds([embed])
    print(f"[OK] Posted: {announcement['title']}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="S&P Global Press Room Monitor")
    parser.add_argument("--test", action="store_true",
                        help="Force-post the most recent announcement regardless of seen state.")
    args = parser.parse_args()

    state = load_state(STATE_FILE, _STATE_DEFAULT)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    seen_urls = set(state.get("seen_urls", []))

    print("[INFO] Fetching S&P Global press room RSS...")

    try:
        announcements = fetch_announcements()
    except Exception as exc:
        msg = f"Failed to fetch/parse S&P Global RSS feed: {exc}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        try:
            post_error("S&P Global Monitor", msg)
        except Exception:
            pass
        save_state(STATE_FILE, state)
        sys.exit(1)

    print(f"[INFO] Found {len(announcements)} S&P index announcement(s) in feed.")

    if args.test:
        if announcements:
            print(f"[TEST] Forcing post of: {announcements[0]['title']}")
            post_announcement(announcements[0])
        else:
            print("[TEST] No announcements found to test with.")
        save_state(STATE_FILE, state)
        return

    is_first_run = not seen_urls
    all_new       = [a for a in announcements if a["url"] not in seen_urls]
    stale_new     = [a for a in all_new if     _is_stale(a)]
    fresh_new     = [a for a in all_new if not _is_stale(a)]

    if stale_new:
        seen_urls.update(a["url"] for a in stale_new)
        print(f"[INFO] Silently marked {len(stale_new)} stale announcement(s) as seen.")

    def _to_kb_entries(announcements: list[dict]) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "id":          f"spglobal|{a['url']}",
                "source":      "spglobal_pressroom",
                "recorded_at": now,
                "date":        a["date"],
                "title":       a["title"],
                "url":         a["url"],
            }
            for a in announcements
        ]

    if is_first_run and fresh_new:
        seen_urls.update(a["url"] for a in fresh_new)
        print(f"[INFO] First run: seeding {len(fresh_new)} recent announcement(s) — no Discord post.")
        save_to_knowledge_base(_to_kb_entries(fresh_new))
    elif fresh_new:
        seen_urls.update(a["url"] for a in fresh_new)
        for announcement in reversed(fresh_new):
            post_announcement(announcement)
        save_to_knowledge_base(_to_kb_entries(fresh_new))
        print(f"[INFO] Posted {len(fresh_new)} new announcement(s).")
    else:
        print("[INFO] No new S&P index announcements.")

    state["seen_urls"] = list(seen_urls)
    save_state(STATE_FILE, state)
    print("[DONE]")


if __name__ == "__main__":
    main()
