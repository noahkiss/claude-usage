# Claude Usage Tracker — Plan

## Problem

The Anthropic usage API endpoint rate-limits us. We need near-realtime usage estimates between API calls by parsing on-disk session logs, then reconciling with official numbers when available.

## Data Sources

### On-Disk Session Logs

Location: `~/.claude/projects/<project-hash>/<session-id>.jsonl`
Subagents: `~/.claude/projects/<project-hash>/<session-id>/subagents/agent-<id>.jsonl`

**Note:** `history.jsonl` is not useful for usage tracking.

### Existing claude-usage Tool

Fetches from Anthropic API, writes to a history file with timestamps and usage numbers. This is the "ground truth" we calibrate against.

---

## Log Parsing Strategy

### Source Priority (highest to lowest)

1. **Subagent file `assistant` records** — authoritative for child usage
2. **Main file `assistant` records** — authoritative for root usage
3. **Main file `agent_progress` nested `assistant`** — provisional mirror only
4. **Main file `toolUseResult`** — completion summary/checkpoint only

### Logical Response Key

Mutable assistant snapshots keyed by: `sessionId + ":" + actorId + ":" + message.id`

- `actorId` = `"root"` for main session, `agentId` for subagents

### Snapshot-to-Delta Conversion

For each assistant snapshot:
```
snapshot_total = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

- First snapshot for a key: emit delta = snapshot values
- Later snapshot for same key: emit field-wise delta (only positive)
- Update stored state to latest snapshot

### Delta Fields

- `delta_input_tokens`
- `delta_output_tokens`
- `delta_cache_creation_input_tokens`
- `delta_cache_read_input_tokens`
- `delta_total_tokens`

### Important JSONL Paths

**Main session file:**
- `timestamp`, `sessionId`, `uuid`, `requestId`
- `message.id`, `message.model`, `message.stop_reason`
- `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`
- `message.usage.server_tool_use.{web_search_requests, web_fetch_requests}`
- `toolUseResult.{agentId, totalTokens, totalDurationMs, totalToolUseCount, usage.*}`
- `data.agentId`, `data.message.message.usage.*`

**Subagent file:**
- `timestamp`, `sessionId`, `agentId`, `uuid`, `requestId`
- `message.id`, `message.model`, `message.stop_reason`
- `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`

### Subagent Handling

- Official accounting: subagent files for child usage
- Provisional UI: optionally show parent `agent_progress` until subagent snapshot arrives
- When authoritative subagent snapshot arrives, supersede provisional mirror

### Agent Completion Reconciliation

When `toolUseResult.status == "completed"`:
- Mark child agent run complete
- Store `totalTokens`, `totalDurationMs`, `totalToolUseCount`, `usage.*`
- Compare to subagent's cumulative totals (cumulative > toolUseResult if child had multiple turns)
- Use as validation signal, NOT as replacement for cumulative totals

---

## Calibration System

### Token-to-Usage-Unit Model

The relationship between raw tokens consumed and the "usage units" reported by the API isn't publicly documented and can change. We build our own model:

1. **Collect pairs**: `(timestamp, estimated_tokens_from_logs, official_usage_from_api)`
2. **Compute ratio**: `usage_units / tokens` for each data point
3. **Weighted average**: exponentially weight recent data points more heavily
4. **Apply**: multiply current token estimate by current ratio to get estimated usage units

### Handling Promotions / Limit Changes

- Sometimes Anthropic runs promotions (e.g. 2x limits outside 8am-2pm ET)
- The calibration system should detect sudden ratio jumps
- Support manual "promotion windows" config: `[{start, end, multiplier}]`
- When a promotion is active, adjust the denominator before calibrating
- Historical analyzer should also flag these automatically when ratio shifts correlate with time-of-day patterns

---

## Architecture

### Two-Layer Design

**Layer 1 — Raw Ingest:**
- One row per JSONL line
- Fields: `source_file`, `byte_offset`, `parsed_json`, `parse_status`
- Enables idempotency and replay

**Layer 2 — Usage State:**
- Mutable assistant-snapshot state keyed by `sessionId + actorId + message.id`
- Delta ledger for time-series output
- Cumulative totals per session, per project, and global

### Normalized Event Schema

```json
{
  "session_id": "string",
  "actor_id": "root|agent-...",
  "actor_type": "root|subagent",
  "source": "main_assistant|subagent_assistant|agent_progress_mirror|agent_completion",
  "logical_message_key": "string",
  "timestamp": "string",
  "request_id": "string|null",
  "message_id": "string|null",
  "model": "string|null",
  "stop_reason": "string|null",
  "input_tokens": "number|null",
  "output_tokens": "number|null",
  "cache_creation_input_tokens": "number|null",
  "cache_read_input_tokens": "number|null",
  "total_tokens_snapshot": "number|null",
  "delta_input_tokens": "number|null",
  "delta_output_tokens": "number|null",
  "delta_cache_creation_input_tokens": "number|null",
  "delta_cache_read_input_tokens": "number|null",
  "delta_total_tokens": "number|null",
  "is_final_snapshot": "boolean",
  "is_provisional": "boolean",
  "agent_total_tokens_reported": "number|null",
  "agent_total_duration_ms": "number|null",
  "agent_total_tool_use_count": "number|null"
}
```

### Storage — SQLite

Tables:
- `ingest_state` — per-file byte offset tracking (what we've already parsed)
- `snapshots` — latest snapshot per logical message key
- `deltas` — time-series delta ledger
- `calibration` — pairs of (timestamp, token_estimate, official_usage) for ratio modeling
- `promotions` — time windows with modified limits

### Process Model

Options (decide during implementation):
1. **Polling daemon** — runs every 5s, scans for new log lines
2. **inotify watcher** — event-driven, reacts to file changes
3. **Hybrid** — inotify for active files, periodic scan for new files

Polling is simpler and good enough given the 5s granularity target.

---

## Implementation Phases

### Phase 1: Core Parser
- JSONL line parser with all the extraction paths documented above
- Snapshot state management with upsert logic
- Delta computation
- Unit tests against sample log snippets

### Phase 2: File Scanner
- Discover all project dirs under `~/.claude/projects/`
- Track byte offsets per file in SQLite
- Incremental read (seek to last offset, read new lines)
- Detect new session files and subagent files

### Phase 3: Aggregation & Output
- Cumulative token totals per session, per 5h window, per 7d window
- Simple CLI output: current estimated usage
- JSON output for integration with existing tools

### Phase 4: Calibration
- Ingest existing claude-usage history file
- Build token-to-usage-unit ratio model
- Weighted averaging with recency bias
- Promotion window detection and configuration

### Phase 5: Integration
- Hook into existing claude-usage workflow
- Replace or supplement the rate-limited API calls
- Status line / quick-check integration

---

## Resolved Questions

### Existing claude-usage tool

**Location:** `~/bin/claude-usage` (bash script, ~450 lines)
**Cron:** `*/5 * * * * ~/bin/claude-usage --fetch`
**API:** `https://api.anthropic.com/api/oauth/usage` with OAuth token from `~/.claude/.credentials.json`

