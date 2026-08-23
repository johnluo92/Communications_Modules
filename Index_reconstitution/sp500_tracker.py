#!/usr/bin/env python3
"""
S&P 500 Reconstitution Tracker
Monitors index additions/removals and posts formatted reports to Discord.
Run weekly via cron: 0 18 * * 5 /usr/bin/python3 /path/to/sp500_tracker.py
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from sp500_common import USER_AGENT, get_session, load_state, post_alert, post_embeds, post_error, save_state, save_to_knowledge_base, save_constituents_to_knowledge_base

STATE_FILE = os.path.join(os.path.dirname(__file__), "sp500_state.json")

# Wikipedia split these into two articles on 2026-08-11 ("move to [[Historical
# components of the S&P 500]]"), which broke a scraper that assumed both tables
# lived on one page. Each lookup searches both pages, so a move in either
# direction is absorbed without a code change.
CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL      = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"

MAX_CHANGES_TO_DISPLAY = 20  # guard against scraping huge history on first run
STALE_THRESHOLD_DAYS = 60   # changes older than this are silently marked seen, never alerted
MIN_EXPECTED_CONSTITUENTS = 450  # a short parse means drift; refuse to overwrite the KB snapshot
ROW_DROP_ALERT = 5  # history only grows; a shrink this large means rows were moved or lost

_STATE_DEFAULT = {"seen_keys": [], "run_count": 0, "last_run": None,
                  "last_heartbeat_month": None, "last_changes_rows": None}

COLOR_ALERT     = 0xE8C547   # gold — new changes
COLOR_HEARTBEAT = 0x2B2D31   # dark — no changes / status
COLOR_DRIFT     = 0xE8A33D   # amber — source structure changed, not an outage


# ─── Scraping ─────────────────────────────────────────────────────────────────

def _cell(cols: list, i: int) -> str:
    return cols[i].get_text(separator=" ", strip=True) if i < len(cols) else ""


def _strip_footnotes(text: str) -> str:
    """Drop the citation suffix, then any trailing pipe. Broken wiki markup leaks a literal '|'
    into rendered cells, and '|' is the dedup-key delimiter — an unsanitised cell both corrupts
    the key and would enter the ledger as a ticker like 'ALLE |'."""
    return text.split("[")[0].strip().rstrip("|").strip()


def _latest_date(changes: list[dict]) -> str | None:
    """Newest change by effective date. Table order is editor-controlled, so the first row is not
    reliably the newest — taking it made the reported 'Last Known Change' run backwards."""
    dated = [(d, c["date"]) for c in changes if (d := _parse_change_date(c["date"]))]
    return max(dated)[1] if dated else (changes[0]["date"] if changes else None)


def _parse_change_date(date_str: str) -> datetime | None:
    for fmt in ("%B %d, %Y", "%B %Y"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_stale(date_str: str) -> bool:
    """True if the change's effective date is older than STALE_THRESHOLD_DAYS."""
    dt = _parse_change_date(date_str)
    if dt is None:
        return False
    return dt < datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS)


_soup_cache: dict[str, BeautifulSoup] = {}


def _soup(url: str) -> BeautifulSoup:
    if url not in _soup_cache:
        resp = get_session().get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        _soup_cache[url] = BeautifulSoup(resp.text, "html.parser")
    return _soup_cache[url]


def _locate_table(label: str, table_id: str, header_hint: str, urls: list[str]):
    """Find a wikitable by anchor id, falling back to its header text, across candidate pages.

    Neither table position nor host article is stable — editors reorder tables and move
    sections between articles. The anchor id and the header wording are the durable keys.
    Raises with page diagnostics so the Discord error alone identifies the next drift.
    """
    diag = []
    for url in urls:
        soup = _soup(url)
        found = soup.find("table", id=table_id)
        if found is not None:
            return found
        for cand in soup.find_all("table", {"class": "wikitable"}):
            head = cand.find("tr")
            if head and header_hint in head.get_text(" ", strip=True).lower():
                return cand
        ids = [t.get("id") or "-" for t in soup.find_all("table", {"class": "wikitable"})]
        diag.append(f"{url.rsplit('/', 1)[-1]} (wikitables: {len(ids)}, ids: {', '.join(ids) or 'none'})")
    raise RuntimeError(
        f"Could not locate the {label} table. Searched id='{table_id}' and a header "
        f"containing '{header_hint}' on: {' | '.join(diag)}. "
        "Wikipedia likely moved or restructured the table again."
    )


