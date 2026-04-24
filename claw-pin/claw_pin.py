#!/usr/bin/env python3
"""
claw-pin -- Version pinning and breaking change detection for OpenClaw.

Tracks the OpenClaw version across runs, snapshots key config values, compares
with previous state, and warns about known breaking changes between versions.

Usage:
  python claw_pin.py                                  # compare with last snapshot
  python claw_pin.py --json                           # machine-readable output
  python claw_pin.py --snapshot                       # record current state only
  python claw_pin.py --config PATH                    # custom config path
  python claw_pin.py --history PATH                   # custom history file

Exit codes:
  0 -- no changes or only MIGRATION-level changes
  1 -- DRIFT detected (unexpected config changes)
  2 -- BREAK detected (known breaking change)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------- constants ----------

VERSION = "1.0"
OPENCLAW_DIR = Path.home() / ".openclaw"
DEFAULT_CONFIG = OPENCLAW_DIR / "openclaw.json"
DEFAULT_HISTORY = OPENCLAW_DIR / "workspace" / "data" / "version-history.json"

# Change classifications
CHANGE_MIGRATION = "MIGRATION"
CHANGE_DRIFT = "DRIFT"
CHANGE_BREAK = "BREAK"


# ---------- known breaking changes ----------

KNOWN_BREAKING: list[dict] = [
    {
        "version_pattern": r"^2026\.4\.10$",
        "description": "Sandbox permissions changed -- skills using filesystem access "
                       "may fail silently. Affected: all bundled skills with file I/O.",
        "affected_keys": ["sandbox"],
        "migration": "Upgrade to 2026.4.11+ or pin to 2026.4.9. If stuck on .10, "
                     "add explicit sandbox.permissions in openclaw.json.",
    },
    {
        "version_pattern": r"^2026\.4\.5$",
        "description": "Tool profile format changed from flat object to nested "
                       "profiles array. Old configs silently ignored.",
        "affected_keys": ["tools", "tool_profiles"],
        "migration": "Convert tools.{name}.{config} to tools.profiles[].{config}. "
                     "See upstream changelog 2026.4.5.",
    },
    {
        "version_pattern": r"^2026\.3\.",
        "description": "ACP dispatch mode flipped from 'sequential' to 'parallel' "
                       "by default. Agents expecting ordered execution may break.",
        "affected_keys": ["acp", "dispatch"],
        "migration": "Set acp.dispatch.mode: 'sequential' explicitly in openclaw.json "
                     "if your agents depend on ordered tool execution.",
    },
]


# ---------- data types ----------

@dataclass
class ConfigSnapshot:
    timestamp: str = ""
    timestamp_epoch: float = 0.0
    openclaw_version: Optional[str] = None
    config_values: dict = field(default_factory=dict)


@dataclass
class Change:
    key: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    classification: str = CHANGE_DRIFT
    note: str = ""


@dataclass
class PinReport:
    config_path: str = ""
    history_path: str = ""
    current_version: Optional[str] = None
    last_version: Optional[str] = None
    snapshot_only: bool = False
    changes: list[Change] = field(default_factory=list)
    breaking_warnings: list[dict] = field(default_factory=list)
    history_entries: int = 0

    @property
    def exit_code(self) -> int:
        if any(c.classification == CHANGE_BREAK for c in self.changes):
            return 2
        if self.breaking_warnings:
            return 2
        if any(c.classification == CHANGE_DRIFT for c in self.changes):
            return 1
        return 0


# ---------- version detection ----------

def detect_version() -> Optional[str]:
    """Detect installed OpenClaw version from CLI."""
    try:
        out = subprocess.check_output(
            ["openclaw", "--version"], text=True,
            stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    # Fallback: try reading from config
    config_path = DEFAULT_CONFIG
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            ver = cfg.get("version") or cfg.get("_version")
            if isinstance(ver, str):
                return ver
        except (json.JSONDecodeError, OSError):
            pass

    return None


# ---------- config snapshot ----------

# Keys we snapshot for drift detection
SNAPSHOT_KEYS = [
    "gateway.port",
    "gateway.bind",
    "session.maxTokens",
    "session.contextWindow",
    "session.timeout",
    "tools.profiles",
    "tools.defaults",
    "acp.dispatch.mode",
    "acp.dispatch.timeout",
    "sandbox.enabled",
    "sandbox.permissions",
    "skills.enabled",
    "agents.defaults.bootstrapMaxChars",
    "agents.defaults.bootstrapTotalMaxChars",
]


def _get_nested(obj: dict, dotpath: str):
    """Get a value from a nested dict using dot notation."""
    keys = dotpath.split(".")
    current = obj
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return None
    return current


def snapshot_config(config_path: Path) -> ConfigSnapshot:
    """Take a snapshot of the current config state."""
    snap = ConfigSnapshot(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        timestamp_epoch=time.time(),
        openclaw_version=detect_version(),
    )

    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    else:
        cfg = {}

    for key in SNAPSHOT_KEYS:
        val = _get_nested(cfg, key)
        if val is not None:
            # Serialize complex values to string for comparison
            snap.config_values[key] = json.dumps(val, sort_keys=True, default=str)

    return snap


# ---------- history ----------

def load_history(history_path: Path) -> list[dict]:
    """Load version history from file."""
    if not history_path.exists():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history_path: Path, history: list[dict]) -> None:
    """Save version history to file."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(history, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


