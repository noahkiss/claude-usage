"""API usage fetcher — reads from Anthropic's OAuth usage endpoint.

Ports the fetch logic from ~/bin/claude-usage (bash) into Python.
Handles OAuth token reading, rate-limit backoff, and writes to
usage.json + history.jsonl for backward compatibility.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from claude_usage.db import TrackerDB

log = logging.getLogger(__name__)

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
API_URL = "https://api.anthropic.com/api/oauth/usage"
STATE_DIR = Path.home() / ".local" / "state" / "claude-usage"
CACHE_FILE = STATE_DIR / "usage.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
BACKOFF_FILE = STATE_DIR / "usage.backoff"

# Default fetch interval and backoff duration (seconds)
FETCH_INTERVAL = 300  # 5 minutes
BACKOFF_DURATION = 600  # 10 minutes after rate limit


def read_oauth_token() -> str | None:
    """Read the OAuth access token from Claude's credentials file."""
    if not CREDENTIALS_FILE.is_file():
        log.warning("Credentials file not found: %s", CREDENTIALS_FILE)
        return None
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text())
        token = creds.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            log.warning("No accessToken in credentials file")
        return token
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to read credentials: %s", e)
        return None


def _is_backing_off() -> bool:
    """Check if we're in a rate-limit backoff period."""
    if not BACKOFF_FILE.is_file():
        return False
    try:
        backoff_until = int(BACKOFF_FILE.read_text().strip())
        if time.time() < backoff_until:
            return True
        # Backoff expired — clean up
        BACKOFF_FILE.unlink(missing_ok=True)
        return False
    except (ValueError, OSError):
        BACKOFF_FILE.unlink(missing_ok=True)
        return False


def _set_backoff(duration: int = BACKOFF_DURATION) -> None:
    """Set a backoff timer after rate limiting."""
    BACKOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKOFF_FILE.write_text(str(int(time.time()) + duration))
    log.warning("Rate limited — backing off for %d seconds", duration)


def fetch_usage(force: bool = False) -> dict | None:
    """Fetch usage data from the Anthropic API.

    Returns the parsed API response with fetched_at added, or None on failure.
    Handles rate-limit backoff automatically.
    """
    if not force and _is_backing_off():
        log.debug("Skipping fetch — in backoff period")
        return None

    token = read_oauth_token()
    if not token:
        return None

    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "user-agent": "claude-usage-tracker/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        if e.code == 429 or "rate limit" in body.lower():
            _set_backoff()
        log.error("API HTTP error %d: %s", e.code, body[:200])
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log.error("API fetch failed: %s", e)
        return None

    # Check for API error in response body
    if "error" in raw:
        msg = raw.get("error", {}).get("message", "unknown")
        if "rate limit" in msg.lower():
            _set_backoff()
        log.error("API error: %s", msg)
        return None

    # Add fetched_at timestamp
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw["fetched_at"] = now_iso

    return raw


def write_cache(data: dict) -> None:
    """Write API response to usage.json (backward compat with statusline)."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def append_history(data: dict) -> None:
    """Append API response to history.jsonl."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(data, separators=(",", ":")) + "\n")


def store_calibration_point(db: TrackerDB, data: dict) -> None:
    """Store an API response as a calibration point in the DB.

    Pairs the official utilization with our current token estimates
    for the matching windows.
    """
    from claude_usage.calibrator import extract_utilization, extract_window_starts

    fetched_at = data.get("fetched_at")
    if not fetched_at:
        return

    util_5h, util_7d = extract_utilization(data)
    if util_5h is None and util_7d is None:
        return

    start_5h, start_7d = extract_window_starts(data)

    tokens_5h = 0
    tokens_7d = 0
    if start_5h:
        t = db.get_cumulative_tokens(since=start_5h, until=fetched_at)
        tokens_5h = t["total_tokens"]
    if start_7d:
        t = db.get_cumulative_tokens(since=start_7d, until=fetched_at)
        tokens_7d = t["total_tokens"]

    plan_tier = db.get_config("plan_tier")
    db.conn.execute(
        """INSERT INTO calibration
           (timestamp, estimated_tokens_5h, estimated_tokens_7d,
            official_util_5h, official_util_7d, plan_tier)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fetched_at, tokens_5h, tokens_7d, util_5h, util_7d, plan_tier),
    )
    db.conn.commit()
    log.info(
        "Stored calibration point: 5h=%.1f%% (%d tok), 7d=%.1f%% (%d tok)",
        util_5h or 0, tokens_5h, util_7d or 0, tokens_7d,
    )


def fetch_and_store(db: TrackerDB, force: bool = False) -> dict | None:
    """Full fetch cycle: API call → cache → history → calibration.

    Returns the API response dict, or None if fetch was skipped/failed.
    """
    data = fetch_usage(force=force)
    if data is None:
        return None

    # Write backward-compat files
    write_cache(data)
    append_history(data)

    # Store as calibration point
    store_calibration_point(db, data)

    log.info("Fetched API usage: 5h=%.1f%%, 7d=%.1f%%",
             data.get("five_hour", {}).get("utilization", 0),
             data.get("seven_day", {}).get("utilization", 0))

    return data
