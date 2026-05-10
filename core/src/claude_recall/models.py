"""Domain models: events, states, and the standard state frame."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    NOTIFICATION = "Notification"
    PERMISSION_REQUEST = "PermissionRequest"


class RecallState(IntEnum):
    """States ordered by priority (higher value = higher priority)."""

    OFF = 0
    IDLE = 10
    WORKING = 30
    TOOL_ACTIVE = 40
    AWAITING_INPUT = 60
    AWAITING_PERMISSION = 80
    NOTIFICATION = 85
    ERROR = 100


class SessionMetadata(BaseModel):
    """Cumulative session metadata, updated incrementally."""

    cwd: str | None = None
    project: str | None = None
    model: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    tool_context: str | None = None
    effort_level: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    error_type: str | None = None


class StateDurations(BaseModel):
    """Cumulative time spent in each state (seconds)."""

    off: float = 0.0
    idle: float = 0.0
    working: float = 0.0
    tool_active: float = 0.0
    awaiting_input: float = 0.0
    awaiting_permission: float = 0.0
    notification: float = 0.0
    error: float = 0.0


DEFAULT_AGENT_KIND = "claude"

# Bump on breaking frame shape changes. Receivers should reject unknown majors.
FRAME_SCHEMA_VERSION = 1


class StateFrame(BaseModel):
    """Per-session state frame."""

    schema_version: int = FRAME_SCHEMA_VERSION
    type: str = "session"
    session_id: str
    agent_kind: str = DEFAULT_AGENT_KIND
    state: RecallState
    previous: RecallState
    duration: float | None = None
    triggered_by: HookEvent | None = None
    metadata: SessionMetadata | None = None
    durations: StateDurations | None = None
    timestamp: datetime


class AggregateFrame(BaseModel):
    """Aggregated state across all active sessions."""

    schema_version: int = FRAME_SCHEMA_VERSION
    type: str = "aggregate"
    state: RecallState
    active_sessions: int
    breakdown: dict[str, int]
    timestamp: datetime