# ---------- comparison ----------

def compare_snapshots(old: ConfigSnapshot, new: ConfigSnapshot) -> list[Change]:
    """Compare two snapshots and classify changes."""
    changes = []

    # Version change
    if old.openclaw_version != new.openclaw_version:
        change = Change(
            key="openclaw_version",
            old_value=old.openclaw_version,
            new_value=new.openclaw_version,
            classification=CHANGE_MIGRATION,
            note=f"Version changed from {old.openclaw_version} to {new.openclaw_version}",
        )

        # Check if this is a known breaking version
        if new.openclaw_version:
            for breaking in KNOWN_BREAKING:
                if re.match(breaking["version_pattern"], new.openclaw_version):
                    change.classification = CHANGE_BREAK
                    change.note = breaking["description"]
                    break

        changes.append(change)

    # Config value changes
    all_keys = sorted(set(list(old.config_values.keys()) + list(new.config_values.keys())))
    for key in all_keys:
        old_val = old.config_values.get(key)
        new_val = new.config_values.get(key)
        if old_val != new_val:
            classification = CHANGE_DRIFT
            note = ""

            # Check if this key is affected by a known breaking change for the new version
            if new.openclaw_version:
                for breaking in KNOWN_BREAKING:
                    if re.match(breaking["version_pattern"], new.openclaw_version):
                        affected = breaking.get("affected_keys", [])
                        if any(key.startswith(ak) or ak in key for ak in affected):
                            classification = CHANGE_BREAK
                            note = breaking["description"]
                            break

            # If version also changed, likely a migration
            if old.openclaw_version != new.openclaw_version and classification != CHANGE_BREAK:
                classification = CHANGE_MIGRATION

            changes.append(Change(
                key=key,
                old_value=old_val,
                new_value=new_val,
                classification=classification,
                note=note,
            ))

    return changes


def check_breaking_warnings(version: Optional[str]) -> list[dict]:
    """Check if the current version has known breaking changes."""
    warnings = []
    if not version:
        return warnings
    for breaking in KNOWN_BREAKING:
        if re.match(breaking["version_pattern"], version):
            warnings.append({
                "version_pattern": breaking["version_pattern"],
                "description": breaking["description"],
                "migration": breaking["migration"],
            })
    return warnings


# ---------- main logic ----------

