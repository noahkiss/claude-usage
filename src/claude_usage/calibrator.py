"""Token-to-utilization calibration using historical API data.

Ingests history.jsonl (API usage snapshots at ~5min intervals) and pairs
each with our token totals for the matching window. Builds a weighted
ratio model to convert raw tokens → estimated utilization %.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from claude_usage.db import TrackerDB
from claude_usage.models import CalibrationPoint

log = logging.getLogger(__name__)

HISTORY_FILE = Path.home() / ".local" / "state" / "claude-usage" / "history.jsonl"

# Default half-life for exponential decay weighting (in days).
# Recent calibration points matter more than old ones.
# Shorter half-life adapts faster to pricing/model changes.
DEFAULT_HALF_LIFE_DAYS = 3.0


# --- Promotion detection ---


class PromotionDetector(Protocol):
    """Interface for detecting whether a timestamp falls in a promotion window."""

    def multiplier_at(self, timestamp: str) -> float:
        """Return the capacity multiplier active at the given time. 1.0 = normal."""
        ...


class ManualPromotionDetector:
    """Detects promotions from manually configured windows in the DB."""

    def __init__(self, db: TrackerDB):
        self._db = db
        self._windows: list[tuple[str, str, float]] | None = None

    def _load(self) -> None:
        rows = self._db.conn.execute(
            "SELECT start_at, end_at, multiplier FROM promotions ORDER BY start_at"
        ).fetchall()
        self._windows = [(r[0], r[1], r[2]) for r in rows]

    def multiplier_at(self, timestamp: str) -> float:
        if self._windows is None:
            self._load()
        for start, end, mult in self._windows:
            if start <= timestamp <= end:
                return mult
        return 1.0


class TimeOfDayPromotionDetector:
    """Detects off-peak promotions based on time-of-day patterns.

    If utilization-to-token ratios are consistently lower during certain
    hours, this suggests an off-peak multiplier is in effect.
    """

    def __init__(self, off_peak_hours: tuple[int, ...] = (), multiplier: float = 2.0):
        self.off_peak_hours = set(off_peak_hours)
        self.multiplier = multiplier

    def multiplier_at(self, timestamp: str) -> float:
        if not self.off_peak_hours:
            return 1.0
        try:
            dt = datetime.fromisoformat(timestamp)
            if dt.hour in self.off_peak_hours:
                return self.multiplier
        except (ValueError, TypeError):
            pass
        return 1.0


class CompositePromotionDetector:
    """Chains multiple detectors — returns the highest multiplier found."""

    def __init__(self, detectors: list[PromotionDetector]):
        self.detectors = detectors

    def multiplier_at(self, timestamp: str) -> float:
        return max((d.multiplier_at(timestamp) for d in self.detectors), default=1.0)


# --- History ingestion ---


def _parse_history_record(line: str) -> dict | None:
    """Parse a single history.jsonl line. Returns None if unparseable."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(rec, dict) or "fetched_at" not in rec:
        return None
    return rec


def extract_utilization(rec: dict) -> tuple[float | None, float | None]:
    """Extract 5h and 7d utilization from a history record."""
    util_5h = None
    util_7d = None
    five = rec.get("five_hour")
    if isinstance(five, dict):
        util_5h = five.get("utilization")
    seven = rec.get("seven_day")
    if isinstance(seven, dict):
        util_7d = seven.get("utilization")
    return util_5h, util_7d


