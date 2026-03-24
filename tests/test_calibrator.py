"""Tests for calibration model."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_usage.calibrator import (
    CalibrationRatio,
    CompositePromotionDetector,
    ManualPromotionDetector,
    TimeOfDayPromotionDetector,
    compute_ratio,
    estimate_utilization,
    ingest_history,
)
from claude_usage.db import TrackerDB
from claude_usage.models import ActorType, SourceType, UsageSnapshot


@pytest.fixture
def db(tmp_path):
    d = TrackerDB(tmp_path / "test.db")
    yield d
    d.close()


def _now_iso(offset_hours=0):
    dt = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snap(key, session="s1", input_t=100, output_t=50, ts=None):
    if ts is None:
        ts = _now_iso()
    return UsageSnapshot(
        logical_key=key,
        session_id=session,
        actor_id="root",
        actor_type=ActorType.ROOT,
        source_type=SourceType.MAIN_ASSISTANT,
        message_id=key.split(":")[-1],
        model="claude-opus-4-6",
        stop_reason=None,
        timestamp=ts,
        request_id=None,
        input_tokens=input_t,
        output_tokens=output_t,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        total_tokens=input_t + output_t,
    )


def _write_history(path, records):
    """Write history records as JSONL."""
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _make_history_record(fetched_at, util_5h=50.0, util_7d=30.0, resets_5h=None, resets_7d=None):
    """Create a history.jsonl record."""
    now = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if resets_5h is None:
        resets_5h = (now + timedelta(hours=2)).isoformat()
    if resets_7d is None:
        resets_7d = (now + timedelta(days=3)).isoformat()
    return {
        "fetched_at": fetched_at,
        "five_hour": {"utilization": util_5h, "resets_at": resets_5h},
        "seven_day": {"utilization": util_7d, "resets_at": resets_7d},
    }


# --- Ingest tests ---


def test_ingest_history_creates_calibration_points(db, tmp_path):
    history = tmp_path / "history.jsonl"
    records = [
        _make_history_record("2026-03-23T10:00:00Z", util_5h=40.0, util_7d=25.0),
        _make_history_record("2026-03-23T10:05:00Z", util_5h=42.0, util_7d=26.0),
    ]
    _write_history(history, records)

    count = ingest_history(db, history)
    assert count == 2

    rows = db.conn.execute("SELECT COUNT(*) FROM calibration").fetchone()
    assert rows[0] == 2


def test_ingest_incremental_skips_already_processed(db, tmp_path):
    history = tmp_path / "history.jsonl"
    records = [
        _make_history_record("2026-03-23T10:00:00Z"),
        _make_history_record("2026-03-23T10:05:00Z"),
    ]
    _write_history(history, records)

    # First ingest
    count1 = ingest_history(db, history)
    assert count1 == 2

    # Second ingest — same file, no new records
    count2 = ingest_history(db, history)
    assert count2 == 0

    # Add a new record and re-ingest
    records.append(_make_history_record("2026-03-23T10:10:00Z"))
    _write_history(history, records)
    count3 = ingest_history(db, history)
    assert count3 == 1


def test_ingest_missing_file(db, tmp_path):
    count = ingest_history(db, tmp_path / "nonexistent.jsonl")
    assert count == 0


def test_ingest_pairs_with_token_data(db, tmp_path):
    """When we have token data in the DB, calibration points get non-zero token estimates."""
    # Insert some token data
    ts = "2026-03-23T09:00:00Z"
    db.upsert_snapshot(_snap("s1:root:m1", input_t=50000, output_t=10000, ts=ts))

    history = tmp_path / "history.jsonl"
    resets_5h = "2026-03-23T14:00:00Z"  # window: 09:00 - 14:00
    records = [
        _make_history_record(
            "2026-03-23T12:00:00Z",
            util_5h=40.0,
            resets_5h=resets_5h,
        ),
    ]
    _write_history(history, records)

    ingest_history(db, history)

    row = db.conn.execute(
        "SELECT estimated_tokens_5h, official_util_5h FROM calibration"
    ).fetchone()
    assert row[0] == 60000  # 50000 + 10000
    assert row[1] == 40.0


# --- Ratio model tests ---


def test_compute_ratio_no_data(db):
    result = compute_ratio(db, "5h", min_tokens=0)
    assert result is None


def test_compute_ratio_uniform_data(db):
    """With uniform data, ratio should converge to util/tokens."""
    # Insert calibration points: 50% utilization at 100k tokens
    for i in range(10):
        ts = _now_iso(offset_hours=-i)
        db.conn.execute(
            "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
            "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
            (ts, 100000, 500000, 50.0, 30.0),
        )
    db.conn.commit()

    result = compute_ratio(db, "5h", min_tokens=0)
    assert result is not None
    # ratio should be approximately 50/100000 = 0.0005
    assert abs(result.ratio - 0.0005) < 0.0001
    assert result.data_points == 10


def test_compute_ratio_recency_bias(db):
    """Recent data points should have more influence than old ones."""
    # Old data: 20% at 100k tokens (ratio = 0.0002)
    for i in range(5):
        ts = _now_iso(offset_hours=-(30 * 24 + i))  # ~30 days ago
        db.conn.execute(
            "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
            "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
            (ts, 100000, 500000, 20.0, 10.0),
        )
    # Recent data: 80% at 100k tokens (ratio = 0.0008)
    for i in range(5):
        ts = _now_iso(offset_hours=-i)
        db.conn.execute(
            "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
            "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
            (ts, 100000, 500000, 80.0, 40.0),
        )
    db.conn.commit()

    result = compute_ratio(db, "5h", half_life_days=7.0, min_tokens=0)
    assert result is not None
    # Should be closer to 0.0008 (recent) than 0.0002 (old)
    assert result.ratio > 0.0006


def test_compute_ratio_skips_zero_tokens(db):
    """Points with zero tokens should be excluded (no division by zero)."""
    ts = _now_iso()
    db.conn.execute(
        "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
        "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
        (ts, 0, 0, 50.0, 30.0),
    )
    db.conn.commit()

    result = compute_ratio(db, "5h", min_tokens=0)
    assert result is None  # No usable data points


# --- Estimate utilization tests ---


def test_estimate_utilization(db):
    # Set up calibration: 50% at 100k tokens
    ts = _now_iso()
    db.conn.execute(
        "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
        "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
        (ts, 100000, 500000, 50.0, 30.0),
    )
    db.conn.commit()

    est = estimate_utilization(db, 200000, "5h", min_tokens=0)
    assert est is not None
    # 200k tokens at ratio 0.0005 = 100% → clamped to 100
    assert est == 100.0

    est2 = estimate_utilization(db, 50000, "5h", min_tokens=0)
    assert est2 is not None
    # 50k tokens at ratio 0.0005 = 25%
    assert abs(est2 - 25.0) < 1.0


def test_estimate_utilization_no_calibration(db):
    est = estimate_utilization(db, 100000, "5h", min_tokens=0)
    assert est is None


# --- Promotion detector tests ---


def test_manual_promotion_detector(db):
    db.conn.execute(
        "INSERT INTO promotions (start_at, end_at, multiplier, description) "
        "VALUES (?, ?, ?, ?)",
        ("2026-03-23T00:00:00Z", "2026-03-23T06:00:00Z", 2.0, "off-peak 2x"),
    )
    db.conn.commit()

    detector = ManualPromotionDetector(db)
    assert detector.multiplier_at("2026-03-23T03:00:00Z") == 2.0
    assert detector.multiplier_at("2026-03-23T12:00:00Z") == 1.0


def test_time_of_day_detector():
    detector = TimeOfDayPromotionDetector(off_peak_hours=(2, 3, 4, 5), multiplier=2.0)
    assert detector.multiplier_at("2026-03-23T03:00:00+00:00") == 2.0
    assert detector.multiplier_at("2026-03-23T12:00:00+00:00") == 1.0


def test_composite_detector(db):
    db.conn.execute(
        "INSERT INTO promotions (start_at, end_at, multiplier, description) "
        "VALUES (?, ?, ?, ?)",
        ("2026-03-23T00:00:00Z", "2026-03-23T06:00:00Z", 2.0, "test"),
    )
    db.conn.commit()

    composite = CompositePromotionDetector([
        ManualPromotionDetector(db),
        TimeOfDayPromotionDetector(off_peak_hours=(3,), multiplier=3.0),
    ])
    # At 3am — both match, composite returns the highest (3.0)
    assert composite.multiplier_at("2026-03-23T03:00:00+00:00") == 3.0
    # At noon — neither matches
    assert composite.multiplier_at("2026-03-23T12:00:00+00:00") == 1.0


def test_estimate_with_api_snapshot_no_new_tokens(db):
    """When no tokens consumed since API snapshot, return API utilization directly."""
    ts = _now_iso()
    # Insert some token data
    db.upsert_snapshot(_snap("s1:root:m1", input_t=50000, output_t=10000, ts=ts))

    api = {
        "five_hour": {
            "utilization": 75.0,
            "resets_at": _now_iso(offset_hours=2),
        },
        "seven_day": {
            "utilization": 40.0,
            "resets_at": _now_iso(offset_hours=72),
        },
        "fetched_at": ts,
    }
    # Request with same token total — no new tokens since API fetch
    est = estimate_utilization(db, 60000, "5h", api_snapshot=api, min_tokens=0)
    assert est is not None
    assert est == 75.0


def test_estimate_with_api_snapshot_delta(db):
    """When tokens grew since API snapshot, estimate increases."""
    base_ts = _now_iso(offset_hours=-1)
    now_ts = _now_iso()

    # Base token data at API fetch time
    db.upsert_snapshot(_snap("s1:root:m1", input_t=50000, output_t=10000, ts=base_ts))

    # More tokens arrived since then
    db.upsert_snapshot(_snap("s1:root:m2", input_t=80000, output_t=20000, ts=now_ts))

    # Add calibration data for marginal ratio computation
    for i in range(5):
        t = _now_iso(offset_hours=-(i + 2))
        db.conn.execute(
            "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
            "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
            (t, 50000 + i * 10000, 500000 + i * 50000, 30.0 + i * 5, 20.0 + i * 3),
        )
    db.conn.commit()

    api = {
        "five_hour": {
            "utilization": 50.0,
            "resets_at": _now_iso(offset_hours=3),
        },
        "fetched_at": base_ts,
    }
    # Total tokens now = 160000 (80000+20000 from m1 + m2 deltas)
    est = estimate_utilization(db, 160000, "5h", api_snapshot=api, min_tokens=0)
    # Should be >= 50% (API value) since we added tokens
    assert est is not None
    assert est >= 50.0


def test_estimate_with_promotion(db):
    """During a promotion, same tokens should yield lower utilization."""
    # Calibration data from yesterday (outside promotion window)
    ts = _now_iso(offset_hours=-24)
    db.conn.execute(
        "INSERT INTO calibration (timestamp, estimated_tokens_5h, estimated_tokens_7d, "
        "official_util_5h, official_util_7d) VALUES (?, ?, ?, ?, ?)",
        (ts, 100000, 500000, 50.0, 30.0),
    )
    db.conn.commit()

    # Without promotion
    est_normal = estimate_utilization(db, 100000, "5h", min_tokens=0)

    # With a promotion active right now
    db.conn.execute(
        "INSERT INTO promotions (start_at, end_at, multiplier, description) "
        "VALUES (?, ?, ?, ?)",
        (_now_iso(offset_hours=-1), _now_iso(offset_hours=1), 2.0, "2x promo"),
    )
    db.conn.commit()

    detector = ManualPromotionDetector(db)
    est_promo = estimate_utilization(db, 100000, "5h", promotion_detector=detector, min_tokens=0)

    assert est_normal is not None
    assert est_promo is not None
    # During 2x promotion, utilization should be roughly half
    assert est_promo < est_normal
