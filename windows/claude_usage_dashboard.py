"""
Claude Usage Dashboard - a tiny always-on-top desk widget.

Reads Claude Code's local session logs (~/.claude/projects/**/*.jsonl),
aggregates token usage + estimated cost per day / per model, and shows
it as a small pinnable graphic. No API key, no network.

Run GUI:       pythonw claude_usage_dashboard.py
Self-test:     python  claude_usage_dashboard.py --selftest
"""

import os
import sys
import json
import time
import glob
import queue
import threading
import datetime as dt
import urllib.request

LOG_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".claude_usage_dashboard.json")
CODEX_AUTH_FILE = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")

# USD per 1,000,000 tokens. Estimates only - not official billing.
PRICING = {
    "opus":   {"in": 15.0, "out": 75.0},
    "sonnet": {"in": 3.0,  "out": 15.0},
    "haiku":  {"in": 1.0,  "out": 5.0},
}
CACHE_READ_MULT = 0.10   # cache read  = 0.10x input rate
CACHE_5M_MULT = 1.25     # 5-min cache write = 1.25x input rate
CACHE_1H_MULT = 2.00     # 1-hour cache write = 2.00x input rate

# Catppuccin Mocha
C_BG = "#1e1e2e"
C_SURFACE = "#313244"
C_TEXT = "#cdd6f4"
C_SUB = "#a6adc8"
C_FAINT = "#585b70"
C_ACCENT = "#89b4fa"
C_GREEN = "#a6e3a1"
C_PEACH = "#fab387"
C_MAUVE = "#cba6f7"
C_RED = "#f38ba8"

MODEL_COLORS = {
    "opus": C_MAUVE,
    "sonnet": C_ACCENT,
    "haiku": C_GREEN,
    "other": C_PEACH,
}


# --------------------------------------------------------------------------
# Codex (ChatGPT) usage
# --------------------------------------------------------------------------
# Auth: reads Codex login from ~/.codex/auth.json. Manual ChatGPT cookie still
# works as a fallback via the "codex_session_token" key in SETTINGS_FILE.
_CODEX_CACHE = {"ts": 0.0, "data": None}
_CODEX_TTL   = 300  # 5 min

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _codex_local_access_token() -> str:
    try:
        with open(CODEX_AUTH_FILE, "r", encoding="utf-8") as f:
            return (((json.load(f).get("tokens") or {}).get("access_token")) or "").strip()
    except Exception:
        return ""


def _codex_fetch_fresh(session_token: str) -> dict:
    hdrs = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
    }
    access_tok = _codex_local_access_token()
    if not access_tok and session_token:
        hdrs["Cookie"] = f"__Secure-next-auth.session-token={session_token}"
        req = urllib.request.Request("https://chatgpt.com/api/auth/session",
                                     headers=hdrs)
        with urllib.request.urlopen(req, timeout=10) as r:
            sess = json.loads(r.read())
        access_tok = sess.get("accessToken")
    if not access_tok:
        return {"error": "no Codex auth - run codex login"}

    hdrs2 = dict(hdrs)
    hdrs2["Authorization"] = f"Bearer {access_tok}"
    req2 = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage",
                                  headers=hdrs2)
    with urllib.request.urlopen(req2, timeout=10) as r2:
        payload = json.loads(r2.read())

    rl = payload.get("rate_limit") or payload.get("rate_limits") or payload
    def _win(keys):
        for k in keys:
            v = rl.get(k)
            if isinstance(v, dict):
                return v
        return None
    primary   = _win(["primary_window",   "primary"])
    secondary = _win(["secondary_window", "secondary"])

    def _pct(d):
        if not d:
            return None
        for k in ("used_percent", "usage_percent", "used_percentage"):
            if k in d:
                try:
                    return float(d[k])
                except Exception:
                    pass
        return None

    def _reset(d):
        if not d:
            return None
        for k in ("resets_in_seconds", "reset_after_seconds", "resets_after_seconds"):
            if k in d:
                try:
                    return time.time() + float(d[k])
                except Exception:
                    pass
        for k in ("resets_at", "reset_at"):
            if k in d:
                try:
                    s = str(d[k]).replace("Z", "+00:00")
                    return dt.datetime.fromisoformat(s).timestamp()
                except Exception:
                    pass
        return None

    def _dur(d):
        if not d:
            return None
        for k in ("limit_window_seconds", "window_seconds"):
            if k in d:
                return float(d[k])
        mins = d.get("window_minutes")
        return float(mins) * 60 if mins else None

    sp, wp = _pct(primary), _pct(secondary)
    pd, sd = _dur(primary), _dur(secondary)
    if pd and sd and pd > sd:
        sp, wp       = wp, sp
        primary, secondary = secondary, primary

    return {
        "session_pct":   sp,
        "weekly_pct":    wp,
        "session_reset": _reset(primary),
        "weekly_reset":  _reset(secondary),
        "plan":          (payload.get("plan_type") or ""),
        "error":         None,
    }


