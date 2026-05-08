"""Tests for multi-session state management."""

import asyncio

import pytest

from claude_recall.config import StatesConfig, StateTTL
from claude_recall.models import HookEvent, RecallState
from claude_recall.session_registry import SessionRegistry


@pytest.fixture
def registry(default_config):
    return SessionRegistry(default_config)


@pytest.mark.asyncio
async def test_new_session_auto_created(registry):
    session_frame, agg_frame = await registry.handle_transition(
        "s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT
    )
    assert session_frame is not None
    assert session_frame.session_id == "s1"
    assert session_frame.state == RecallState.WORKING


@pytest.mark.asyncio
async def test_aggregate_is_max_priority(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s2", RecallState.ERROR, HookEvent.STOP_FAILURE)

    agg = await registry.get_aggregate()
    assert agg.state == RecallState.ERROR
    assert agg.active_sessions == 2


@pytest.mark.asyncio
async def test_session_end_removes_session(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s2", RecallState.IDLE, HookEvent.SESSION_START)

    session_frame, agg_frame = await registry.handle_transition(
        "s1", RecallState.OFF, HookEvent.SESSION_END
    )

    assert session_frame is not None
    assert session_frame.state == RecallState.OFF
    assert agg_frame.active_sessions == 1
    assert agg_frame.state == RecallState.IDLE


@pytest.mark.asyncio
async def test_all_sessions_end_gives_off(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s1", RecallState.OFF, HookEvent.SESSION_END)

    agg = await registry.get_aggregate()
    assert agg.state == RecallState.OFF
    assert agg.active_sessions == 0


@pytest.mark.asyncio
async def test_one_session_change_doesnt_affect_other(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s2", RecallState.IDLE, HookEvent.SESSION_START)

    await registry.handle_transition("s1", RecallState.ERROR, HookEvent.STOP_FAILURE)

    s2_state = await registry.get_session_state("s2")
    assert s2_state == RecallState.IDLE


@pytest.mark.asyncio
async def test_breakdown_counts(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s2", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s3", RecallState.IDLE, HookEvent.SESSION_START)

    agg = await registry.get_aggregate()
    assert agg.breakdown == {"working": 2, "idle": 1}
    assert agg.active_sessions == 3


@pytest.mark.asyncio
async def test_cleanup_expired():
    config = StatesConfig()
    registry = SessionRegistry(config, session_timeout_sec=0.1)

    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await asyncio.sleep(0.15)

    removed = await registry.cleanup_expired()
    assert "s1" in removed

    sessions = await registry.list_sessions()
    assert "s1" not in sessions


@pytest.mark.asyncio
async def test_get_nonexistent_session_returns_none(registry):
    state = await registry.get_session_state("nonexistent")
    assert state is None


@pytest.mark.asyncio
async def test_list_sessions(registry):
    await registry.handle_transition("s1", RecallState.WORKING, HookEvent.USER_PROMPT_SUBMIT)
    await registry.handle_transition("s2", RecallState.IDLE, HookEvent.SESSION_START)

    sessions = await registry.list_sessions()
    assert sessions == {"s1": "working", "s2": "idle"}
