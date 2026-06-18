# Windows Port

Python tkinter widget for Windows. Reads the same `~/.claude/projects/**/*.jsonl` files as the macOS app — no API key, no network.

## Features

- Today / Month / All-time cost + token cards
- Last 7/14/30-day bar chart
- By-model breakdown
- **Session (5h billing block)** — auto-detected % used + countdown to reset
- **Weekly usage** — token count + countdown to next Monday reset
- Session limit auto-detected from `rate_limit` events in logs
- Drag to move, pin on top, right-click menu, auto-refresh every 60s

## Requirements

Python 3.8+ (stdlib only — no pip installs needed). Tested on Windows 10/11.

## Run

```bat
pythonw windows\claude_usage_dashboard.py
```

## Install shortcuts

Run `make_shortcuts.vbs` once to create Desktop + Startup shortcuts.

```bat
cscript windows\make_shortcuts.vbs
```
