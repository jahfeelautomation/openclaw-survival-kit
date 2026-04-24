# claw-pin

Version pinning and breaking change detection for OpenClaw installations.

## What it does

- Detects the installed OpenClaw version (CLI or config fallback)
- Snapshots key config values on each run (gateway, session, tools, ACP, sandbox)
- Compares current state with the previous snapshot
- Classifies changes as **MIGRATION** (expected), **DRIFT** (unexpected), or **BREAK** (known breaking)
- Maintains a version history file for audit trail

## Known breaking changes tracked

| Version | Issue |
|---------|-------|
| 2026.4.10 | Sandbox permissions changed, skills with file I/O may fail |
| 2026.4.5 | Tool profile format changed from flat to nested |
| 2026.3.x | ACP dispatch default flipped to parallel |

## Usage

```bash
# Compare with last snapshot and record current state
python claw_pin.py

# JSON output for automation
python claw_pin.py --json

# Record current state without comparison
python claw_pin.py --snapshot

# Custom paths
python claw_pin.py --config /path/to/openclaw.json --history /path/to/history.json
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | No changes, or only MIGRATION-level changes |
| 1 | DRIFT detected (unexpected config changes) |
| 2 | BREAK detected (known breaking change in current version) |

## Requirements

Python 3.8+, stdlib only. No external dependencies.
