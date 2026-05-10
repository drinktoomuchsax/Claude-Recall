"""Tests for frame schema versioning."""

from datetime import datetime, timezone

import pytest

from claude_recall.models import (
    FRAME_SCHEMA_VERSION,
    AggregateFrame,
    HookEvent,
    RecallState,
    StateFrame,
)
from claude_recall.session_registry import SessionRegistry


def test_schema_version_is_positive_int():
    assert isinstance(FRAME_SCHEMA_VERSION, int)
    assert FRAME_SCHEMA_VERSION >= 1


def test_state_frame_defaults_schema_version():
    frame = StateFrame(
        session_id="s1",
        state=RecallState.WORKING,
        previous=RecallState.IDLE,
        timestamp=datetime.now(timezone.utc),
    )
    assert frame.schema_version == FRAME_SCHEMA_VERSION


def test_aggregate_frame_defaults_schema_version():
    frame = AggregateFrame(
        state=RecallState.WORKING,
        active_sessions=1,
        breakdown={"working": 1},
        timestamp=datetime.now(timezone.utc),
    )
    assert frame.schema_version == FRAME_SCHEMA_VERSION


def test_schema_version_serialized_in_json():
    frame = AggregateFrame(
        state=RecallState.IDLE,
        active_sessions=0,
        breakdown={},
        timestamp=datetime.now(timezone.utc),
    )
    payload = frame.model_dump()
    assert payload["schema_version"] == FRAME_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_registry_emits_frames_with_schema_version(default_config):
    registry = SessionRegistry(default_config)
    session_frame, aggregate_frame = await registry.handle_transition(
        "s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT
    )
    assert session_frame is not None
    assert session_frame.schema_version == FRAME_SCHEMA_VERSION
    assert aggregate_frame is not None
    assert aggregate_frame.schema_version == FRAME_SCHEMA_VERSION