**State files** (all under `~/.local/state/claude-usage/`):
- `usage.json` — latest API snapshot (5-min TTL cache)
- `history.jsonl` — append-only log of every fetch (~36k records, ~12MB)
- `usage.backoff` — rate-limit backoff timestamp

**History record schema:**
```json
{
  "fetched_at": "2026-03-23T17:45:01Z",
  "five_hour": {"utilization": 32.0, "resets_at": "2026-03-23T22:00:00.354709+00:00"},
  "seven_day": {"utilization": 57.0, "resets_at": "2026-03-27T04:00:00.354738+00:00"},
  "seven_day_oauth_apps": null,
  "seven_day_opus": null,
  "seven_day_sonnet": {"utilization": 13.0, "resets_at": "..."},
  "seven_day_cowork": null,
  "iguana_necktie": null,
  "extra_usage": {"is_enabled": false, "monthly_limit": null, "used_credits": null, "utilization": null}
}
```

**Output modes:** `--json` (enhanced with calculated fields), `--seconds` (for scripts), `--raw`, default table.

### Output strategy

The tracker writes to SQLite at `~/.local/state/claude-usage/tracker.db` (co-located with existing state). The existing bash tool stays as the user-facing interface. Phase 5 integration: update the bash script to check tracker DB for realtime estimates when API cache is stale, falling back to API fetch.

### Process model

Systemd user service. Polling daemon at ~5s interval — simple, no inotify complexity.

## Remaining Questions

- Per-model calibration: do opus/sonnet/haiku have meaningfully different token-to-utilization ratios? History data has model-specific buckets (seven_day_opus, seven_day_sonnet) that may help answer this.
- The `iguana_necktie` field in the API response is unknown — monitor if it gains a value.
