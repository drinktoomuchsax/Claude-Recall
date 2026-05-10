"""Configuration loading with sensible defaults."""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class HostConfig(BaseModel):
    """Identifies this daemon among peers.

    `id` defaults to socket.gethostname() when unset. It can also be
    overridden by the CLAUDE_RECALL_HOST_ID environment variable.
    `display_name` is a human-readable label shown on dashboards; it
    has no role in routing or dedup.
    """

    id: str | None = None
    display_name: str | None = None

    def resolve_id(self) -> str:
        env = os.environ.get("CLAUDE_RECALL_HOST_ID")
        if env:
            return env
        if self.id:
            return self.id
        hostname = socket.gethostname()
        if hostname:
            # Cache the resolved hostname on the instance so subsequent
            # callers (presence, relay, etc.) see a stable host_id even
            # if gethostname() ever changes during the process lifetime.
            self.id = hostname
            return hostname
        # No hostname available: generate a unique fallback so two such
        # machines don't silently collide into a single "unknown-host"
        # identity on a shared dashboard. Cache on the instance so the
        # same daemon yields a stable id across multiple resolve calls.
        self.id = f"unknown-host-{uuid.uuid4().hex[:8]}"
        return self.id

    def resolve_display_name(self) -> str | None:
        env = os.environ.get("CLAUDE_RECALL_HOST_DISPLAY_NAME")
        if env:
            return env
        return self.display_name


class StateTTL(BaseModel):
    ttl_sec: float
    degrade_to: str


class StatesConfig(BaseModel):
    error: StateTTL = StateTTL(ttl_sec=30.0, degrade_to="awaiting_input")
    notification: StateTTL = StateTTL(ttl_sec=60.0, degrade_to="awaiting_input")
    awaiting_permission: StateTTL = StateTTL(ttl_sec=600.0, degrade_to="awaiting_input")
    awaiting_input: StateTTL = StateTTL(ttl_sec=1800.0, degrade_to="idle")
    tool_active: StateTTL = StateTTL(ttl_sec=10.0, degrade_to="working")
    working: StateTTL = StateTTL(ttl_sec=60.0, degrade_to="awaiting_input")
    idle: StateTTL = StateTTL(ttl_sec=3600.0, degrade_to="off")


class TransportConfig(BaseModel):
    type: str
    enabled: bool = True
    options: dict[str, Any] = {}


class RuleConfig(BaseModel):
    event: str
    state: str
    debounce_ms: int = 0
    force: bool = False


class IngestConfig(BaseModel):
    """Settings for the /ingest WebSocket endpoint (receives frames from
    downstream daemons for cascading topologies).

    Disabled by default so existing single-daemon deployments are not
    exposed to unauthenticated relays.
    """

    enabled: bool = False
    # Bearer tokens accepted on /ingest. Empty list means "allow any"
    # when enabled — fine for localhost/dev, but production should set
    # explicit tokens.
    allowed_tokens: list[str] = []
    # message_id dedup cache bounds.
    dedup_ttl_sec: float = 600.0
    dedup_max_size: int = 1000


class RecallConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    host: HostConfig = HostConfig()
    states: StatesConfig = StatesConfig()
    transports: dict[str, TransportConfig] = {}
    rules: list[RuleConfig] = []
    ingest: IngestConfig = IngestConfig()


DEFAULT_RULES: list[dict[str, Any]] = [
    {"event": "SessionStart", "state": "idle", "force": True},
    {"event": "UserPromptSubmit", "state": "working", "force": True},
    {"event": "PreToolUse", "state": "tool_active", "debounce_ms": 2000},
    {"event": "PostToolUse", "state": "working", "debounce_ms": 2000},
    {"event": "Stop", "state": "awaiting_input", "force": True},
    {"event": "Notification", "state": "notification"},
    {"event": "PermissionRequest", "state": "awaiting_permission"},
    {"event": "StopFailure", "state": "error"},
    {"event": "SessionEnd", "state": "off", "force": True},
]

DEFAULT_TRANSPORTS: dict[str, dict[str, Any]] = {
    "websocket": {"type": "websocket", "enabled": True},
    "terminal": {"type": "terminal", "enabled": True},
}


def load_config(path: Path | None = None) -> RecallConfig:
    candidates = [path] if path else [
        Path.home() / ".config" / "claude-recall" / "config.yaml",
        Path.cwd() / ".claude-recall.yaml",
    ]

    merged: dict[str, Any] = {}
    for p in candidates:
        if p and p.exists():
            with open(p) as f:
                data = yaml.safe_load(f)
                if data:
                    merged = _deep_merge(merged, data)

    if "rules" not in merged:
        # Copy to avoid mutating the module-level default when env overrides apply.
        merged["rules"] = [dict(r) for r in DEFAULT_RULES]
    if "transports" not in merged:
        merged["transports"] = {
            name: dict(opts) for name, opts in DEFAULT_TRANSPORTS.items()
        }

    _apply_push_env_overrides(merged)
    _apply_ingest_env_overrides(merged)

    return RecallConfig.model_validate(merged)


def _apply_push_env_overrides(merged: dict[str, Any]) -> None:
    """Allow enabling PushTransport purely via environment variables.

    If CLAUDE_RECALL_UPSTREAM_URL is set and no explicit `push` transport
    is configured, inject one. This lets users opt into cascading without
    touching config files.
    """
    upstream = os.environ.get("CLAUDE_RECALL_UPSTREAM_URL")
    if not upstream:
        return

    transports = merged.setdefault("transports", {})
    existing = transports.get("push")
    if existing is None:
        transports["push"] = {
            "type": "push",
            "enabled": True,
            "options": {
                "upstream_url": upstream,
            },
        }
        existing = transports["push"]

    options = existing.setdefault("options", {})
    # Env always takes precedence over config file values.
    options["upstream_url"] = upstream
    token = os.environ.get("CLAUDE_RECALL_TOKEN")
    if token:
        options["auth_token"] = token


def _apply_ingest_env_overrides(merged: dict[str, Any]) -> None:
    """Allow enabling /ingest purely via environment variables.

    - CLAUDE_RECALL_INGEST_ENABLED=1 turns ingest on.
    - CLAUDE_RECALL_INGEST_TOKENS is a comma-separated list of allowed
      Bearer tokens. If unset (and ingest is enabled), any token is
      accepted — only safe for localhost/dev.
    """
    if not os.environ.get("CLAUDE_RECALL_INGEST_ENABLED"):
        return
    ingest = merged.setdefault("ingest", {})
    ingest["enabled"] = True
    tokens_env = os.environ.get("CLAUDE_RECALL_INGEST_TOKENS")
    if tokens_env:
        ingest["allowed_tokens"] = [t.strip() for t in tokens_env.split(",") if t.strip()]


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
