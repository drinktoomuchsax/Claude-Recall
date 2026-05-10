"""FastAPI application: receives events, manages sessions, dispatches to transports."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from claude_recall.config import RecallConfig, load_config
from claude_recall.message_cache import MessageIdCache
from claude_recall.models import (
    DEFAULT_AGENT_KIND,
    AggregateFrame,
    HookEvent,
    HostIdentity,
    PresenceFrame,
    RecallState,
    StateFrame,
)
from claude_recall.rules import RuleEngine
from claude_recall.session_registry import SessionRegistry
from claude_recall.transports import get_transport_class
from claude_recall.transports.base import BaseTransport
from claude_recall.transports.push import PushTransport
from claude_recall.transports.websocket import WebSocketTransport

logger = logging.getLogger(__name__)


class EventPayload(BaseModel):
    event: str
    session_id: str | None = None
    agent_kind: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] | None = None
    raw: dict[str, Any] = {}


class App:
    def __init__(self, config: RecallConfig):
        self.config = config
        self.host_identity = HostIdentity(
            host_id=config.host.resolve_id(),
            display_name=config.host.resolve_display_name(),
        )
        self.registry = SessionRegistry(config.states, host_identity=self.host_identity)
        self.rules = RuleEngine(config.rules)
        self.transports: list[BaseTransport] = []
        self._ws_transport: WebSocketTransport | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._ingest_cache = MessageIdCache(
            ttl_sec=config.ingest.dedup_ttl_sec,
            max_size=config.ingest.dedup_max_size,
        )

    async def start(self) -> None:
        for name, tc in self.config.transports.items():
            if not tc.enabled:
                continue
            cls = get_transport_class(tc.type)
            options = dict(tc.options)
            # Transports that need this daemon's identity (e.g. push) get it
            # injected here so they don't have to re-resolve the config.
            options.setdefault("host_identity", self.host_identity)
            transport = cls(name=name, options=options)
            await transport.start()
            self.transports.append(transport)
            if isinstance(transport, WebSocketTransport):
                self._ws_transport = transport

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        # Announce ourselves as online to local viewers and (if configured)
        # any upstream daemon. Done after transports are up so PushTransport
        # can actually relay it — though there is an inherent race: the push
        # connection may still be establishing. The reconnect loop doesn't
        # replay missed frames, so this frame may be lost on first start.
        # That's acceptable for a best-effort signal; the next event-driven
        # frame will carry correct status anyway.
        await self._emit_local_presence("online")

    async def _emit_local_presence(self, status: str) -> None:
        frame = PresenceFrame(
            host=self.host_identity,
            status=status,  # type: ignore[arg-type]
            timestamp=datetime.now(timezone.utc),
        )
        await self._broadcast_presence_frame(frame)

    async def stop(self) -> None:
        # Best-effort farewell before transports tear down.
        try:
            await self._emit_local_presence("offline")
        except Exception as e:
            logger.debug("failed to emit offline presence on stop: %s", e)
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for t in self.transports:
            await t.stop()

    async def handle_event(self, payload: EventPayload) -> dict:
        try:
            hook_event = HookEvent(payload.event)
        except ValueError:
            return {"status": "unknown_event"}

        result = self.rules.resolve(hook_event)
        if result is None:
            return {"status": "debounced"}

        session_id = payload.session_id or self._default_session_id()
        agent_kind = payload.agent_kind or DEFAULT_AGENT_KIND

        session_frame, aggregate_frame = await self.registry.handle_transition(
            session_id,
            result.state,
            hook_event,
            force=result.force,
            metadata=payload.metadata,
            agent_kind=agent_kind,
        )

        if session_frame:
            await self._broadcast_session_frame(session_frame)
        if aggregate_frame:
            await self._broadcast_aggregate_frame(aggregate_frame)

        if not session_frame and not aggregate_frame:
            return {"status": "no_change"}

        return {"status": "ok", "state": result.state.name.lower(), "session_id": session_id}

    async def _broadcast_session_frame(self, frame: StateFrame) -> None:
        for transport in self.transports:
            try:
                await transport.send(frame)
            except Exception:
                pass

    async def _broadcast_aggregate_frame(self, frame: AggregateFrame) -> None:
        for transport in self.transports:
            try:
                await transport.send_aggregate(frame)
            except Exception:
                pass

    async def _broadcast_presence_frame(self, frame: PresenceFrame) -> None:
        """Broadcast a presence frame to local viewers and (if configured) upstream.

        PresenceFrame currently doesn't have a dedicated transport method, so
        we pipe it through the WebSocket transport as a session-shaped broadcast
        (viewers in mode=all will receive it) and through any push transport
        using the same send() method.
        """
        for transport in self.transports:
            try:
                # Reuse the per-session send path: subscribers in mode=all or
                # mode=session will get the frame; aggregate-only subscribers
                # will not — which is what we want for presence traffic.
                await transport.send(frame)
            except Exception:
                pass

    async def ingest_relay_frame(self, frame: StateFrame | AggregateFrame | PresenceFrame) -> bool:
        """Dispatch a frame received from /ingest.

        Returns True if the frame was relayed, False if it was dropped
        (either by split-horizon or by message_id dedup).
        """
        # Split horizon: refuse frames that already bear our own host_id in
        # the forwarded_by chain — they would loop back on themselves.
        if self.host_identity.host_id in frame.forwarded_by:
            return False

        # Dedup: reject message_ids we've already relayed recently.
        if not await self._ingest_cache.add(frame.message_id):
            return False

        # Stamp: append ourselves to the forwarded_by chain so downstream
        # hops (and any loop-back) can recognize us.
        frame.forwarded_by.append(self.host_identity.host_id)

        if isinstance(frame, StateFrame):
            await self._broadcast_session_frame(frame)
        elif isinstance(frame, AggregateFrame):
            await self._broadcast_aggregate_frame(frame)
        elif isinstance(frame, PresenceFrame):
            await self._broadcast_presence_frame(frame)
        return True

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(300)
            await self.registry.cleanup_expired()

    def _default_session_id(self) -> str:
        return "default"

    async def ws_connect(self, ws: WebSocket, mode: str, session_filter: str | None) -> None:
        if self._ws_transport:
            await self._ws_transport.connect(ws, mode=mode, session_filter=session_filter)

    async def ws_disconnect(self, ws: WebSocket) -> None:
        if self._ws_transport:
            await self._ws_transport.disconnect(ws)


_app_instance: App | None = None


def get_app_instance(fastapi_app: FastAPI | None = None) -> App:
    """Return the App bound to this FastAPI instance.

    Each FastAPI instance carries its own App in `app.state.recall_app`.
    We fall back to the module-level global for backward compatibility
    with setups that don't go through create_api().
    """
    if fastapi_app is not None:
        recall_app = getattr(fastapi_app.state, "recall_app", None)
        if recall_app is not None:
            return recall_app
    assert _app_instance is not None
    return _app_instance


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    global _app_instance
    config = load_config()
    app_obj = App(config)
    fastapi_app.state.recall_app = app_obj
    _app_instance = app_obj
    await app_obj.start()
    yield
    await app_obj.stop()
    _app_instance = None


def create_api(app_obj: App | None = None) -> FastAPI:
    """Build a FastAPI instance. If `app_obj` is given, bind it directly
    (useful for tests and for running multiple daemons in one process).
    Otherwise, the lifespan hook constructs the App from load_config().
    """
    fastapi_app = FastAPI(title="Claude Recall", lifespan=lifespan)

    @fastapi_app.post("/events")
    async def post_event(payload: EventPayload):
        app = get_app_instance(fastapi_app)
        return await app.handle_event(payload)

    @fastapi_app.get("/state")
    async def get_state():
        app = get_app_instance(fastapi_app)
        agg = await app.registry.get_aggregate()
        return {
            "state": agg.state.name.lower(),
            "active_sessions": agg.active_sessions,
            "breakdown": agg.breakdown,
        }

    @fastapi_app.get("/sessions")
    async def get_sessions():
        app = get_app_instance(fastapi_app)
        sessions = await app.registry.list_sessions()
        return {"sessions": sessions}

    @fastapi_app.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        app = get_app_instance(fastapi_app)
        info = await app.registry.get_session_info(session_id)
        if info is None:
            return {"error": "session not found"}
        return info

    @fastapi_app.websocket("/ws")
    async def websocket_endpoint(
        ws: WebSocket,
        mode: str = Query(default="aggregate"),
        session: str | None = Query(default=None),
    ):
        """
        WebSocket subscription.
        mode=aggregate: only aggregated frames (default, for simple devices)
        mode=all: both per-session and aggregated frames
        mode=session: only frames for a specific session (requires ?session=ID)
        """
        app = get_app_instance(fastapi_app)
        await app.ws_connect(ws, mode=mode, session_filter=session)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await app.ws_disconnect(ws)

    @fastapi_app.websocket("/ingest")
    async def ingest_endpoint(ws: WebSocket):
        await _handle_ingest(fastapi_app, ws)

    if app_obj is not None:
        fastapi_app.state.recall_app = app_obj

    return fastapi_app


# Module-level default `api` instance preserves the existing import path
# (e.g. `uvicorn claude_recall.server:api`).
api = create_api()


# ---- /ingest endpoint (PR 3: cascading daemons) --------------------------


def _authorize_ingest(ws: WebSocket, allowed_tokens: list[str]) -> bool:
    """Check the Authorization header against the configured allowlist.

    Empty allowlist means "any token accepted" — intended for localhost/dev
    deployments where ingest is enabled behind a firewall/tunnel.
    """
    if not allowed_tokens:
        return True
    auth = ws.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[len("Bearer "):].strip()
    return token in allowed_tokens


def _parse_ingest_frame(raw: str) -> StateFrame | AggregateFrame | PresenceFrame | None:
    """Try to parse a JSON payload as one of the known frame types.

    Returns None for unparseable JSON or unknown `type` values.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    ftype = data.get("type")
    try:
        if ftype == "session":
            return StateFrame.model_validate(data)
        if ftype == "aggregate":
            return AggregateFrame.model_validate(data)
        if ftype == "presence":
            return PresenceFrame.model_validate(data)
    except ValidationError:
        return None
    return None