def _data_rows(table) -> list:
    """Rows carrying actual data. Header rows are pure <th>, and these tables use one or
    two of them (the changes table has a merged header plus a sub-header)."""
    return [r for r in table.find_all("tr") if r.find("td") is not None]


def fetch_constituents() -> list[dict]:
    table = _locate_table("constituents", "constituents", "symbol", [CONSTITUENTS_URL, CHANGES_URL])

    constituents = []
    for row in _data_rows(table):
        cols = row.find_all(["td", "th"])
        if len(cols) < 4:
            continue
        constituents.append({
            "ticker":        _strip_footnotes(_cell(cols, 0)),
            "name":          _strip_footnotes(_cell(cols, 1)),
            "sector":        _strip_footnotes(_cell(cols, 2)),
            "sub_industry":  _strip_footnotes(_cell(cols, 3)),
            "headquarters":  _strip_footnotes(_cell(cols, 4)) if len(cols) > 4 else "",
            "date_added":    _strip_footnotes(_cell(cols, 5)) if len(cols) > 5 else "",
            "cik":           _strip_footnotes(_cell(cols, 6)) if len(cols) > 6 else "",
            "founded":       _strip_footnotes(_cell(cols, 7)) if len(cols) > 7 else "",
        })

    return constituents


def fetch_changes() -> list[dict]:
    table = _locate_table("changes", "changes", "effective date", [CHANGES_URL, CONSTITUENTS_URL])

    changes = []
    for row in _data_rows(table):
        cols = row.find_all(["td", "th"])
        if len(cols) < 4:
            continue

        date_str       = _cell(cols, 0)
        added_ticker   = _strip_footnotes(_cell(cols, 1))
        added_name     = _strip_footnotes(_cell(cols, 2))
        removed_ticker = _strip_footnotes(_cell(cols, 3))
        removed_name   = _strip_footnotes(_cell(cols, 4))
        reason         = _strip_footnotes(_cell(cols, 5))

        if not date_str or date_str.lower() == "date":
            continue

        key = f"{date_str}|{added_ticker}|{removed_ticker}"
        changes.append({
            "date":            date_str,
            "added_ticker":    added_ticker,
            "added_name":      added_name,
            "removed_ticker":  removed_ticker,
            "removed_name":    removed_name,
            "reason":          reason,
            "key":             key,
        })

    return changes


# ─── Discord ──────────────────────────────────────────────────────────────────

