# claw-medic

**The emergency CLI for when your OpenClaw agent is down and you don't know why.**

One command. Checks everything that can break the OpenClaw gateway. Reports in plain English. Offers a one-shot fix.

No daemon to install, no config to write, no new scheduled task. You run it when something's wrong, it diagnoses + heals, it gets out of the way.

---

## The incident that inspired this

On April 14, 2026, our daily-driver agent went offline for 45 minutes. The gateway process died. Our custom watchdog didn't revive it. Our watchdog-of-the-watchdog scheduled task ran every 5 minutes and reported `LastResult=0` — "success." Except "success" was just "my PowerShell script exited cleanly." Nobody was actually checking if the gateway was serving requests on port 18789.

Five separate things contributed:

1. OpenClaw v2026.4.9 switched from a scheduled-task launcher to a **Startup-folder .cmd launcher** (`~/.openclaw/gateway.cmd` + a Start-Menu Startup shortcut). Older versions of many watchdog scripts were still watching `schtasks`, which is wrong for v2026.4.9.
2. The `OpenClaw Gateway` Windows scheduled task was **disabled** — a stale legacy object from a previous install. It looked broken, but it was actually just irrelevant to the running version.
3. Multiple scheduled tasks had `LastRun=11/30/1999` (never run) with error code `267011` — the Windows null timestamp for "task has not yet run." Operators can't tell these apart from "ran at midnight 1999" at a glance.
4. `openclaw gateway start` is a **thin wrapper around `schtasks /Run /TN "OpenClaw Gateway"`** — when the task is disabled, the CLI silently fails with a non-zero exit that operators miss in script output. Watchdogs based on the CLI inherit this failure mode.
5. Scheduled-task `LastResult=0` doesn't mean **the thing the task was supposed to do actually worked** — it just means the script exited zero. This is why our watchdog-of-the-watchdog kept reporting green while the actual gateway was dead.

Any one of these is a bug. All five at once is a 45-minute outage. claw-medic checks all five, plus a dozen other things.

---

## Port is auto-detected (v0.2)

Your gateway might not be on 18789. OpenClaw resolves port in this order:

1. `--port` CLI flag
2. `OPENCLAW_GATEWAY_PORT` / `OPENCLAW_PORT` environment variable
3. `~/.openclaw/openclaw.json` → `gateway.port`
4. default `18789`

claw-medic follows the same order. All port-related checks use the resolved port, not a hardcoded default. You can override with `--port 19000`.

## Startup mechanisms are auto-detected (v0.2)

OpenClaw supports multiple ways to auto-start the gateway, and which one is active depends on your OS, your install history, and your permission state:

- **Windows Scheduled Task** (default first choice on Windows)
- **Windows Startup-folder launcher** (fallback when Scheduled Task creation is denied — e.g., no admin)
- **macOS launchd plist** (`~/Library/LaunchAgents/*openclaw*.plist`)
- **Linux systemd user unit** (`~/.config/systemd/user/openclaw-gateway.service` etc.)
- **Launcher script** — `~/.openclaw/gateway.cmd` or `gateway.sh`

claw-medic reports which one it found, flags if multiple are present (you can end up with duplicate gateway instances), and flags if none are present (no auto-start at login).

## Session 1 check is OPT-IN

On Windows, a gateway started via Scheduled Task with "Run whether user is logged on or not" lands in Session 0 — the non-interactive service session. That's fine for most users. But if you rely on the desktop-control skill (screen capture, mouse/keyboard automation of the logged-in user's UI), the gateway MUST be in the user's interactive session.

Pass `--require-session 1` to enforce this check. Default: off.

## What it checks

When you run `claw-medic` (no args), it does this in under 10 seconds:

