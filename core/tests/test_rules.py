"""Tests for the rule engine."""

import asyncio

import pytest

from claude_recall.config import DEFAULT_RULES, RuleConfig
from claude_recall.models import HookEvent, RecallState
from claude_recall.rules import RuleEngine


@pytest.fixture
def engine():
    rules = [RuleConfig.model_validate(r) for r in DEFAULT_RULES]
    return RuleEngine(rules)


def test_stop_maps_to_awaiting_input(engine):
    result = engine.resolve(HookEvent.STOP)
    assert result == RecallState.AWAITING_INPUT


def test_permission_request_maps_to_awaiting_permission(engine):
    result = engine.resolve(HookEvent.PERMISSION_REQUEST)
    assert result == RecallState.AWAITING_PERMISSION


def test_user_prompt_maps_to_working(engine):
    result = engine.resolve(HookEvent.USER_PROMPT_SUBMIT)
    assert result == RecallState.WORKING


def test_session_end_maps_to_off(engine):
    result = engine.resolve(HookEvent.SESSION_END)
    assert result == RecallState.OFF


def test_stop_failure_maps_to_error(engine):
    result = engine.resolve(HookEvent.STOP_FAILURE)
    assert result == RecallState.ERROR


def test_all_hook_events_have_mapping(engine):
    for event in HookEvent:
        result = engine.resolve(event)
        assert result is not None, f"No rule for {event}"


def test_debounce_blocks_rapid_fire():
    rules = [RuleConfig(event="PreToolUse", state="tool_active", debounce_ms=200)]
    engine = RuleEngine(rules)

    first = engine.resolve(HookEvent.PRE_TOOL_USE)
    assert first == RecallState.TOOL_ACTIVE

    second = engine.resolve(HookEvent.PRE_TOOL_USE)
    assert second is None


@pytest.mark.asyncio
async def test_debounce_expires():
    rules = [RuleConfig(event="PreToolUse", state="tool_active", debounce_ms=100)]
    engine = RuleEngine(rules)

    first = engine.resolve(HookEvent.PRE_TOOL_USE)
    assert first == RecallState.TOOL_ACTIVE

    await asyncio.sleep(0.15)

    second = engine.resolve(HookEvent.PRE_TOOL_USE)
    assert second == RecallState.TOOL_ACTIVE
