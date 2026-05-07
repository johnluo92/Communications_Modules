# Communications Modules — Project Context

**Owner:** Byzantium Technologies  
**Knowledge base:** `~/Desktop/Byzantium_Knowledge/`

---

## What's here

Two independent modules, each in its own subdirectory:

### Index_reconstitution/
Monitors S&P index constituent changes and posts alerts to Discord.

| File | Role |
|------|------|
| `sp500_tracker.py` | Scrapes Wikipedia S&P 500 changes table. Run Fridays ~18:00 ET. |
| `spglobal_tracker.py` | Polls S&P Global Press Room RSS every 30 min (Mon–Fri market hours). |
| `sp500_common.py` | Shared utilities: HTTP session, Discord webhook, state I/O, knowledge base writer. |
| `sp500_state.json` | Dedup state for Wikipedia tracker (seen change keys). |
| `spglobal_state.json` | Dedup state for RSS tracker (seen URLs). |

**Knowledge base pipeline:** On every new event, both trackers call `save_to_knowledge_base()` (in `sp500_common.py`), which appends to `~/Desktop/Byzantium_Knowledge/Trading/Index_Reconstitution/reconstitutions.json` and auto-commits to that git repo.  
→ Full spec: `~/Desktop/Byzantium_Knowledge/Trading/index_reconstitution_pipeline.md`

**Backfill:** `python3 sp500_tracker.py --backfill` re-seeds the knowledge base from the full Wikipedia history without posting to Discord.

**Cron (reference):**
```
0 18 * * 5   python3 /path/to/sp500_tracker.py       # weekly, Fridays
*/30 9-17 * * 1-5  python3 /path/to/spglobal_tracker.py  # every 30 min, market hours
```

**Env:** Requires `DISCORD_WEBHOOK_URL` in `.env` (see `env.example`).

---

### Discord summary/
One-shot summarizer for the `compound-knowledge` channel export (9,710 messages, 2022–2026).  
→ Full instructions in `Discord summary/HANDOFF.md`  
→ Output goes to `Discord summary/output/summary.md`  
No API key or script needed — Claude processes the JSON directly in-session.

---

## Cross-instance handoff

If another Claude instance needs context on this project, point it at this file and the relevant knowledge base doc:
- Index reconstitution: `~/Desktop/Byzantium_Knowledge/Trading/index_reconstitution_pipeline.md`
- General Byzantium context: `~/Desktop/Byzantium_Knowledge/CLAUDE.md`
