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

## What it checks

When you run `claw-medic` (no args), it does this in under 10 seconds:

1. **Gateway process alive?** Looks for a `node.exe` (or `node`) running `openclaw/dist/index.js gateway --port 18789`.
2. **Port actually bound?** Opens a TCP connection to `localhost:18789` to confirm the port is accepting connections.
3. **HTTP 200 from `/healthz`?** Real end-to-end health check. Follows redirects.
4. **Startup launcher present?** Confirms `~/.openclaw/gateway.cmd` exists AND the Startup-folder shortcut exists at `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\OpenClaw Gateway.cmd`.
5. **Watchdog process alive?** Looks for a `powershell.exe` running `openclaw-watchdog.ps1` (or similarly named watchdog script).
6. **Watchdog-checker scheduled task enabled + recently fired?** Also flags if the task reports `LastResult=0` but the watchdog it's supposed to supervise isn't running (false-green state).
7. **Any orphan scheduled tasks?** Flags tasks with `LastRun=11/30/1999` or `LastResult=267011` (never-run + error code) that were configured but never triggered.
8. **OpenClaw version?** Reads `openclaw --version` and cross-references known breaking releases (e.g., v2026.4.10 sandbox path change).
9. **SOUL.md / AGENTS.md bootstrap budget?** Reports char count per file, flags any file over `bootstrapMaxChars` (default 20,000) that will get silently truncated.
10. **Recent log errors?** Tails `~/.openclaw/gateway.log` for the last 50 lines, highlights any lines containing `error`, `failed`, `rate_limit`, `truncating`, `SIGTERM`.

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

- [x] **v0.1** — 10 core checks, Windows + macOS + Linux, `--fix` mode, JSON output
- [ ] **v0.2** — Slack / Discord webhook alert on FAIL
- [ ] **v0.3** — `--watch` mode: keep running, re-check every N seconds, alert on state change
- [ ] **v0.4** — Automatic log collection → creates a single diagnostic zip for forum posts
- [ ] **v0.5** — Backport checks from `openclaw doctor` so it's a drop-in superset

---

## License

MIT.

**Reported a new failure mode?** Open an issue tagged `claw-medic`. Include your claw-medic output (paste the JSON with `--json`). The tool gets smarter every time someone hits a new bug.
