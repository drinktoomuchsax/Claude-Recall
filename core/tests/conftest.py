"""Shared test fixtures."""

import pytest

from claude_recall.config import StatesConfig, StateTTL


@pytest.fixture
def fast_ttl_config():
    """Config with very short TTLs for testing expiry."""
    return StatesConfig(
        error=StateTTL(ttl_sec=0.1, degrade_to="awaiting_input"),
        notification=StateTTL(ttl_sec=0.1, degrade_to="awaiting_input"),
        awaiting_permission=StateTTL(ttl_sec=0.1, degrade_to="awaiting_input"),
        awaiting_input=StateTTL(ttl_sec=0.1, degrade_to="idle"),
        tool_active=StateTTL(ttl_sec=0.1, degrade_to="working"),
        working=StateTTL(ttl_sec=0.1, degrade_to="awaiting_input"),
        idle=StateTTL(ttl_sec=0.1, degrade_to="off"),
    )


@pytest.fixture
def default_config():
    """Default config with normal TTLs."""
    return StatesConfig()
