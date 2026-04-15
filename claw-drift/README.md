# claw-drift

**Bootstrap-file sanity scanner for OpenClaw. CLI + JSON output.**

> Inspired by and a direct companion to [DanAndBub/Driftwatch](https://github.com/DanAndBub/Driftwatch) — the original tool that first made OpenClaw's silent bootstrap problems visible. Their work is client-side and built for human eyeballs in a browser. **Go use theirs when you want to spot-check a workspace interactively.** This one is the automation twin: runs in CI, pipes into a watchdog, gets scheduled by cron, exits non-zero when something's wrong.

Full credit to **Dan and Bub** for the checks list, the 70/20/10 truncation preview idea, and the "what silently eats your agent" framing. This tool adds nothing they didn't figure out first — it just ships those checks as a Python CLI so they can run unattended.

---

## What it checks

1. **Truncation risk** — any bootstrap file >20,000 characters will be silently truncated by OpenClaw using a 70% head / 20% tail / 10% discard-marker split. Your agent loses instructions and you get no warning.
2. **Budget overrun** — the total across all bootstrap files must stay under 150,000 characters. Over that, OpenClaw starts dropping files.
3. **Heading integrity** — basic structural sanity (H1/H2/H3 counts per file).
4. **Cross-file contradictions** — rules that say different things across files. Default rule set catches: conflicting model primaries, fork/no-fork ambiguity, customer-UI naming drift, time-estimate leaks when rule forbids them. Easy to edit the `CONTRADICTIONS` list in `claw_drift.py` to fit your ops.
5. **Drift over time** — compares today's sizes + heading counts against the last snapshot you saved. Flags files that grew or lost structure.

The 8 files scanned (same as Driftwatch): SOUL.md, AGENTS.md, MEMORY.md, IDENTITY.md, TOOLS.md, USER.md, HEARTBEAT.md, BOOTSTRAP.md.

---

## Install

```bash
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit/claw-drift
python3 --version  # 3.8+
```

No pip dependencies. Standard library only.

---

## Usage

Basic scan (read-only, pretty output):
```bash
python3 claw_drift.py --workspace ~/.openclaw/workspace
```

Machine-readable JSON for piping into other tools:
```bash
python3 claw_drift.py --workspace ~/.openclaw/workspace --json
```

Scan + save snapshot for drift tracking on next run:
```bash
python3 claw_drift.py --workspace ~/.openclaw/workspace --snapshot
```

Use in CI / watchdog — exit code reflects severity:
```bash
python3 claw_drift.py --workspace ~/.openclaw/workspace --exit-nonzero --quiet
# exit 0: all healthy
# exit 1: at-risk or bloated files, or budget >90%
# exit 2: truncated files, budget over limit, or contradictions
```

---

## Typical output

```
claw-drift — scanned 8 bootstrap files
  inspired by DanAndBub/Driftwatch (github.com/DanAndBub/Driftwatch)
  generated:  2026-04-15T03:12:40+00:00
  total bytes: 92,337 / 150,000  (62% — healthy)

 ●  SOUL.md           7,842  ( 39%)  healthy
 ●  AGENTS.md         9,104  ( 46%)  healthy
 ●  MEMORY.md        21,430  (107%)  truncated  → LOSES 10% of content
 ●  IDENTITY.md       3,221  ( 16%)  healthy
 ●  TOOLS.md          5,012  ( 25%)  healthy
 ●  USER.md           2,100  ( 11%)  healthy
 ●  HEARTBEAT.md      4,628  ( 23%)  healthy
 ○  BOOTSTRAP.md          0  (  0%)  missing

Contradictions found:
  ⚠ Model primary contradiction — Two files name different primary models.
    side A: SOUL.md
    side B: AGENTS.md
```

---

## Why a CLI when a browser tool exists

[DanAndBub/Driftwatch](https://github.com/DanAndBub/Driftwatch) is the right tool when you're actively triaging a workspace and want the visual drill-down. claw-drift exists for the opposite shape:

- **Continuous monitoring**: a cron job or watchdog that runs every hour + alerts when something trips, without anybody having to open a browser
- **Pre-commit hook**: refuse to push if MEMORY.md just crossed the truncation threshold
- **CI check**: fail the build when bootstrap file totals drift beyond a threshold in a PR
- **Composable JSON**: feed drift data into dashboards, Slack alerts, paging systems

Both tools run the same checks. Pick the right shape for the job.

---

## Roadmap

- v1.0 (shipped) — above
- v1.1 — configurable contradiction rules via YAML file; exit-code policy knobs
- v1.2 — remediation suggestions (which file to trim, what to move to AGENTS.md)
- v1.3 — Sentry/PagerDuty/Discord webhook integrations for non-zero exits

Upstream PRs and issue filings welcome. Use GitHub Issues or reach the maintainers on the [`openclaw-survival-kit` root README](../README.md).

---

## License

MIT.

```
Copyright (c) 2026 JahFeel Automation
Some rights reserved to DanAndBub for the original Driftwatch concept:
https://github.com/DanAndBub/Driftwatch (also MIT).
```

Treat Dan + Bub's name like you treat any attribution you keep in source — it stays even if you fork.
