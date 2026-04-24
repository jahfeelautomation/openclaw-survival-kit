# claw-session-repair

Clean up orphaned tool_result entries and malformed session data that accumulate during OpenClaw crashes.

## What it detects

- **Orphaned tool_results** — `tool_result` entries without a matching `tool_use` (context pollution)
- **Malformed JSON** — unparseable lines in JSONL transcripts
- **Oversized transcripts** — files over 5 MB (warn) or 20 MB (critical)
- **Stale sessions** — no activity in 7+ days (configurable)
- **Duplicate entries** — same timestamp and content repeated

## Usage

```bash
# Read-only diagnostic
python claw_session_repair.py

# JSON output for automation
python claw_session_repair.py --json

# Fix: remove bad entries, archive stale sessions
python claw_session_repair.py --fix

# Custom stale threshold
python claw_session_repair.py --max-age 14

# Custom sessions directory
python claw_session_repair.py --sessions-dir /path/to/sessions/
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All sessions healthy |
| 1 | WARN — stale sessions or minor issues |
| 2 | FAIL — orphaned entries, malformed data, or critical size |

## Requirements

Python 3.8+, stdlib only. No external dependencies.
