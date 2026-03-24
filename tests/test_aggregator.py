"""Tests for usage aggregation."""

import json
from datetime import datetime, timezone

import pytest

from claude_usage.aggregator import (
    aggregate_usage,
    format_report_table,
    format_tokens_human,
    get_window_boundaries,
)
from claude_usage.db import TrackerDB
from claude_usage.models import ActorType, SourceType, UsageSnapshot


@pytest.fixture
def db(tmp_path):
    d = TrackerDB(tmp_path / "test.db")
    yield d
    d.close()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def test_format_tokens_human():
    assert format_tokens_human(500) == "500"
    assert format_tokens_human(1500) == "1.5k"
    assert format_tokens_human(1_500_000) == "1.5M"


def test_window_boundaries_without_api():
    bounds = get_window_boundaries(None)
    assert "five_hour" in bounds
    assert "seven_day" in bounds
    assert "since" in bounds["five_hour"]


def test_window_boundaries_with_api():
    snapshot = {
        "five_hour": {
            "utilization": 30.0,
            "resets_at": "2026-03-23T22:00:00+00:00",
        },
        "seven_day": {
            "utilization": 50.0,
            "resets_at": "2026-03-27T04:00:00+00:00",
        },
    }
    bounds = get_window_boundaries(snapshot)
    assert bounds["five_hour"]["resets_at"] == "2026-03-23T22:00:00+00:00"
    # Window start should be 5h before reset
    assert "2026-03-23T17" in bounds["five_hour"]["since"]


def test_aggregate_usage(db):
    db.upsert_snapshot(_snap("s1:root:m1", input_t=1000, output_t=500))
    db.upsert_snapshot(_snap("s1:root:m2", input_t=2000, output_t=1000))

    report = aggregate_usage(db)
    assert report["five_hour"]["tokens"]["input_tokens"] == 3000
    assert report["five_hour"]["tokens"]["output_tokens"] == 1500
    assert "generated_at" in report


def test_aggregate_with_api_snapshot(db):
    db.upsert_snapshot(_snap("s1:root:m1"))
    api = {
        "five_hour": {"utilization": 32.0, "resets_at": "2026-03-23T22:00:00+00:00"},
        "seven_day": {"utilization": 57.0, "resets_at": "2026-03-27T04:00:00+00:00"},
        "fetched_at": "2026-03-23T17:45:00Z",
    }
    report = aggregate_usage(db, api)
    assert report["five_hour"]["api_utilization"] == 32.0
    assert report["api_fetched_at"] == "2026-03-23T17:45:00Z"


def test_format_report_table(db):
    db.upsert_snapshot(_snap("s1:root:m1", input_t=50000, output_t=10000))
    report = aggregate_usage(db)
    table = format_report_table(report)
    assert "5-hour" in table
    assert "7-day" in table
    assert "50.0k" in table


def test_aggregate_zero_usage(db):
    report = aggregate_usage(db)
    assert report["five_hour"]["tokens"]["total_tokens"] == 0
    assert report["seven_day"]["tokens"]["total_tokens"] == 0
