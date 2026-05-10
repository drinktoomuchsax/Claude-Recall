# Claude Recall Protocol

## Overview

Claude Recall broadcasts **state frames** to all connected consumers whenever Claude Code's state changes. It supports multiple concurrent sessions and provides both per-session and aggregated state views.

Consumers (lights, apps, widgets) connect via WebSocket or receive frames through push transports (serial, MQTT, HTTP webhook).

## Versioning

Every frame carries a `schema_version` integer. The current version is **1**.

- **Non-breaking additions** (new optional fields) do NOT bump the version.
- **Breaking changes** (renaming, removing, or changing field types) bump `schema_version` by 1.
- Receivers **should** check `schema_version` on first frame and reject or degrade gracefully on unknown versions.

## Frame Types

### Session Frame

Emitted when a single session's state changes:

```json
{
  "schema_version": 1,
  "type": "session",
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
  "schema_version": 1,
  "type": "aggregate",
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
| `state` | integer | Highest-priority state across all sessions |
| `active_sessions` | integer | Number of currently active sessions |
| `breakdown` | object | Count of sessions in each state (state name → count) |
| `timestamp` | ISO 8601 | When the aggregate was computed |

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
{"schema_version":1,"type":"aggregate","state":60,"active_sessions":1,"breakdown":{"awaiting_input":1},"timestamp":"2026-05-08T12:00:00Z"}\n
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
