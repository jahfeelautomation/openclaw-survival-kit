#!/usr/bin/env python3
"""
claw-channel-watch — channel adapter health monitor for OpenClaw.

Detects when Telegram, Discord, WhatsApp, or other channel adapters
silently die without the gateway noticing. The gateway keeps running,
the heartbeat keeps firing, but user messages stop flowing because the
adapter's connection dropped and nobody re-established it.

Checks per configured channel:
  1. Silent death — channel configured but no activity in last N minutes.
  2. Stale connection — connection timestamp older than expected keepalive.
  3. Configuration errors — channel enabled but missing required fields.

Usage:
  python3 claw_channel_watch.py                        # scan, human-readable
  python3 claw_channel_watch.py --json                 # machine-readable JSON
  python3 claw_channel_watch.py --config PATH          # custom openclaw.json
  python3 claw_channel_watch.py --timeout 60           # custom staleness threshold

Exit codes:
  0 — all channels healthy (OK)
  1 — at least one WARN finding
  2 — at least one FAIL finding
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_CONFIG_PATH = "~/.openclaw/openclaw.json"
DEFAULT_TIMEOUT_MINUTES = 30

# Required fields per channel type. If a channel is enabled but missing
# any of these, it's a configuration error.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "telegram": ["token", "chat_id"],
    "discord": ["token"],
    "whatsapp": ["session_id"],
    "slack": ["token", "channel"],
    "webhook": ["url"],
}

# Known state/log file patterns relative to ~/.openclaw/
CHANNEL_STATE_PATHS: dict[str, list[str]] = {
    "telegram": [
        "channels/telegram/state.json",
        "channels/telegram/last_update.json",
        "logs/telegram.log",
        "logs/telegram.jsonl",
    ],
    "discord": [
        "channels/discord/state.json",
        "channels/discord/ws_state.json",
        "logs/discord.log",
        "logs/discord.jsonl",
    ],
    "whatsapp": [
        "channels/whatsapp/session.json",
        "channels/whatsapp/state.json",
        "logs/whatsapp.log",
        "logs/whatsapp.jsonl",
    ],
    "slack": [
        "channels/slack/state.json",
        "logs/slack.log",
        "logs/slack.jsonl",
    ],
    "webhook": [
        "channels/webhook/state.json",
        "logs/webhook.log",
    ],
}


@dataclass
class ChannelStatus:
    name: str
    enabled: bool
    status: str          # "healthy" | "silent" | "stale" | "misconfigured" | "unknown"
    level: str           # "OK" | "WARN" | "FAIL"
    message: str
    last_activity: Optional[str] = None
    age_minutes: Optional[float] = None
    missing_fields: Optional[list[str]] = None


@dataclass
class Report:
    generated_at: str
    severity: str        # "OK" | "WARN" | "FAIL"
    config_path: str
    timeout_minutes: int
    channels: list
    channels_checked: int = 0


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _newest_mtime(paths: list[Path]) -> Optional[float]:
    """Return the most recent mtime among existing paths, or None."""
    mtimes = []
    for p in paths:
        try:
            if p.exists():
                mtimes.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes) if mtimes else None


def _extract_last_timestamp(path: Path) -> Optional[float]:
    """Try to extract a timestamp from a JSON state file."""
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    for key in ("last_activity", "lastActivity", "timestamp", "ts",
                "last_message", "lastMessage", "connected_at", "connectedAt",
                "last_update", "lastUpdate"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return val if val < 1e12 else val / 1000.0
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.timestamp()
            except (ValueError, TypeError):
                pass
    return None


def _check_channel_config(name: str, channel_cfg: dict) -> Optional[list[str]]:
    """Check for missing required fields. Returns list of missing field names."""
    required = REQUIRED_FIELDS.get(name.lower(), [])
    missing = [f for f in required if not channel_cfg.get(f)]
    return missing if missing else None


def analyze(config_path: Path, timeout_minutes: int) -> Report:
    channels: list[ChannelStatus] = []

    # Load OpenClaw config
    cfg = _load_json(config_path)
    if cfg is None:
        return Report(
            generated_at=datetime.now(timezone.utc).isoformat(),
            severity="FAIL",
            config_path=str(config_path),
            timeout_minutes=timeout_minutes,
            channels=[asdict(ChannelStatus(
                name="<config>",
                enabled=False,
                status="misconfigured",
                level="FAIL",
                message=f"OpenClaw config not found or unreadable: {config_path}",
            ))],
            channels_checked=0,
        )

    # Find channel configurations — try several known config shapes
    channel_cfgs: dict[str, dict] = {}
    for key in ("channels", "adapters", "integrations"):
        section = cfg.get(key, {})
        if isinstance(section, dict):
            for ch_name, ch_cfg in section.items():
                if isinstance(ch_cfg, dict):
                    channel_cfgs[ch_name.lower()] = ch_cfg
        elif isinstance(section, list):
            for item in section:
                if isinstance(item, dict) and ("name" in item or "type" in item):
                    ch_name = (item.get("name") or item.get("type", "")).lower()
                    if ch_name:
                        channel_cfgs[ch_name] = item

    openclaw_root = _expand("~/.openclaw")
    now = time.time()

    for ch_name, ch_cfg in channel_cfgs.items():
        enabled = ch_cfg.get("enabled", True)
        if ch_cfg.get("disabled"):
            enabled = False

        # Check 3: Configuration errors
        missing = _check_channel_config(ch_name, ch_cfg)
        if missing and enabled:
            channels.append(ChannelStatus(
                name=ch_name,
                enabled=enabled,
                status="misconfigured",
                level="FAIL",
                message=f"Channel '{ch_name}' enabled but missing required fields: "
                        f"{', '.join(missing)}",
                missing_fields=missing,
            ))
            continue

        if not enabled:
            channels.append(ChannelStatus(
                name=ch_name,
                enabled=False,
                status="disabled",
                level="OK",
                message=f"Channel '{ch_name}' is disabled.",
            ))
            continue

        # Gather activity evidence from state/log files
        state_paths_rel = CHANNEL_STATE_PATHS.get(ch_name, [])
        state_paths = [openclaw_root / rel for rel in state_paths_rel]

        # Also check channel-specific paths from config
        for key in ("state_file", "stateFile", "log_file", "logFile"):
            extra = ch_cfg.get(key)
            if extra:
                state_paths.append(_expand(extra))

        # Find the most recent activity timestamp
        best_ts: Optional[float] = None

        # Try JSON state files for explicit timestamps
        for sp in state_paths:
            if sp.exists() and sp.suffix in (".json", ".jsonl"):
                ts = _extract_last_timestamp(sp)
                if ts and (best_ts is None or ts > best_ts):
                    best_ts = ts

        # Fall back to file mtime
        if best_ts is None:
            best_ts = _newest_mtime(state_paths)

        if best_ts is None:
            # No state files found at all
            channels.append(ChannelStatus(
                name=ch_name,
                enabled=True,
                status="unknown",
                level="WARN",
                message=f"Channel '{ch_name}' is enabled but no state/log files found. "
                        f"Cannot determine health.",
            ))
            continue

        age_min = (now - best_ts) / 60.0
        last_activity_iso = datetime.fromtimestamp(best_ts, tz=timezone.utc).isoformat()

        # Check 1: Silent death
        if age_min > timeout_minutes:
            channels.append(ChannelStatus(
                name=ch_name,
                enabled=True,
                status="silent",
                level="FAIL",
                message=f"Channel '{ch_name}' has had no activity for {age_min:.0f} minutes "
                        f"(threshold: {timeout_minutes}m). Possible silent death.",
                last_activity=last_activity_iso,
                age_minutes=round(age_min, 1),
            ))
            continue

        # Check 2: Stale connection — within timeout but nearing it
        if age_min > timeout_minutes * 0.75:
            channels.append(ChannelStatus(
                name=ch_name,
                enabled=True,
                status="stale",
                level="WARN",
                message=f"Channel '{ch_name}' last active {age_min:.0f} minutes ago "
                        f"(approaching {timeout_minutes}m threshold).",
                last_activity=last_activity_iso,
                age_minutes=round(age_min, 1),
            ))
            continue

        # Healthy
        channels.append(ChannelStatus(
            name=ch_name,
            enabled=True,
            status="healthy",
            level="OK",
            message=f"Channel '{ch_name}' active {age_min:.0f} minutes ago.",
            last_activity=last_activity_iso,
            age_minutes=round(age_min, 1),
        ))

    # --- Build report ---
    severity = "OK"
    if any(ch["level"] == "WARN" for ch in [asdict(c) for c in channels]):
        severity = "WARN"
    if any(ch["level"] == "FAIL" for ch in [asdict(c) for c in channels]):
        severity = "FAIL"

    return Report(
        generated_at=datetime.now(timezone.utc).isoformat(),
        severity=severity,
        config_path=str(config_path),
        timeout_minutes=timeout_minutes,
        channels=[asdict(c) for c in channels],
        channels_checked=len(channels),
    )


def _color(s: str, code: str) -> str:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def print_human(report: Report) -> None:
    sev_color = {"OK": "32", "WARN": "33", "FAIL": "31"}

    print("claw-channel-watch — channel adapter health monitor")
    print(f"  generated:  {report.generated_at}")
    print(f"  config:     {report.config_path}")
    print(f"  timeout:    {report.timeout_minutes} minutes")
    print(f"  channels:   {report.channels_checked} checked")
    print(f"  severity:   {_color(report.severity, sev_color.get(report.severity, '0'))}")
    print()

    status_icons = {
        "healthy":       _color("[OK]  ", "32"),
        "disabled":      _color("[----]", "90"),
        "stale":         _color("[WARN]", "33"),
        "silent":        _color("[FAIL]", "31"),
        "misconfigured": _color("[FAIL]", "31"),
        "unknown":       _color("[WARN]", "33"),
    }

    for ch in report.channels:
        icon = status_icons.get(ch["status"], "[????]")
        line = f"  {icon} {ch['name']:<12} {ch['message']}"
        print(line)
        if ch.get("last_activity"):
            print(f"              last activity: {ch['last_activity']}")
        if ch.get("missing_fields"):
            print(f"              missing: {', '.join(ch['missing_fields'])}")

    print()


def determine_exit_code(report: Report) -> int:
    if report.severity == "FAIL":
        return 2
    if report.severity == "WARN":
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="claw-channel-watch",
        description="claw-channel-watch — channel adapter health monitor",
    )
    p.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help=f"Path to openclaw.json (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_MINUTES,
        help=f"Minutes of inactivity before flagging silent death (default: {DEFAULT_TIMEOUT_MINUTES})",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = p.parse_args(argv)

    config_path = _expand(args.config)
    report = analyze(config_path, args.timeout)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print_human(report)

    return determine_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
