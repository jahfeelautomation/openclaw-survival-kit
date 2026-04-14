# claw-medic

**The emergency CLI for when your OpenClaw agent is down and you don't know why.**

One command. Checks everything that can break the OpenClaw gateway. Reports in plain English. Offers a one-shot fix.

No daemon to install, no config to write, no new scheduled task. You run it when something's wrong, it diagnoses + heals, it gets out of the way.

---

## Safe operation on production infrastructure (READ THIS FIRST)

This tool can touch processes, delete scheduled tasks, and reinstall your gateway. On your laptop that's fine. On a production machine running an agent that has child services depending on it — HQ servers, watchdogs, API bridges — default `--fix` can cascade and take those down with the gateway. The fix goes through; the child services don't come back.

We ship with **safe defaults**. Follow these rules the first time you run claw-medic on a machine that matters:

1. **Run diagnostic-only for the first week.** `python3 claw_medic.py` (no flags) just reports. Nothing changes. Read the output, confirm the checks match reality on your box. Do this for several days before you let it touch anything.
2. **Always pair `--fix` with `--conservative`.** Plain `--fix` will run `openclaw gateway install --force` when it thinks the gateway needs reinstalling. `--force` kills whatever is on the gateway port so the new install can take over — which kills child services bound to or spawned by the gateway. `--conservative` skips that class of fix and prints the command for you to run manually when you're ready. Use it on any machine that isn't your throwaway laptop.
3. **`--require-session 1` is opt-in for a reason.** It checks whether the gateway is in an interactive user session (needed for the desktop-control skill). On multi-PID gateway setups the check used to deadlock; v0.3+ fixed that, but the default is still off. Only turn it on if you actually use the desktop skill.
4. **`--cleanup-orphans` deletes scheduled tasks.** Specifically, tasks that have `LastResult=267011` and have never run — in practice these are legacy cruft from old OpenClaw installs. But if you've just created a new scheduled task and it hasn't had a chance to run yet, claw-medic will think it's orphan. Either wait for your task to run once, or review the list before agreeing to the cleanup.
5. **Always review what `--fix` wants to do before approving.** Run diagnostic-only first, read the `Suggested fix:` lines, then run `--fix --conservative` if they look reasonable.

If your setup is anything beyond a single dev machine — think "a customer depends on this" — run claw-medic **read-only** on a cron, log the output somewhere, and let a human decide when to actually apply fixes. The checks are designed to be safe to run on a timer; the fixes are designed to be safe with `--conservative`; mixing the two in production without review is not the intended flow.

The tool will never phone home, never open a network connection except to your local gateway, never write files outside the current directory (and only when you pass `--report`). No telemetry, no lock-in.

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

## What it does NOT do (safety)

- **No blanket process-killing.** claw-medic never runs `taskkill /F /IM node.exe`, never greps for "openclaw" and kills every match, never kills processes by name pattern.
- **Targeted actions only.** Every process-level action is against a specific PID the tool started, or against the gateway port specifically.
- **Scheduled-task cleanup removes task REGISTRATIONS, not running processes.** `schtasks /Delete` unregisters the task; any process currently running from that task keeps running until it exits naturally.
- **`--fix` is off by default.** Diagnostic runs are read-only.

## Warning: `openclaw gateway install --force` kills child services

One of the suggested fixes is `openclaw gateway install --force`, which we delegate to OpenClaw's CLI. The `--force` flag kills whatever is bound to the gateway port so the new install can take it over. If you have other services spawned as CHILDREN of the gateway (e.g., a custom HQ server the gateway launches on startup, a companion HTTP API, a Jeff HQ auth server), **they may die with the gateway and not automatically respawn** depending on how they're wired.

If you're in that setup, before running `--fix`:

1. Check `claw-medic` output for which fixes it wants to apply
2. Decide whether to run those fixes manually one at a time
3. Have your child-service restart commands ready

**v0.4 ships this as `--fix --conservative`.** In conservative mode, claw-medic treats any fix backed by `openclaw gateway install --force` as a manual step: it prints the exact command and the reason it was skipped, but doesn't run it. You decide when to take the hit on child services.

---

## Report a failed fix (v0.5)

The kit is only useful if it knows which fixes actually work on real machines. If a fix failed for you, file a short report:

```bash
python3 claw_medic.py --report \
    --fix-name gateway_process \
    --outcome "fix ran but port 18789 still not bound"
```

