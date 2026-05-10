# Claude Recall Protocol

## Overview

Claude Recall broadcasts **state frames** to all connected consumers whenever Claude Code's state changes. It supports multiple concurrent sessions and provides both per-session and aggregated state views.

Consumers (lights, apps, widgets) connect via WebSocket or receive frames through push transports (serial, MQTT, HTTP webhook).

## Versioning

Every frame carries a `schema_version` integer. The current version is **2**.

- **Non-breaking additions** (new optional fields) do NOT bump the version.
- **Breaking changes** (renaming, removing, or changing field types) bump `schema_version` by 1.
- Receivers **should** check `schema_version` on first frame and reject or degrade gracefully on unknown versions.

### What's new in v2

- Every frame now carries a `host` identity, `message_id`, and `forwarded_by` list — enabling multi-daemon cascading topologies (see [Multi-Host Protocol](#multi-host-protocol) below).
- New `PresenceFrame` announces host online/offline transitions.
- v1 receivers are forward-compatible: unknown fields are silently ignored.

## Frame Types

### Session Frame

Emitted when a single session's state changes:

```json
{
  "schema_version": 2,
  "type": "session",
  "message_id": "3f8c2e11-4b5a-4c9d-8e7f-123456789abc",
  "host": {
    "host_id": "zhang-mbp",
    "display_name": "Zhang's Macbook"
  },
  "forwarded_by": [],
  "session_id": "abc123",
  "state": 60,
  "previous": 30,
  "duration": 12.5,
  "triggered_by": "Stop",
  "metadata": {
    "cwd": "/home/user/my-project",
    "project": "my-project",
    "model": "claude-sonnet-4-20250514",
    "prompt": "Fix the login bug in auth.py",
    "tool_name": null,
    "tool_context": null,
    "effort_level": "high",
    "agent_id": null,
    "agent_type": null,
    "error_type": null
  },
  "durations": {
    "off": 0.0,
    "idle": 2.1,
    "working": 45.3,
    "tool_active": 18.7,
    "awaiting_input": 120.0,
    "awaiting_permission": 0.0,
    "notification": 0.0,
    "error": 0.0
  },
  "timestamp": "2026-05-08T12:00:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | Frame schema version. Bumped on breaking changes. Receivers should reject unknown majors. |
| `type` | string | Always `"session"` |
| `message_id` | string | UUID, unique per frame. Used for loop-prevention dedup in cascaded topologies. |
| `host` | object \| null | Origin host identity (see [Host Identity](#host-identity)). `null` only for v1 frames. |
| `forwarded_by` | array of string | host_ids of daemons that have relayed this frame. Used for split-horizon loop prevention. |
| `session_id` | string | Unique identifier for the Claude Code session |
| `state` | integer | New state value (see States below) |
| `previous` | integer | State before this transition |
| `duration` | float \| null | How long the **previous** state lasted (seconds) |
| `triggered_by` | string \| null | The Claude Code hook event that caused the transition |
| `metadata` | object \| null | Cumulative session metadata (see below) |
| `durations` | object \| null | Cumulative time spent in each state since session start (seconds) |
| `timestamp` | ISO 8601 | When the transition occurred |

### State Durations

The `durations` object tracks total time spent in each state for the session's lifetime. Updated on every state transition.

| Field | Type | Description |
|-------|------|-------------|
| `off` | float | Seconds in OFF state |
| `idle` | float | Seconds in IDLE state |
| `working` | float | Seconds in WORKING state |
| `tool_active` | float | Seconds in TOOL_ACTIVE state |
| `awaiting_input` | float | Seconds in AWAITING_INPUT state |
| `awaiting_permission` | float | Seconds in AWAITING_PERMISSION state |
| `notification` | float | Seconds in NOTIFICATION state |
| `error` | float | Seconds in ERROR state |

### Session Metadata

Metadata is accumulated per session. Fields are set when first seen and updated when new non-null values arrive. Receivers always get the full snapshot — no need to maintain local state.

| Field | Type | Updated on | Description |
|-------|------|------------|-------------|
| `cwd` | string\|null | Every event | Working directory |
| `project` | string\|null | Every event | Last path segment of cwd (project name) |
| `model` | string\|null | SessionStart | Model identifier (e.g. `claude-sonnet-4-20250514`) |
| `prompt` | string\|null | UserPromptSubmit | Last user message (max 100 chars) |
| `tool_name` | string\|null | PreToolUse/PostToolUse | Current/last tool name (Bash, Edit, Read, etc.) |
| `tool_context` | string\|null | PreToolUse/PostToolUse | Key tool_input value — file path, command, or URL (max 200 chars) |
| `effort_level` | string\|null | Events with effort | Thinking effort (low/medium/high/xhigh/max) |
| `agent_id` | string\|null | Subagent events | Subagent identifier |
| `agent_type` | string\|null | Subagent events | Subagent type name |
| `error_type` | string\|null | StopFailure | Error classification (rate_limit, server_error, etc.) |

### Aggregate Frame

Emitted when the overall aggregated state changes (computed as the max priority across all active sessions):

```json
{
  "schema_version": 2,
  "type": "aggregate",
  "message_id": "a1b2c3d4-...",
  "host": {
    "host_id": "zhang-mbp",
    "display_name": "Zhang's Macbook"
  },
  "forwarded_by": [],
  "state": 80,
  "active_sessions": 3,
  "breakdown": {
    "working": 1,
    "awaiting_permission": 1,
    "idle": 1
  },
  "timestamp": "2026-05-08T12:00:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | Frame schema version. Bumped on breaking changes. Receivers should reject unknown majors. |
| `type` | string | Always `"aggregate"` |
| `message_id` | string | UUID, unique per frame. Used for dedup in cascaded topologies. |
| `host` | object \| null | Origin host identity. `null` only for v1 frames. |
| `forwarded_by` | array of string | host_ids of daemons that have relayed this frame. |
| `state` | integer | Highest-priority state across all sessions |
| `active_sessions` | integer | Number of currently active sessions |
| `breakdown` | object | Count of sessions in each state (state name → count) |
| `timestamp` | ISO 8601 | When the aggregate was computed |

### Presence Frame

Emitted when a daemon comes online or goes offline. In a single-daemon deployment, the daemon emits `online` at startup (via `App.start()`) and best-effort `offline` during `App.stop()`. In cascaded topologies, the upstream daemon emits `online` on behalf of a downstream when its `/ingest` connection is established, and `offline` when the connection drops.

Presence frames are delivered to `/ws` subscribers in `mode=all` and are propagated upstream through `PushTransport` like any other frame.

```json
{
  "schema_version": 2,
  "type": "presence",
  "message_id": "b7f8a2c5-...",
  "host": {
    "host_id": "zhang-mbp",
    "display_name": "Zhang's Macbook"
  },
  "status": "online",
  "last_active_ago_ms": null,
  "forwarded_by": [],
  "timestamp": "2026-05-08T12:00:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | Frame schema version. |
| `type` | string | Always `"presence"` |
| `message_id` | string | UUID, unique per frame. |
| `host` | object | Which host this presence event is about. Required. |
| `status` | string | `"online"` or `"offline"` |
| `last_active_ago_ms` | integer \| null | For offline frames, ms since last activity. `null` for online frames. |
| `forwarded_by` | array of string | Relay chain. |
| `timestamp` | ISO 8601 | When the event occurred. |

## Host Identity

Every frame produced by a v2 daemon carries a `host` object identifying its origin:

```json
{
  "host_id": "zhang-mbp",
  "display_name": "Zhang's Macbook"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `host_id` | string | Stable, unique identifier. Defaults to `socket.gethostname()`. Used for routing, dedup, and loop prevention. |
| `display_name` | string \| null | Human-readable label. Purely cosmetic — never used for routing. |

**Resolution order for `host_id`:**
1. `CLAUDE_RECALL_HOST_ID` environment variable
2. `host.id` in `config.yaml`
3. `socket.gethostname()` (cached on first resolution)
4. `unknown-host-<8-hex-suffix>` fallback — a per-instance random suffix
   prevents two hostname-less daemons from colliding into a single
   identity. The generated value is cached on the `HostConfig` instance,
   so `host_id` remains stable for the lifetime of the daemon.

**Resolution order for `display_name`:**
1. `CLAUDE_RECALL_HOST_DISPLAY_NAME` environment variable
2. `host.display_name` in `config.yaml`
3. `null`

## Multi-Host Protocol

Starting in schema v2, the protocol supports cascading multiple daemons into a single topology. This enables use cases like "company-wide dashboard aggregating N engineers' Claude Code status" without introducing a separate aggregator component — each daemon can simultaneously act as server, client, and relay.

**See [Multi-Host Roadmap](multi-host-roadmap.md) for the full deployment guide and design rationale.**

### Protocol additions

**On every frame:**
- `host`: who produced this frame (the origin, not the relay).
- `message_id`: UUID for dedup across cascade paths.
- `forwarded_by`: list of host_ids that have relayed this frame. Used for split-horizon loop prevention.

**New frame type:**
- `PresenceFrame`: announces online/offline transitions.

### Loop prevention (coming in PR 3)

Cascaded topologies use two mechanisms together:
1. **Split horizon via `forwarded_by`**: a daemon appends its own host_id before relaying. Any frame where `self.host_id ∈ forwarded_by` is dropped.
2. **Message ID dedup**: a short-TTL LRU cache of seen `message_id`s rejects repeats.

### Current status

What works now:
- ✓ Frames carry host identity and cascading metadata (PR 1).
- ✓ `PresenceFrame` is defined and emitted on daemon start/stop and on `/ingest` connect/disconnect (PR 1 schema, PR 3 behavior).
- ✓ v1 receivers stay forward-compatible (PR 1).
- ✓ `PushTransport` — daemon can act as a WebSocket client and relay frames to an upstream (PR 2).
- ✓ `/ingest` endpoint — daemon accepts frames pushed from downstream peers, with split-horizon + `message_id` dedup for loop prevention (PR 3).

What's coming:
- PR 4: Token-based configuration + deployment docs.

## Push Mode (Schema v2, PR 2)

A daemon can be configured to forward its emitted frames to an upstream daemon's `/ingest` endpoint. This enables reverse-direction communication: the downstream daemon initiates an outbound WebSocket connection, eliminating the need for public inbound reachability (NAT-friendly).

### How it connects

```text
downstream daemon ──wss──▶ upstream daemon (/ingest, PR 3)
                  │
                  └─ outbound connection
                     Authorization: Bearer <token>
```

On startup, `PushTransport`:
1. Opens a WebSocket connection to the configured `upstream_url`.
2. Sends a `hello` message announcing this daemon's `HostIdentity`:
   ```json
   {"type": "hello", "host": {"host_id": "zhang-mbp", "display_name": "Zhang's Mac"}}
   ```
3. For every frame emitted locally (state, aggregate), forwards it as-is.
4. On disconnect, reconnects with exponential backoff (1s → 60s cap).

### Enabling push mode

**Environment variables (simplest):**
```bash
export CLAUDE_RECALL_UPSTREAM_URL=wss://recall.company.com/ingest
export CLAUDE_RECALL_TOKEN=xxx   # optional, sent as Bearer token
# Restart daemon.
```

**YAML config:**
```yaml
transports:
  push:
    type: push
    enabled: true
    options:
      upstream_url: "wss://recall.company.com/ingest"
      auth_token: "xxx"
```

Environment variables take precedence over YAML values.

### Behavioral notes

- **Inert when unconfigured**: no `upstream_url` → transport does nothing, no errors, no resource use.
- **Fail-silent on send**: if the connection is temporarily down, `send()` drops the frame rather than raising. The reconnect loop recovers on its own.

## /ingest Endpoint (Schema v2, PR 3)

The inbound counterpart to Push Mode. An upstream daemon exposes `/ingest` to accept frames from downstream daemons, enabling cascading topologies (company-wide dashboards, departmental aggregation, etc.).

A daemon can simultaneously:
- serve local viewers on `/ws`
- accept upstream pushes on `/ingest`
- push to its own upstream via `PushTransport`

That's the "infinite cascade" primitive — the same protocol is symmetric between clients and servers.

### Handshake

```
downstream ──Authorization: Bearer <token>──▶ upstream GET /ingest
              ↓ (upgrade)
downstream ──{"type":"hello","host":{"host_id":"zhang-mbp",...}}──▶ upstream
              ↓
upstream broadcasts PresenceFrame { status: "online", host: downstream }
              ↓
downstream ──StateFrame / AggregateFrame / PresenceFrame──▶ upstream (repeatedly)
              ↓
(on downstream disconnect)
upstream broadcasts PresenceFrame { status: "offline", host: downstream }
```

### Authorization

`Authorization: Bearer <token>` is checked against `ingest.allowed_tokens` in config. An empty allowlist accepts any connection (only safe on localhost / behind a tunnel with its own auth layer).

### Loop prevention

Each received frame goes through two checks before being broadcast locally:

1. **Split horizon**: if `self.host_id ∈ frame.forwarded_by`, the frame is dropped — it would loop back on itself.
2. **Dedup**: if `frame.message_id` was seen recently (LRU cache bounded by `ingest.dedup_max_size` with TTL `ingest.dedup_ttl_sec`), the frame is dropped.

Frames that pass both checks are stamped: the receiving daemon appends its own `host_id` to `forwarded_by` before broadcasting to local transports (which may include further push relays upstream — extending the cascade).

### Enabling /ingest

Disabled by default so single-daemon deployments are not exposed to unauthenticated relays.

**Environment variables (simplest):**
```bash
export CLAUDE_RECALL_INGEST_ENABLED=1
export CLAUDE_RECALL_INGEST_TOKENS=token-a,token-b   # optional allowlist
```

**YAML config:**
```yaml
ingest:
  enabled: true
  allowed_tokens:
    - "child-token-a"
    - "child-token-b"
  dedup_ttl_sec: 600        # optional, default 600
  dedup_max_size: 1000      # optional, default 1000
```

### Behavioral notes

- **Malformed frames are skipped, not fatal**: bad JSON or unknown `type` values drop the single message but keep the connection open.
- **A malformed `hello` closes the connection**: presence state must be established cleanly, so we fail fast on the handshake.
- **Presence frames are delivered to `/ws` subscribers in `mode=all`**: `mode=aggregate` viewers don't receive them (aggregate data is per-local-daemon, while presence is per-host).

## States

| Value | Name | Meaning |
|-------|------|---------|
| 0 | OFF | No active session |
| 10 | IDLE | Session exists, nothing happening |
| 30 | WORKING | Claude is thinking / generating |
| 40 | TOOL_ACTIVE | Claude is executing a tool |
| 60 | AWAITING_INPUT | Claude finished, waiting for user's next instruction |
| 80 | AWAITING_PERMISSION | Claude blocked on a permission request |
| 85 | NOTIFICATION | Claude sent a notification |
| 100 | ERROR | Something went wrong |

### Priority & TTL

States have priority ordering (higher value = higher priority). A session can only transition upward immediately; downward transitions require the current state's TTL to expire.

| State | TTL (default) | Degrades to |
|-------|---------------|-------------|
| ERROR | 30s | AWAITING_INPUT |
| NOTIFICATION | 60s | AWAITING_INPUT |
| AWAITING_PERMISSION | 600s (10min) | AWAITING_INPUT |
| AWAITING_INPUT | 1800s (30min) | IDLE |
| TOOL_ACTIVE | 10s | WORKING |
| WORKING | 60s | AWAITING_INPUT |
| IDLE | 3600s (1h) | OFF |

## WebSocket API

### Subscription Modes

```
ws://127.0.0.1:8765/ws?mode=aggregate
ws://127.0.0.1:8765/ws?mode=all
ws://127.0.0.1:8765/ws?mode=session&session=<session_id>
```

| Mode | Receives | Best for |
|------|----------|----------|
| `aggregate` (default) | Aggregate frames only | Simple devices (single light, bell) |
| `all` | Both session frames and aggregate frames | Apps that show per-session detail |
| `session` | Frames for one specific session only | Multi-light setups (one light per session) |

After connecting, the server pushes JSON text messages on each state change. No authentication required (localhost only).

## HTTP API

### POST /events

Submit a Claude Code hook event.

```
POST http://127.0.0.1:8765/events
Content-Type: application/json

{
  "event": "Stop",
  "session_id": "abc123",
  "metadata": {
    "cwd": "/home/user/my-project",
    "project": "my-project",
    "effort_level": "high"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event` | string | yes | Claude Code hook event name |
| `session_id` | string | no | Session identifier (defaults to `"default"`) |
| `metadata` | object | no | Session metadata fields (see Session Metadata) |
| `raw` | object | no | Raw hook payload for debugging |

Response:

```json
{"status": "ok", "state": "awaiting_input", "session_id": "abc123"}
```

Possible `status` values: `"ok"`, `"no_change"`, `"debounced"`, `"unknown_event"`

### GET /state

Get aggregated state across all sessions.

```
GET http://127.0.0.1:8765/state
```

Response:

```json
{
  "state": "awaiting_permission",
  "active_sessions": 2,
  "breakdown": {"working": 1, "awaiting_permission": 1}
}
```

### GET /sessions

List all active sessions with metadata.

```
GET http://127.0.0.1:8765/sessions
```

Response:

```json
{
  "sessions": {
    "abc123": {
      "state": "working",
      "metadata": {
        "project": "my-app",
        "model": "claude-sonnet-4-20250514",
        "prompt": "Fix the login bug"
      }
    },
    "def456": {
      "state": "awaiting_permission",
      "metadata": {
        "project": "api-server",
        "tool_name": "Bash",
        "tool_context": "rm -rf node_modules"
      }
    }
  }
}
```

### GET /sessions/{session_id}

Get a specific session's state and metadata.

```
GET http://127.0.0.1:8765/sessions/abc123
```

Response:

```json
{
  "session_id": "abc123",
  "state": "working",
  "metadata": {
    "cwd": "/home/user/my-app",
    "project": "my-app",
    "model": "claude-sonnet-4-20250514",
    "prompt": "Fix the login bug"
  },
  "durations": {
    "off": 0.0,
    "idle": 3.2,
    "working": 28.5,
    "tool_active": 12.0,
    "awaiting_input": 0.0,
    "awaiting_permission": 0.0,
    "notification": 0.0,
    "error": 0.0
  }
}
```

## Push Transport Frame Format

For push-type transports (serial, MQTT), frames are sent as compact JSON lines terminated by `\n`:

```
{"schema_version":2,"type":"aggregate","message_id":"abc-123","host":{"host_id":"zhang-mbp"},"forwarded_by":[],"state":60,"active_sessions":1,"breakdown":{"awaiting_input":1},"timestamp":"2026-05-08T12:00:00Z"}\n
```

Serial transports use 115200 baud, 8N1 by default.

## Building a Receiver

A receiver is any program that consumes state frames and produces a physical or digital output.

### Minimal Example (Python)

```python
import asyncio
import json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws?mode=aggregate") as ws:
        async for message in ws:
            frame = json.loads(message)
            print(f"State: {frame['state']} ({frame['active_sessions']} sessions)")

asyncio.run(main())
```

### Design Principles

1. **The receiver decides presentation.** The core never specifies colors, sounds, or effects.
2. **Use `mode=aggregate`** for simple single-output devices (one light, one buzzer).
3. **Use `mode=all`** if you need per-session detail (multi-light, app with session list).
4. **Handle reconnection.** If the daemon restarts, reconnect and call `GET /state` to sync.
5. **Be graceful on disconnect.** The daemon may not always be running.

## Hook Events Reference

| Event | Trigger | Default Target State |
|-------|---------|---------------------|
| SessionStart | Claude Code session begins | IDLE |
| SessionEnd | Session closed | OFF |
| UserPromptSubmit | User sends a message | WORKING |
| Stop | Claude finishes generating | AWAITING_INPUT |
| StopFailure | Generation failed | ERROR |
| PreToolUse | About to execute a tool | TOOL_ACTIVE |
| PostToolUse | Tool execution completed | WORKING |
| Notification | Claude sends a notification | NOTIFICATION |
| PermissionRequest | Tool needs user approval | AWAITING_PERMISSION |
