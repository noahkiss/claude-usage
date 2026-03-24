"""Tests for file discovery and incremental reading."""

import json

import pytest

from claude_usage.db import TrackerDB
from claude_usage.scanner import discover_jsonl_files, read_new_lines, scan_and_ingest


@pytest.fixture
def db(tmp_path):
    d = TrackerDB(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def projects_dir(tmp_path):
    """Create a mock projects directory structure."""
    proj = tmp_path / "projects" / "-home-user-project"
    proj.mkdir(parents=True)
    return tmp_path / "projects"


def _assistant_line(session_id="sess1", msg_id="msg1", output_tokens=10):
    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-03-23T12:00:00Z",
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-6",
            "usage": {
                "input_tokens": 100,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    })


# --- File discovery ---


def test_discover_finds_session_files(projects_dir):
    proj = projects_dir / "-home-user-project"
    (proj / "session1.jsonl").write_text("")
    (proj / "session2.jsonl").write_text("")

    files = discover_jsonl_files(projects_dir)
    assert len(files) == 2


def test_discover_excludes_history(projects_dir):
    proj = projects_dir / "-home-user-project"
    (proj / "session1.jsonl").write_text("")
    (proj / "history.jsonl").write_text("")

    files = discover_jsonl_files(projects_dir)
    assert len(files) == 1
    assert files[0].name == "session1.jsonl"


def test_discover_finds_subagent_files(projects_dir):
    proj = projects_dir / "-home-user-project"
    sess_dir = proj / "session1" / "subagents"
    sess_dir.mkdir(parents=True)
    (proj / "session1.jsonl").write_text("")
    (sess_dir / "agent-abc.jsonl").write_text("")

    files = discover_jsonl_files(projects_dir)
    assert len(files) == 2


def test_discover_empty_dir(tmp_path):
    files = discover_jsonl_files(tmp_path / "nonexistent")
    assert files == []


# --- Incremental reading ---


def test_read_new_lines_from_start(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text('{"a":1}\n{"b":2}\n')

    lines, offset = read_new_lines(f, 0)
    assert len(lines) == 2
    assert offset > 0


def test_read_new_lines_incremental(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text('{"a":1}\n')
    _, offset = read_new_lines(f, 0)

    # Append more data
    with open(f, "a") as fh:
        fh.write('{"b":2}\n')

    lines, new_offset = read_new_lines(f, offset)
    assert len(lines) == 1
    assert '{"b":2}' in lines[0]
    assert new_offset > offset


def test_read_new_lines_no_change(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text('{"a":1}\n')
    _, offset = read_new_lines(f, 0)

    lines, new_offset = read_new_lines(f, offset)
    assert lines == []
    assert new_offset == offset


def test_read_new_lines_partial_line(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text('{"a":1}\n{"incomplete')

    lines, offset = read_new_lines(f, 0)
    assert len(lines) == 1  # Only complete line
    assert '{"a":1}' in lines[0]


def test_read_new_lines_truncated_file(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
    _, offset = read_new_lines(f, 0)

    # Truncate
    f.write_text('{"x":1}\n')
    lines, new_offset = read_new_lines(f, offset)
    # Should reset to beginning
    assert len(lines) == 1


def test_read_new_lines_missing_file(tmp_path):
    f = tmp_path / "gone.jsonl"
    lines, offset = read_new_lines(f, 0)
    assert lines == []
    assert offset == 0


# --- Full scan cycle ---


def test_scan_and_ingest(db, projects_dir):
    proj = projects_dir / "-home-user-project"
    f = proj / "session1.jsonl"
    f.write_text(_assistant_line() + "\n")

    count = scan_and_ingest(db, projects_dir)
    assert count == 1

    # Second scan with no new data
    count = scan_and_ingest(db, projects_dir)
    assert count == 0


def test_scan_incremental(db, projects_dir):
    proj = projects_dir / "-home-user-project"
    f = proj / "session1.jsonl"
    f.write_text(_assistant_line(msg_id="m1") + "\n")

    scan_and_ingest(db, projects_dir)

    # Append new line
    with open(f, "a") as fh:
        fh.write(_assistant_line(msg_id="m2", output_tokens=20) + "\n")

    count = scan_and_ingest(db, projects_dir)
    assert count == 1

    totals = db.get_cumulative_tokens()
    assert totals["total_tokens"] == (100 + 10) + (100 + 20)
