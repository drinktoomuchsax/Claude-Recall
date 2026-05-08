"""Tests for the HTTP API."""

import pytest
from httpx import ASGITransport, AsyncClient

from claude_recall.server import api, lifespan


@pytest.fixture
async def client():
    async with lifespan(api):
        transport = ASGITransport(app=api)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_post_event_triggers_state_change(client):
    r = await client.post("/events", json={"event": "UserPromptSubmit", "session_id": "t1"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["state"] == "working"


@pytest.mark.asyncio
async def test_post_event_with_session_id(client):
    r = await client.post("/events", json={"event": "Stop", "session_id": "sess-abc"})
    data = r.json()
    assert data["session_id"] == "sess-abc"


@pytest.mark.asyncio
async def test_post_event_without_session_id_uses_default(client):
    r = await client.post("/events", json={"event": "UserPromptSubmit"})
    data = r.json()
    assert data["session_id"] == "default"


@pytest.mark.asyncio
async def test_get_state_returns_aggregate(client):
    await client.post("/events", json={"event": "UserPromptSubmit", "session_id": "s1"})
    await client.post("/events", json={"event": "PermissionRequest", "session_id": "s2"})

    r = await client.get("/state")
    data = r.json()
    assert data["state"] == "awaiting_permission"
    assert data["active_sessions"] == 2


@pytest.mark.asyncio
async def test_get_sessions_lists_all(client):
    await client.post("/events", json={"event": "UserPromptSubmit", "session_id": "s1"})
    await client.post("/events", json={"event": "SessionStart", "session_id": "s2"})

    r = await client.get("/sessions")
    data = r.json()
    assert "s1" in data["sessions"]
    assert "s2" in data["sessions"]


@pytest.mark.asyncio
async def test_get_session_by_id(client):
    await client.post("/events", json={"event": "UserPromptSubmit", "session_id": "s1"})

    r = await client.get("/sessions/s1")
    data = r.json()
    assert data["state"] == "working"


@pytest.mark.asyncio
async def test_get_nonexistent_session(client):
    r = await client.get("/sessions/nonexistent")
    data = r.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_unknown_event(client):
    r = await client.post("/events", json={"event": "FakeEvent", "session_id": "s1"})
    data = r.json()
    assert data["status"] == "unknown_event"


@pytest.mark.asyncio
async def test_debounced_event(client):
    # PreToolUse has 2000ms debounce
    await client.post("/events", json={"event": "PreToolUse", "session_id": "s1"})
    r = await client.post("/events", json={"event": "PreToolUse", "session_id": "s1"})
    data = r.json()
    assert data["status"] == "debounced"