def post_changes(new_changes: list[dict]):
    by_date: dict[str, list[dict]] = {}
    for c in new_changes:
        by_date.setdefault(c["date"], []).append(c)

    embeds = []
    for change_date, changes in by_date.items():
        additions = [
            (c["added_ticker"], c["added_name"])
            for c in changes
            if c["added_ticker"] and c["added_ticker"] != "—"
        ]
        removals = [
            (c["removed_ticker"], c["removed_name"])
            for c in changes
            if c["removed_ticker"] and c["removed_ticker"] != "—"
        ]
        reasons = list({c["reason"] for c in changes if c["reason"]})

        fields = []

        if additions:
            fields.append({
                "name":   "✅  Joining the Index",
                "value":  "\n".join(f"`{t:<6}` **{n}**" for t, n in additions),
                "inline": False,
            })

        if removals:
            fields.append({
                "name":   "❌  Leaving the Index",
                "value":  "\n".join(f"`{t:<6}` ~~{n}~~" for t, n in removals),
                "inline": False,
            })

        if reasons:
            fields.append({
                "name":   "📋  Rationale",
                "value":  "\n".join(f"• {r}" for r in reasons),
                "inline": False,
            })

        fields += [
            {"name": "📅  Effective Date", "value": change_date,                              "inline": True},
            {"name": "🔢  Changes",        "value": f"{len(additions)} in · {len(removals)} out", "inline": True},
        ]

        embeds.append({
            "title":       "📊  S&P 500 — Index Reconstitution Alert",
            "description": (
                "The S&P Index Committee has confirmed the following constituent changes.\n"
                "Passive funds will execute corresponding trades at the effective date."
            ),
            "color":  COLOR_ALERT,
            "fields": fields,
            "footer": {"text": "Source: S&P Global via Wikipedia  •  Byzantium Technologies"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    if embeds:
        post_embeds(embeds)
        print(f"[OK] Posted {len(embeds)} embed(s) covering {len(new_changes)} change(s).")


def post_heartbeat(run_count: int, last_change_date: str | None):
    embed = {
        "title":       "🟢  S&P 500 Tracker — Weekly Status",
        "description": "Routine scan complete. No new reconstitution events detected this cycle.",
        "color":       COLOR_HEARTBEAT,
        "fields":      [
            {"name": "Last Known Change",    "value": last_change_date or "None on record", "inline": True},
            {"name": "Total Runs",           "value": str(run_count),                       "inline": True},
            {"name": "Next Scheduled Scan",  "value": "Friday ~18:00 ET",                  "inline": True},
        ],
        "footer":    {"text": "Byzantium Technologies  •  S&P 500 Reconstitution Monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_embeds([embed])
    print("[OK] Posted heartbeat (no changes).")


# ─── Heartbeat ────────────────────────────────────────────────────────────────

def _send_heartbeat_if_due(state: dict):
    """Post a status heartbeat at most once per calendar month."""
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_heartbeat_month") == current_month:
        return
    post_heartbeat(state["run_count"], state.get("last_change_date"))
    state["last_heartbeat_month"] = current_month


# ─── Main ─────────────────────────────────────────────────────────────────────

def _to_kb_entries(changes: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id":              f"sp500|{c['key']}",
            "source":          "sp500_wikipedia",
            "recorded_at":     now,
            "date":            c["date"],
            "added_ticker":    c["added_ticker"],
            "added_name":      c["added_name"],
            "removed_ticker":  c["removed_ticker"],
            "removed_name":    c["removed_name"],
            "reason":          c["reason"],
        }
        for c in changes
    ]


def main():
    parser = argparse.ArgumentParser(description="S&P 500 Reconstitution Tracker")
    parser.add_argument("--test",      action="store_true", help="Force-post the most recent change.")
    parser.add_argument("--heartbeat", action="store_true", help="Force-post a heartbeat status message.")
    parser.add_argument("--reset",     action="store_true", help="Wipe local state (use with care).")
    parser.add_argument("--backfill",  action="store_true", help="Seed knowledge base from full Wikipedia history (no Discord post).")
    args = parser.parse_args()

    if args.reset:
        try:
            os.remove(STATE_FILE)
            print("[OK] State reset.")
        except FileNotFoundError:
            print("[OK] No state file found.")
        return

    state = load_state(STATE_FILE, _STATE_DEFAULT)
    state["run_count"] = state.get("run_count", 0) + 1
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    seen_keys = set(state.get("seen_keys", []))

    if args.heartbeat:
        post_heartbeat(state["run_count"], state.get("last_change_date"))
        save_state(STATE_FILE, state)
        return

    print(f"[INFO] Run #{state['run_count']} — fetching changes from Wikipedia...")

    try:
        all_changes = fetch_changes()
    except Exception as exc:
        msg = f"Failed to fetch/parse Wikipedia: {exc}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        try:
            post_error("S&P 500 Tracker", msg)
        except Exception:
            pass
        save_state(STATE_FILE, state)
        sys.exit(1)

    print(f"[INFO] Found {len(all_changes)} total historical rows.")

    # The changes table is append-only in practice, so a shrink means an editor moved,
    # merged, or deleted rows — the silent half of the failure that a hard scrape error
    # makes loud. Alert, but never block: the rows we did get are still valid.
    prev_rows = state.get("last_changes_rows")
    state["last_changes_rows"] = len(all_changes)
    if prev_rows and len(all_changes) < prev_rows - ROW_DROP_ALERT:
        drift = (f"Changes table shrank {prev_rows} → {len(all_changes)} rows since the last run.\n"
                 f"Source: {CHANGES_URL}\nRows were moved, merged, or deleted upstream. "
                 "Scraping continued normally.")
        print(f"[WARN] {drift}", file=sys.stderr)
        try:
            post_alert("🟠  S&P 500 Tracker — Source Drift", drift, COLOR_DRIFT)
        except Exception:
            pass

    try:
        constituents = fetch_constituents()
        if len(constituents) < MIN_EXPECTED_CONSTITUENTS:
            raise RuntimeError(
                f"only {len(constituents)} constituents parsed (expected ≥{MIN_EXPECTED_CONSTITUENTS}) "
                "— refusing to overwrite the knowledge-base snapshot with a partial parse"
            )
        print(f"[INFO] Fetched {len(constituents)} current constituents.")
        save_constituents_to_knowledge_base(constituents)
    except Exception as exc:
        print(f"[WARN] Could not update constituents: {exc}", file=sys.stderr)

    if args.backfill:
        save_to_knowledge_base(_to_kb_entries(all_changes))
        print("[BACKFILL] Knowledge base seeded from full Wikipedia history.")
        save_state(STATE_FILE, state)
        return

    if args.test:
        if all_changes:
            print(f"[TEST] Forcing post of: {all_changes[0]['key']}")
            post_changes([all_changes[0]])
        else:
            print("[TEST] No changes found to test with.")
        save_state(STATE_FILE, state)
        return

    is_first_run = not seen_keys
    all_new = [c for c in all_changes if c["key"] not in seen_keys]

    # Silently absorb stale historical entries regardless of first-run status.
    # This prevents old Wikipedia rows from being posted if state is ever reset.
    stale_new  = [c for c in all_new if     _is_stale(c["date"])]
    fresh_new  = [c for c in all_new if not _is_stale(c["date"])]

    if stale_new:
        seen_keys.update(c["key"] for c in stale_new)
        print(f"[INFO] Silently marked {len(stale_new)} stale historical change(s) as seen.")

    new_changes = fresh_new[:MAX_CHANGES_TO_DISPLAY]

    if is_first_run and new_changes:
        seen_keys.update(c["key"] for c in fresh_new)
        state["last_change_date"] = _latest_date(fresh_new)
        print(f"[INFO] First run: seeding {len(fresh_new)} recent change(s), no Discord post.")
        save_to_knowledge_base(_to_kb_entries(fresh_new))
        _send_heartbeat_if_due(state)
    elif new_changes:
        seen_keys.update(c["key"] for c in fresh_new)  # mark all as seen, not just displayed
        state["last_change_date"] = _latest_date(fresh_new)
        print(f"[INFO] {len(new_changes)} new change(s) detected.")
        # A failed post must not cost the run's state: seen_keys is already updated, so an
        # exception escaping here would discard it and re-post everything next week.
        try:
            post_changes(new_changes)
        except Exception as exc:
            print(f"[ERROR] Discord post failed, state still saved: {exc}", file=sys.stderr)
        save_to_knowledge_base(_to_kb_entries(fresh_new))
    else:
        print("[INFO] No new changes.")
        try:
            _send_heartbeat_if_due(state)
        except Exception as exc:
            print(f"[WARN] Heartbeat post failed: {exc}", file=sys.stderr)

    state["seen_keys"] = sorted(seen_keys)   # stable order — see spglobal_tracker
    save_state(STATE_FILE, state)
    print("[DONE]")


if __name__ == "__main__":
    main()