1. **Gateway process alive?** Any process with `openclaw gateway` in the command line (we don't hardcode `--port` so we catch instances on custom ports).
2. **Configured port bound?** TCP connect to the port resolved from config/env/flag. If your gateway is on 19000, we check 19000.
3. **HTTP 200 from `/healthz`?** Real end-to-end health check against the resolved port.
4. **Startup mechanism?** Reports which of Scheduled Task / Startup-folder / launchd / systemd / launcher script are in use. Flags multiple-mechanism conflicts.
5. **(Opt-in) Session 1 check** — with `--require-session 1`, verifies gateway PID is in an interactive user session.
6. **Watchdog process alive?** Looks for `openclaw-watchdog` in running process cmdlines.
7. **Orphan scheduled tasks?** Flags Windows tasks with `LastRun=1999` or `LastResult=267011` (never-run + error code) that were configured but never triggered.
8. **OpenClaw version?** Reads `openclaw --version`, cross-references the known-bad-version registry (e.g., v2026.4.10 sandbox path change).
9. **Bootstrap budget?** Reads SOUL.md, USER.md, AGENTS.md, MEMORY.md, IDENTITY.md, PROJECT.md char counts. Flags any file over the per-file 20,000 limit (silent truncation). Flags total over 150,000 (over budget).
10. **Recent log errors?** Scans `~/.openclaw/gateway.log` tail for error/failed/rate_limit/truncating/SIGTERM patterns.

Each check prints `OK` (green), `WARN` (yellow), or `FAIL` (red) with a one-line plain-English explanation.

---

## What it fixes

Every `FAIL` comes with a `Suggested fix:` line. If you pass `--fix`, claw-medic runs the fix.

Current fixes (v0.1):
- Gateway dead → run `openclaw gateway install --force` + `Start-Process gateway.cmd`
- Startup launcher missing → run `openclaw gateway install --force`
- Watchdog process dead → start `openclaw-watchdog.ps1` in hidden PowerShell
- Watchdog-checker task disabled → re-enable (if task ownership allows, otherwise prints elevated-shell command)
- Orphan scheduled tasks → optionally unregister (opt-in with `--cleanup-orphans`)
- Gateway version has known critical bug → flag + suggest pin command

`--fix` will NEVER:
- Modify running production state without asking
- Delete tasks you didn't explicitly flag for removal
- Change gateway config without a backup first

---

## Install

One file, one dependency. Works on Windows, macOS, Linux.

```bash
# from the kit root, or wherever you cloned it
cd openclaw-survival-kit/claw-medic
python3 -m pip install --user psutil
python3 claw_medic.py
```

Or just download `claw_medic.py` as a standalone script and run it. It has zero dependencies besides `psutil` (for cross-platform process listing).

No daemon. No scheduled task. No config file. Run it when something feels wrong.

---

## Usage

```bash
# Full diagnostic + report (no changes made)
python3 claw_medic.py

# Apply suggested fixes automatically (with prompts for anything destructive)
python3 claw_medic.py --fix

# Also clean up orphan scheduled tasks
python3 claw_medic.py --fix --cleanup-orphans

# Specific check categories
python3 claw_medic.py --checks gateway,watchdog,bootstrap

# Override the gateway port (otherwise read from ~/.openclaw/openclaw.json)
python3 claw_medic.py --port 19000

# Windows + desktop-control skill users: require gateway in Session 1
python3 claw_medic.py --require-session 1

# JSON output (for piping into monitoring)
python3 claw_medic.py --json

# Quiet mode (only FAILs printed)
python3 claw_medic.py --quiet
```

Exit codes:
- `0` — all checks OK
- `1` — one or more WARN
- `2` — one or more FAIL

This makes it easy to wire into CI, cron, or another watchdog layer: `python3 claw_medic.py --quiet || alert`.

---

## Why not just use `openclaw doctor`?

`openclaw doctor` is a great upstream tool for checking OpenClaw's view of its own state. claw-medic checks things OpenClaw itself can't see:

- The Windows/launchd/systemd startup mechanism
- Whether your supervisor scripts are actually running (vs just scheduled)
- Whether scheduled tasks that reported success actually accomplished their goal
- Bootstrap file truncation that happens silently in-memory
- Version-specific launch-mechanism mismatches

They're complementary, not competing.

---

## Upstream bugs this tool works around

| Upstream issue | Workaround claw-medic applies |
|---|---|
| `openclaw gateway start` returns 0 even when schtasks fails | Verifies actual port binding, not CLI exit code |
| v2026.4.10 sandbox path check breaks bundled skills (#64985) | Flags when you're on the buggy version |
| Heartbeat interval collapse (#27807) | Reads heartbeat log, flags when interval drifts below configured `every` |
| 3s handshake timeout (#47931) | Reads current `DEFAULT_HANDSHAKE_TIMEOUT_MS`, flags if unset/default |
| Orphaned tool_result corrupting sessions (#3409) | Scans session JSONL for orphaned tool_use entries, reports count |

---

## Roadmap

- [x] **v0.1** — 10 core checks, `--fix` mode, JSON output
- [x] **v0.2** — Auto-detect port from `openclaw.json` / env / flag; auto-detect startup mechanism (Scheduled Task / Startup-folder / launchd / systemd / launcher script); Session-1 check made opt-in via `--require-session 1`; broader process matching (no hardcoded `--port 18789` assumption); filters gateway.log tail to last 24h
- [ ] **v0.3** — Slack / Discord webhook alert on FAIL
- [ ] **v0.4** — `--watch` mode: keep running, re-check every N seconds, alert on state change
- [ ] **v0.5** — Automatic log collection → creates a single diagnostic zip for forum posts
- [ ] **v0.6** — Backport checks from `openclaw doctor` so it's a drop-in superset

---

## License

MIT.

**Reported a new failure mode?** Open an issue tagged `claw-medic`. Include your claw-medic output (paste the JSON with `--json`). The tool gets smarter every time someone hits a new bug.