claw-medic saves a **PII-scrubbed JSON** (`claw-medic-report-YYYYMMDD-HHMMSS.json`) to your current directory and prints a pre-filled GitHub issue URL. The scrubber replaces home directories (`~`), Windows user profiles (`%USERPROFILE%`), public IPs (`[ip-redacted]`), and email addresses (`[email-redacted]`). Loopback and ports stay intact because they're useful. Nothing is posted — you open the URL, attach the JSON, review, submit.

We process reports via a daily triage task against the repo's issues tracker. Duplicates get linked to the original, version-specific ones get routed to the right milestone, and patterns across reports become new checks or tightened fixes in the next claw-medic release. Every fix that gets smarter this way is credited in the commit message to the report that caught it.

If you're seeing a bug claw-medic doesn't check for at all, use the "New OpenClaw bug / new check request" template on the same issues page instead — that template captures a different shape of report.

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

# Safer --fix: skip `openclaw gateway install --force` (protects child services
# like HQ servers, watchdog children, etc. that were launched by the gateway).
python3 claw_medic.py --fix --conservative

# Report a failed fix back to the kit (PII-scrubbed, nothing is posted — you review).
python3 claw_medic.py --report \
    --fix-name gateway_process \
    --outcome "fix ran but port 18789 still not bound"

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

## Releases

### v1.0 — first public release (April 2026)

Ships with:

- **10 core health checks** across gateway process state, port binding, HTTP health, startup mechanism detection, watchdog presence, OpenClaw version, bootstrap budget, and 24-hour gateway log analysis.
- **`--fix`** applies one-shot fixes for anything `FAIL` with a suggested fix.
- **`--conservative`** safer `--fix` that never runs `openclaw gateway install --force` (which can kill child services spawned by the gateway). Prints the command as a manual step instead.
- **`--report`** generates a PII-scrubbed diagnostic JSON and a pre-filled GitHub issue URL for fix-failure reports. Nothing is auto-posted.
- **`--cleanup-orphans`** removes never-run scheduled tasks that clutter up `schtasks`.
- **`--require-session N`** (Windows) asserts the gateway is running in an interactive user session (required for the desktop-control skill).
- **`--json`**, **`--quiet`**, **`--checks`**, **`--port`** for scripting and narrow runs.
- Auto-detects gateway port from `--port` flag → `OPENCLAW_GATEWAY_PORT` env → `~/.openclaw/openclaw.json` → `18789` (upstream default), matching OpenClaw's own precedence.
- Auto-detects startup mechanism (Windows Scheduled Task, Startup-folder shortcut, `gateway.cmd`/`gateway.sh`, systemd unit, launchd plist) — no hardcoded assumption.
- Structured `.github/ISSUE_TEMPLATE/` for fix-failure reports and new-bug / new-check requests so community feedback is machine-readable from day one.

Pre-1.0 commit history is visible in `git log` and documents the iteration that got us here — multi-PID PowerShell deadlocks, stale log false positives, child-service cascades from `--force`, and the first cut of the community feedback loop. Every concern real users hit got a commit with a fix.

### What's next

- **v1.1** — Daily triage task extension: scan incoming kit issues, deduplicate automatically against what's already known, draft PRs and replies for a maintainer to review. Never auto-merges. Design doc: [`docs/v0.6-triage-design.md`](../docs/v0.6-triage-design.md) (named per the internal working label; shipped as v1.1).
- **v1.2** — Slack / Discord webhook alerts when a check flips from `OK` to `FAIL`.
- **v1.3** — `--watch` mode: stay running, re-check every N seconds, only emit on state change.
- **v1.4** — `--collect` bundles gateway.log tail + openclaw.json + claw-medic output into a single `.zip` suitable for attaching to a forum post.
- **v1.5** — Backport checks from `openclaw doctor` so claw-medic is a drop-in superset.
- **v2.0 — "background agent mode" (the Kairos moment)** — When v1.1 + v1.2 + v1.3 + v1.4 combine, claw-medic can run as a single continuous background daemon: consolidates memory at idle, auto-heals the gateway, ships diagnostic reports back to maintainers, processes incoming bug reports, drafts fixes. Same shape as Anthropic's leaked Kairos design ([source code leak, March 31 2026](https://www.infoq.com/news/2026/04/claude-code-source-leak/)), built in public, MIT-licensed, inspectable. This is when the kit stops being a tool you run and becomes an agent that runs alongside yours.

---

## License

MIT.

**Reported a new failure mode?** Open an issue tagged `claw-medic`. Include your claw-medic output (paste the JSON with `--json`). The tool gets smarter every time someone hits a new bug.