def extract_window_starts(rec: dict) -> tuple[str | None, str | None]:
    """Compute window start times from resets_at fields."""
    start_5h = None
    start_7d = None
    five = rec.get("five_hour")
    if isinstance(five, dict) and five.get("resets_at"):
        try:
            reset = datetime.fromisoformat(five["resets_at"])
            start_5h = (reset - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass
    seven = rec.get("seven_day")
    if isinstance(seven, dict) and seven.get("resets_at"):
        try:
            reset = datetime.fromisoformat(seven["resets_at"])
            start_7d = (reset - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass
    return start_5h, start_7d


def ingest_history(
    db: TrackerDB,
    history_file: Path = HISTORY_FILE,
) -> int:
    """Ingest history.jsonl into calibration table.

    Pairs each API snapshot with our token totals for the matching window.
    Only processes records newer than the last ingested fetched_at.

    Returns the number of calibration points added.
    """
    if not history_file.is_file():
        log.warning("History file not found: %s", history_file)
        return 0

    # Find the last ingested timestamp
    row = db.conn.execute(
        "SELECT MAX(timestamp) FROM calibration"
    ).fetchone()
    last_ts = row[0] if row and row[0] else None

    count = 0
    with open(history_file) as f:
        for line in f:
            rec = _parse_history_record(line)
            if rec is None:
                continue

            fetched_at = rec["fetched_at"]

            # Skip already-ingested records
            if last_ts and fetched_at <= last_ts:
                continue

            util_5h, util_7d = extract_utilization(rec)
            if util_5h is None and util_7d is None:
                continue

            start_5h, start_7d = extract_window_starts(rec)

            # Query our token totals for matching windows
            tokens_5h = 0
            tokens_7d = 0
            if start_5h:
                t = db.get_cumulative_tokens(since=start_5h, until=fetched_at)
                tokens_5h = t["total_tokens"]
            if start_7d:
                t = db.get_cumulative_tokens(since=start_7d, until=fetched_at)
                tokens_7d = t["total_tokens"]

            # Store calibration point
            db.conn.execute(
                """INSERT INTO calibration
                   (timestamp, estimated_tokens_5h, estimated_tokens_7d,
                    official_util_5h, official_util_7d)
                   VALUES (?, ?, ?, ?, ?)""",
                (fetched_at, tokens_5h, tokens_7d, util_5h, util_7d),
            )
            count += 1

    if count:
        db.conn.commit()
        log.info("Ingested %d calibration points from history", count)

    return count


# --- Ratio model ---


@dataclass
class CalibrationRatio:
    """Result of ratio computation for a window."""

    ratio: float  # utilization_pct per token
    confidence: float  # 0.0–1.0 based on data point count
    data_points: int
    effective_half_life_days: float


# Minimum token count for a calibration point to be usable.
# Low-volume points produce noisy ratios (e.g. 5% / 1000 tokens = huge ratio).
MIN_TOKENS_5H = 1_000_000
MIN_TOKENS_7D = 10_000_000


def compute_ratio(
    db: TrackerDB,
    window: str = "5h",
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    promotion_detector: PromotionDetector | None = None,
    min_tokens: int | None = None,
) -> CalibrationRatio | None:
    """Compute weighted ratio: utilization_pct / tokens for a window.

    Uses exponential decay so recent data points carry more weight.
    If a promotion detector is provided, adjusts token denominators
    by the promotion multiplier.

    Returns None if insufficient data.
    """
    if window == "5h":
        col_tokens = "estimated_tokens_5h"
        col_util = "official_util_5h"
        default_min = MIN_TOKENS_5H
    elif window == "7d":
        col_tokens = "estimated_tokens_7d"
        col_util = "official_util_7d"
        default_min = MIN_TOKENS_7D
    else:
        raise ValueError(f"Unknown window: {window}")

    threshold = min_tokens if min_tokens is not None else default_min

    rows = db.conn.execute(
        f"SELECT timestamp, {col_tokens}, {col_util} FROM calibration "
        f"WHERE {col_tokens} > 0 AND {col_tokens} >= ? AND {col_util} IS NOT NULL AND {col_util} > 0 "
        "ORDER BY timestamp",
        (threshold,),
    ).fetchall()

    if not rows:
        return None

    now = datetime.now(timezone.utc)
    decay_lambda = 0.693147 / (half_life_days * 86400)  # ln(2) / half_life_seconds

    weighted_sum = 0.0
    weight_total = 0.0

    for ts, tokens, util in rows:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        age_seconds = max(0, (now - dt).total_seconds())
        weight = 2.718282 ** (-decay_lambda * age_seconds)

        # Adjust tokens by promotion multiplier
        effective_tokens = tokens
        if promotion_detector:
            mult = promotion_detector.multiplier_at(ts)
            if mult > 0:
                effective_tokens = tokens / mult

        ratio = util / effective_tokens
        weighted_sum += ratio * weight
        weight_total += weight

    if weight_total == 0:
        return None

    # Confidence based on data point count (saturates around 100 points)
    confidence = min(1.0, len(rows) / 100.0)

    return CalibrationRatio(
        ratio=weighted_sum / weight_total,
        confidence=confidence,
        data_points=len(rows),
        effective_half_life_days=half_life_days,
    )


def estimate_utilization(
    db: TrackerDB,
    tokens: int,
    window: str = "5h",
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    promotion_detector: PromotionDetector | None = None,
    min_tokens: int | None = None,
    api_snapshot: dict | None = None,
) -> float | None:
    """Estimate utilization % using delta-from-last-snapshot approach.

    Primary strategy: anchor on the most recent API snapshot and extrapolate
    from token growth since then using the marginal ratio from recent
    calibration data. Falls back to the global ratio model when no recent
    API snapshot is available.

    The global ratio model is inherently noisy because the relationship
    between total_tokens and utilization is non-linear (cache reads are
    cheap, output tokens are expensive, and the mix shifts over time).
    The delta approach avoids this by only predicting the MARGINAL change.
    """
    window_key = "five_hour" if window == "5h" else "seven_day"

    # --- Strategy 1: Delta from last API snapshot ---
    if api_snapshot:
        bucket = api_snapshot.get(window_key)
        fetched_at = api_snapshot.get("fetched_at")
        if isinstance(bucket, dict) and fetched_at and bucket.get("utilization") is not None:
            api_util = bucket["utilization"]
            api_tokens = db.get_cumulative_tokens(
                since=_window_start_from_snapshot(api_snapshot, window_key),
                until=fetched_at,
            )["total_tokens"]

            delta_tokens = tokens - api_tokens
            if delta_tokens <= 0:
                # No new tokens since API snapshot — use API value directly
                return min(100.0, max(0.0, api_util))

            # Compute marginal ratio from recent calibration pairs
            marginal = _compute_marginal_ratio(db, window)
            if marginal is not None and marginal > 0:
                delta_util = delta_tokens * marginal
                return min(100.0, max(0.0, api_util + delta_util))

    # --- Strategy 2: Global ratio fallback ---
    cal = compute_ratio(db, window, half_life_days, promotion_detector, min_tokens)
    if cal is None:
        return None

    multiplier = 1.0
    if promotion_detector:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        multiplier = promotion_detector.multiplier_at(now_iso)

    raw_estimate = tokens * cal.ratio
    if multiplier > 0:
        raw_estimate /= multiplier

    return min(100.0, max(0.0, raw_estimate))


def _window_start_from_snapshot(api_snapshot: dict, window_key: str) -> str | None:
    """Compute window start time from an API snapshot's resets_at."""
    bucket = api_snapshot.get(window_key)
    if not isinstance(bucket, dict):
        return None
    resets_at = bucket.get("resets_at")
    if not resets_at:
        return None
    try:
        reset_dt = datetime.fromisoformat(resets_at)
        if window_key == "five_hour":
            start = reset_dt - timedelta(hours=5)
        else:
            start = reset_dt - timedelta(days=7)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _compute_marginal_ratio(db: TrackerDB, window: str = "5h") -> float | None:
    """Compute marginal utilization per token from recent calibration deltas.

    Looks at consecutive calibration points where both tokens and utilization
    increased, computing the marginal ratio (delta_util / delta_tokens).
    Uses only the most recent pairs for responsiveness.
    """
    if window == "5h":
        col_tokens = "estimated_tokens_5h"
        col_util = "official_util_5h"
    else:
        col_tokens = "estimated_tokens_7d"
        col_util = "official_util_7d"

    # Get recent calibration data (last 48h)
    rows = db.conn.execute(
        f"SELECT timestamp, {col_tokens}, {col_util} FROM calibration "
        f"WHERE {col_tokens} > 0 AND {col_util} IS NOT NULL "
        "AND timestamp >= datetime('now', '-2 days') "
        "ORDER BY timestamp",
    ).fetchall()

    if len(rows) < 2:
        return None

    # Collect marginal ratios from consecutive pairs where both increased
    marginals = []
    for i in range(1, len(rows)):
        _, tok_prev, util_prev = rows[i - 1]
        _, tok_curr, util_curr = rows[i]
        dt = tok_curr - tok_prev
        du = util_curr - util_prev
        # Only use pairs where tokens grew meaningfully and util increased
        if dt > 100_000 and du > 0:
            marginals.append(du / dt)

    if not marginals:
        return None

    # Use median to be robust against outliers
    marginals.sort()
    return marginals[len(marginals) // 2]
