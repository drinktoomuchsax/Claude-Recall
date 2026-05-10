# Multi-Host Deployment Guide

This guide shows how to aggregate Claude Code state from multiple machines — your own laptops, a small team, or a whole company — onto a single dashboard.

If you only use Claude-Recall on one machine, you don't need this guide. The default single-daemon setup is documented in the root `README.md`.

For the wire protocol details (frame formats, state machine, loop prevention), see [`protocol.md`](protocol.md). This document is about **operational deployment**, not protocol internals.

---

## The mental model

```text
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ Alice's Mac    │  │ Bob's Linux    │  │ Carol's Mac    │
│ claude-recall  │  │ claude-recall  │  │ claude-recall  │
└────────┬───────┘  └────────┬───────┘  └────────┬───────┘
         │                   │                   │
         │ wss outbound      │ wss outbound      │ wss outbound
         │ (Bearer token)    │ (Bearer token)    │ (Bearer token)
         └─────────┬─────────┴─────────┬─────────┘
                   │                   │
                   ▼                   │
           ┌───────────────┐           │  (over the public internet
           │  Cloudflare   │◄──────────┘   via a single tunnel)
           │     Tunnel    │
           └───────┬───────┘
                   │
                   ▼
         ┌─────────────────┐          ┌─────────────────┐
         │  Upstream       │─────────▶│  Browser        │
         │  claude-recall  │ /ws      │  dashboard      │
         │  daemon         │          │  (any viewer)   │
         │  (/ingest)      │          └─────────────────┘
         └─────────────────┘
```

Three roles:

| Role | What it does | Who runs it |
|------|--------------|-------------|
| **Downstream daemon** | Forwards local Claude Code state up to an upstream via `PushTransport`. | Every developer, on their own laptop. |
| **Upstream daemon** | Accepts pushes on `/ingest`, aggregates, broadcasts to viewers on `/ws`. | One instance, ideally on a small cloud VM or internal server. |
| **Viewer** | Browser, USB light, Slack bot — any client of `/ws`. Agnostic to whether the daemon is personal or company-wide. | Wherever you want to see the status. |

Downstream daemons initiate outbound connections, so no developer laptop needs a public IP or open port. Only the upstream needs to be reachable.

---

## Deployment recipes by scale

Three patterns that match three scales. You can grow from one to the next without changing the protocol.

### Recipe 1 — Personal (1-3 machines)

Your own machines only: work laptop + home machine + a VPS you already have. You don't need Cloudflare; Tailscale or a direct VPS port is enough.

**Upstream** (the VPS or the machine that stays on):
```bash
uv sync
export CLAUDE_RECALL_INGEST_ENABLED=1
# No allowlist — only accessible over private network.
uv run claude-recall daemon --host 0.0.0.0 --port 8765
```

**Downstream** (each laptop):
```bash
export CLAUDE_RECALL_UPSTREAM_URL=ws://vps.internal:8765/ingest
uv run claude-recall daemon
```

Point your browser viewer at `http://vps.internal:8765/ws?mode=all`.

### Recipe 2 — Small team (5-20 people)

One shared dashboard reachable over the internet. Cloudflare Tunnel gives you TLS + public URL without opening ports on any machine.

**One-time upstream setup** (on the machine that will host the dashboard):

```bash
# 1. Install cloudflared.
brew install cloudflared   # or: apt install cloudflared
cloudflared tunnel login
cloudflared tunnel create recall
# Output: tunnel UUID. Cloudflare writes credentials to ~/.cloudflared/<UUID>.json

# 2. Point a DNS name at the tunnel.
cloudflared tunnel route dns recall recall.yourteam.com

# 3. Create ~/.cloudflared/config.yml:
cat > ~/.cloudflared/config.yml <<'YAML'
tunnel: <UUID>
credentials-file: /home/you/.cloudflared/<UUID>.json
ingress:
  - hostname: recall.yourteam.com
    service: http://localhost:8765
  - service: http_status:404
YAML

# 4. Start the tunnel.
cloudflared tunnel run recall &

# 5. Start the upstream daemon with ingest enabled.
#    Persist the secret to a 0600 file instead of echoing it — the
#    terminal scrollback, tmux buffers, `history`, and any log shipper
#    watching stdout would otherwise capture the bearer token verbatim.
export CLAUDE_RECALL_INGEST_ENABLED=1
export CLAUDE_RECALL_INGEST_TOKENS=$(openssl rand -hex 16)
umask 077
mkdir -p ~/.config/claude-recall
printf '%s\n' "$CLAUDE_RECALL_INGEST_TOKENS" > ~/.config/claude-recall/ingest-secret
echo "Ingest secret saved to ~/.config/claude-recall/ingest-secret (0600)"
uv run claude-recall daemon
```

**Issue a token per teammate** (from the admin machine):
```bash
uv run claude-recall issue \
  --upstream wss://recall.yourteam.com/ingest \
  --secret   "$(< ~/.config/claude-recall/ingest-secret)" \
  --issuer   "Your Team Name"
# Prints an opaque token string — share privately (1Password, DM, etc.).
```

