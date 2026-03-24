"""Tests for JSONL line parser."""

import json
from pathlib import Path

from claude_usage.models import ActorType, AgentCompletion, SourceType, UsageSnapshot
from claude_usage.parser import parse_line

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text().strip().splitlines()


# --- Assistant root records ---


def test_parse_assistant_root():
    line = _load_fixture("assistant_root.jsonl")[0]
    result = parse_line(line)
    assert isinstance(result, UsageSnapshot)
    assert result.actor_type == ActorType.ROOT
    assert result.actor_id == "root"
    assert result.source_type == SourceType.MAIN_ASSISTANT
    assert result.model == "claude-opus-4-6"
    assert result.input_tokens == 3
    assert result.output_tokens == 11
    assert result.cache_creation_input_tokens == 13097
    assert result.cache_read_input_tokens == 28125
    assert result.total_tokens == 3 + 11 + 13097 + 28125
    assert result.is_provisional is False
    assert result.session_id
    assert result.message_id


def test_parse_assistant_root_logical_key():
    line = _load_fixture("assistant_root.jsonl")[0]
    rec = json.loads(line)
    result = parse_line(line)
    expected_key = f"{rec['sessionId']}:root:{rec['message']['id']}"
    assert result.logical_key == expected_key


# --- Assistant subagent records ---


def test_parse_assistant_subagent():
    line = _load_fixture("assistant_subagent.jsonl")[0]
    result = parse_line(line)
    assert isinstance(result, UsageSnapshot)
    assert result.actor_type == ActorType.SUBAGENT
    assert result.source_type == SourceType.SUBAGENT_ASSISTANT
    assert result.is_provisional is False

    rec = json.loads(line)
    assert result.actor_id == rec["agentId"]
    assert result.session_id == rec["sessionId"]


# --- Agent progress (provisional) ---


def test_parse_agent_progress():
    line = _load_fixture("agent_progress.jsonl")[0]
    result = parse_line(line)
    assert isinstance(result, UsageSnapshot)
    assert result.actor_type == ActorType.SUBAGENT
    assert result.source_type == SourceType.AGENT_PROGRESS
    assert result.is_provisional is True

    rec = json.loads(line)
    assert result.actor_id == rec["data"]["agentId"]
    assert result.input_tokens >= 0
    assert result.total_tokens > 0


# --- Tool use result (agent completion) ---


def test_parse_tool_use_result():
    line = _load_fixture("tool_use_result.jsonl")[0]
    result = parse_line(line)
    assert isinstance(result, AgentCompletion)
    assert result.total_tokens == 101698
    assert result.total_duration_ms == 135454
    assert result.total_tool_use_count == 19
    assert result.agent_id

    rec = json.loads(line)
    assert result.agent_id == rec["toolUseResult"]["agentId"]
    assert result.session_id == rec["sessionId"]


# --- Records that should be skipped ---


def test_parse_user_message_returns_none():
    line = _load_fixture("user_message.jsonl")[0]
    assert parse_line(line) is None


def test_parse_system_message_returns_none():
    line = _load_fixture("system_message.jsonl")[0]
    assert parse_line(line) is None


# --- Edge cases ---


def test_parse_malformed_json():
    assert parse_line("{not valid json") is None
    assert parse_line("") is None
    assert parse_line("null") is None


def test_parse_empty_object():
    assert parse_line("{}") is None


def test_parse_assistant_missing_usage():
    line = json.dumps({"type": "assistant", "message": {"id": "x"}, "sessionId": "s"})
    assert parse_line(line) is None


def test_parse_assistant_missing_message_id():
    line = json.dumps({
        "type": "assistant",
        "message": {"usage": {"input_tokens": 1, "output_tokens": 1}},
        "sessionId": "s",
    })
    assert parse_line(line) is None