def run_pin(config_path: Path, history_path: Path, snapshot_only: bool) -> PinReport:
    """Run the version pin check."""
    report = PinReport(
        config_path=str(config_path),
        history_path=str(history_path),
        snapshot_only=snapshot_only,
    )

    current = snapshot_config(config_path)
    report.current_version = current.openclaw_version

    history = load_history(history_path)
    report.history_entries = len(history)

    # Check for breaking warnings on current version
    report.breaking_warnings = check_breaking_warnings(current.openclaw_version)

    if snapshot_only:
        # Just record and exit
        history.append(asdict(current))
        save_history(history_path, history)
        report.history_entries = len(history)
        return report

    # Compare with last snapshot
    if history:
        last_entry = history[-1]
        last = ConfigSnapshot(
            timestamp=last_entry.get("timestamp", ""),
            timestamp_epoch=last_entry.get("timestamp_epoch", 0),
            openclaw_version=last_entry.get("openclaw_version"),
            config_values=last_entry.get("config_values", {}),
        )
        report.last_version = last.openclaw_version
        report.changes = compare_snapshots(last, current)

    # Record current snapshot
    history.append(asdict(current))
    save_history(history_path, history)
    report.history_entries = len(history)

    return report


# ---------- output ----------

def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


CHANGE_COLORS = {
    CHANGE_MIGRATION: "36;1",  # cyan
    CHANGE_DRIFT: "33;1",      # yellow
    CHANGE_BREAK: "31;1",      # red
}


def print_report(report: PinReport) -> None:
    print(f"Config:          {report.config_path}")
    print(f"History:         {report.history_path} ({report.history_entries} entries)")
    print(f"Current version: {report.current_version or 'unknown'}")
    if report.last_version:
        print(f"Last version:    {report.last_version}")
    print()

    if report.snapshot_only:
        print("Snapshot recorded. No comparison performed.")
        return

    if report.breaking_warnings:
        print(_color("=== BREAKING CHANGE WARNINGS ===", "31;1"))
        for w in report.breaking_warnings:
            print(f"  {_color('BREAK', '31;1')}: {w['description']}")
            print(f"  Migration: {w['migration']}")
            print()

    if not report.changes:
        print("No changes detected since last snapshot.")
        return

    print("Changes detected:")
    for c in report.changes:
        color = CHANGE_COLORS.get(c.classification, "0")
        label = _color(f"[{c.classification:>9s}]", color)
        print(f"  {label} {c.key}")
        if c.old_value is not None:
            print(f"             was: {c.old_value}")
        if c.new_value is not None:
            print(f"             now: {c.new_value}")
        if c.note:
            print(f"             {c.note}")
        print()

    # Summary
    counts = {}
    for c in report.changes:
        counts[c.classification] = counts.get(c.classification, 0) + 1
    parts = []
    for cls in [CHANGE_MIGRATION, CHANGE_DRIFT, CHANGE_BREAK]:
        if cls in counts:
            parts.append(f"{_color(str(counts[cls]), CHANGE_COLORS[cls])} {cls.lower()}")
    print(f"Summary: {', '.join(parts)}")


# ---------- CLI ----------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claw-pin",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"path to openclaw.json (default: {DEFAULT_CONFIG})")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY),
                        help=f"path to version history file (default: {DEFAULT_HISTORY})")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--snapshot", action="store_true",
                        help="record current state without comparison")
    parser.add_argument("--version", action="version", version=f"claw-pin {VERSION}")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    history_path = Path(args.history)

    report = run_pin(config_path, history_path, snapshot_only=args.snapshot)

    if args.json:
        out = {
            "config_path": report.config_path,
            "history_path": report.history_path,
            "current_version": report.current_version,
            "last_version": report.last_version,
            "snapshot_only": report.snapshot_only,
            "history_entries": report.history_entries,
            "changes": [asdict(c) for c in report.changes],
            "breaking_warnings": report.breaking_warnings,
            "exit_code": report.exit_code,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(report)

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
