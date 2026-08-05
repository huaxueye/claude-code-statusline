#!/usr/bin/env python3
"""Custom Claude Code status line with multi-period usage summary.

Features:
- 24h / 7d / 30d / all-time aggregated token & cost stats from DB
- Cache utilization rate (cumulative over selected period, not per-turn flicker)
- Togglable time-period via `sl-period` (bind a key to cycle)
- Auto-compact-aware context bar, current task label, tool-loop detection, tips
"""
import sys, json, os, re, time, datetime, sqlite3, secrets, hashlib

# ── ANSI helpers ──────────────────────────────────────────────────────
RESET = "\033[0m"

def usage_color(pct):
    if pct < 50:   return "\033[38;5;33m"
    elif pct < 70: return "\033[38;5;39m"
    elif pct < 85: return "\033[38;5;220m"
    elif pct < 95: return "\033[38;5;208m"
    else:          return "\033[38;5;196m"

EMPTY = "\033[38;5;236m"; DIM = "\033[38;5;240m"; BOLD = "\033[1m"
BLOCKS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
TRACK = "░"
LOOP_WINDOW = 5; LOOP_THRESHOLD = 3

# ── Pricing (¥ / 1M tokens) ───────────────────────────────────────────
PRICING = {
    "deepseek-v4-pro":   {"in": 3.0, "cache": 0.025, "out": 6.0, "cur": "¥"},
    "deepseek-v4-flash": {"in": 1.0, "cache": 0.02,  "out": 2.0, "cur": "¥"},
    "glm-5.2":           {"in": 2.0, "cache": 0.02,  "out": 4.0, "cur": "¥"},
    "claude-sonnet-5":   {"in": 21.0,"cache": 2.1,   "out": 84.0,"cur": "¥"},
    "claude-haiku-4-5":  {"in": 5.6, "cache": 0.56,  "out": 28.0,"cur": "¥"},
    "claude-opus-4-8":   {"in": 105.,"cache": 10.5,  "out": 420.,"cur": "¥"},
}

# ── Paths ─────────────────────────────────────────────────────────────
TRACKER_DIR  = os.path.expanduser("~/.claude-cost-tracker")
DB_PATH      = os.path.join(TRACKER_DIR, "usage.db")
THROTTLE     = os.path.join(TRACKER_DIR, ".last_write")
PERIOD_FILE  = os.path.join(TRACKER_DIR, ".period")

PERIODS = {
    "24h": (datetime.timedelta(hours=24), "24h"),
    "7d":  (datetime.timedelta(days=7),  "7d"),
    "30d": (datetime.timedelta(days=30), "30d"),
    "all": (None,                        "∞"),
}

def _state_path(sid): return os.path.join(TRACKER_DIR, f".acc_{sid}.json")

# ── Safety helpers ────────────────────────────────────────────────────
def _safe_session_id(raw):
    if not raw or not isinstance(raw, str): return None
    if "/" in raw or "\\" in raw or ".." in raw: return None
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:64] or None

def _atomic_write(path, payload):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nonce = secrets.token_hex(4)
        tmp = f"{path}.{os.getpid()}.{nonce}.tmp"
        with open(tmp, "w") as f: json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass

# ── Period state ──────────────────────────────────────────────────────
def _read_period():
    try:
        with open(PERIOD_FILE) as f: v = f.read().strip()
        return v if v in PERIODS else "24h"
    except Exception: return "24h"

