![Claude Usage Dashboard](docs/banner.png)

# Claude Usage Tracker

Realtime Claude Code usage estimation between API billing calls. Anthropic's usage endpoint is rate-limited, so you can't poll it continuously. This tool fills the gap by parsing Claude Code's on-disk JSONL session logs every few seconds, estimating token consumption in near-realtime, then reconciling with official API numbers when they arrive.

The result: always-current usage percentages for both the 5-hour and 7-day rate limit windows, a web dashboard with historical charts, and a Claude Code hook that injects usage context into every prompt so the model can self-regulate when limits are approaching.

![Dashboard](docs/dashboard.png)

## Features

- **Continuous log scanning** — tails all active session logs (~5s intervals), including subagent files
- **API reconciliation** — fetches official usage from Anthropic's OAuth endpoint and merges with local estimates
- **Dynamic calibration** — builds a tokens-to-utilization model from historical data, weighted toward recent observations
- **Promotion-aware** — flag time windows with modified rate limits (e.g. 2x off-peak) so calibration stays accurate
- **Web dashboard** — embedded single-page UI with utilization gauges, session breakdowns, and history charts
- **Rate limit hook** — bash hook for Claude Code that injects current usage into system context, with tiered steering messages
- **Per-session breakdown** — see token counts and activity per conversation
- **Zero dependencies** — pure Python, stdlib only (no Flask, no requests)

## Quick Start

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install as a CLI tool
uv tool install .

# Or into the current environment
uv pip install .
```

The CLI is available as `claude-usage-tracker`.

## Usage

### Daemon mode

Start the tracker daemon. It scans session logs every 5 seconds and fetches API usage every 5 minutes:

```bash
claude-usage-tracker run
```

Options:
- `--interval N` — scan interval in seconds (default: 5)
- `--fetch-interval N` — API fetch interval in seconds (default: 300)
- `--no-fetch` — scan-only mode, skip API calls

### Status

Print current usage estimates for both rate limit windows:

```bash
claude-usage-tracker status
claude-usage-tracker status --json
```

### Session breakdown

List active sessions with token counts:

```bash
claude-usage-tracker session
claude-usage-tracker session <session-id>
claude-usage-tracker session --since 2025-03-23T00:00:00Z
```

### One-shot commands

```bash
# Scan and ingest all log files once
claude-usage-tracker ingest

# Fetch API usage now (bypassing backoff with --force)
claude-usage-tracker fetch
claude-usage-tracker fetch --force --json

# Ingest calibration history and show current ratios
claude-usage-tracker calibrate
claude-usage-tracker calibrate --history /path/to/history.jsonl
```

### Web dashboard

Start the web UI (default: `0.0.0.0:2725`):

```bash
claude-usage-tracker serve
claude-usage-tracker serve --port 8080 --host 127.0.0.1
```

### Global options

```bash
claude-usage-tracker --db /path/to/tracker.db  # Custom DB path
claude-usage-tracker -v run                     # Debug logging
```

## Rate Limit Hook

The included `extras/rate-limit-hook.sh` is a Claude Code `UserPromptSubmit` hook. It reads the tracker's `usage.json` cache and injects a one-line usage summary into every prompt. When utilization is high, it adds steering instructions telling the model to conserve tokens, wrap up, or defer work.

Install it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "\"$HOME/.claude/hooks/rate-limit-hook.sh\"",
        "timeout": 1
      }]
    }]
  }
}
```

Copy the script to `~/.claude/hooks/` or symlink it from `extras/`.

The hook runs in <100ms, reads a single JSON file, and outputs tiered guidance:
- **75%+** — elevated usage warnings, efficiency reminders
- **90%+** — critical warnings, minimize tool calls
- **95%+** — wrap up, defer new work until window resets

It also detects when the 7-day window is about to reset and relaxes conservation guidance ("use it or lose it").

## Calibration

The tracker doesn't know the exact conversion between raw tokens and Anthropic's utilization percentage — this varies by model, pricing changes, and plan tier. Calibration solves this.

Each time the API is fetched, the tracker records a calibration point: the official utilization percentage paired with the token count from local logs for the same window. Over time, this builds a weighted regression model (exponential decay, 3-day half-life) that converts tokens to estimated utilization.

Calibration points from promotional periods (2x capacity, etc.) are automatically excluded or adjusted so they don't skew the model.

```bash
# View current calibration ratios
claude-usage-tracker calibrate

# Output:
#   5h ratio: 0.0000001234 util%/token (confidence: 85%, 42 points)
#   7d ratio: 0.0000001198 util%/token (confidence: 92%, 156 points)
```

## Architecture

```
~/.claude/projects/*/*.jsonl   →   scanner   →   parser   →   SQLite DB
                                                                  ↑
                          Anthropic API   →   fetcher   →   calibrator
                                                                  ↓
                                              aggregator   →   usage.json
                                                                  ↓
                                                web (dashboard + API)
                                                rate-limit-hook.sh
```

| Module | Role |
|--------|------|
| `parser.py` | Extracts token counts, message IDs, and session metadata from JSONL lines |
| `scanner.py` | Discovers session files, tracks read offsets, feeds new lines to the parser |
| `db.py` | SQLite schema and queries — events, calibration points, promotions, file tracking |
| `fetcher.py` | OAuth-authenticated API calls to Anthropic's usage endpoint with backoff |
| `calibrator.py` | Builds weighted token-to-utilization ratios from paired local/API observations |
| `aggregator.py` | Combines API snapshots with calibrated local estimates into a unified report |
| `history.py` | Reads `history.jsonl` for utilization trends, plan transitions, and extra usage periods |
| `web.py` | Embedded HTTP server and single-page dashboard (no external dependencies) |
| `cli.py` | Argument parsing and command dispatch |

## Systemd Setup

Example systemd user service files are in `extras/`:

- `claude-usage-tracker.service` — runs the daemon (`claude-usage-tracker run`)
- `claude-usage-web.service` — runs the web dashboard (`claude-usage-tracker serve`)

Install them:

```bash
cp extras/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-usage-tracker.service
systemctl --user enable --now claude-usage-web.service
```

Edit the `ExecStart` and `WorkingDirectory` paths in each service file to match your installation.
