"""End-to-end cascade tests: two and three-level daemon topologies.

Each test wires real uvicorn servers together via PushTransport /
/ingest and verifies that frames flow across hops with correct host
identity, forwarded_by path accumulation, and loop prevention.
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
from claude_recall.models import HostIdentity, RecallState, StateFrame
from claude_recall.server import App, create_api


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _spawn_daemon(
    host_id: str,
    *,
    upstream_url: str | None = None,
    ingest_enabled: bool = False,
) -> AsyncIterator[tuple[App, int]]:
    """Launch one daemon on a free port. Yields (app, port).

    Each daemon gets its own FastAPI instance via create_api() so multiple
    daemons can coexist in-process without trampling on each other.
    """
    port = _free_port()
    transports: dict[str, TransportConfig] = {
        "websocket": TransportConfig(type="websocket", enabled=True),
    }
    if upstream_url:
        transports["push"] = TransportConfig(
            type="push",
            enabled=True,
            options={"upstream_url": upstream_url},
        )
    config = RecallConfig(
        host=HostConfig(id=host_id),
        states=StatesConfig(),
        transports=transports,
        rules=[],
        ingest=IngestConfig(enabled=ingest_enabled),
    )
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
        yield app_obj, port
    finally:
        server.should_exit = True
        await task
        await app_obj.stop()


async def _wait_for_push_connection(app_obj: App, timeout: float = 3.0) -> None:
    """Wait until an App's push transport (if configured) is connected."""
    from claude_recall.transports.push import PushTransport
    deadline = asyncio.get_event_loop().time() + timeout
    push = next((t for t in app_obj.transports if isinstance(t, PushTransport)), None)
    if push is None:
        return
    while asyncio.get_event_loop().time() < deadline:
        if push.is_connected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("push transport did not connect")


# ---- two-level cascade ----------------------------------------------------


async def test_two_level_cascade_relays_state_frame():
    """Downstream daemon A pushes to upstream B; A-originated frame
    reaches B's /ws viewer with A as origin host."""
    async with _spawn_daemon("upstream-B", ingest_enabled=True) as (B, port_b):
        upstream_url = f"ws://127.0.0.1:{port_b}/ingest"
        async with _spawn_daemon("downstream-A", upstream_url=upstream_url) as (A, _):
            await _wait_for_push_connection(A)

            async with websockets.connect(f"ws://127.0.0.1:{port_b}/ws?mode=all") as viewer:
                # Drain any initial presence/state noise.
                try:
                    while True:
                        await asyncio.wait_for(viewer.recv(), 0.1)
                except asyncio.TimeoutError:
                    pass

                from claude_recall.models import HookEvent
                session_frame, _ = await A.registry.handle_transition(
                    "sess-Z",
                    RecallState.WORKING,
                    HookEvent.USER_PROMPT_SUBMIT,
                    force=True,
                )
                assert session_frame is not None
                assert session_frame.host.host_id == "downstream-A"

                await A._broadcast_session_frame(session_frame)

                deadline = asyncio.get_event_loop().time() + 3.0
                relayed_raw = None
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(viewer.recv(), 0.5)
                        data = json.loads(msg)
                        if data.get("type") == "session" and data.get("session_id") == "sess-Z":
                            relayed_raw = data
                            break
                    except asyncio.TimeoutError:
                        continue
                assert relayed_raw is not None, "relayed frame not observed on upstream viewer"
                assert relayed_raw["host"]["host_id"] == "downstream-A"
                assert "upstream-B" in relayed_raw["forwarded_by"]


# ---- loop prevention ------------------------------------------------------


async def test_loop_prevention_when_frame_already_passed_through_self():
    """If a frame claims it already visited us, we must drop it."""
    async with _spawn_daemon("upstream-B", ingest_enabled=True) as (_B, port_b):
        async with websockets.connect(f"ws://127.0.0.1:{port_b}/ws?mode=all") as viewer:
            async with websockets.connect(f"ws://127.0.0.1:{port_b}/ingest") as ingest:
                await ingest.send(json.dumps({
                    "type": "hello",
                    "host": {"host_id": "attacker"},
                }))
                await asyncio.wait_for(viewer.recv(), 2.0)  # drain online

                forged = StateFrame(
                    host=HostIdentity(host_id="attacker"),
                    forwarded_by=["upstream-B"],
                    session_id="evil",
                    state=RecallState.ERROR,
                    previous=RecallState.IDLE,
                    timestamp=datetime.now(timezone.utc),
                )
                await ingest.send(forged.model_dump_json())

                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(viewer.recv(), 0.3)


# ---- three-level cascade (forwarded_by accumulation) ----------------------


async def test_three_level_cascade_accumulates_forwarded_by():
    """Chain: A → B → C. When A emits a frame, C's viewer sees host=A
    with forwarded_by containing both intermediate hops."""
    async with _spawn_daemon("upstream-C", ingest_enabled=True) as (_C, port_c):
        upstream_for_B = f"ws://127.0.0.1:{port_c}/ingest"
        async with _spawn_daemon(
            "middle-B",
            upstream_url=upstream_for_B,
            ingest_enabled=True,
        ) as (B, port_b):
            await _wait_for_push_connection(B)

            upstream_for_A = f"ws://127.0.0.1:{port_b}/ingest"
            async with _spawn_daemon(
                "downstream-A",
                upstream_url=upstream_for_A,
            ) as (A, _):
                await _wait_for_push_connection(A)

                async with websockets.connect(f"ws://127.0.0.1:{port_c}/ws?mode=all") as viewer:
                    try:
                        while True:
                            await asyncio.wait_for(viewer.recv(), 0.1)
                    except asyncio.TimeoutError:
                        pass

                    from claude_recall.models import HookEvent
                    session_frame, _ = await A.registry.handle_transition(
                        "trace-me",
                        RecallState.WORKING,
                        HookEvent.USER_PROMPT_SUBMIT,
                        force=True,
                    )
                    assert session_frame is not None
                    await A._broadcast_session_frame(session_frame)

                    deadline = asyncio.get_event_loop().time() + 5.0
                    seen = None
                    while asyncio.get_event_loop().time() < deadline:
                        try:
                            msg = await asyncio.wait_for(viewer.recv(), 0.5)
                            data = json.loads(msg)
                            if data.get("type") == "session" and data.get("session_id") == "trace-me":
                                seen = data
                                break
                        except asyncio.TimeoutError:
                            continue
                    assert seen is not None, "frame did not reach end of 3-hop chain"
                    assert seen["host"]["host_id"] == "downstream-A"
                    assert "middle-B" in seen["forwarded_by"]
                    assert "upstream-C" in seen["forwarded_by"]
