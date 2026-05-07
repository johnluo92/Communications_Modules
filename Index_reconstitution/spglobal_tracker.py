#!/usr/bin/env python3
"""
S&P Global Press Room monitor.
Polls the press room RSS feed every 30 min (Mon–Fri, market hours) for new
S&P 500 / MidCap 400 / SmallCap 600 constituent change announcements.
Posts to Discord only when a new announcement is found — silent on quiet runs.
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from sp500_common import USER_AGENT, get_session, load_state, post_embeds, post_error, save_state, save_to_knowledge_base

STATE_FILE = os.path.join(os.path.dirname(__file__), "spglobal_state.json")
RSS_URL = "https://press.spglobal.com/index.php?s=2429&l=25&pagetemplate=rss"

_STATE_DEFAULT = {"seen_urls": [], "last_run": None}

COLOR_ALERT = 0x4A90D9
STALE_THRESHOLD_DAYS = 60

_INDEX_KEYWORDS = ("S&P 500", "S&P MidCap 400", "S&P SmallCap 600")


# ─── Helpers ──────────────────────────────────────────────────────────────────

_EXCHANGE_TICKER_RE = re.compile(
    r'(?:NYSE(?:\s+(?:American|Arca))?|NASDAQ|NASD|BATS):\s*([A-Z]{1,5}(?:\.[A-Z]{1,2})?)',
    re.IGNORECASE,
)
_BOILERPLATE_TICKERS = {"SPGI"}


def _fetch_tickers(url: str) -> dict[str, list[str]]:
    """Fetch the press release and return {'additions': [...], 'removals': [...]}.

    Parses each change bullet: '[Joiner (TICKER_A)] will replace [Leaver (TICKER_B)].
    [Acquirer context...]' — splits on 'will replace' and ignores sentences after the
    first to avoid picking up acquirer tickers as index members.
    """
    try:
        resp = get_session().get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[WARN] Could not fetch press release {url}: {exc}")
        return {"additions": [], "removals": []}

    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.find("div", class_="wd_news_body") or soup.find("div", class_="wd_body")

    additions: list[str] = []
    removals:  list[str] = []

    if body:
        # Some releases use <li> per change; others use <p> paragraphs.
        segments = [el.get_text(strip=True) for el in body.find_all("li")]
        if not segments:
            segments = [
                el.get_text(strip=True) for el in body.find_all("p")
                if "will replace" in el.get_text(strip=True).lower()
            ]

        for text in segments:
            idx = text.lower().find("will replace")
            if idx == -1:
                for m in _EXCHANGE_TICKER_RE.finditer(text):
                    t = m.group(1).upper()
                    if t not in _BOILERPLATE_TICKERS:
                        additions.append(t)
            else:
                for m in _EXCHANGE_TICKER_RE.finditer(text[:idx]):
                    t = m.group(1).upper()
                    if t not in _BOILERPLATE_TICKERS:
                        additions.append(t)
                # Take only the first ticker after "will replace" — that's the leaver.
                # Subsequent tickers belong to acquirer context sentences.
                m = _EXCHANGE_TICKER_RE.search(text[idx:])
                if m:
                    t = m.group(1).upper()
                    if t not in _BOILERPLATE_TICKERS:
                        removals.append(t)
    else:
        for m in _EXCHANGE_TICKER_RE.finditer(resp.text):
            t = m.group(1).upper()
            if t not in _BOILERPLATE_TICKERS:
                additions.append(t)

    return {
        "additions": list(dict.fromkeys(additions)),
        "removals":  list(dict.fromkeys(removals)),
    }


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
    fields = []

    tickers = announcement.get("tickers", {})
    additions = tickers.get("additions", [])
    removals  = tickers.get("removals",  [])

    if additions:
        fields.append({
            "name":   "✅  Joining",
            "value":  "  ".join(f"`{t}`" for t in additions),
            "inline": True,
        })
    if removals:
        fields.append({
            "name":   "❌  Leaving",
            "value":  "  ".join(f"`{t}`" for t in removals),
            "inline": True,
        })

    fields += [
        {"name": "📅  Announced",         "value": announcement["date"],                             "inline": True},
        {"name": "🔗  Full Press Release", "value": f"[Read on S&P Global]({announcement['url']})", "inline": True},
    ]

    embed = {
        "title":       "📢  S&P Index Change — Official Announcement",
        "description": f"**{announcement['title']}**",
        "url":         announcement["url"],
        "color":       COLOR_ALERT,
        "fields":      fields,
        "footer":      {"text": "Source: S&P Global Press Room  •  Byzantium Technologies"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    post_embeds([embed])
    print(f"[OK] Posted: {announcement['title']} | +{additions} -{removals}")


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
            a = announcements[0]
            a["tickers"] = _fetch_tickers(a["url"])
            print(f"[TEST] Forcing post of: {a['title']}")
            print(f"[TEST] Tickers: {a['tickers']}")
            post_announcement(a)
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

    def _enrich(announcements: list[dict]) -> list[dict]:
        for a in announcements:
            a["tickers"] = _fetch_tickers(a["url"])
        return announcements

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
                "tickers":     a.get("tickers", {"additions": [], "removals": []}),
            }
            for a in announcements
        ]

    if is_first_run and fresh_new:
        seen_urls.update(a["url"] for a in fresh_new)
        print(f"[INFO] First run: seeding {len(fresh_new)} recent announcement(s) — no Discord post.")
        save_to_knowledge_base(_to_kb_entries(_enrich(fresh_new)))
    elif fresh_new:
        seen_urls.update(a["url"] for a in fresh_new)
        _enrich(fresh_new)
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
