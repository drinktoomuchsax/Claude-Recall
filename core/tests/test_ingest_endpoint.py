"""Tests for the /ingest WebSocket endpoint.

Uses a live uvicorn server since FastAPI's ASGITransport doesn't handle
WebSockets. Each test brings up its own daemon on a random free port
with its own config (ingest enabled/disabled, tokens, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
import uvicorn
import websockets

from claude_recall.config import (
    HostConfig,
    IngestConfig,
    RecallConfig,
    StatesConfig,
    TransportConfig,
)
from claude_recall.models import (
    AggregateFrame,
    HostIdentity,
    PresenceFrame,
    RecallState,
    StateFrame,
)
from claude_recall.rules import RuleEngine
from claude_recall.server import App, create_api


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _running_daemon(config: RecallConfig) -> AsyncIterator[tuple[App, str]]:
    """Start a live daemon with the given config; yield (app, ws_base_url).

    We build the App manually and hand it to a dedicated FastAPI instance
    so multiple daemons can coexist without clobbering shared state.
    """
    port = _free_port()
    app_obj = App(config)
    await app_obj.start()

    fastapi_app = create_api(app_obj=app_obj)
    cfg = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())

    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)

        yield app_obj, f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        await app_obj.stop()


def _default_config(
    *,
    ingest_enabled: bool = False,
    allowed_tokens: list[str] | None = None,
    host_id: str = "upstream-host",
) -> RecallConfig:
    """Minimal config for ingest tests — no transports by default."""
    return RecallConfig(
        host=HostConfig(id=host_id),
        states=StatesConfig(),
        transports={
            # Enable the ws broadcast transport so /ws viewers can connect.
            "websocket": TransportConfig(type="websocket", enabled=True),
        },
        rules=[],
        ingest=IngestConfig(
            enabled=ingest_enabled,
            allowed_tokens=allowed_tokens or [],
        ),
    )


def _make_state_frame(session_id="s1", host_id="downstream-host") -> StateFrame:
    return StateFrame(
        host=HostIdentity(host_id=host_id),
        session_id=session_id,
        state=RecallState.WORKING,
        previous=RecallState.IDLE,
        timestamp=datetime.now(timezone.utc),
    )


# ---- tests ---------------------------------------------------------------


async def test_ingest_rejected_when_disabled():
    cfg = _default_config(ingest_enabled=False)
    async with _running_daemon(cfg) as (_, base):
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with websockets.connect(f"{base}/ingest"):
                pass
        assert ei.value.response.status_code == 403


async def test_ingest_rejects_missing_token_when_required():
    cfg = _default_config(ingest_enabled=True, allowed_tokens=["secret"])
    async with _running_daemon(cfg) as (_, base):
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with websockets.connect(f"{base}/ingest"):
                pass
        # Upstream rejects during WS handshake → HTTP-level rejection.
        # Both 401 (unauthorized) and 403 (forbidden) are acceptable;
        # uvicorn collapses pre-accept WebSocket closes into 403.
        assert ei.value.response.status_code in (401, 403)


async def test_ingest_rejects_wrong_token():
    cfg = _default_config(ingest_enabled=True, allowed_tokens=["secret"])
    async with _running_daemon(cfg) as (_, base):
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with websockets.connect(
                f"{base}/ingest",
                additional_headers={"Authorization": "Bearer wrong"},
            ):
                pass
        assert ei.value.response.status_code in (401, 403)


async def test_ingest_accepts_correct_token_and_hello():
    cfg = _default_config(ingest_enabled=True, allowed_tokens=["secret"])
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(
            f"{base}/ingest",
            additional_headers={"Authorization": "Bearer secret"},
        ) as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "host": {"host_id": "downstream", "display_name": "Downstream"},
            }))
            # Connection should remain open after hello.
            await asyncio.sleep(0.05)
            assert not ws.close_code


async def test_ingest_no_token_required_when_allowlist_empty():
    cfg = _default_config(ingest_enabled=True, allowed_tokens=[])
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ingest") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "host": {"host_id": "downstream"},
            }))
            await asyncio.sleep(0.05)
            assert not ws.close_code


async def test_relay_frame_is_broadcast_to_local_viewers():
    cfg = _default_config(ingest_enabled=True)
    async with _running_daemon(cfg) as (_, base):
        # Connect as a viewer in mode=all to receive both frame types.
        async with websockets.connect(f"{base}/ws?mode=all") as viewer:
            async with websockets.connect(f"{base}/ingest") as ingest_ws:
                await ingest_ws.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "downstream"},
                }))
                # Drain the online presence broadcast (emitted after hello).
                online = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
                assert online["type"] == "presence"
                assert online["status"] == "online"

                frame = _make_state_frame(session_id="sess-X")
                await ingest_ws.send(frame.model_dump_json())
                relayed = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
                assert relayed["type"] == "session"
                assert relayed["session_id"] == "sess-X"
                # Upstream (us) should have stamped host_id onto forwarded_by.
                assert "upstream-host" in relayed["forwarded_by"]


async def test_split_horizon_drops_frames_that_visited_us():
    cfg = _default_config(ingest_enabled=True, host_id="upstream-host")
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ws?mode=all") as viewer:
            async with websockets.connect(f"{base}/ingest") as ingest_ws:
                await ingest_ws.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "downstream"},
                }))
                # Drain online presence.
                await asyncio.wait_for(viewer.recv(), 2.0)

                # Frame crafted to look like it already passed through us.
                frame = _make_state_frame()
                frame.forwarded_by = ["upstream-host"]
                await ingest_ws.send(frame.model_dump_json())

                # Viewer should NOT receive the frame. Give it a short
                # window; any other traffic would be a failure.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(viewer.recv(), 0.3)


async def test_message_id_dedup():
    cfg = _default_config(ingest_enabled=True)
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ws?mode=all") as viewer:
            async with websockets.connect(f"{base}/ingest") as ingest_ws:
                await ingest_ws.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "downstream"},
                }))
                await asyncio.wait_for(viewer.recv(), 2.0)  # online presence

                frame = _make_state_frame()
                await ingest_ws.send(frame.model_dump_json())
                # First arrival — viewer sees it.
                first = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
                assert first["message_id"] == frame.message_id

                # Second arrival with same message_id — dropped.
                await ingest_ws.send(frame.model_dump_json())
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(viewer.recv(), 0.3)


async def test_offline_presence_on_disconnect():
    cfg = _default_config(ingest_enabled=True)
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ws?mode=all") as viewer:
            async with websockets.connect(f"{base}/ingest") as ingest_ws:
                await ingest_ws.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "downstream"},
                }))
                online = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
                assert online["status"] == "online"

            # `async with` above exits → ingest_ws closes → offline announced.
            offline = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
            assert offline["type"] == "presence"
            assert offline["status"] == "offline"
            assert offline["host"]["host_id"] == "downstream"


async def test_malformed_hello_closes_connection():
    cfg = _default_config(ingest_enabled=True)
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ingest") as ws:
            await ws.send("not json at all")
            # Server should close with a 4400-series code.
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2.0)


async def test_malformed_frame_does_not_drop_connection():
    cfg = _default_config(ingest_enabled=True)
    async with _running_daemon(cfg) as (_, base):
        async with websockets.connect(f"{base}/ws?mode=all") as viewer:
            async with websockets.connect(f"{base}/ingest") as ingest_ws:
                await ingest_ws.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "downstream"},
                }))
                await asyncio.wait_for(viewer.recv(), 2.0)  # online

                # Send garbage — server should skip it.
                await ingest_ws.send("garbage not json")
                await ingest_ws.send(json.dumps({"type": "unknown_type"}))

                # Then a real frame — should still get through.
                good = _make_state_frame()
                await ingest_ws.send(good.model_dump_json())
                relayed = json.loads(await asyncio.wait_for(viewer.recv(), 2.0))
                assert relayed["session_id"] == "s1"
