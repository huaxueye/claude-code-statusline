#!/usr/bin/env python3
"""Backfill sessions table with authoritative transcript token counts.

CC's context_window.total_output_tokens under-reports (often ~0). This script
recomputes fresh_in / cache_read / out for every session from its transcript
and updates the DB, so historical rows reflect true usage.
"""
import json, os, re, sqlite3, glob

DB_PATH = os.path.expanduser("~/.claude-cost-tracker/usage.db")
PROJECTS = os.path.expanduser("~/.claude/projects")

PRICING = {
    "deepseek-v4-pro":   {"in": 3.0, "cache": 0.025, "out": 6.0, "cur": "¥"},
    "deepseek-v4-flash": {"in": 1.0, "cache": 0.02,  "out": 2.0, "cur": "¥"},
    "glm-5.2":           {"in": 2.0, "cache": 0.02,  "out": 4.0, "cur": "¥"},
    "claude-sonnet-5":   {"in": 21.0,"cache": 2.1,   "out": 84.0,"cur": "¥"},
    "claude-haiku-4-5":  {"in": 5.6, "cache": 0.56,  "out": 28.0,"cur": "¥"},
    "claude-opus-4-8":   {"in": 105.,"cache": 10.5,  "out": 420.,"cur": "¥"},
}

def cost(total_in, total_out, model_id, cache_read=0):
    base = re.sub(r"\[\d+[km]\]$", "", model_id or "", flags=re.I)
    for key, p in PRICING.items():
        if key in base:
            return ((total_in / 1_000_000) * p["in"] +
                    (cache_read / 1_000_000) * p["cache"] +
                    (total_out / 1_000_000) * p["out"])
    return 0.0

def find_transcript(session_id):
    for p in glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl")):
        return p
    return None

def parse_transcript(path):
    fresh = cache = out = 0
    try:
        with open(path) as f:
            for line in f:
                try: row = json.loads(line)
                except Exception: continue
                if row.get("type") != "assistant": continue
                u = (row.get("message") or {}).get("usage") or {}
                fresh += u.get("input_tokens", 0) or 0
                cache += u.get("cache_read_input_tokens", 0) or 0
                out   += u.get("output_tokens", 0) or 0
    except Exception:
        return None
    return fresh, cache, out

conn = sqlite3.connect(DB_PATH)
rows = conn.execute("SELECT session_id, model FROM sessions").fetchall()
print(f"found {len(rows)} sessions in DB")
updated = 0
for sid, model in rows:
    path = find_transcript(sid)
    if not path:
        print(f"  - {sid[:8]}  (transcript not found)")
        continue
    stats = parse_transcript(path)
    if not stats:
        print(f"  - {sid[:8]}  (transcript unreadable)")
        continue
    fresh, cache, out = stats
    c = cost(fresh, out, model, cache)
    conn.execute(
        "UPDATE sessions SET total_input_tokens=?, total_output_tokens=?, "
        "cache_read_tokens=?, total_cost_usd=? WHERE session_id=?",
        (fresh, out, cache, round(c, 6), sid))
    updated += 1
    print(f"  + {sid[:8]}  in={fresh:,}  out={out:,}  cache={cache:,}  cost={c:.2f}")
conn.commit()
print(f"\ndone: {updated}/{len(rows)} sessions updated")
conn.close()
