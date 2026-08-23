import json
import os
import subprocess
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

COLOR_ERROR = 0xD9534F

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        _session.mount("https://", HTTPAdapter(max_retries=retry))
    return _session


def load_state(path: str, default: dict) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default.copy()


def save_state(path: str, state: dict):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def post_embeds(embeds: list[dict]):
    if not _WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL not set — printing embed instead.")
        print(json.dumps(embeds, indent=2))
        return
    session = get_session()
    for i in range(0, len(embeds), 10):  # Discord allows max 10 embeds per request
        resp = session.post(
            _WEBHOOK_URL,
            json={"embeds": embeds[i:i + 10]},
            timeout=15,
        )
        resp.raise_for_status()


_KNOWLEDGE_BASE_DIR = os.path.expanduser(
    "~/Desktop/Byzantium_Knowledge/Trading/Index_Reconstitution"
)
_KNOWLEDGE_BASE_FILE = os.path.join(_KNOWLEDGE_BASE_DIR, "reconstitutions.json")


def save_to_knowledge_base(entries: list[dict]) -> None:
    """Append new entries to the JSON ledger and auto-commit to git."""
    if not entries:
        return

    os.makedirs(_KNOWLEDGE_BASE_DIR, exist_ok=True)

    try:
        with open(_KNOWLEDGE_BASE_FILE) as f:
            existing: list[dict] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    existing_ids = {e["id"] for e in existing}
    new_entries = [e for e in entries if e["id"] not in existing_ids]

    if not new_entries:
        return

    existing.extend(new_entries)
    with open(_KNOWLEDGE_BASE_FILE, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")

    try:
        subprocess.run(
            ["git", "add", "reconstitutions.json"],
            cwd=_KNOWLEDGE_BASE_DIR, check=True, capture_output=True,
        )
        date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        msg = f"data: record {len(new_entries)} reconstitution event(s) [{date_tag}]"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=_KNOWLEDGE_BASE_DIR, check=True, capture_output=True,
        )
        print(f"[KB] Committed {len(new_entries)} new entry(ies) to knowledge base.")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode().strip() if exc.stderr else ""
        print(f"[KB] Warning: git commit failed — {stderr}")


_CONSTITUENTS_FILE = os.path.join(_KNOWLEDGE_BASE_DIR, "constituents.json")


def save_constituents_to_knowledge_base(constituents: list[dict]) -> None:
    """Overwrite constituents.json with the latest snapshot and commit if changed."""
    if not constituents:
        return

    os.makedirs(_KNOWLEDGE_BASE_DIR, exist_ok=True)

    snapshot = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(constituents),
        "constituents": constituents,
    }

    try:
        with open(_CONSTITUENTS_FILE) as f:
            old = json.load(f)
        old_tickers = {c["ticker"] for c in old["constituents"]}
        had_snapshot = True
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        old_tickers = set()
        had_snapshot = False

    new_tickers = {c["ticker"] for c in constituents}

    # Membership is the payload; as_of alone is not a change. Returning before the write
    # keeps the knowledge-base repo clean instead of dirtying it on every scheduled run.
    if had_snapshot and old_tickers == new_tickers:
        print(f"[KB] Constituents unchanged ({len(constituents)} members) — no commit.")
        return

    with open(_CONSTITUENTS_FILE, "w") as f:
        f.write(json.dumps(snapshot, indent=2) + "\n")

    try:
        subprocess.run(
            ["git", "add", "constituents.json"],
            cwd=_KNOWLEDGE_BASE_DIR, check=True, capture_output=True,
        )
        date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        added   = new_tickers - old_tickers
        removed = old_tickers - new_tickers
        if added or removed:
            detail = f"+{len(added)}/-{len(removed)} members"
        else:
            detail = f"initial snapshot, {len(constituents)} members"
        msg = f"data: update constituents [{date_tag}] {detail}"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=_KNOWLEDGE_BASE_DIR, check=True, capture_output=True,
        )
        print(f"[KB] Committed constituent snapshot ({len(constituents)} members).")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode().strip() if exc.stderr else ""
        print(f"[KB] Warning: constituents git commit failed — {stderr}")


def post_alert(title: str, body: str, color: int = COLOR_ERROR):
    post_embeds([{
        "title":       title,
        "description": f"```{body[:1800]}```",
        "color":       color,
        "footer":      {"text": "Byzantium Technologies"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }])


def post_error(source_title: str, error_msg: str):
    post_alert(f"🔴  {source_title} — Scrape Error", error_msg)
