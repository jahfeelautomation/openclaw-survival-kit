# claw-channel-watch

**Channel adapter health monitor for OpenClaw. CLI + JSON output.**

Detects when Telegram, Discord, WhatsApp, or other channel adapters silently die without the gateway noticing. The gateway keeps heartbeating, the cron keeps firing, but no user messages flow because the adapter connection dropped and nobody re-established it.

---

## What it checks

1. **Silent death** — channel configured and enabled but no activity in the last N minutes (default: 30).
2. **Stale connection** — activity detected but approaching the staleness threshold (>75% of timeout).
3. **Configuration errors** — channel enabled but missing required fields (token, chat_id, etc.).

Supported channels: Telegram, Discord, WhatsApp, Slack, Webhook. Unknown channel types are checked via state file mtime only.

---

## Install

```bash
cd openclaw-survival-kit/claw-channel-watch
python3 --version  # 3.8+
```

No pip dependencies. Standard library only.

---

## Usage

```bash
# Basic scan
python3 claw_channel_watch.py

# JSON output for piping into watchdogs
python3 claw_channel_watch.py --json

# Custom config path and timeout
python3 claw_channel_watch.py --config /path/to/openclaw.json --timeout 60
```

Exit codes: `0` = OK, `1` = WARN, `2` = FAIL.

---

## License

MIT. Copyright (c) 2026 JahFeel Automation.
