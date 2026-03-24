from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActorType(str, Enum):
    ROOT = "root"
    SUBAGENT = "subagent"


class SourceType(str, Enum):
    MAIN_ASSISTANT = "main_assistant"
    SUBAGENT_ASSISTANT = "subagent_assistant"
    AGENT_PROGRESS = "agent_progress_mirror"
    AGENT_COMPLETION = "agent_completion"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """A single assistant message's token usage at a point in time.

    Same message.id can appear multiple times with growing token counts
    (streaming). The logical_key uniquely identifies the message across
    snapshots: sessionId:actorId:messageId.
    """

    logical_key: str
    session_id: str
    actor_id: str
    actor_type: ActorType
    source_type: SourceType
    message_id: str
    model: str | None
    stop_reason: str | None
    timestamp: str
    request_id: str | None

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_tokens: int

    is_provisional: bool = False

    @staticmethod
    def make_logical_key(session_id: str, actor_id: str, message_id: str) -> str:
        return f"{session_id}:{actor_id}:{message_id}"


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """Difference between two snapshots for the same logical key."""

    logical_key: str
    timestamp: str

    delta_input: int
    delta_output: int
    delta_cache_creation: int
    delta_cache_read: int
    delta_total: int


@dataclass(frozen=True, slots=True)
class AgentCompletion:
    """Captured from toolUseResult when a subagent finishes."""

    agent_id: str
    session_id: str
    total_tokens: int
    total_duration_ms: int
    total_tool_use_count: int
    completed_at: str

    # Breakdown if available
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    """A pair of (our token estimate, official API utilization) at a point in time."""

    timestamp: str
    estimated_tokens_5h: int
    estimated_tokens_7d: int
    official_util_5h: float
    official_util_7d: float


@dataclass(frozen=True, slots=True)
class PromotionWindow:
    """Time range with modified usage limits."""

    start_at: str
    end_at: str
    multiplier: float
    description: str = ""
