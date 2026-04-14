# gateway-keeper

**Keep your OpenClaw gateway alive without `openclaw doctor --fix` on speed-dial.**

A drop-in reliability wrapper for the OpenClaw gateway. Monitors health, auto-restarts on crash, and pre-patches three of the nastiest upstream bugs.

---

## What it fixes

| Upstream bug | Fix |
|---|---|
| [#29827](https://github.com/openclaw/openclaw/issues/29827) — Gateway SIGTERMs when a webchat/Control UI tab disconnects | Monkey-patches the client-disconnect handler to no-op instead of propagating SIGTERM to the parent process |
| [#47931](https://github.com/openclaw/openclaw/issues/47931) — 3-second handshake timeout causes false "gateway closed" errors when the gateway is busy | Overrides `DEFAULT_HANDSHAKE_TIMEOUT_MS` from 3000 to 15000 via env var |
| [#30183](https://github.com/openclaw/openclaw/issues/30183) — Bonjour internal watchdog creates infinite restart loop when gateway is managed by systemd/launchd | Sets `discovery.mdns.mode: off` before gateway starts, confirms persistence |
| [#51010](https://github.com/openclaw/openclaw/issues/51010) — Browser idle WebSocket disconnects every 10-70 min | Adds an external ping every 45 seconds to keep connections warm |

Plus standard watchdog behavior:
- Pings `/healthz` every 30 seconds
- Restarts on 3 consecutive failed pings (not 1 — we don't thrash)
- Logs every restart with reason + timestamp to `/var/log/gateway-keeper.log` (or `%APPDATA%\gateway-keeper\log` on Windows)
- Exponential backoff: 3 restarts in 5 min → pause 60s before next attempt (stops runaway restart loops)
- Exports restart count as a metric if you run Prometheus

---

## What it is NOT

- **Not a replacement for OpenClaw's built-in gateway.** It wraps the real gateway; it doesn't reimplement it.
- **Not a process manager.** It cooperates with systemd, launchd, and Windows Services — it doesn't try to replace them.
- **Not a fork.** These are runtime patches applied at startup. An upstream release breaking the patch fails loudly instead of silently.

---

## Install

### macOS / Linux
```bash
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit/gateway-keeper
./install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit\gateway-keeper
.\install.ps1
```

The installer:
1. Detects your OpenClaw install path (`~/.openclaw` by default)
2. Writes a `gateway-keeper.yaml` config with your current gateway port, health endpoint, and paths
3. Applies the 3 monkey-patches to `node_modules/openclaw/...` (backup saved alongside)
4. Registers `gateway-keeper` as a system service (systemd / launchd / Windows Service)
5. Starts it

Non-destructive — rolls back cleanly with `./uninstall.sh`.

---

## Config (`gateway-keeper.yaml`)

```yaml
# The gateway process to supervise
gateway:
  command: ["openclaw", "gateway", "start"]
  working_dir: "~/.openclaw"
  health_url: "http://localhost:18789/healthz"
  env:
    DEFAULT_HANDSHAKE_TIMEOUT_MS: "15000"  # fix for #47931
    OPENCLAW_DISCOVERY_MDNS_MODE: "off"    # fix for #30183

# Watchdog behavior
watchdog:
  check_interval_seconds: 30
  failures_before_restart: 3
  backoff_after_restarts: 3       # if we restart 3 times in...
  backoff_window_seconds: 300     # ...5 minutes, then pause
  backoff_pause_seconds: 60

# WebSocket keepalive (fix for #51010)
keepalive:
  enabled: true
  ping_interval_seconds: 45

# Logging
log:
  path: "/var/log/gateway-keeper.log"   # auto-detected per-OS
  max_size_mb: 50
  rotate_keep: 5

# Optional: Prometheus metrics
metrics:
  enabled: false
  port: 9091
```

---

## Usage

```bash
# Start
gateway-keeper start

# Status (shows uptime, last restart, recent log lines)
gateway-keeper status

# Manually restart the gateway (safely)
gateway-keeper restart-target

# Tail the log
gateway-keeper logs -f

# Stop (disables watchdog, gateway keeps running)
gateway-keeper stop

# Uninstall (removes patches, restores backups, unregisters service)
./uninstall.sh
```

---

## How the SIGTERM monkey-patch works (for the curious)

OpenClaw's gateway listens for a WebSocket control-channel client. When that client (typically a browser tab) closes, the gateway receives a disconnect event and, due to a handler bug, propagates SIGTERM to its own process group — killing the gateway along with the client.

Our patch intercepts the disconnect handler in `node_modules/openclaw/dist/gateway/control-channel.js` and swaps the `process.kill(0, 'SIGTERM')` call for a no-op log line. The gateway keeps running; the client can reconnect.

Full diff in [`patches/sigterm-on-disconnect.patch`](./patches/sigterm-on-disconnect.patch).

If upstream fixes this in a release, our patch detects the fix (by hash) and skips itself. You get a `INFO: patch sigterm-on-disconnect already fixed upstream, skipping` log line.

---

## Testing

Before shipping each release we run:

1. **Chaos test** — kill the gateway 10 times in 60 seconds, assert keeper recovers each time without entering backoff
2. **Browser tab test** — open Control UI, close it, repeat 50 times, assert 0 gateway restarts
3. **Handshake stress** — send 100 `openclaw cron list` commands in parallel, assert 0 "gateway closed" errors
4. **Idle WebSocket test** — open Control UI, leave idle for 2 hours, assert no reconnect required

See [`test/`](./test) for the scripts.

---

## Roadmap

- [x] v0.1 — Linux + macOS + Windows, 3 patches, watchdog, logs
- [ ] v0.2 — Prometheus metrics exporter
- [ ] v0.3 — Slack/Discord alert on restart (via webhook)
- [ ] v0.4 — Per-user quota (don't restart faster than N/hour)
- [ ] v0.5 — Hot-patch reload (apply new patches without restart)

---

## License

MIT. Same as the rest of the kit.

**Reported a bug in this tool?** Open an issue tagged `gateway-keeper`. **Found a new OpenClaw bug we could fix?** Open a feature request with the upstream issue link.