def codex_fetch(session_token: str) -> dict:
    """Cached fetch. Empty session_token uses ~/.codex/auth.json."""
    now = time.time()
    if now - _CODEX_CACHE["ts"] < _CODEX_TTL and _CODEX_CACHE["data"] is not None:
        return _CODEX_CACHE["data"]
    try:
        result = _codex_fetch_fresh(session_token)
    except Exception as e:
        result = {"error": str(e)[:60], "session_pct": None, "weekly_pct": None}
    _CODEX_CACHE["ts"]   = now
    _CODEX_CACHE["data"] = result
    return result


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------
def model_family(model):
    if not model:
        return "other"
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return "other"


def _record_cost(fam, inp, out, cr, c5, c1):
    p = PRICING.get(fam, PRICING["sonnet"])
    return (
        inp * p["in"]
        + out * p["out"]
        + cr * p["in"] * CACHE_READ_MULT
        + c5 * p["in"] * CACHE_5M_MULT
        + c1 * p["in"] * CACHE_1H_MULT
    ) / 1_000_000.0


def _parse_records(path):
    """Parse one jsonl file -> list of normalized records."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                try:
                    s = (rec.get("timestamp") or "").replace("Z", "+00:00")
                    ts = dt.datetime.fromisoformat(s)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=dt.timezone.utc)
                    epoch = ts.timestamp()
                    day = dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
                except Exception:
                    continue
                dedup_key = (msg.get("id"), rec.get("requestId"))
                if dedup_key == (None, None):
                    dedup_key = (rec.get("uuid"), None)
                fam = model_family(msg.get("model"))
                inp = int(usage.get("input_tokens", 0) or 0)
                o = int(usage.get("output_tokens", 0) or 0)
                cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                cc = usage.get("cache_creation") or {}
                if cc:
                    c5 = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
                    c1 = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
                else:
                    c5 = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    c1 = 0
                err = rec.get("error", "") or ""
                text = ""
                content = msg.get("content") or []
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        text = first.get("text", "") or ""
                out.append({
                    "k": dedup_key,
                    "day": day,
                    "fam": fam,
                    "in": inp,
                    "out": o,
                    "cache": cr + c5 + c1,
                    "cost": _record_cost(fam, inp, o, cr, c5, c1),
                    "epoch": epoch,
                    "total": inp + o + cr + c5 + c1,
                    "is_rl": err == "rate_limit" and "hit your" in text and "limit" in text,
                    "is_eu": err == "rate_limit" and "out of extra usage" in text,
                })
    except Exception:
        pass
    return out


class UsageData:
    """Parses logs with a per-file mtime cache so refreshes stay cheap."""

    def __init__(self):
        self._cache = {}  # path -> (mtime, size, [records])
        self._refresh_lock = threading.Lock()
        self.file_count = 0
        self.error = None

    def refresh(self):
        with self._refresh_lock:
            self.error = None
            try:
                paths = glob.glob(os.path.join(LOG_ROOT, "**", "*.jsonl"), recursive=True)
            except Exception as e:
                self.error = str(e)
                paths = []
            self.file_count = len(paths)

            seen_paths = set(paths)
            updates = {}
            for p in paths:
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                sig = (st.st_mtime, st.st_size)
                cached = self._cache.get(p)
                if cached and cached[0] == sig[0] and cached[1] == sig[1]:
                    continue
                updates[p] = (sig[0], sig[1], _parse_records(p))

            self._cache.update(updates)
            stale = self._cache.keys() - seen_paths
            for p in stale:
                del self._cache[p]

            try:
                return self._aggregate()
            except Exception as e:
                self.error = str(e)
                empty = {"in": 0, "out": 0, "cache": 0, "cost": 0.0}
                today = dt.datetime.now().strftime("%Y-%m-%d")
                return {
                    "per_day": {}, "per_model": {}, "totals": dict(empty),
                    "today": dict(empty), "today_date": today,
                    "mtd": dict(empty), "file_count": self.file_count,
                    "error": self.error, "session": None, "weekly": None,
                }

    def _compute_session_weekly(self, all_recs):
        if not all_recs:
            return {"session": None, "weekly": None}

        window = 5 * 3600
        now_epoch = time.time()

        # build 5h blocks and detect limits simultaneously
        blocks = []          # (block_start_epoch, tokens)
        cur_start = cur_tok = 0
        session_cands = []
        weekly_cands = []
        scan_start = scan_tok = 0

        for r in all_recs:
            epoch, total, is_rl, is_eu = r["epoch"], r["total"], r["is_rl"], r["is_eu"]
            # block builder
            if not blocks and cur_start == 0:
                cur_start = epoch
            elif epoch >= cur_start + window:
                blocks.append((cur_start, cur_tok))
                cur_start = epoch
                cur_tok = 0
            if not is_rl and not is_eu:
                cur_tok += total

            # limit detector
            if scan_start == 0:
                scan_start = epoch
            elif epoch >= scan_start + window:
                scan_start = epoch
                scan_tok = 0
            if is_rl:
                session_cands.append(scan_tok)
            elif is_eu:
                weekly_cands.append(scan_tok)
            elif not is_rl and not is_eu:
                scan_tok += total

        if cur_start:
            blocks.append((cur_start, cur_tok))

        # session limit: most recent rate_limit value, ignoring outliers < 50% of max
        raw_sess = max(session_cands) if session_cands else 0
        if raw_sess > 0:
            session_limit = max(
                (v for v in session_cands if v >= raw_sess // 2),
                default=raw_sess,
            )
        else:
            session_limit = max((t for _, t in blocks), default=1) or 1

        # current active block (last block still within 5h window)
        session_info = None
        if blocks:
            b_start, b_tok = blocks[-1]
            reset_at = b_start + window
            if now_epoch < reset_at:
                session_info = {
                    "tokens": b_tok,
                    "limit": session_limit,
                    "frac": min(1.0, b_tok / max(1, session_limit)),
                    "reset_at_epoch": reset_at,
                }

        # weekly block (Mon 00:00 local -> Sun 23:59 local)
        now_local = dt.datetime.now()
        wday = now_local.weekday()  # 0=Mon
        week_start_local = (now_local - dt.timedelta(days=wday)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        week_start_epoch = week_start_local.timestamp()
        days_to_mon = (7 - wday) % 7 or 7
        next_mon = (now_local + dt.timedelta(days=days_to_mon)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        next_mon_epoch = next_mon.timestamp()

        weekly_tokens = sum(
            r["total"] for r in all_recs
            if r["epoch"] >= week_start_epoch and not r["is_rl"] and not r["is_eu"]
        )
        weekly_limit = weekly_cands[-1] if weekly_cands else 0
        weekly_frac = (
            min(1.0, weekly_tokens / max(1, weekly_limit))
            if weekly_limit > 0 else None
        )
        weekly_info = {
            "tokens": weekly_tokens,
            "limit": weekly_limit,
            "frac": weekly_frac,
            "next_mon_epoch": next_mon_epoch,
        }
        return {"session": session_info, "weekly": weekly_info}

    def _aggregate(self):
        seen = set()
        per_day = {}
        per_model = {}
        totals = {"in": 0, "out": 0, "cache": 0, "cost": 0.0}
        all_recs = []

        for _, _, recs in self._cache.values():
            for r in recs:
                k = r["k"]
                if k in seen:
                    continue
                seen.add(k)
                all_recs.append(r)

                d = per_day.setdefault(
                    r["day"], {"in": 0, "out": 0, "cache": 0, "cost": 0.0})
                m = per_model.setdefault(
                    r["fam"], {"in": 0, "out": 0, "cache": 0, "cost": 0.0})
                for key in ("in", "out", "cache", "cost"):
                    d[key] += r[key]
                    m[key] += r[key]
                    totals[key] += r[key]

        now = dt.datetime.now()
        today = now.strftime("%Y-%m-%d")
        month_prefix = now.strftime("%Y-%m")
        mtd = {"in": 0, "out": 0, "cache": 0, "cost": 0.0}
        for day, v in per_day.items():
            if day.startswith(month_prefix):
                for key in mtd:
                    mtd[key] += v[key]

        all_recs.sort(key=lambda r: r["epoch"])
        sw = self._compute_session_weekly(all_recs)
        return {
            "per_day": per_day,
            "per_model": per_model,
            "totals": totals,
            "today": per_day.get(
                today, {"in": 0, "out": 0, "cache": 0, "cost": 0.0}),
            "today_date": today,
            "mtd": mtd,
            "file_count": self.file_count,
            "error": self.error,
            "session": sw["session"],
            "weekly": sw["weekly"],
        }


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def fmt_tokens(n):
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(int(n))


def fmt_cost(c):
    if c >= 100:
        return f"${c:,.0f}"
    if c >= 1:
        return f"${c:.2f}"
    return f"${c:.3f}"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def load_settings():
    base = {"x": None, "y": None, "topmost": True, "metric": "cost", "days": 14,
            "codex_session_token": ""}
    try:
        with open(SETTINGS_FILE) as f:
            base.update(json.load(f))
    except Exception:
        pass
    return base


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui():
    import tkinter as tk

    W, H = 380, 640
    settings = load_settings()
    data = UsageData()
    result_q = queue.Queue()

    root = tk.Tk()
    root.title("Claude Usage")
    root.overrideredirect(True)
    root.configure(bg=C_BG)
    root.attributes("-alpha", 0.96)
    root.attributes("-topmost", bool(settings["topmost"]))

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x = settings["x"] if settings["x"] is not None else sw - W - 24
    y = settings["y"] if settings["y"] is not None else sh - H - 60
    x = max(0, min(x, sw - W))
    y = max(0, min(y, sh - H))
    root.geometry(f"{W}x{H}+{x}+{y}")

    state = {
        "metric": settings["metric"],
        "days": settings["days"],
        "topmost": bool(settings["topmost"]),
        "drag_x": 0,
        "drag_y": 0,
        "agg": None,
    }

    def persist():
        settings.update({
            "x": root.winfo_x(), "y": root.winfo_y(),
            "topmost": state["topmost"], "metric": state["metric"],
            "days": state["days"],
        })
        save_settings(settings)

    # ---- title bar -------------------------------------------------------
    bar = tk.Frame(root, bg=C_BG, height=34)
    bar.pack(fill="x", side="top")
    bar.pack_propagate(False)

    title = tk.Label(bar, text="◆  Claude Usage", bg=C_BG, fg=C_TEXT,
                     font=("Segoe UI Semibold", 11))
    title.pack(side="left", padx=12)

    def mk_btn(parent, txt, cmd, fg=C_SUB):
        b = tk.Label(parent, text=txt, bg=C_BG, fg=fg,
                     font=("Segoe UI", 11), cursor="hand2", padx=7)
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=C_TEXT))
        b.bind("<Leave>", lambda e: b.configure(
            fg=C_GREEN if (txt.startswith("●")) else fg))
        return b

    close_btn = mk_btn(bar, "✕", lambda: (persist(), root.destroy()),
                       fg=C_SUB)
    close_btn.pack(side="right", padx=(0, 8))
    refresh_btn = mk_btn(bar, "↻", lambda: (trigger_refresh(), trigger_codex_refresh()))
    refresh_btn.pack(side="right")
    pin_btn = mk_btn(bar, "", None)
    pin_btn.pack(side="right")

    def update_pin_btn():
        on = state["topmost"]
        pin_btn.configure(text=("● pin" if on else "○ pin"),
                          fg=C_GREEN if on else C_SUB)

    def toggle_pin():
        state["topmost"] = not state["topmost"]
        root.attributes("-topmost", state["topmost"])
        update_pin_btn()
        persist()

    pin_btn.bind("<Button-1>", lambda e: toggle_pin())
    update_pin_btn()

    def drag_start(e):
        state["drag_x"], state["drag_y"] = e.x, e.y

    def drag_move(e):
        nx = root.winfo_x() + (e.x - state["drag_x"])
        ny = root.winfo_y() + (e.y - state["drag_y"])
        root.geometry(f"+{nx}+{ny}")

    for w in (bar, title):
        w.bind("<ButtonPress-1>", drag_start)
        w.bind("<B1-Motion>", drag_move)
        w.bind("<ButtonRelease-1>", lambda e: persist())

    # ---- summary cards ---------------------------------------------------
    cards = tk.Frame(root, bg=C_BG)
    cards.pack(fill="x", padx=12, pady=(2, 6))

    def make_card(col, label, color):
        f = tk.Frame(cards, bg=C_SURFACE)
        f.grid(row=0, column=col, sticky="nsew", padx=3)
        cards.columnconfigure(col, weight=1)
        tk.Label(f, text=label, bg=C_SURFACE, fg=C_SUB,
                 font=("Segoe UI", 7)).pack(anchor="w", padx=8, pady=(6, 0))
        val = tk.Label(f, text="-", bg=C_SURFACE, fg=color,
                       font=("Segoe UI Semibold", 13))
        val.pack(anchor="w", padx=8, pady=(0, 4))
        sub = tk.Label(f, text="", bg=C_SURFACE, fg=C_FAINT,
                       font=("Segoe UI", 7))
        sub.pack(anchor="w", padx=8, pady=(0, 6))
        return val, sub

    today_val, today_sub = make_card(0, "TODAY", C_GREEN)
    mtd_val, mtd_sub = make_card(1, "THIS MONTH", C_PEACH)
    total_val, total_sub = make_card(2, "ALL TIME", C_ACCENT)

    # ---- bar chart -------------------------------------------------------
    chart_hdr = tk.Frame(root, bg=C_BG)
    chart_hdr.pack(fill="x", padx=14, pady=(4, 0))
    chart_title = tk.Label(chart_hdr, bg=C_BG, fg=C_TEXT,
                           font=("Segoe UI Semibold", 9))
    chart_title.pack(side="left")
    chart_hint = tk.Label(chart_hdr, text="right-click: options",
                          bg=C_BG, fg=C_FAINT, font=("Segoe UI", 7))
    chart_hint.pack(side="right")

    CH_W, CH_H = 352, 132
    chart = tk.Canvas(root, width=CH_W, height=CH_H, bg=C_BG,
                      highlightthickness=0)
    chart.pack(padx=14, pady=(2, 6))

    # ---- model breakdown -------------------------------------------------
    tk.Label(root, text="BY MODEL", bg=C_BG, fg=C_SUB,
             font=("Segoe UI", 7)).pack(anchor="w", padx=14, pady=(2, 0))
    models_box = tk.Frame(root, bg=C_BG)
    models_box.pack(fill="x", padx=14, pady=(2, 4))

    # ---- Codex section -------------------------------------------------------
    tk.Frame(root, bg=C_FAINT, height=1).pack(fill="x", padx=14, pady=(6, 0))
    codex_hdr_row = tk.Frame(root, bg=C_BG)
    codex_hdr_row.pack(fill="x", padx=14, pady=(4, 0))
    tk.Label(codex_hdr_row, text="CODEX", bg=C_BG, fg=C_SUB,
             font=("Segoe UI", 7)).pack(side="left")
    codex_plan_lbl = tk.Label(codex_hdr_row, text="", bg=C_BG, fg=C_FAINT,
                               font=("Segoe UI", 7))
    codex_plan_lbl.pack(side="left", padx=(6, 0))
    codex_err_lbl = tk.Label(codex_hdr_row, text="", bg=C_BG, fg=C_RED,
                              font=("Segoe UI", 7))
    codex_err_lbl.pack(side="right")

    codex_q = queue.Queue()

    def _make_codex_bar(label_text, bar_color):
        row = tk.Frame(root, bg=C_BG)
        row.pack(fill="x", padx=14, pady=(2, 2))
        tk.Label(row, text=label_text, bg=C_BG, fg=C_SUB,
                 font=("Segoe UI", 7), width=8, anchor="w").pack(side="left")
        track = tk.Frame(row, bg=C_SURFACE, height=10)
        track.pack(side="left", fill="x", expand=True, padx=(4, 4))
        track.pack_propagate(False)
        fill = tk.Frame(track, bg=bar_color, height=10)
        fill.place(relwidth=0.0, relheight=1.0)
        pct = tk.Label(row, text="--", bg=C_BG, fg=C_FAINT,
                       font=("Segoe UI", 7), width=5, anchor="e")
        pct.pack(side="left")
        eta = tk.Label(row, text="", bg=C_BG, fg=C_FAINT,
                       font=("Segoe UI", 7), width=12, anchor="e")
        eta.pack(side="right")
        return fill, pct, eta

    cx_sess_fill, cx_sess_pct, cx_sess_eta = _make_codex_bar("SESSION", C_PEACH)
    cx_week_fill, cx_week_pct, cx_week_eta = _make_codex_bar("WEEKLY",  C_MAUVE)

    state["codex"] = None

    def _bg_codex_refresh():
        tok = settings.get("codex_session_token", "")
        codex_q.put(codex_fetch(tok))

    def trigger_codex_refresh():
        threading.Thread(target=_bg_codex_refresh, daemon=True).start()

    def render_codex(cd):
        state["codex"] = cd
        if cd.get("error"):
            short_err = cd["error"][:40]
            codex_err_lbl.configure(text=short_err)
            cx_sess_fill.place(relwidth=0.0, relheight=1.0)
            cx_week_fill.place(relwidth=0.0, relheight=1.0)
            cx_sess_pct.configure(text="--", fg=C_FAINT)
            cx_week_pct.configure(text="--", fg=C_FAINT)
            codex_plan_lbl.configure(text="")
        else:
            codex_err_lbl.configure(text="")
            plan = cd.get("plan") or ""
            codex_plan_lbl.configure(text=plan.capitalize() if plan else "")
            sp = cd.get("session_pct")
            if sp is not None:
                cx_sess_fill.place(relwidth=min(1.0, sp / 100), relheight=1.0)
                cx_sess_pct.configure(text=f"{sp:.0f}%", fg=C_PEACH)
            else:
                cx_sess_fill.place(relwidth=0.0, relheight=1.0)
                cx_sess_pct.configure(text="--", fg=C_FAINT)
            wp = cd.get("weekly_pct")
            if wp is not None:
                cx_week_fill.place(relwidth=min(1.0, wp / 100), relheight=1.0)
                cx_week_pct.configure(text=f"{wp:.0f}%", fg=C_MAUVE)
            else:
                cx_week_fill.place(relwidth=0.0, relheight=1.0)
                cx_week_pct.configure(text="--", fg=C_FAINT)

    def poll_codex_queue():
        try:
            while True:
                cd = codex_q.get_nowait()
                render_codex(cd)
        except queue.Empty:
            pass
        root.after(500, poll_codex_queue)

    def codex_tick():
        cd = state.get("codex") or {}
        if cd:
            sr = cd.get("session_reset")
            if sr:
                r = sr - time.time()
                cx_sess_eta.configure(
                    text=(_fmt_countdown(r) if r > 0 else "reset!"),
                    fg=(C_PEACH if r > 0 else C_GREEN))
            wr = cd.get("weekly_reset")
            if wr:
                r = wr - time.time()
                cx_week_eta.configure(
                    text=(_fmt_countdown(r) if r > 0 else "reset!"),
                    fg=(C_MAUVE if r > 0 else C_GREEN))
        root.after(1000, codex_tick)

    # ---- session / weekly tracking ----------------------------------------
    tk.Frame(root, bg=C_FAINT, height=1).pack(fill="x", padx=14, pady=(6, 0))

    def _make_bar_row(label_text, bar_color):
        row = tk.Frame(root, bg=C_BG)
        row.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(row, text=label_text, bg=C_BG, fg=C_SUB,
                 font=("Segoe UI", 7), width=8, anchor="w").pack(side="left")
        track = tk.Frame(row, bg=C_SURFACE, height=10)
        track.pack(side="left", fill="x", expand=True, padx=(4, 4))
        track.pack_propagate(False)
        fill = tk.Frame(track, bg=bar_color, height=10)
        fill.place(relwidth=0.0, relheight=1.0)
        pct = tk.Label(row, text="--", bg=C_BG, fg=C_FAINT,
                       font=("Segoe UI", 7), width=5, anchor="e")
        pct.pack(side="left")
        eta = tk.Label(row, text="--", bg=C_BG, fg=C_FAINT,
                       font=("Segoe UI", 7), width=12, anchor="e")
        eta.pack(side="right")
        return fill, pct, eta

    session_fill, session_pct, session_eta = _make_bar_row("SESSION", C_ACCENT)
    weekly_fill, weekly_pct, weekly_eta = _make_bar_row("WEEKLY", C_MAUVE)

    footer = tk.Label(root, text="loading...", bg=C_BG, fg=C_FAINT,
                      font=("Segoe UI", 7))
    footer.pack(side="bottom", pady=(0, 6))

    # ---- drawing ---------------------------------------------------------
    def metric_of(d):
        if state["metric"] == "cost":
            return d["cost"]
        return d["in"] + d["out"] + d["cache"]

    def metric_label(v):
        return fmt_cost(v) if state["metric"] == "cost" else fmt_tokens(v)

    def draw_chart(agg):
        chart.delete("all")
        days = state["days"]
        today = dt.datetime.now().date()
        seq = []
        for i in range(days - 1, -1, -1):
            dd = today - dt.timedelta(days=i)
            key = dd.strftime("%Y-%m-%d")
            seq.append((dd, agg["per_day"].get(
                key, {"in": 0, "out": 0, "cache": 0, "cost": 0.0})))

        vals = [metric_of(d) for _, d in seq]
        vmax = max(vals) if vals else 0
        chart_title.configure(
            text=f"LAST {days} DAYS  -  "
                 f"{'COST' if state['metric'] == 'cost' else 'TOKENS'}")

        pad_l, pad_b, pad_t = 6, 16, 10
        plot_w = CH_W - pad_l - 4
        plot_h = CH_H - pad_b - pad_t
        n = len(seq)
        gap = 3
        bw = max(2, (plot_w - gap * (n - 1)) / n)
        base_y = CH_H - pad_b

        chart.create_line(pad_l, base_y, CH_W - 2, base_y, fill=C_FAINT)
        if vmax > 0:
            chart.create_text(CH_W - 2, pad_t - 2, anchor="ne",
                              text=metric_label(vmax),
                              fill=C_SUB, font=("Segoe UI", 7))

        for idx, (dd, d) in enumerate(seq):
            v = metric_of(d)
            bx = pad_l + idx * (bw + gap)
            bh = (v / vmax) * plot_h if vmax > 0 else 0
            is_today = (dd == today)
            color = C_GREEN if is_today else (
                C_ACCENT if state["metric"] == "cost" else C_MAUVE)
            if bh > 0:
                chart.create_rectangle(bx, base_y - bh, bx + bw, base_y,
                                       fill=color, outline="")
            if n <= 16 or idx % 3 == 0 or is_today:
                chart.create_text(bx + bw / 2, base_y + 8,
                                  text=dd.strftime("%d"),
                                  fill=C_TEXT if is_today else C_FAINT,
                                  font=("Segoe UI", 6))
        if vmax == 0:
            chart.create_text(CH_W / 2, CH_H / 2, text="no usage in range",
                              fill=C_FAINT, font=("Segoe UI", 9))

    def draw_models(agg):
        for w in models_box.winfo_children():
            w.destroy()
        pm = agg["per_model"]
        order = sorted(pm.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        total_cost = agg["totals"]["cost"] or 1.0
        if not order:
            tk.Label(models_box, text="no model data", bg=C_BG, fg=C_FAINT,
                     font=("Segoe UI", 8)).pack(anchor="w")
            return
        for fam, v in order:
            row = tk.Frame(models_box, bg=C_BG)
            row.pack(fill="x", pady=2)
            col = MODEL_COLORS.get(fam, C_PEACH)
            tk.Label(row, text="●", bg=C_BG, fg=col,
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(row, text=fam, bg=C_BG, fg=C_TEXT,
                     font=("Segoe UI", 8), width=7, anchor="w").pack(
                side="left", padx=(2, 6))
            track = tk.Frame(row, bg=C_SURFACE, height=10)
            track.pack(side="left", fill="x", expand=True, padx=(0, 6))
            track.pack_propagate(False)
            frac = max(0.02, v["cost"] / total_cost)
            fill = tk.Frame(track, bg=col, height=10)
            fill.place(relwidth=frac, relheight=1)
            tk.Label(row, text=f'{fmt_cost(v["cost"])}', bg=C_BG, fg=C_SUB,
                     font=("Segoe UI", 8), width=8, anchor="e").pack(
                side="right")

    def render(agg):
        state["agg"] = agg
        t, mtd, tot = agg["today"], agg["mtd"], agg["totals"]
        today_val.configure(text=fmt_cost(t["cost"]))
        today_sub.configure(
            text=f'{fmt_tokens(t["in"] + t["out"] + t["cache"])} tok')
        mtd_val.configure(text=fmt_cost(mtd["cost"]))
        mtd_sub.configure(
            text=f'{fmt_tokens(mtd["in"] + mtd["out"] + mtd["cache"])} tok')
        total_val.configure(text=fmt_cost(tot["cost"]))
        total_sub.configure(
            text=f'{fmt_tokens(tot["in"] + tot["out"] + tot["cache"])} tok')
        draw_chart(agg)
        draw_models(agg)

        s = agg.get("session")
        if s:
            session_fill.place(relwidth=s["frac"], relheight=1.0)
            session_pct.configure(text=f'{s["frac"]*100:.0f}%', fg=C_ACCENT)
        else:
            session_fill.place(relwidth=0.0, relheight=1.0)
            session_pct.configure(text="--", fg=C_FAINT)
            session_eta.configure(text="no session", fg=C_FAINT)

        w = agg.get("weekly")
        if w and w["frac"] is not None:
            weekly_fill.place(relwidth=w["frac"], relheight=1.0)
            weekly_pct.configure(text=f'{w["frac"]*100:.0f}%', fg=C_MAUVE)
        elif w:
            weekly_fill.place(relwidth=0.05, relheight=1.0)
            weekly_pct.configure(text="?%", fg=C_FAINT)
        else:
            weekly_fill.place(relwidth=0.0, relheight=1.0)
            weekly_pct.configure(text="--", fg=C_FAINT)

        if agg.get("error"):
            footer.configure(text=f'error: {agg["error"]}', fg=C_RED)
        else:
            footer.configure(
                text=f'{agg["file_count"]} logs  -  est. cost  -  '
                     f'updated {dt.datetime.now().strftime("%H:%M:%S")}  -  '
                     f'auto 60s',
                fg=C_FAINT)

    # ---- refresh plumbing ------------------------------------------------
    def _bg_refresh():
        result_q.put(data.refresh())

    def trigger_refresh():
        footer.configure(text="refreshing...", fg=C_SUB)
        threading.Thread(target=_bg_refresh, daemon=True).start()

    def poll_queue():
        try:
            while True:
                agg = result_q.get_nowait()
                render(agg)
        except queue.Empty:
            pass
        root.after(300, poll_queue)

    def auto_refresh():
        trigger_refresh()
        root.after(60_000, auto_refresh)

    def _fmt_countdown(secs):
        secs = int(secs)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        if h > 0:
            return f"→ {h}h {m:02d}m"
        return f"→ {m}m {s:02d}s"

    def _tick():
        agg = state.get("agg") or {}
        s = agg.get("session")
        if s and s.get("reset_at_epoch"):
            r = s["reset_at_epoch"] - time.time()
            if r > 0:
                session_eta.configure(text=_fmt_countdown(r), fg=C_ACCENT)
            else:
                session_eta.configure(text="reset!", fg=C_GREEN)
        elif s is None and agg:
            session_eta.configure(text="no session", fg=C_FAINT)

        w = agg.get("weekly")
        if w and w.get("next_mon_epoch"):
            r = w["next_mon_epoch"] - time.time()
            if r > 0:
                weekly_eta.configure(text=_fmt_countdown(r), fg=C_MAUVE)
            else:
                weekly_eta.configure(text="resetting", fg=C_GREEN)

        root.after(1000, _tick)

    # ---- context menu ----------------------------------------------------
    menu = tk.Menu(root, tearoff=0, bg=C_SURFACE, fg=C_TEXT,
                   activebackground=C_ACCENT, activeforeground=C_BG,
                   relief="flat", bd=0, font=("Segoe UI", 9))

    def set_codex_token():
        import tkinter.simpledialog as sd
        msg = (
            "Optional fallback. Usually this uses ~/.codex/auth.json automatically.\n\n"
            "Paste __Secure-next-auth.session-token from ChatGPT if auto auth fails.\n\n"
            "How: chatgpt.com -> F12 -> Application -> Cookies\n"
            "-> .chatgpt.com -> __Secure-next-auth.session-token\n"
            "(concatenate .0 + .1 if split)"
        )
        tok = sd.askstring("Codex session token", msg,
                           initialvalue=settings.get("codex_session_token", ""),
                           parent=root)
        if tok is not None:
            settings["codex_session_token"] = tok.strip()
            _CODEX_CACHE["ts"] = 0  # invalidate cache
            persist()
            trigger_codex_refresh()

    def rebuild_menu():
        menu.delete(0, "end")
        menu.add_command(
            label=("Unpin (allow behind)" if state["topmost"]
                   else "Pin on top"),
            command=toggle_pin)
        menu.add_command(label="Refresh now", command=trigger_refresh)
        menu.add_separator()
        m = state["metric"]
        menu.add_command(
            label=f'Metric: {"Cost  v" if m == "cost" else "Cost"}',
            command=lambda: set_metric("cost"))
        menu.add_command(
            label=f'Metric: {"Tokens  v" if m == "tokens" else "Tokens"}',
            command=lambda: set_metric("tokens"))
        menu.add_separator()
        for dval in (7, 14, 30):
            menu.add_command(
                label=f'Range: {dval} days{"  v" if state["days"] == dval else ""}',
                command=lambda d=dval: set_days(d))
        menu.add_separator()
        if _codex_local_access_token():
            auth_state = "auto"
        elif settings.get("codex_session_token"):
            auth_state = "manual  v"
        else:
            auth_state = "missing"
        menu.add_command(
            label=f"Codex auth: {auth_state}",
            command=set_codex_token)
        menu.add_separator()
        menu.add_command(label="Quit", command=lambda: (persist(),
                                                        root.destroy()))

    def set_metric(m):
        state["metric"] = m
        persist()
        if state["agg"]:
            draw_chart(state["agg"])

    def set_days(d):
        state["days"] = d
        persist()
        if state["agg"]:
            draw_chart(state["agg"])

    def show_menu(e):
        rebuild_menu()
        menu.tk_popup(e.x_root, e.y_root)

    root.bind("<Button-3>", show_menu)
    chart.bind("<Button-3>", show_menu)

    # double-click chart toggles metric quickly
    chart.bind("<Double-Button-1>",
               lambda e: set_metric("tokens" if state["metric"] == "cost"
                                    else "cost"))

    _tick()
    codex_tick()

    poll_queue()
    poll_codex_queue()
    auto_refresh()
    trigger_codex_refresh()

    if "--smoke" in sys.argv:
        # Build UI, render once, then self-close. Used for headless verify.
        root.after(1500, root.destroy)

    root.mainloop()


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def run_selftest():
    t0 = time.time()
    print(f"LOG_ROOT = {LOG_ROOT}")
    print(f"exists   = {os.path.isdir(LOG_ROOT)}")
    d = UsageData()
    agg = d.refresh()
    el = time.time() - t0
    if agg.get("error"):
        print(f"ERROR: {agg['error']}")
    print(f"files parsed : {agg['file_count']}")
    print(f"parse time   : {el:.2f}s")
    tot = agg["totals"]
    print("\n== ALL TIME ==")
    print(f"  tokens  in/out/cache = {fmt_tokens(tot['in'])} / "
          f"{fmt_tokens(tot['out'])} / {fmt_tokens(tot['cache'])}")
    print(f"  est cost            = {fmt_cost(tot['cost'])}")
    t = agg["today"]
    print(f"\n== TODAY ({agg['today_date']}) ==")
    print(f"  tokens = {fmt_tokens(t['in'] + t['out'] + t['cache'])}  "
          f"cost = {fmt_cost(t['cost'])}")
    m = agg["mtd"]
    print(f"\n== MONTH TO DATE ==")
    print(f"  tokens = {fmt_tokens(m['in'] + m['out'] + m['cache'])}  "
          f"cost = {fmt_cost(m['cost'])}")
    print("\n== BY MODEL ==")
    for fam, v in sorted(agg["per_model"].items(),
                         key=lambda kv: kv[1]["cost"], reverse=True):
        print(f"  {fam:8s} {fmt_cost(v['cost']):>10s}  "
              f"{fmt_tokens(v['in'] + v['out'] + v['cache']):>9s} tok")
    print("\n== LAST 7 DAYS ==")
    today = dt.datetime.now().date()
    for i in range(6, -1, -1):
        dd = today - dt.timedelta(days=i)
        key = dd.strftime("%Y-%m-%d")
        v = agg["per_day"].get(key, {"in": 0, "out": 0, "cache": 0,
                                     "cost": 0.0})
        print(f"  {key}  {fmt_cost(v['cost']):>10s}  "
              f"{fmt_tokens(v['in'] + v['out'] + v['cache']):>9s} tok")
    print(f"\nOK in {el:.2f}s")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        run_gui()  # also handles --smoke (auto-closes after first render)
