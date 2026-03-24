"""Usage aggregation and windowed totals."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_usage.calibrator import (
    CompositePromotionDetector,
    ManualPromotionDetector,
    PromotionDetector,
    estimate_utilization,
)
from claude_usage.db import TrackerDB

HISTORY_FILE = Path.home() / ".local" / "state" / "claude-usage" / "history.jsonl"
CACHE_FILE = Path.home() / ".local" / "state" / "claude-usage" / "usage.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_last_api_snapshot(cache_file: Path = CACHE_FILE) -> dict | None:
    """Read the latest API usage snapshot from the claude-usage cache."""
    if not cache_file.is_file():
        return None
    try:
        return json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def get_window_boundaries(api_snapshot: dict | None = None) -> dict:
    """Determine the 5h and 7d window start times.

    If we have an API snapshot with resets_at, compute window start from that.
    Otherwise fall back to simple offsets from now.
    """
    now = _now_utc()

    result = {
        "five_hour": {"since": _iso(now - timedelta(hours=5)), "until": _iso(now)},
        "seven_day": {"since": _iso(now - timedelta(days=7)), "until": _iso(now)},
    }

    if api_snapshot:
        for window, key in [("five_hour", "five_hour"), ("seven_day", "seven_day")]:
            bucket = api_snapshot.get(key)
            if not isinstance(bucket, dict):
                continue
            resets_at = bucket.get("resets_at")
            if not resets_at:
                continue
            try:
                reset_dt = datetime.fromisoformat(resets_at)
                if window == "five_hour":
                    start = reset_dt - timedelta(hours=5)
                else:
                    start = reset_dt - timedelta(days=7)
                result[window]["since"] = _iso(start)
                result[window]["resets_at"] = resets_at
            except (ValueError, TypeError):
                pass

    return result


def aggregate_usage(
    db: TrackerDB,
    api_snapshot: dict | None = None,
    promotion_detector: PromotionDetector | None = None,
) -> dict:
    """Build a complete usage report with token totals per window.

    Returns a dict compatible with downstream consumers.
    """
    windows = get_window_boundaries(api_snapshot)

    five_hour_tokens = db.get_cumulative_tokens(
        since=windows["five_hour"]["since"],
        until=windows["five_hour"].get("until"),
    )
    seven_day_tokens = db.get_cumulative_tokens(
        since=windows["seven_day"]["since"],
        until=windows["seven_day"].get("until"),
    )

    five_hour_models = db.get_model_breakdown(
        since=windows["five_hour"]["since"],
        until=windows["five_hour"].get("until"),
    )
    seven_day_models = db.get_model_breakdown(
        since=windows["seven_day"]["since"],
        until=windows["seven_day"].get("until"),
    )

    # Build default promotion detector from DB if none provided
    if promotion_detector is None:
        promotion_detector = ManualPromotionDetector(db)

    report = {
        "generated_at": _iso(_now_utc()),
        "five_hour": {
            "tokens": five_hour_tokens,
            "by_model": five_hour_models,
            "window": windows["five_hour"],
        },
        "seven_day": {
            "tokens": seven_day_tokens,
            "by_model": seven_day_models,
            "window": windows["seven_day"],
        },
    }

    # Calibrated utilization estimates
    for window_key, cal_window in [("five_hour", "5h"), ("seven_day", "7d")]:
        total = report[window_key]["tokens"]["total_tokens"]
        est = estimate_utilization(
            db, total, cal_window,
            promotion_detector=promotion_detector,
            api_snapshot=api_snapshot,
        )
        if est is not None:
            report[window_key]["estimated_utilization"] = round(est, 1)

    # Include API utilization if available
    if api_snapshot:
        for window in ("five_hour", "seven_day"):
            bucket = api_snapshot.get(window)
            if isinstance(bucket, dict):
                report[window]["api_utilization"] = bucket.get("utilization")
                report[window]["api_resets_at"] = bucket.get("resets_at")
        report["api_fetched_at"] = api_snapshot.get("fetched_at")

    return report


def write_status_cache(
    db: TrackerDB,
    cache_file: Path = CACHE_FILE,
) -> None:
    """Write enriched usage.json with calibrated estimates alongside API data.

    Preserves all original API fields for backward compatibility, adds a
    `tracker` key with calibrated utilization estimates updated every scan cycle.
    """
    api_snapshot = get_last_api_snapshot(cache_file)
    report = aggregate_usage(db, api_snapshot)

    # Start from existing API data (preserves all fields for backward compat)
    data = dict(api_snapshot) if api_snapshot else {}

    # Add tracker estimates
    tracker = {"updated_at": report["generated_at"]}
    for window in ("five_hour", "seven_day"):
        est = report[window].get("estimated_utilization")
        if est is not None:
            tracker[window] = round(est, 1)
        # Include token totals for the bash script
        tracker[f"{window}_tokens"] = report[window]["tokens"]["total_tokens"]

    data["tracker"] = tracker

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, indent=2))


def format_tokens_human(tokens: int) -> str:
    """Format token count for human display."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def format_report_table(report: dict) -> str:
    """Format a usage report as a human-readable table."""
    lines = []
    lines.append("")
    lines.append("┌─────────────┬──────────────┬──────────────┬──────────────┬──────────────┐")
    lines.append("│ Window      │ Input        │ Output       │ Cache Create │ Cache Read   │")
    lines.append("├─────────────┼──────────────┼──────────────┼──────────────┼──────────────┤")

    for window, label in [("five_hour", "5-hour"), ("seven_day", "7-day")]:
        t = report[window]["tokens"]
        api_util = report[window].get("api_utilization")
        est_util = report[window].get("estimated_utilization")

        # Show estimated util, or API util, or nothing
        if est_util is not None and api_util is not None:
            util_str = f" (~{est_util:.0f}%)"
        elif api_util is not None:
            util_str = f" ({api_util:.0f}%)"
        elif est_util is not None:
            util_str = f" (~{est_util:.0f}%)"
        else:
            util_str = ""

        lines.append(
            f"│ {label + util_str:<11} "
            f"│ {format_tokens_human(t['input_tokens']):>12} "
            f"│ {format_tokens_human(t['output_tokens']):>12} "
            f"│ {format_tokens_human(t['cache_creation_input_tokens']):>12} "
            f"│ {format_tokens_human(t['cache_read_input_tokens']):>12} │"
        )

    lines.append("└─────────────┴──────────────┴──────────────┴──────────────┴──────────────┘")

    # Model breakdown for 5h
    models = report["five_hour"].get("by_model", {})
    if models:
        lines.append("")
        lines.append("  5h by model:")
        for model, t in sorted(models.items()):
            lines.append(
                f"    {model:<30} "
                f"total: {format_tokens_human(t['total_tokens']):>8}  "
                f"(in: {format_tokens_human(t['input_tokens'])}, "
                f"out: {format_tokens_human(t['output_tokens'])})"
            )

    if report.get("api_fetched_at"):
        lines.append(f"\n  API data from: {report['api_fetched_at']}")

    lines.append("")
    return "\n".join(lines)
