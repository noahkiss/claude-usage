"""History.jsonl reader for extra_usage, plan tier, and utilization data."""

from __future__ import annotations

import json
import time
from pathlib import Path

HISTORY_FILE = Path.home() / ".local" / "state" / "claude-usage" / "history.jsonl"

# In-memory cache with TTL
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300  # 5 minutes


def _cached(key: str, ttl: int = _CACHE_TTL):
    """Simple TTL cache decorator using module-level dict."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            now = time.monotonic()
            if key in _cache:
                ts, val = _cache[key]
                if now - ts < ttl:
                    return val
            val = fn(*args, **kwargs)
            _cache[key] = (now, val)
            return val
        return wrapper
    return decorator


def _read_records(history_file: Path = HISTORY_FILE) -> list[dict]:
    """Read all valid records from history.jsonl."""
    if not history_file.is_file():
        return []
    records = []
    with open(history_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and "fetched_at" in rec:
                    records.append(rec)
            except (json.JSONDecodeError, TypeError):
                continue
    return records


@_cached("extra_usage")
def get_extra_usage_periods(history_file: Path = HISTORY_FILE) -> list[dict]:
    """Find time ranges where extra_usage was enabled.

    Returns list of {start, end, monthly_limit, used_credits}.
    """
    records = _read_records(history_file)
    periods: list[dict] = []
    current: dict | None = None

    for rec in records:
        eu = rec.get("extra_usage")
        if not isinstance(eu, dict):
            continue
        enabled = eu.get("is_enabled", False)
        ts = rec["fetched_at"]

        if enabled and current is None:
            current = {
                "start": ts,
                "end": ts,
                "monthly_limit": eu.get("monthly_limit"),
                "used_credits": eu.get("used_credits"),
            }
        elif enabled and current is not None:
            current["end"] = ts
            current["used_credits"] = eu.get("used_credits")
        elif not enabled and current is not None:
            periods.append(current)
            current = None

    if current is not None:
        periods.append(current)

    return periods


@_cached("plan_transitions")
def get_plan_transitions(history_file: Path = HISTORY_FILE) -> list[dict]:
    """Detect plan tier transitions based on schema changes.

    Looks for appearance/disappearance of seven_day_opus, extra_usage fields.
    Returns list of {timestamp, from_tier, to_tier}.
    """
    records = _read_records(history_file)
    transitions: list[dict] = []

    prev_tier = None
    for rec in records:
        tier = _detect_tier(rec)
        if tier and tier != prev_tier and prev_tier is not None:
            transitions.append({
                "timestamp": rec["fetched_at"],
                "from_tier": prev_tier,
                "to_tier": tier,
            })
        if tier:
            prev_tier = tier

    return transitions


def _detect_tier(rec: dict) -> str | None:
    """Infer plan tier from record fields."""
    has_opus = rec.get("seven_day_opus") is not None
    has_extra = isinstance(rec.get("extra_usage"), dict)

    if has_opus or has_extra:
        return "max"
    if rec.get("five_hour") is not None:
        return "pro"
    return None


@_cached("utilization_history", ttl=60)
def get_utilization_history(
    hours: int = 168, history_file: Path = HISTORY_FILE
) -> list[dict]:
    """Recent utilization % readings from history.jsonl.

    Returns [{fetched_at, util_5h, util_7d}] for the last N hours.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    records = _read_records(history_file)
    result = []
    for rec in records:
        if rec["fetched_at"] < cutoff:
            continue
        five = rec.get("five_hour")
        seven = rec.get("seven_day")
        result.append({
            "fetched_at": rec["fetched_at"],
            "util_5h": five.get("utilization") if isinstance(five, dict) else None,
            "util_7d": seven.get("utilization") if isinstance(seven, dict) else None,
        })

    return result