**Each teammate:**
```bash
uv run claude-recall join <token>
# Restart their daemon; it now pushes to recall.yourteam.com.
```

Dashboard URL to share: `https://recall.yourteam.com/ws?mode=all` (or a purpose-built front-end pointed at that).

### Recipe 3 — Company (50+ people, multiple teams)

Same protocol, richer topology. Two realistic approaches:

**3a. Flat**: one dashboard serves the whole company. This works up to ~200 concurrent daemons on modest hardware (single WebSocket per daemon + small memory per session). Monitor connection count and roll to recipe 3b if it starts straining.

**3b. Hierarchical**: one upstream per team/department, all of which push to a company-wide upstream. This is where the "output-feeds-input" protocol pays off:

```text
developer laptops ─► team upstream ─► company upstream ─► CEO dashboard
                       (also has
                        its own /ws
                        for the team
                        lead's view)
```

Each team gets a sub-dashboard and sees their own people in real time; the company-wide view is aggregated from the team tier. You deploy the middle tier exactly like Recipe 2, except it also sets `CLAUDE_RECALL_UPSTREAM_URL` to push to the parent.

**Split-horizon and `message_id` dedup (see [protocol.md](protocol.md#ingest-endpoint-schema-v2-pr-3)) make this safe** — frames never loop back, and duplicates from reconnect storms are discarded.

---

## Securing the dashboard

### Add Cloudflare Access (SSO)

Cloudflare Tunnel plus Cloudflare Access gives you email-domain SSO for free (on the Zero Trust tier):

1. Cloudflare dashboard → **Zero Trust** → **Access** → **Applications** → **Add an application** → **Self-hosted**.
2. Set the application domain to your tunnel hostname (e.g. `recall.yourteam.com`).
3. Add a policy: "Allow if email domain is `yourteam.com`" (or tighter — specific users, groups, IdP rules).
4. Save. Requests to the tunnel now require SSO before they ever reach your daemon.

This is defense in depth. Access authenticates the *human* at the TLS edge; the daemon's Bearer token authenticates each *daemon's push*. Compromising one doesn't compromise the other.

### Rotating tokens

Tokens are not cryptographically signed in the current release. If you suspect a token is compromised, or an employee leaves:

```bash
# Remove the old secret from the upstream's allowlist.
export CLAUDE_RECALL_INGEST_TOKENS="new-secret-1,new-secret-2"   # without the old one
# Restart the upstream. Old tokens now get 401 on /ingest.
```

The leftover downstream daemons will retry indefinitely with the dead token; they're not hazardous (upstream rejects them before accepting any frame).

### What tokens do and don't protect

Tokens protect against **anyone without the token pushing garbage into your dashboard**. They do **not** protect against:

- An employee with a valid token sending fake frames (hypothetical — Claude Code hooks write these frames, not humans).
- Tampering in flight (Cloudflare Tunnel's TLS handles that).
- Dashboard viewers seeing each other's data — the `/ws` endpoint is open to anyone who can connect (but Cloudflare Access blocks unauthorized connections at the TLS edge).

A future release will sign tokens as JWTs for cryptographic provenance; the `decode_token(token, verify_key=...)` API is already plumbed for that.

---

## Configuring downstream daemons

Three ways for a daemon to know where to push. Pick whichever suits your ops style — they compose in [this resolution order](protocol.md#resolution-order).

### Via token (recommended for non-developers)

```bash
uv run claude-recall issue --upstream wss://... --secret ... > team-token.txt
# Distribute team-token.txt (privately).

# Each teammate:
uv run claude-recall join "$(< team-token.txt)"
```

The token file lands at `~/.config/claude-recall/token` with `0600` permissions. Removing is symmetric:

```bash
uv run claude-recall leave
```

### Via environment variables (recommended for CI / scripted setups)

```bash
export CLAUDE_RECALL_UPSTREAM_URL=wss://recall.yourteam.com/ingest
export CLAUDE_RECALL_TOKEN=<the bearer secret>
uv run claude-recall daemon
```

This is the simplest form when you already have a secrets manager injecting env vars.

### Via yaml (recommended for version-controlled dev-machine configs)

```yaml
# ~/.config/claude-recall/config.yaml
transports:
  push:
    type: push
    enabled: true
    options:
      upstream_url: "wss://recall.yourteam.com/ingest"
      auth_token: "the-bearer-secret"
```

Does not work well for distribution (secret ends up in config files); best for personal dev machines.

---

## Operating the upstream

### Process management

Treat the upstream like any other lightweight service. A systemd unit example:

```ini
# /etc/systemd/system/claude-recall.service
[Unit]
Description=Claude-Recall upstream daemon
After=network.target

[Service]
User=recall
WorkingDirectory=/opt/claude-recall
Environment="CLAUDE_RECALL_INGEST_ENABLED=1"
Environment="CLAUDE_RECALL_INGEST_TOKENS=<secret1>,<secret2>"
ExecStart=/opt/claude-recall/.venv/bin/claude-recall daemon --host 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The daemon is stateless across restarts — sessions and presence reconstruct themselves as downstream daemons reconnect (with some delay, bounded by the push reconnect backoff of up to 60s).

### Hardware sizing

Claude-Recall is extremely lightweight; state machines and WebSocket fan-out are the whole workload. Reference numbers from internal benchmarks:

| Scale | RAM (approx) | CPU | Notes |
|-------|--------------|-----|-------|
| 10 daemons | 50 MB | <1% of one core | Raspberry Pi-class is fine. |
| 50 daemons | 100 MB | 1-2% | A 1 vCPU / 1 GB cloud VM. |
| 200 daemons | 300 MB | 5-10% | Any small VPS. |
| 500+ daemons | 1 GB + | 15-25% | Consider Recipe 3b (hierarchical). |

The real bottleneck past a few hundred daemons is **WebSocket fan-out**: every StateFrame is broadcast to every `mode=all` viewer. If you have many simultaneous viewers, memory scales with viewers × frame rate, not with daemons.

### Logs and observability

The daemon logs to stdout/stderr; under systemd that routes to journald. Useful checks:

```bash
# Ingest connection drops, token rejections, loop-prevention hits.
journalctl -u claude-recall -f | grep -i "ingest\|reject\|loop\|presence"

# How many daemons are currently connected:
curl -s http://localhost:8765/state | jq .active_sessions

# Current session list with identity:
curl -s http://localhost:8765/sessions | jq
```

No built-in Prometheus metrics today — you can infer health from `/state` polling, and from HTTP 4xx/5xx on `/ingest` (Cloudflare logs them at the tunnel edge).

---

## Troubleshooting

### "My daemon started but nothing shows up on the dashboard"

Walk down the chain:

1. **Is push enabled?** Check `~/.config/claude-recall/token` exists, or the env vars are set in the daemon's environment (not just your shell — systemd services have their own env).
2. **Is the upstream reachable?** `curl -I https://recall.yourteam.com/` should return 200 (or 403 if Access is enabled; that's fine too).
3. **Is the token valid?** On the upstream, check journal for `401` entries around the time the daemon started.
4. **Is `/ingest` actually enabled?** Upstream returns `403` on `/ingest` if `CLAUDE_RECALL_INGEST_ENABLED` is unset. Curl it with a bogus Bearer to see:
   ```bash
   curl -i -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Authorization: Bearer test" \
     -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
     https://recall.yourteam.com/ingest
   ```

### "I see presence flicker online/offline"

Usually one of:

- **Push reconnect**: daemon lost its WebSocket (network hiccup) and reconnected. You'll see offline → online within ~1-60s. Expected behavior.
- **Duplicate daemon**: two processes on the same host both pushing with the same `host_id`. Pick one and kill the other. `host_id` defaults to `socket.gethostname()`; override with `CLAUDE_RECALL_HOST_ID` if you genuinely need two instances on one machine.

### "A frame I expected never arrived"

Order of suspicion:

1. Split-horizon dropped it because the source daemon's `host_id` already appears in `forwarded_by`. This happens if you accidentally pointed an upstream back at a machine that pushes *to* that upstream, forming a loop. Check the topology.
2. `message_id` dedup treated it as a replay. The dedup cache has a TTL (default 10 minutes, 1000 entries); it should never falsely reject fresh frames with unique UUIDs. If you're seeing this, something's re-emitting the same `message_id` — not supposed to happen.
3. Push transport dropped it during a disconnect (fail-silent by design; see [protocol.md §Push Mode behavioral notes](protocol.md#behavioral-notes)). State reconciles on next event.

### "I joined a token but nothing happens"

You need to restart the daemon after `join` — `join` only writes the file. The daemon reads it at startup.

```bash
# If running as a user process:
pkill -f 'claude-recall daemon' && uv run claude-recall daemon &

# If running under systemd:
sudo systemctl restart claude-recall
```

---

## FAQ

**Q. Is there a hosted/managed upstream?**
No. Claude-Recall is intentionally self-host-only for now.

**Q. Can I use my existing OIDC / SAML IdP?**
For the browser dashboard: yes, via Cloudflare Access (it supports Okta, Azure AD, Google Workspace, etc.). For daemon-to-daemon auth, no — Bearer tokens only, until the signed-JWT PR lands.

**Q. Is there rate limiting?**
Not in the daemon itself. Put Cloudflare or another reverse proxy in front if you're worried about a rogue downstream flooding `/ingest`.

**Q. Can multiple dashboards watch the same upstream?**
Yes — `/ws` is multi-subscriber. Each viewer opens its own WebSocket with its own `mode=` filter; the daemon multiplexes internally.

**Q. Does anything break if the upstream restarts?**
Downstreams will reconnect automatically (exponential backoff, 60s max). Sessions are re-established as events flow. You'll see a brief gap in the dashboard during the restart window.
