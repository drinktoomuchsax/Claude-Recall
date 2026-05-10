"""Push transport: this daemon acts as a WebSocket client and forwards
frames to an upstream daemon's /ingest endpoint.

This is the producer half of the cascade protocol (PR 2). The consumer
half — the /ingest endpoint that accepts these frames — lands in PR 3.

Contract:
- On start, spawn a background task that connects to upstream_url.
- On connect, send a `hello` message announcing this daemon's host identity.
- For every frame emitted locally, relay it upstream.
- If the connection drops, reconnect with exponential backoff.
- If upstream_url is unset, the transport is inert (no connection, no errors).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.protocol import State

from claude_recall.models import AggregateFrame, HostIdentity, PresenceFrame, StateFrame
from claude_recall.transports import register_transport
from claude_recall.transports.base import BaseTransport

logger = logging.getLogger(__name__)

_MIN_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 60.0


@register_transport("push")
class PushTransport(BaseTransport):
    """Relays local frames to an upstream daemon via WebSocket.

    Options:
      - upstream_url (str): wss:// or ws:// URL of upstream daemon's /ingest
      - auth_token (str, optional): sent as `Authorization: Bearer <token>`
      - host_identity (HostIdentity, required): this daemon's identity,
        announced in the hello handshake so upstream knows who we are.
    """

    def __init__(self, name: str, options: dict[str, Any]):
        super().__init__(name, options)
        self._upstream_url: str | None = options.get("upstream_url")
        self._auth_token: str | None = options.get("auth_token")

        host = options.get("host_identity")
        if host is None:
            # Allow instantiation without host — transport stays inert.
            # In production, server.py always passes the daemon's identity.
            self._host_identity: HostIdentity | None = None
        elif isinstance(host, HostIdentity):
            self._host_identity = host
        else:
            self._host_identity = HostIdentity(**host)

        self._ws = None
        self._connect_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        """For tests and observability."""
        ws = self._ws
        return ws is not None and ws.state == State.OPEN

    async def start(self) -> None:
        if not self._upstream_url:
            # No upstream configured; transport is inert. Don't log noisily.
            return
        if self._host_identity is None:
            logger.warning(
                "PushTransport started without host_identity; disabling"
            )
            return
        self._connect_task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._connect_task:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send(self, frame: StateFrame | PresenceFrame) -> None:
        """Accepts both StateFrame and PresenceFrame — both are relayed via
        the same channel and deserialized by the upstream /ingest endpoint
        based on the `type` field in the JSON."""
        await self._send_payload(frame.model_dump_json())

    async def send_aggregate(self, frame: AggregateFrame) -> None:
        await self._send_payload(frame.model_dump_json())

    async def _send_payload(self, payload: str) -> None:
        ws = self._ws
        if ws is None or ws.state != State.OPEN:
            return  # Drop silently when disconnected; reconnect loop will recover.
        try:
            await ws.send(payload)
        except Exception as e:
            logger.debug("PushTransport send failed, will reconnect: %s", e)

    async def _connect_loop(self) -> None:
        backoff = _MIN_BACKOFF_SEC
        while not self._stopped.is_set():
            try:
                async with self._open_connection() as ws:
                    self._ws = ws
                    backoff = _MIN_BACKOFF_SEC  # reset on successful connect
                    await self._send_hello(ws)
                    # Keep connection open until the server closes it or
                    # we get cancelled. We don't consume incoming frames
                    # yet (PR 3 will wire bidirectional forwarding).
                    await ws.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "PushTransport connect failed (url=%s): %s; retrying in %.1fs",
                    self._upstream_url,
                    e,
                    backoff,
                )
            finally:
                self._ws = None

            if self._stopped.is_set():
                return
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                return  # stopped during backoff
            except asyncio.TimeoutError:
                pass  # backoff elapsed, retry
            backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    def _open_connection(self):
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return websockets.connect(
            self._upstream_url,
            additional_headers=headers if headers else None,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
        )

    async def _send_hello(self, ws) -> None:
        assert self._host_identity is not None
        hello = {
            "type": "hello",
            "host": self._host_identity.model_dump(),
        }
        await ws.send(json.dumps(hello))
