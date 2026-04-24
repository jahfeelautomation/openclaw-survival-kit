# claw-reaper

**Stop OpenClaw agents from eating your RAM until the box OOMs.**

A pre-flight config validator and runtime watchdog for OpenClaw's compaction + subagent settings. Computes the expected steady-state transcript size from your `openclaw.json` and warns when the math says "this will explode in N hours." Reaps stuck checkpoints before they pile up.

**Status:** v0.1 alpha (April 2026). Running on the author's rig in production. Config math is validated against dist behavior for the three known RAM-bomb patterns below. Please open issues for patterns we missed.

---

## What it fixes

| Upstream bug | Fix |
|---|---|
| [#54102](https://github.com/openclaw/openclaw/issues/54102) — `agents.defaults.compaction.truncateAfterCompaction=false` leaves full pre-compaction transcript in memory | Validates that either the flag is true OR `memoryFlush.forceFlushTranscriptBytes` is set low enough that unbounded growth is impossible |
| [#59823](https://github.com/openclaw/openclaw/issues/59823) — Subagents with `spawnMode:session` are exempt from parent compaction/archival, causing transcript to grow without bound | Detects this pattern, computes projected growth rate from recent JSONL append velocity, warns when projection exceeds configured RAM budget |
| [#61447](https://github.com/openclaw/openclaw/issues/61447) — Checkpoint rotation does not run when compaction mode is `safeguard` but not `default` | Reaps checkpoints older than the configured TTL regardless of compaction mode |

Plus the checkpoint math runtime:
- Scans `~/.openclaw/sessions/**/*.jsonl` and associated checkpoints every 60 seconds
- Computes: `projected_steady_state = current_size + (append_rate_bytes_per_min × minutes_until_next_compaction)`
- If projected steady-state > configured RAM budget, logs a warning and (optionally) triggers an early compaction
- Exponential backoff on reaps: 3 forced compactions in 10 min → pause 5 min (don't thrash the agent)

---

## What it is NOT

- **Not a fork of the compaction engine.** It observes and triggers; it doesn't rewrite.
- **Not a memory profiler.** It uses transcript size on disk as a proxy for RAM, which is a good-enough approximation for text-heavy sessions but not for image-heavy ones.
- **Not a replacement for setting your config correctly.** It tells you when your config is wrong; you still have to fix it.

---

## Install

### macOS / Linux
```bash
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit/claw-reaper
./install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit\claw-reaper
.\install.ps1
```

The installer:
1. Detects your OpenClaw install path (`~/.openclaw` by default) and your `openclaw.json`
2. Runs `claw-reaper check` once against your current config and prints a report
3. Writes a `claw-reaper.yaml` with your RAM budget, reap TTLs, and watchdog interval
4. Registers `claw-reaper` as a system service (systemd / launchd / Windows Service), but leaves it **stopped** — you start it after reviewing the config report

Non-destructive — rolls back cleanly with `./uninstall.sh`.

---

## Pre-flight check

Before enabling the runtime, run once:

```bash
claw-reaper check --config ~/.openclaw/openclaw.json
```

Example output on a box heading for a RAM bomb:

```
claw-reaper v0.1 — config check for ~/.openclaw/openclaw.json

[WARN] agents.defaults.compaction.truncateAfterCompaction = false
       Upstream #54102: full pre-compaction transcript stays in memory.
       Fix: set to true, OR set memoryFlush.forceFlushTranscriptBytes ≤ 8388608 (8 MiB).

[WARN] agents.hermes.spawnMode = "session"  (subagent)
       Upstream #59823: subagent is exempt from parent compaction.
       Current Hermes JSONL append rate: 142 KB/min
       Projected 4h steady-state: 33.3 MB (exceeds 16 MB budget)
       Fix: change spawnMode to "isolated" OR set agents.hermes.compaction.truncateAfterCompaction = true

[OK]   agents.defaults.compaction.mode = "default"
[OK]   cron.sessionTarget values: all "isolated"  (no main-session pollution)
[OK]   checkpoint TTL: 7 days  (rotation will run)

Config grade: C — 2 RAM-bomb patterns detected, see fixes above.
```

Grade legend: A (no issues) / B (minor) / C (RAM bomb likely within 24h) / D (already bleeding memory) / F (OOM imminent).

---

## Config (`claw-reaper.yaml`)

```yaml
# Path to the openclaw.json we're watching
target:
  openclaw_json: "~/.openclaw/openclaw.json"
  sessions_dir: "~/.openclaw/sessions"

# RAM budget for steady-state projection
# If projected > budget, reaper warns and optionally triggers compaction
budget:
  ram_bytes_per_agent: 16777216    # 16 MiB — conservative for text-only
  projection_window_minutes: 240   # 4h lookahead

# Runtime watchdog
watchdog:
  check_interval_seconds: 60
  trigger_compaction_on_projection_breach: false  # dry-run by default
  backoff_after_forced_compactions: 3
  backoff_window_seconds: 600
  backoff_pause_seconds: 300

# Checkpoint reaper
reaper:
  enabled: true
  checkpoint_ttl_days: 7
  run_interval_hours: 6
  respect_compaction_mode: false  # fix for #61447 — reap regardless of mode

# Logging
log:
  path: "/var/log/claw-reaper.log"   # auto-detected per-OS
  max_size_mb: 50
  rotate_keep: 5

# Optional: Prometheus metrics
metrics:
  enabled: false
  port: 9092
```

---

## Usage

```bash
# One-shot config check (safe to run anytime, doesn't touch sessions)
claw-reaper check

# Start the runtime watchdog + reaper
claw-reaper start

# Status (projected steady-state per agent + last reap time)
claw-reaper status

# Force a reap right now (respects RAM budget logic)
claw-reaper reap-now

# Tail the log
claw-reaper logs -f

# Stop (disables watchdog, does NOT undo any reaps)
claw-reaper stop

# Uninstall (unregisters service, leaves logs)
./uninstall.sh
```

---

## Checkpoint math (for the curious)

The RAM-bomb prediction uses three inputs:

1. **Current transcript size** — `os.path.getsize()` on the active session JSONL
2. **Append velocity** — bytes appended in the last 5 minutes, divided by 300 seconds
3. **Time until next compaction** — derived from `agents.<name>.compaction.everyMessages` or `everySeconds`, whichever fires first, minus time-since-last-compaction

Formula:

```
projected_bytes = current_bytes + (append_rate_bytes_per_sec × seconds_to_next_compaction)
```

Then:
- If the agent has `truncateAfterCompaction=true`, projected is bounded above by `everyMessages × avg_message_bytes`.
- If `truncateAfterCompaction=false`, projected **has no upper bound** — this is the RAM bomb. We flag it hard.
- If the agent is a subagent with `spawnMode=session`, we also check that its parent has truncation on, because the subagent rides the parent's transcript.

The 4-hour projection window is a default; tune `budget.projection_window_minutes` for your appetite. A shorter window catches bombs later; a longer one has more noise.

Full worked example in [`docs/checkpoint-math.md`](./docs/checkpoint-math.md).

---

## Testing

Before shipping each release we run:

1. **RAM-bomb repro** — intentionally misconfigure `truncateAfterCompaction=false` on a chatty agent, run for 2 hours, assert reaper warns within the first projection window
2. **Subagent growth** — spawn a `session`-mode Hermes subagent and hammer it with research queries, assert reaper detects the parent-growth pattern
3. **Checkpoint TTL** — create 20 old checkpoints by touching `.ctime`, assert reaper removes everything over 7 days
4. **Backoff** — force 5 compactions in 1 minute, assert reaper enters backoff after the 3rd

See [`test/`](./test) for the scripts.

---

## Roadmap

- [x] v0.1 — Pre-flight checker, runtime watchdog, checkpoint reaper, 3 upstream patches
- [ ] v0.2 — Pair with `claw-drift --schema-check` to catch phantom config keys before reaper runs
- [ ] v0.3 — Prometheus metrics + Grafana dashboard template
- [ ] v0.4 — Learn-your-workload mode (adjusts projection window from history)
- [ ] v0.5 — Slack/Discord alerts on grade drop (A→C)

---

## License

MIT. Same as the rest of the kit.

**Related tools in this kit:**
- [`claw-drift/`](../claw-drift/) — catches phantom config keys that look valid but aren't in the schema
- [`claw-medic/`](../claw-medic/) — triage stuck sessions and orphaned subagents (complements reaper when things already went wrong)