# ── DB helpers ────────────────────────────────────────────────────────
def _query_period(period_key, exclude_sid=None):
    td, _ = PERIODS.get(period_key, PERIODS["24h"])
    conn = sqlite3.connect(DB_PATH); conn.execute("PRAGMA journal_mode=WAL")
    try:
        if exclude_sid:
            if td is not None:
                since = (datetime.datetime.now() - td).isoformat()
                row = conn.execute(
                    """SELECT COALESCE(SUM(total_input_tokens),0),
                              COALESCE(SUM(total_output_tokens),0),
                              COALESCE(SUM(cache_read_tokens),0),
                              COALESCE(SUM(total_cost_usd),0),
                              COUNT(DISTINCT session_id)
                       FROM sessions
                       WHERE updated_at >= ? AND session_id != ?""",
                    (since, exclude_sid)).fetchone()
            else:
                row = conn.execute(
                    """SELECT COALESCE(SUM(total_input_tokens),0),
                              COALESCE(SUM(total_output_tokens),0),
                              COALESCE(SUM(cache_read_tokens),0),
                              COALESCE(SUM(total_cost_usd),0),
                              COUNT(DISTINCT session_id)
                       FROM sessions
                       WHERE session_id != ?""",
                    (exclude_sid,)).fetchone()
        else:
            if td is not None:
                since = (datetime.datetime.now() - td).isoformat()
                row = conn.execute(
                    """SELECT COALESCE(SUM(total_input_tokens),0),
                              COALESCE(SUM(total_output_tokens),0),
                              COALESCE(SUM(cache_read_tokens),0),
                              COALESCE(SUM(total_cost_usd),0),
                              COUNT(DISTINCT session_id)
                       FROM sessions WHERE updated_at >= ?""",
                    (since,)).fetchone()
            else:
                row = conn.execute(
                    """SELECT COALESCE(SUM(total_input_tokens),0),
                              COALESCE(SUM(total_output_tokens),0),
                              COALESCE(SUM(cache_read_tokens),0),
                              COALESCE(SUM(total_cost_usd),0),
                              COUNT(DISTINCT session_id)
                       FROM sessions""").fetchone()
        return row if row else (0, 0, 0, 0, 0)
    except Exception: return (0, 0, 0, 0, 0)
    finally: conn.close()

# ── Task helpers ──────────────────────────────────────────────────────
def _tasks_dir(): return os.path.expanduser("~/.claude/tasks")

def _current_task(session_id):
    sid = _safe_session_id(session_id)
    if not sid: return "", 0, 0
    d = os.path.join(_tasks_dir(), sid)
    if not os.path.isdir(d): return "", 0, 0
    try:
        in_progress = None; pending = None; completed = 0; total = 0
        for name in os.listdir(d):
            if not name.endswith(".json"): continue
            try:
                with open(os.path.join(d, name)) as f: t = json.load(f)
            except Exception: continue
            total += 1
            status = t.get("status", "")
            if status == "completed": completed += 1; continue
            try: nid = int(name.rsplit(".", 1)[0])
            except ValueError: nid = 10 ** 9
            if status == "in_progress":
                if in_progress is None or nid < in_progress[0]:
                    in_progress = (nid, t.get("activeForm") or t.get("subject") or "")
            elif status == "pending":
                if pending is None or nid < pending[0]:
                    pending = (nid, t.get("activeForm") or t.get("subject") or "")
        if in_progress:   return in_progress[1], completed, total
        elif pending:     return pending[1], completed, total
        return "", completed, total
    except Exception: return "", 0, 0

# ── Tool-loop detection ───────────────────────────────────────────────
def _hash_tool_call(name, tool_input):
    name = str(name or ""); ti = tool_input or {}
    if name == "Bash":
        key = str(ti.get("command", ""))[:160]
    elif name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        key = hashlib.sha256(json.dumps(
            {"fp": ti.get("file_path"), "os": ti.get("old_string"),
             "ns": ti.get("new_string"), "c": ti.get("content")},
            sort_keys=True, default=str).encode()).hexdigest()
    elif name in ("Read", "Grep", "Glob"):
        key = hashlib.sha256(json.dumps(
            {"fp": ti.get("file_path"), "off": ti.get("offset"),
             "lim": ti.get("limit"), "pat": ti.get("pattern")},
            sort_keys=True, default=str).encode()).hexdigest()
    elif ti.get("file_path"):
        key = str(ti["file_path"])
    else:
        key = json.dumps(ti, sort_keys=True, default=str)[:2048]
    return hashlib.sha256(f"{name}:{key}".encode()).hexdigest()[:8]

def _detect_loop(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path): return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > 256 * 1024:
                f.seek(-256 * 1024, os.SEEK_END); f.readline()
            tail = f.read().decode("utf-8", errors="ignore")
        recent = []
        for line in tail.splitlines():
            if not line.startswith("{"): continue
            try: row = json.loads(line)
            except Exception: continue
            msg = row.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list): continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    recent.append((block.get("name", ""),
                                   _hash_tool_call(block.get("name", ""),
                                                   block.get("input", {}) or {})))
        recent = recent[-LOOP_WINDOW:]
        if len(recent) < LOOP_THRESHOLD: return None
        counts = {}
        for e in recent: counts[e] = counts.get(e, 0) + 1
        for (tname, _h), c in counts.items():
            if c >= LOOP_THRESHOLD: return (tname, c)
    except Exception: return None
    return None

