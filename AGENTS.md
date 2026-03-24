# Claude Usage Tracker

## Project Purpose

Realtime Claude Code usage estimation between API billing endpoint calls. The existing `claude-usage` tool fetches usage from Anthropic's API, but that endpoint rate-limits us. This project fills the gap by parsing on-disk JSONL session logs to estimate token consumption in near-realtime, then reconciling with official API numbers when they arrive.

## Architecture Goals

- **Daemon or polling process** (e.g. every 5 seconds) that tails active session logs across all Claude project directories
- **SQLite database** tracking: what's been parsed, cumulative token estimates, calibration data
- **Dynamic calibration**: correlate historical token counts from logs with official API usage numbers to build a tokens-to-usage-units model, weighted toward recent data points
- **Promotion-aware**: ability to flag time windows with modified limits (e.g. 2x off-peak) so calibration doesn't get thrown off by promotional periods

## Key Constraints

- **Never read full chat logs into context** — they are enormous. All parsing must be done programmatically (streaming/tailing line-by-line)
- Session logs live under `~/.claude/projects/*/` as `.jsonl` files, with subagent logs in `subagents/` subdirectories
- Assistant message snapshots are mutable (same `message.id` appears multiple times with growing token counts) — must upsert, not sum
- Parent `agent_progress` records are provisional mirrors; subagent files are authoritative for child usage
- `toolUseResult.totalTokens` is a completion checkpoint, not cumulative child usage

## Tech Preferences

- Python with uv (see global CLAUDE.md for uv patterns)
- SQLite for state persistence
- Keep it simple — this is a utility, not a platform