async def _handle_ingest(fastapi_app: FastAPI, ws: WebSocket) -> None:
    """Accept frames from a downstream daemon (cascading topology).

    Protocol:
    1. Upstream verifies `Authorization: Bearer <token>` against the
       configured allowlist.
    2. Client sends a `hello` JSON message announcing its HostIdentity.
       This host is marked online via a PresenceFrame.
    3. Subsequent messages are StateFrame / AggregateFrame / PresenceFrame
       objects. Each is deduped and relayed to local viewers (and any
       upstream this daemon is pushing to).
    4. On disconnect, an offline PresenceFrame is emitted on the client's
       behalf so the chain learns the downstream went away.
    """
    app = get_app_instance(fastapi_app)
    cfg = app.config.ingest

    if not cfg.enabled:
        await ws.close(code=4403)  # policy violation
        return

    if not _authorize_ingest(ws, cfg.allowed_tokens):
        await ws.close(code=4401)  # unauthorized
        return

    await ws.accept()

    peer_host: HostIdentity | None = None
    try:
        hello_raw = await ws.receive_text()
        try:
            hello = json.loads(hello_raw)
        except json.JSONDecodeError:
            await ws.close(code=4400)  # bad request
            return

        if hello.get("type") != "hello" or "host" not in hello:
            await ws.close(code=4400)
            return

        try:
            peer_host = HostIdentity.model_validate(hello["host"])
        except ValidationError:
            await ws.close(code=4400)
            return

        # Announce the peer as online to local viewers + any upstream.
        online_frame = PresenceFrame(
            host=peer_host,
            status="online",
            timestamp=datetime.now(timezone.utc),
        )
        await app.ingest_relay_frame(online_frame)

        # Main loop: receive frames until the peer disconnects.
        while True:
            raw = await ws.receive_text()
            frame = _parse_ingest_frame(raw)
            if frame is None:
                # Unknown or malformed — skip but keep the connection open.
                continue
            await app.ingest_relay_frame(frame)

    except WebSocketDisconnect:
        pass
    finally:
        # Best-effort offline announcement on behalf of the peer.
        if peer_host is not None:
            try:
                offline_frame = PresenceFrame(
                    host=peer_host,
                    status="offline",
                    timestamp=datetime.now(timezone.utc),
                )
                await app.ingest_relay_frame(offline_frame)
            except Exception as e:
                logger.debug("failed to emit offline presence: %s", e)