# ── Live cache rate (per-turn, no accumulation) ───────────────────────
def _live_cache(d):
    """Return (turn_input, turn_cache, cache_pct) for the current turn only."""
    cu = d.get("context_window", {}).get("current_usage", {})
    turn_input = cu.get("input_tokens", 0)
    turn_cache = cu.get("cache_read_input_tokens", 0)
    total = turn_input + turn_cache
    pct = round(turn_cache / max(total, 1) * 100) if total > 0 else 0
    return turn_input, turn_cache, pct

# ── Session stats (borrowed from pulse-line: [N] / [Rules] / [MCP])
def _transcript_stats(transcript_path):
    """Incremental parse of the transcript JSONL.

    Returns (turns, fresh_in, cache_read, out) where:
      - turns      = count of user/assistant messages
      - fresh_in   = Σ usage.input_tokens              (non-cached input)
      - cache_read = Σ usage.cache_read_input_tokens   (cached input)
      - out        = Σ usage.output_tokens

    Only the appended tail (bytes after the cached offset) is re-parsed, so a
    growing transcript costs ~nothing per render. The offset cache lives on
    disk (statusline runs as a fresh process each time).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0, 0, 0, 0
    base = os.path.basename(transcript_path) or "t"
    cache_file = os.path.join(TRACKER_DIR, f".stats_{base[:20]}.cache")
    offset = turns = fresh_in = cache_read = out = 0
    try:
        size = os.path.getsize(transcript_path)
        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    parts = f.read().strip().split("|")
                if len(parts) == 5:
                    offset, turns, fresh_in, cache_read, out = (int(x) for x in parts)
            except Exception:
                offset = turns = fresh_in = cache_read = out = 0
        if offset > size:  # transcript rewritten (e.g. compact) → restart
            offset = turns = fresh_in = cache_read = out = 0
        if offset < size:
            with open(transcript_path, "rb") as f:
                f.seek(offset)
                f.readline()  # discard possibly-partial first line
                for raw in f:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line[:1] != "{":
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    t = row.get("type")
                    if t in ("user", "assistant"):
                        turns += 1
                    if t == "assistant":
                        u = (row.get("message") or {}).get("usage") or {}
                        fresh_in  += u.get("input_tokens", 0) or 0
                        cache_read += u.get("cache_read_input_tokens", 0) or 0
                        out       += u.get("output_tokens", 0) or 0
                offset = f.tell()
            os.makedirs(TRACKER_DIR, exist_ok=True)
            try:
                with open(cache_file, "w") as f:
                    f.write("|".join(str(x) for x in (offset, turns, fresh_in, cache_read, out)))
            except Exception:
                pass
        return turns, fresh_in, cache_read, out
    except Exception:
        return turns, fresh_in, cache_read, out

def _count_rules(cwd):
    """Count project rule/skill files: CLAUDE.md/AGENTS.md + .claude/{rules,skills,agents}/*.md."""
    if not cwd:
        return 0
    count = 0
    try:
        for name in ("CLAUDE.md", "AGENTS.md", "claude.md", "agents.md"):
            if os.path.exists(os.path.join(cwd, name)):
                count += 1
        for sub in ("rules", "skills", "agents"):
            root = os.path.join(cwd, ".claude", sub)
            if os.path.isdir(root):
                for _, _, files in os.walk(root):
                    count += sum(1 for f in files if f.endswith(".md"))
    except Exception:
        pass
    return count

def _count_mcp(cwd):
    """Count configured MCP servers from global + user + project configs."""
    count = 0
    candidates = [os.path.expanduser("~/.claude.json"),
                  os.path.expanduser("~/.claude/.mcp.json")]
    if cwd:
        candidates.append(os.path.join(cwd, ".mcp.json"))
    for p in candidates:
        try:
            with open(p) as f:
                count += len((json.load(f).get("mcpServers") or {}))
        except Exception:
            pass
    return count

# ── Cost ───────────────────────────────────────────────────────────────
def _compute_cost_db(total_in, total_out, model_id, cache_read=0):
    """Estimate cost from cumulative tokens (fresh-in, cache-read, output)."""
    base = re.sub(r"\[\d+[km]\]$", "", model_id, flags=re.I)
    for key, p in PRICING.items():
        if key in base:
            return ((total_in / 1_000_000) * p["in"] +
                    (cache_read / 1_000_000) * p["cache"] +
                    (total_out / 1_000_000) * p["out"]), p["cur"]
    return 0, "¥"

# ── DB snapshot (throttled 30 s, uses cumulative values from CC) ─────
def _snapshot(d):
    now = time.time()
    try:
        if os.path.exists(THROTTLE):
            with open(THROTTLE) as f:
                if now - float(f.read().strip()) < 30: return
    except: pass
    try:
        mid = d.get("model", {}).get("id", "")
        # CC's context_window.total_* under-reports tokens (output often ~0),
        # so the transcript is the authoritative source for in/out/cache.
        _, total_in, cache_sum, total_ou = _transcript_stats(d.get("transcript_path", ""))
        c, _ = _compute_cost_db(total_in, total_ou, mid, cache_sum)
        if c is None: c = d.get("cost", {}).get("total_cost_usd", 0)
        conn = sqlite3.connect(DB_PATH); conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, timestamp, project, model, total_cost_usd,
                total_input_tokens, total_output_tokens, cache_read_tokens,
                total_duration_ms, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (d.get("session_id", ""), datetime.datetime.now().isoformat(),
             d.get("workspace", {}).get("current_dir", ""), mid,
             round(c, 6), total_in, total_ou, cache_sum,
             d.get("cost", {}).get("total_duration_ms", 0),
             datetime.datetime.now().isoformat()))
        conn.commit(); conn.close()
        os.makedirs(os.path.dirname(THROTTLE), exist_ok=True)
        with open(THROTTLE, "w") as f: f.write(str(now))
    except: pass

# ── Tip rotation ──────────────────────────────────────────────────────
import subprocess
TIP_SCRIPT = os.path.expanduser("~/.claude/tips/random_tip.py")
_tip_cache = None; _tip_ts = 0; _TIP_INTERVAL = 45

def _maybe_set_title_tip():
    global _tip_cache, _tip_ts
    now = time.time()
    if now - _tip_ts > _TIP_INTERVAL:
        try:
            r = subprocess.run([sys.executable, TIP_SCRIPT],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                _tip_cache = r.stdout.strip()
                sys.stdout.write(f"\033k💡 {_tip_cache}\033\\"); sys.stdout.flush()
        except: pass
        _tip_ts = now

# ── Formatters ────────────────────────────────────────────────────────
def _fmt_tok(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1000:      return f"{n//1000}K"
    return str(n)

def _fmt_cost(usd):
    cny = usd
    if cny < 0.005:  return ""
    if cny < 0.01:   return "<¥0.01"
    if cny < 1:      return f"¥{cny:.2f}"
    if cny < 100:    return f"¥{cny:.1f}"
    return f"¥{cny:.0f}"

# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════
d = json.loads(sys.stdin.read())
_maybe_set_title_tip()

m  = d.get("model", {}).get("display_name", "?")
m  = re.sub(r"\[\d+[km]\]$", "", m, flags=re.I)
ef = d.get("effort", {}).get("level", "")
cw = d.get("context_window", {})
u  = cw.get("used_percentage", "")
remaining_pct = cw.get("remaining_percentage")
it = cw.get("total_input_tokens", cw.get("current_usage", {}).get("input_tokens", 0))
cs = cw.get("context_window_size", 0)
wd = d.get("workspace", {}).get("current_dir", "")
sid_raw = d.get("session_id", "")
transcript_path = d.get("transcript_path", "")

period = _read_period()
_, period_label = PERIODS.get(period, PERIODS["24h"])
turn_in, turn_cache, live_cache_pct = _live_cache(d)

# Live current-session values (always up-to-date, no throttle).
# Transcript is the authoritative token source: CC's context_window.total_*
# under-reports (output is frequently ~0), so don't trust it for in/out.
live_sid = d.get("session_id", "")
_turns, _t_in, _t_cache, _t_out = _transcript_stats(transcript_path)
live_in  = _t_in
live_out = _t_out

# DB query: sum all sessions EXCEPT current (avoids staleness).
# display_in/out/cache computed in Group 3 (DB + current transcript).
db_in, db_out, _db_cache, db_cost, _db_sessions = _query_period(period, exclude_sid=live_sid)

# ── Group 1: identity ────────────────────────────────────────────────
group_identity = []
ident = f"{BOLD}{m}{RESET}"
if ef: ident += f"{DIM}·{ef}{RESET}"
group_identity.append(ident)
task_active, task_done, task_total = _current_task(sid_raw)
if task_active:
    short = task_active if len(task_active) <= 30 else task_active[:27] + "…"
    task_label = f"\033[1;97m▸ {short}{RESET}"
    if task_total > 0: task_label += f"{DIM} [{task_done}/{task_total}]{RESET}"
    group_identity.append(task_label)

# ── Group 2: context bar + tokens ─────────────────────────────────────
group_progress = []
pct = None
if remaining_pct is not None:
    try: pct = max(0, min(100, round(100 - float(remaining_pct))))
    except Exception: pct = 0
elif u != "":
    uv = float(u)
    if uv <= 1: uv *= 100
    pct = round(uv)
if pct is not None:
    color = usage_color(pct)
    bar = ""; remaining = round(pct * 0.8)
    for _ in range(10):
        slot_fill = min(remaining, 8); remaining -= slot_fill
        bar += (color + BLOCKS[slot_fill]) if slot_fill > 0 else (EMPTY + TRACK)
    bar += RESET
    group_progress.append(f"{bar} {color}{pct:>2}%{RESET}")
if cs:
    ck = round(cs / 1000)
    uk = round(cs * pct / 100 / 1000) if pct is not None else 0
    group_progress.append(f"{DIM}{uk}K/{ck}K{RESET}" if uk > 0 else f"{DIM}<1K/{ck}K{RESET}")

# ── Group 3: multi-period usage summary ───────────────────────────────
group_cost = []
group_cost.append(f"\033[38;5;117m[{period_label}]{RESET}")
# Current-session in/out/cache all come from the transcript (authoritative);
# other sessions come from the DB. Cache: DB(others) + transcript(current).
display_cache = _db_cache + _t_cache
display_in  = db_in  + _t_in
display_out = db_out + _t_out
# Hit-rate over the period = cache / (cache + fresh-in).
hit24 = round(display_cache / max(display_in + display_cache, 1) * 100) if display_in + display_cache > 0 else 0
if hit24 >= 80:   c_cache = "\033[38;5;42m"
elif hit24 >= 50: c_cache = "\033[38;5;220m"
else:             c_cache = "\033[38;5;208m"
# I/O/R color scheme: I=blue, O=purple, R=green (cache = savings), ¥=gold
C_IN  = "\033[38;5;39m"    # light blue
C_OUT = "\033[38;5;135m"   # purple
C_CACHE = "\033[38;5;42m"  # green
C_COST = "\033[38;5;220m"  # gold
# I / O / R together (I = fresh input, O = output, R = cache read over period)
group_cost.append(f"{C_IN}I{_fmt_tok(display_in)}{RESET}")
group_cost.append(f"{C_OUT}Out{_fmt_tok(display_out)}{RESET}")
group_cost.append(f"{C_CACHE}R{_fmt_tok(display_cache)}{RESET}")
group_cost.append(f"{c_cache}↺ {hit24}%{RESET}")
# cost: DB (past sessions) + live estimate (current session)
live_cost, _ = _compute_cost_db(live_in, live_out, d.get("model", {}).get("id", ""), _t_cache)
display_cost = db_cost + live_cost
if display_cost > 0:
    group_cost.append(f"{C_COST}{_fmt_cost(display_cost)}{RESET}")

# ── Group 4: session stats (pulse-line style: [N] / [Rules] / [MCP]) ─
group_stats = []
n_turns = _transcript_stats(transcript_path)[0]
n_rules = _count_rules(d.get("workspace", {}).get("current_dir", ""))
n_mcp   = _count_mcp(d.get("workspace", {}).get("current_dir", ""))
if n_turns:
    group_stats.append(f"{DIM}[N]{RESET} {n_turns}")
group_stats.append(f"{DIM}[Rules]{RESET} {n_rules}")
if n_mcp:
    group_stats.append(f"{DIM}[MCP]{RESET} {n_mcp}")

# ── Group 5: path & loop alarm ────────────────────────────────────────
group_path = []
if wd:
    home = os.path.expanduser("~")
    if wd.startswith(home): wd = "~" + wd[len(home):]
    parts = wd.split("/")
    group_path.append(f"{DIM}{'/'.join(parts[-2:]) if len(parts)>2 else wd}{RESET}")
loop = _detect_loop(transcript_path)
if loop:
    group_path.append(f"\033[1;31m⟲ {loop[0]}×{loop[1]}{RESET}")

# ── Render ────────────────────────────────────────────────────────────
SEP = f" {DIM}│{RESET} "
groups = [g for g in (group_identity, group_progress, group_cost, group_stats, group_path) if g]
print(SEP.join(" ".join(g) for g in groups))

_snapshot(d)
