"""JSONL line parser — classify records, extract tokens, compute logical keys."""

from __future__ import annotations

import json
from typing import Union

from claude_usage.models import (
    ActorType,
    AgentCompletion,
    ConversationBoundary,
    SourceType,
    UsageSnapshot,
)

ParsedEvent = Union[UsageSnapshot, AgentCompletion, ConversationBoundary, None]


def parse_line(line: str) -> ParsedEvent:
    """Parse a single JSONL line into a structured event.

    Returns UsageSnapshot, AgentCompletion, or None (irrelevant record).
    """
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(rec, dict):
        return None

    rec_type = rec.get("type")

    if rec_type == "assistant":
        return _parse_assistant(rec)
    elif rec_type == "progress":
        return _parse_progress(rec)
    elif rec_type == "user":
        return _parse_tool_use_result(rec)
    elif rec_type == "system":
        return _parse_system(rec)
    return None


def _parse_assistant(rec: dict) -> UsageSnapshot | None:
    """Parse an assistant record (main session or subagent file)."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None

    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    message_id = msg.get("id")
    if not message_id:
        return None

    session_id = rec.get("sessionId", "")
    is_subagent = rec.get("isSidechain", False)
    agent_id = rec.get("agentId", "")

    if is_subagent and agent_id:
        actor_id = agent_id
        actor_type = ActorType.SUBAGENT
        source_type = SourceType.SUBAGENT_ASSISTANT
    else:
        actor_id = "root"
        actor_type = ActorType.ROOT
        source_type = SourceType.MAIN_ASSISTANT

    input_t = usage.get("input_tokens", 0) or 0
    output_t = usage.get("output_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    total = input_t + output_t + cache_create + cache_read

    return UsageSnapshot(
        logical_key=UsageSnapshot.make_logical_key(session_id, actor_id, message_id),
        session_id=session_id,
        actor_id=actor_id,
        actor_type=actor_type,
        source_type=source_type,
        message_id=message_id,
        model=msg.get("model"),
        stop_reason=msg.get("stop_reason"),
        timestamp=rec.get("timestamp", ""),
        request_id=rec.get("requestId"),
        input_tokens=input_t,
        output_tokens=output_t,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
        total_tokens=total,
    )


def _parse_progress(rec: dict) -> UsageSnapshot | None:
    """Parse an agent_progress record (provisional mirror of subagent work)."""
    data = rec.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("type") != "agent_progress":
        return None

    agent_id = data.get("agentId", "")
    inner_msg_wrapper = data.get("message")
    if not isinstance(inner_msg_wrapper, dict):
        return None

    inner_msg = inner_msg_wrapper.get("message")
    if not isinstance(inner_msg, dict):
        return None

    usage = inner_msg.get("usage")
    if not isinstance(usage, dict):
        return None

    message_id = inner_msg.get("id")
    if not message_id:
        return None

    session_id = rec.get("sessionId", "")
    actor_id = agent_id or "unknown"

    input_t = usage.get("input_tokens", 0) or 0
    output_t = usage.get("output_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    total = input_t + output_t + cache_create + cache_read

    return UsageSnapshot(
        logical_key=UsageSnapshot.make_logical_key(session_id, actor_id, message_id),
        session_id=session_id,
        actor_id=actor_id,
        actor_type=ActorType.SUBAGENT,
        source_type=SourceType.AGENT_PROGRESS,
        message_id=message_id,
        model=inner_msg.get("model"),
        stop_reason=inner_msg.get("stop_reason"),
        timestamp=rec.get("timestamp", ""),
        request_id=inner_msg_wrapper.get("requestId"),
        input_tokens=input_t,
        output_tokens=output_t,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
        total_tokens=total,
        is_provisional=True,
    )


def _parse_tool_use_result(rec: dict) -> AgentCompletion | None:
    """Parse a toolUseResult record for agent completion data."""
    tur = rec.get("toolUseResult")
    if not isinstance(tur, dict):
        return None

    total_tokens = tur.get("totalTokens")
    if total_tokens is None:
        return None

    agent_id = tur.get("agentId", "")
    if not agent_id:
        return None

    usage = tur.get("usage", {}) or {}

    return AgentCompletion(
        agent_id=agent_id,
        session_id=rec.get("sessionId", ""),
        total_tokens=total_tokens,
        total_duration_ms=tur.get("totalDurationMs", 0) or 0,
        total_tool_use_count=tur.get("totalToolUseCount", 0) or 0,
        completed_at=rec.get("timestamp", ""),
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
    )


def _parse_system(rec: dict) -> ConversationBoundary | None:
    """Parse a system record for conversation boundaries (compact/clear)."""
    if rec.get("subtype") != "compact_boundary":
        return None

    session_id = rec.get("sessionId", "")
    if not session_id:
        return None

    metadata = rec.get("compactMetadata", {}) or {}
    trigger = metadata.get("trigger", "unknown")

    return ConversationBoundary(
        session_id=session_id,
        timestamp=rec.get("timestamp", ""),
        trigger=trigger,
    )
