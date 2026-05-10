"""Configuration loading with sensible defaults."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from claude_recall.auth import RecallToken, TokenDecodeError, decode_token

logger = logging.getLogger(__name__)

TOKEN_FILE_PATH = Path.home() / ".config" / "claude-recall" / "token"


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


class RecallConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    host: HostConfig = HostConfig()
    states: StatesConfig = StatesConfig()
    transports: dict[str, TransportConfig] = {}
    rules: list[RuleConfig] = []


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

    return RecallConfig.model_validate(merged)


def _apply_push_env_overrides(merged: dict[str, Any]) -> None:
    """Allow enabling PushTransport via environment / token file.

    Resolution order (highest wins):

    1. CLAUDE_RECALL_UPSTREAM_URL (+ CLAUDE_RECALL_TOKEN as plain Bearer).
       The "power-user" path: URL and bearer are separate strings.
    2. CLAUDE_RECALL_TOKEN containing an encoded RecallToken bundle.
       The "one-string join" path: URL + secret travel together.
    3. ~/.config/claude-recall/token file (written by `claude-recall join`).
       The "persistent join" path: no env setup required.
    4. Explicit config.yaml push transport.
       The legacy path from PR 2.
    """
    url, auth = _resolve_push_credentials()
    if url is None:
        return

    transports = merged.setdefault("transports", {})
    existing = transports.get("push")
    if existing is None:
        transports["push"] = {
            "type": "push",
            "enabled": True,
            "options": {"upstream_url": url},
        }
        existing = transports["push"]

    options = existing.setdefault("options", {})
    options["upstream_url"] = url
    if auth:
        options["auth_token"] = auth


def _resolve_push_credentials() -> tuple[str | None, str | None]:
    """Return (upstream_url, auth_token) resolved from env / token file.

    Returns (None, None) when nothing is configured, leaving any
    config.yaml push block untouched so the legacy path still works.
    """
    # Level 1: explicit URL env var always wins. Bearer can be either a
    # plain string or an encoded token (we only care that it gets sent).
    explicit_url = os.environ.get("CLAUDE_RECALL_UPSTREAM_URL")
    explicit_token = os.environ.get("CLAUDE_RECALL_TOKEN")
    if explicit_url:
        return explicit_url, explicit_token

    # Level 2: CLAUDE_RECALL_TOKEN alone, treated as an encoded bundle.
    if explicit_token:
        bundle = _try_decode_token(explicit_token)
        if bundle is not None:
            return bundle.upstream_url, bundle.auth_secret
        # Env variable looked like a plain bearer — but we have no URL
        # to pair it with. Fall through; a later layer may supply one.

    # Level 3: ~/.config/claude-recall/token file.
    file_token = _read_token_file()
    if file_token:
        bundle = _try_decode_token(file_token)
        if bundle is not None:
            return bundle.upstream_url, bundle.auth_secret
        logger.warning(
            "Ignoring malformed token at %s; run `claude-recall leave` to remove it.",
            TOKEN_FILE_PATH,
        )

    return None, None


def _try_decode_token(raw: str) -> RecallToken | None:
    try:
        return decode_token(raw)
    except TokenDecodeError:
        return None


def _read_token_file() -> str | None:
    try:
        if TOKEN_FILE_PATH.is_file():
            return TOKEN_FILE_PATH.read_text(encoding="utf-8").strip() or None
    except OSError as e:
        logger.debug("could not read token file %s: %s", TOKEN_FILE_PATH, e)
    return None


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
