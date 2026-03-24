#!/bin/bash
# Usage-awareness hook for Claude Code — injects rate limit context into sessions.
# Install as a UserPromptSubmit hook in ~/.claude/settings.json.
# Reads from usage.json (updated by the tracker daemon every ~5s).
# Must be fast (<100ms).
#
# SETUP:
# Add to ~/.claude/settings.json:
#   "hooks": {
#     "UserPromptSubmit": [{
#       "hooks": [{"type": "command", "command": "\"$HOME/.claude/hooks/rate-limit-hook.sh\"", "timeout": 1}]
#     }]
#   }

USAGE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/claude-usage/usage.json"

[[ -f "$USAGE_FILE" ]] || exit 0

# Prefer calibrated estimates from tracker, fall back to API values
read -r pct5h pct7d reset5h reset7d < <(jq -r '
  [
    (.tracker.five_hour // .five_hour.utilization // empty),
    (.tracker.seven_day // .seven_day.utilization // empty),
    (.five_hour.resets_at // empty),
    (.seven_day.resets_at // empty)
  ] | @tsv
' "$USAGE_FILE" 2>/dev/null)

[[ -z "$pct5h" ]] && exit 0

# Round to one decimal place (avoids 99.5 displaying as 100)
r5h=$(printf "%.1f" "$pct5h")
r7d=$(printf "%.1f" "$pct7d")

# Compute time-until-reset as compact string (e.g. "3h12m", "2d5h")
countdown() {
  local reset="$1"
  [[ -z "$reset" ]] && return
  local now=$(date +%s)
  local target=$(date -d "$reset" +%s 2>/dev/null) || return
  local diff=$(( target - now ))
  (( diff <= 0 )) && { echo "resetting"; return; }
  local days=$(( diff / 86400 ))
  local hours=$(( (diff % 86400) / 3600 ))
  local mins=$(( (diff % 3600) / 60 ))
  if (( days > 0 )); then
    echo "${days}d${hours}h"
  elif (( hours > 0 )); then
    echo "${hours}h${mins}m"
  else
    echo "${mins}m"
  fi
}

c5h=$(countdown "$reset5h")
c7d=$(countdown "$reset7d")

# Local-time reset timestamps for steering messages (e.g. "at 4:35 PM EDT")
# Change TZ to your local timezone
local5h=""
local7d=""
[[ -n "$reset5h" ]] && local5h=$(date -d "$reset5h" "+%-I:%M %p %Z" 2>/dev/null)
[[ -n "$reset7d" ]] && local7d=$(date -d "$reset7d" "+%-I:%M %p %Z" 2>/dev/null)

r5h_part="5h: ${r5h}%"
[[ -n "$c5h" ]] && r5h_part+=" (resets ${c5h})"
r7d_part="7d: ${r7d}%"
[[ -n "$c7d" ]] && r7d_part+=" (resets ${c7d})"

now_local=$(date "+%-I:%M %p %Z")
msg="Claude Code subscription rate limits (${now_local}) — ${r5h_part}, ${r7d_part}."

# Compute hours to 7d reset for relaxation logic
h7d=999
if [[ -n "$reset7d" ]]; then
  now_s=$(date +%s)
  r7d_s=$(date -d "$reset7d" +%s 2>/dev/null) || r7d_s=$now_s
  h7d=$(( (r7d_s - now_s) / 3600 ))
fi

# Steering based on utilization level
# 7d guidance relaxes when reset is <6h away (use it or lose it)
# Steering messages are explicit about WHY (which limit) and WHEN to resume (reset time).
# Helper: float comparison via bc (bash can't compare decimals natively)
_gte() { (( $(echo "$1 >= $2" | bc -l) )); }

r7d_relaxed=0
_gte "$r7d" 75 && (( h7d <= 6 )) && r7d_relaxed=1

reset5h_at=""
[[ -n "$local5h" ]] && reset5h_at=" (resets at ${local5h})"
reset7d_at=""
[[ -n "$local7d" ]] && reset7d_at=" (resets at ${local7d})"

if _gte "$r5h" 95; then
  msg+=" 5h limit near cap${reset5h_at} — update STATUS.local.md and wrap up. Defer new work until after the 5h window resets. Always mention the reset time when suggesting the user wait or resume later."
elif _gte "$r5h" 90; then
  msg+=" 5h usage is critical${reset5h_at}. Avoid large searches, unnecessary agent spawns, and speculative tool calls. Prefer targeted, minimal-context approaches."
elif _gte "$r5h" 75; then
  msg+=" 5h usage is elevated${reset5h_at}. Be mindful of high-token operations — prefer concise tool calls, limit unnecessary file reads, and batch work efficiently."
fi

if _gte "$r7d" 95 && [[ "$r7d_relaxed" == 0 ]]; then
  msg+=" 7d limit near cap${reset7d_at} — wrap up. Reserve capacity for other sessions this week. Always mention the reset time when suggesting the user wait."
elif _gte "$r7d" 90 && [[ "$r7d_relaxed" == 0 ]]; then
  msg+=" 7d usage is critical${reset7d_at}. Conserve tokens across sessions — avoid speculative work."
elif _gte "$r7d" 75 && [[ "$r7d_relaxed" == 0 ]]; then
  msg+=" 7d usage is elevated. Be efficient with weekly budget."
elif _gte "$r7d" 75 && [[ "$r7d_relaxed" == 1 ]]; then
  msg+=" 7d window resets soon${reset7d_at} — no need to conserve, use it up."
fi

echo "$msg"
