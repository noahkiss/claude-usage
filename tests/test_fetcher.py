"""Tests for API fetcher."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from claude_usage.fetcher import (
    BACKOFF_DURATION,
    _is_backing_off,
    _set_backoff,
    fetch_usage,
    read_oauth_token,
    store_calibration_point,
    write_cache,
    append_history,
    fetch_and_store,
)
from claude_usage.db import TrackerDB


@pytest.fixture
def db(tmp_path):
    d = TrackerDB(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def creds_file(tmp_path):
    creds = {"claudeAiOauth": {"accessToken": "test-token-123"}}
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(creds))
    return path


@pytest.fixture
def backoff_dir(tmp_path):
    """Provide a temp dir for backoff file."""
    return tmp_path


def _make_api_response(util_5h=50.0, util_7d=30.0):
    """Create a realistic API response."""
    now = datetime.now(timezone.utc)
    return {
        "five_hour": {
            "utilization": util_5h,
            "resets_at": (now + timedelta(hours=2)).isoformat(),
        },
        "seven_day": {
            "utilization": util_7d,
            "resets_at": (now + timedelta(days=3)).isoformat(),
        },
    }


# --- Token reading ---


def test_read_oauth_token(creds_file):
    with patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file):
        token = read_oauth_token()
    assert token == "test-token-123"


def test_read_oauth_token_missing(tmp_path):
    with patch("claude_usage.fetcher.CREDENTIALS_FILE", tmp_path / "nope.json"):
        token = read_oauth_token()
    assert token is None


def test_read_oauth_token_no_access_token(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"claudeAiOauth": {}}))
    with patch("claude_usage.fetcher.CREDENTIALS_FILE", path):
        token = read_oauth_token()
    assert token is None


# --- Backoff ---


def test_backoff_not_set(tmp_path):
    with patch("claude_usage.fetcher.BACKOFF_FILE", tmp_path / "backoff"):
        assert _is_backing_off() is False


def test_backoff_active(tmp_path):
    bf = tmp_path / "backoff"
    bf.write_text(str(int(time.time()) + 600))
    with patch("claude_usage.fetcher.BACKOFF_FILE", bf):
        assert _is_backing_off() is True


def test_backoff_expired(tmp_path):
    bf = tmp_path / "backoff"
    bf.write_text(str(int(time.time()) - 10))
    with patch("claude_usage.fetcher.BACKOFF_FILE", bf):
        assert _is_backing_off() is False
        assert not bf.exists()  # cleaned up


def test_set_backoff(tmp_path):
    bf = tmp_path / "backoff"
    with patch("claude_usage.fetcher.BACKOFF_FILE", bf):
        _set_backoff(60)
    assert bf.exists()
    val = int(bf.read_text().strip())
    assert val > int(time.time())


# --- fetch_usage ---


def test_fetch_usage_skips_during_backoff(tmp_path, creds_file):
    bf = tmp_path / "backoff"
    bf.write_text(str(int(time.time()) + 600))
    with patch("claude_usage.fetcher.BACKOFF_FILE", bf), \
         patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file):
        result = fetch_usage()
    assert result is None


def test_fetch_usage_force_ignores_backoff(tmp_path, creds_file):
    bf = tmp_path / "backoff"
    bf.write_text(str(int(time.time()) + 600))

    api_resp = _make_api_response()
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(api_resp).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("claude_usage.fetcher.BACKOFF_FILE", bf), \
         patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = fetch_usage(force=True)

    assert result is not None
    assert "fetched_at" in result
    assert result["five_hour"]["utilization"] == 50.0


def test_fetch_usage_success(creds_file, tmp_path):
    api_resp = _make_api_response(util_5h=75.0, util_7d=40.0)
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(api_resp).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file), \
         patch("claude_usage.fetcher.BACKOFF_FILE", tmp_path / "backoff"), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = fetch_usage()

    assert result is not None
    assert result["five_hour"]["utilization"] == 75.0
    assert result["seven_day"]["utilization"] == 40.0
    assert "fetched_at" in result


def test_fetch_usage_api_error_sets_backoff(creds_file, tmp_path):
    """API error with 'rate limit' should trigger backoff."""
    import urllib.error

    bf = tmp_path / "backoff"
    error_body = json.dumps({"error": {"message": "Rate limit exceeded"}}).encode()
    http_error = urllib.error.HTTPError(
        url="", code=429, msg="", hdrs=None, fp=MagicMock(read=lambda: error_body)
    )

    with patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file), \
         patch("claude_usage.fetcher.BACKOFF_FILE", bf), \
         patch("urllib.request.urlopen", side_effect=http_error):
        result = fetch_usage()

    assert result is None
    assert bf.exists()


# --- Cache and history ---


def test_write_cache(tmp_path):
    cache = tmp_path / "usage.json"
    data = {"five_hour": {"utilization": 50.0}}
    with patch("claude_usage.fetcher.CACHE_FILE", cache):
        write_cache(data)
    assert json.loads(cache.read_text()) == data


def test_append_history(tmp_path):
    history = tmp_path / "history.jsonl"
    data1 = {"five_hour": {"utilization": 50.0}, "fetched_at": "2026-03-23T10:00:00Z"}
    data2 = {"five_hour": {"utilization": 60.0}, "fetched_at": "2026-03-23T10:05:00Z"}
    with patch("claude_usage.fetcher.HISTORY_FILE", history):
        append_history(data1)
        append_history(data2)
    lines = history.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["fetched_at"] == "2026-03-23T10:00:00Z"
    assert json.loads(lines[1])["fetched_at"] == "2026-03-23T10:05:00Z"


# --- store_calibration_point ---


def test_store_calibration_point(db):
    now = datetime.now(timezone.utc)
    data = {
        "five_hour": {
            "utilization": 60.0,
            "resets_at": (now + timedelta(hours=2)).isoformat(),
        },
        "seven_day": {
            "utilization": 35.0,
            "resets_at": (now + timedelta(days=3)).isoformat(),
        },
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    store_calibration_point(db, data)

    row = db.conn.execute(
        "SELECT official_util_5h, official_util_7d FROM calibration"
    ).fetchone()
    assert row is not None
    assert row[0] == 60.0
    assert row[1] == 35.0


def test_store_calibration_point_no_fetched_at(db):
    """Missing fetched_at should skip silently."""
    data = {"five_hour": {"utilization": 50.0}}
    store_calibration_point(db, data)
    row = db.conn.execute("SELECT COUNT(*) FROM calibration").fetchone()
    assert row[0] == 0


# --- fetch_and_store integration ---


def test_fetch_and_store(db, creds_file, tmp_path):
    api_resp = _make_api_response(util_5h=80.0, util_7d=45.0)
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(api_resp).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    cache = tmp_path / "usage.json"
    history = tmp_path / "history.jsonl"
    bf = tmp_path / "backoff"

    with patch("claude_usage.fetcher.CREDENTIALS_FILE", creds_file), \
         patch("claude_usage.fetcher.CACHE_FILE", cache), \
         patch("claude_usage.fetcher.HISTORY_FILE", history), \
         patch("claude_usage.fetcher.BACKOFF_FILE", bf), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = fetch_and_store(db)

    assert result is not None
    assert result["five_hour"]["utilization"] == 80.0

    # Cache written
    assert cache.exists()
    cached = json.loads(cache.read_text())
    assert cached["five_hour"]["utilization"] == 80.0

    # History appended
    assert history.exists()
    lines = history.read_text().strip().split("\n")
    assert len(lines) == 1

    # Calibration point stored
    row = db.conn.execute("SELECT official_util_5h FROM calibration").fetchone()
    assert row[0] == 80.0
