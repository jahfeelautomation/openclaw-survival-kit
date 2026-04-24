# claw-cron

**Heartbeat interval collapse detector and fixer for OpenClaw. CLI + JSON output.**

Detects the silent cost explosion where OpenClaw's heartbeat interval drops from its configured value (typically 90 minutes) to dangerously short intervals (1-8 minutes), multiplying API spend without any visible error.

---

## What it checks

1. **Interval collapse** — actual heartbeat interval < 50% of configured value.
2. **Disabled-but-running** — tasks marked disabled that still have recent execution timestamps.
3. **Silent failures** — tasks with exit code 0 but no output or evidence of work.
4. **Never-ran tasks** — tasks with no execution history at all.

---

## Install

```bash
cd openclaw-survival-kit/claw-cron
python3 --version  # 3.8+
```

No pip dependencies. Standard library only.

---

## Usage

```bash
# Basic scan
python3 claw_cron.py

# JSON output for piping into watchdogs
python3 claw_cron.py --json

# Auto-repair collapsed intervals and stale disabled tasks
python3 claw_cron.py --fix

# Custom file paths
python3 claw_cron.py --jobs /path/to/jobs.json --heartbeat /path/to/heartbeat-state.json
```

Exit codes: `0` = OK, `1` = WARN, `2` = FAIL.

---

## License

MIT. Copyright (c) 2026 JahFeel Automation.
