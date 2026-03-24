"""Tests for SQLite schema and data access."""

import pytest

from claude_usage.db import TrackerDB
from claude_usage.models import (
    ActorType,
    AgentCompletion,
    SourceType,
    UsageDelta,
    UsageSnapshot,
)


@pytest.fixture
def db(tmp_path):
    d = TrackerDB(tmp_path / "test.db")
    yield d
    d.close()


def _make_snap(
    logical_key="s:root:m1",
    session_id="s",
    actor_id="root",
    message_id="m1",
    input_tokens=100,
    output_tokens=50,
    cache_creation=200,
    cache_read=300,
    timestamp="2026-03-23T12:00:00Z",
    is_provisional=False,
    source_type=SourceType.MAIN_ASSISTANT,
    actor_type=ActorType.ROOT,
    model="claude-opus-4-6",
) -> UsageSnapshot:
    return UsageSnapshot(
        logical_key=logical_key,
        session_id=session_id,
        actor_id=actor_id,
        actor_type=actor_type,
        source_type=source_type,
        message_id=message_id,
        model=model,
        stop_reason=None,
        timestamp=timestamp,
        request_id=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
        is_provisional=is_provisional,
    )


# --- Schema ---


def test_schema_creation(db):
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {t[0] for t in tables}
    assert "ingest_state" in names
    assert "snapshots" in names
    assert "deltas" in names
    assert "agent_completions" in names
    assert "calibration" in names


# --- File offsets ---


def test_file_offset_default_zero(db):
    assert db.get_file_offset("/some/file.jsonl") == 0


def test_file_offset_roundtrip(db):
    db.set_file_offset("/some/file.jsonl", 12345, 1700000000.0)
    assert db.get_file_offset("/some/file.jsonl") == 12345


def test_file_offset_update(db):
    db.set_file_offset("/f.jsonl", 100)
    db.set_file_offset("/f.jsonl", 200)
    assert db.get_file_offset("/f.jsonl") == 200


# --- Snapshot upsert ---


def test_upsert_first_insert(db):
    snap = _make_snap()
    delta = db.upsert_snapshot(snap)
    assert isinstance(delta, UsageDelta)
    assert delta.delta_input == 100
    assert delta.delta_output == 50
    assert delta.delta_cache_creation == 200
    assert delta.delta_cache_read == 300
    assert delta.delta_total == 650


def test_upsert_update_computes_delta(db):
    snap1 = _make_snap(input_tokens=100, output_tokens=10, cache_creation=0, cache_read=0)
    snap2 = _make_snap(input_tokens=100, output_tokens=50, cache_creation=0, cache_read=0)
    db.upsert_snapshot(snap1)
    delta = db.upsert_snapshot(snap2)
    assert delta.delta_input == 0
    assert delta.delta_output == 40
    assert delta.delta_total == 40


def test_upsert_no_change_returns_none(db):
    snap = _make_snap()
    db.upsert_snapshot(snap)
    assert db.upsert_snapshot(snap) is None


def test_upsert_provisional_does_not_overwrite_authoritative(db):
    auth = _make_snap(is_provisional=False, source_type=SourceType.SUBAGENT_ASSISTANT)
    prov = _make_snap(
        is_provisional=True,
        source_type=SourceType.AGENT_PROGRESS,
        output_tokens=999,
    )
    db.upsert_snapshot(auth)
    assert db.upsert_snapshot(prov) is None


def test_upsert_authoritative_overwrites_provisional(db):
    prov = _make_snap(
        is_provisional=True,
        source_type=SourceType.AGENT_PROGRESS,
        output_tokens=10,
    )
    auth = _make_snap(
        is_provisional=False,
        source_type=SourceType.SUBAGENT_ASSISTANT,
        output_tokens=50,
    )
    db.upsert_snapshot(prov)
    delta = db.upsert_snapshot(auth)
    assert delta is not None
    assert delta.delta_output == 40


# --- Aggregation ---


def test_cumulative_tokens(db):
    db.upsert_snapshot(_make_snap(
        logical_key="s:root:m1", message_id="m1",
        input_tokens=100, output_tokens=50, cache_creation=0, cache_read=0,
        timestamp="2026-03-23T10:00:00Z",
    ))
    db.upsert_snapshot(_make_snap(
        logical_key="s:root:m2", message_id="m2",
        input_tokens=200, output_tokens=100, cache_creation=0, cache_read=0,
        timestamp="2026-03-23T11:00:00Z",
    ))
    totals = db.get_cumulative_tokens()
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 150
    assert totals["total_tokens"] == 450


def test_cumulative_tokens_with_window(db):
    db.upsert_snapshot(_make_snap(
        logical_key="s:root:m1", message_id="m1",
        input_tokens=100, output_tokens=50, cache_creation=0, cache_read=0,
        timestamp="2026-03-23T08:00:00Z",
    ))
    db.upsert_snapshot(_make_snap(
        logical_key="s:root:m2", message_id="m2",
        input_tokens=200, output_tokens=100, cache_creation=0, cache_read=0,
        timestamp="2026-03-23T11:00:00Z",
    ))
    totals = db.get_cumulative_tokens(since="2026-03-23T10:00:00Z")
    assert totals["input_tokens"] == 200
    assert totals["total_tokens"] == 300


def test_session_tokens(db):
    db.upsert_snapshot(_make_snap(
        logical_key="s1:root:m1", session_id="s1", message_id="m1",
        input_tokens=100, output_tokens=0, cache_creation=0, cache_read=0,
    ))
    db.upsert_snapshot(_make_snap(
        logical_key="s2:root:m2", session_id="s2", message_id="m2",
        input_tokens=999, output_tokens=0, cache_creation=0, cache_read=0,
    ))
    totals = db.get_session_tokens("s1")
    assert totals["input_tokens"] == 100
    assert totals["total_tokens"] == 100


# --- Agent completions ---


def test_record_agent_completion(db):
    comp = AgentCompletion(
        agent_id="a123",
        session_id="s1",
        total_tokens=50000,
        total_duration_ms=10000,
        total_tool_use_count=5,
        completed_at="2026-03-23T12:00:00Z",
        input_tokens=40000,
        output_tokens=10000,
    )
    db.record_agent_completion(comp)
    row = db.conn.execute(
        "SELECT total_tokens FROM agent_completions WHERE agent_id = ?",
        ("a123",),
    ).fetchone()
    assert row[0] == 50000
