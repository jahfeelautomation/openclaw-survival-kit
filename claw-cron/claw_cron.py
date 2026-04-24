#!/usr/bin/env python3
"""
claw-cron — heartbeat interval collapse detector and fixer for OpenClaw.

Monitors the cron/heartbeat subsystem for four failure modes that silently
drain operator budgets or let tasks rot unnoticed:

  1. Interval collapse — heartbeat interval drops from configured value
     (typically 90 min) to 1-8 minutes, causing cost explosions.
  2. Disabled-but-running — tasks marked disabled that still show recent
     execution timestamps (the disable flag didn't stick).
  3. Silent failures — tasks with exit code 0 but no output or evidence
     of work (the job "ran" but did nothing).
  4. Never-ran tasks — tasks with no execution history at all.

Usage:
  python3 claw_cron.py                          # scan, human-readable
  python3 claw_cron.py --json                   # machine-readable JSON
  python3 claw_cron.py --fix                    # attempt auto-repair
  python3 claw_cron.py --jobs PATH              # custom jobs file
  python3 claw_cron.py --heartbeat PATH         # custom heartbeat state

Exit codes:
  0 — all checks passed (OK)
  1 — at least one WARN finding
  2 — at least one FAIL finding
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_JOBS_PATH = "~/.openclaw/cron/jobs.json"
DEFAULT_HEARTBEAT_PATH = "~/.openclaw/workspace/heartbeat-state.json"
COLLAPSE_THRESHOLD = 0.50  # actual interval < 50% of configured = collapse
STALE_MINUTES = 60  # disabled task ran within this many minutes = suspicious


@dataclass
class Finding:
    level: str       # "OK" | "WARN" | "FAIL"
    code: str        # short slug
    message: str
    fix: Optional[str] = None


@dataclass
class Report:
    generated_at: str
    severity: str    # "OK" | "WARN" | "FAIL"
    findings: list
    jobs_path: str
    heartbeat_path: str
    jobs_scanned: int = 0
    heartbeats_scanned: int = 0


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def _load_json(path: Path) -> Optional[dict | list]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _epoch_age_minutes(epoch_val: float) -> float:
    return (time.time() - epoch_val) / 60.0


def analyze(jobs_path: Path, heartbeat_path: Path) -> Report:
    findings: list[Finding] = []

    # --- Load jobs ---
    jobs_data = _load_json(jobs_path)
    if jobs_data is None:
        findings.append(Finding(
            "FAIL", "jobs_missing",
            f"Cron jobs file not found or unreadable: {jobs_path}",
        ))
        jobs_list: list[dict] = []
    elif isinstance(jobs_data, list):
        jobs_list = jobs_data
    elif isinstance(jobs_data, dict):
        jobs_list = jobs_data.get("jobs", [])
    else:
        jobs_list = []

    # --- Load heartbeat state ---
    hb_data = _load_json(heartbeat_path)
    if hb_data is None:
        findings.append(Finding(
            "WARN", "heartbeat_missing",
            f"Heartbeat state file not found or unreadable: {heartbeat_path}",
        ))
        hb_data = {}

    # --- Check 1: Interval collapse ---
    configured_interval = hb_data.get("interval_minutes") or hb_data.get("intervalMinutes")
    recent_timestamps = hb_data.get("recent_timestamps") or hb_data.get("recentTimestamps") or []
    history = hb_data.get("history", [])

    # Try to build a timestamp list from history entries if recent_timestamps is empty
    if not recent_timestamps and history:
        recent_timestamps = [
            h.get("timestamp") or h.get("ts") or h.get("time")
            for h in history if isinstance(h, dict)
        ]
        recent_timestamps = [t for t in recent_timestamps if t is not None]

    if configured_interval and len(recent_timestamps) >= 2:
        # Sort and compute deltas between consecutive heartbeats
        ts_sorted = sorted(recent_timestamps)
        deltas_minutes = []
        for i in range(1, len(ts_sorted)):
            prev, curr = ts_sorted[i - 1], ts_sorted[i]
            # Handle both epoch-seconds and epoch-milliseconds
            if prev > 1e12:
                prev /= 1000.0
            if curr > 1e12:
                curr /= 1000.0
            delta = (curr - prev) / 60.0
            if delta > 0:
                deltas_minutes.append(delta)

        if deltas_minutes:
            avg_actual = sum(deltas_minutes) / len(deltas_minutes)
            min_actual = min(deltas_minutes)
            threshold = configured_interval * COLLAPSE_THRESHOLD

            if avg_actual < threshold:
                findings.append(Finding(
                    "FAIL", "interval_collapse",
                    f"Heartbeat interval collapsed: configured={configured_interval}m, "
                    f"actual avg={avg_actual:.1f}m, min={min_actual:.1f}m "
                    f"(threshold: <{threshold:.0f}m)",
                    fix=f"Reset heartbeat interval to {configured_interval} minutes.",
                ))
            elif min_actual < threshold:
                findings.append(Finding(
                    "WARN", "interval_spike",
                    f"Some heartbeat intervals below threshold: "
                    f"configured={configured_interval}m, min={min_actual:.1f}m, "
                    f"avg={avg_actual:.1f}m",
                ))
            else:
                findings.append(Finding(
                    "OK", "interval_healthy",
                    f"Heartbeat interval stable: avg={avg_actual:.1f}m vs "
                    f"configured={configured_interval}m",
                ))
    elif configured_interval:
        findings.append(Finding(
            "WARN", "insufficient_timestamps",
            "Not enough heartbeat timestamps to check interval collapse "
            f"(need >=2, have {len(recent_timestamps)})",
        ))

    # --- Check 2-4: Per-job checks ---
    now = time.time()
    for job in jobs_list:
        name = job.get("name") or job.get("id") or "<unnamed>"
        disabled = job.get("disabled", False) or job.get("enabled") is False
        last_run = job.get("last_run") or job.get("lastRun") or job.get("lastExecution")
        exit_code = job.get("last_exit_code") or job.get("lastExitCode")
        last_output = job.get("last_output") or job.get("lastOutput") or ""
        run_count = job.get("run_count") or job.get("runCount") or job.get("executions")

        # Normalize last_run to epoch seconds
        last_run_epoch = None
        if isinstance(last_run, (int, float)):
            last_run_epoch = last_run if last_run < 1e12 else last_run / 1000.0
        elif isinstance(last_run, str):
            try:
                dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                last_run_epoch = dt.timestamp()
            except (ValueError, TypeError):
                pass

        # Check 4: Never-ran
        if last_run_epoch is None and (run_count is None or run_count == 0):
            findings.append(Finding(
                "WARN", "never_ran",
                f"Task '{name}' has no execution history.",
            ))
            continue

        # Check 2: Disabled but running
        if disabled and last_run_epoch is not None:
            age = _epoch_age_minutes(last_run_epoch)
            if age < STALE_MINUTES:
                findings.append(Finding(
                    "FAIL", "disabled_but_running",
                    f"Task '{name}' is disabled but ran {age:.0f} minutes ago.",
                    fix=f"Investigate why '{name}' executes while disabled. "
                        "Possible: scheduler ignoring disabled flag, or external trigger.",
                ))

        # Check 3: Silent failure
        if exit_code == 0 and last_run_epoch is not None:
            output_str = str(last_output).strip()
            if not output_str:
                findings.append(Finding(
                    "WARN", "silent_failure",
                    f"Task '{name}' exited 0 but produced no output.",
                    fix=f"Check '{name}' for no-op execution (empty handler, "
                        "swallowed errors, missing target).",
                ))

    # --- Build report ---
    severity = "OK"
    if any(f.level == "WARN" for f in findings):
        severity = "WARN"
    if any(f.level == "FAIL" for f in findings):
        severity = "FAIL"

    return Report(
        generated_at=datetime.now(timezone.utc).isoformat(),
        severity=severity,
        findings=[asdict(f) for f in findings],
        jobs_path=str(jobs_path),
        heartbeat_path=str(heartbeat_path),
        jobs_scanned=len(jobs_list),
        heartbeats_scanned=len(recent_timestamps) if recent_timestamps else 0,
    )


def apply_fix(jobs_path: Path, heartbeat_path: Path) -> list[str]:
    """Attempt to fix detected problems. Returns list of actions taken."""
    actions: list[str] = []

    # --- Fix heartbeat interval collapse ---
    hb_data = _load_json(heartbeat_path)
    if hb_data and isinstance(hb_data, dict):
        configured = hb_data.get("interval_minutes") or hb_data.get("intervalMinutes")
        actual_key = None
        actual_val = None
        for key in ("actual_interval", "actualInterval", "current_interval", "currentInterval"):
            if key in hb_data:
                actual_key = key
                actual_val = hb_data[key]
                break

        if configured and actual_val is not None and actual_val < configured * COLLAPSE_THRESHOLD:
            # Backup
            backup = heartbeat_path.with_suffix(".json.bak")
            shutil.copy2(heartbeat_path, backup)
            actions.append(f"Backed up heartbeat state to {backup}")

            hb_data[actual_key] = configured
            with heartbeat_path.open("w", encoding="utf-8") as fh:
                json.dump(hb_data, fh, indent=2)
            actions.append(
                f"Reset heartbeat interval from {actual_val}m to {configured}m"
            )

    # --- Fix disabled-but-running tasks ---
    jobs_data = _load_json(jobs_path)
    if jobs_data is not None:
        jobs_list = jobs_data if isinstance(jobs_data, list) else jobs_data.get("jobs", [])
        modified = False
        now = time.time()

        for job in jobs_list:
            disabled = job.get("disabled", False) or job.get("enabled") is False
            if not disabled:
                continue
            last_run = job.get("last_run") or job.get("lastRun") or job.get("lastExecution")
            last_run_epoch = None
            if isinstance(last_run, (int, float)):
                last_run_epoch = last_run if last_run < 1e12 else last_run / 1000.0
            elif isinstance(last_run, str):
                try:
                    dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                    last_run_epoch = dt.timestamp()
                except (ValueError, TypeError):
                    pass

            if last_run_epoch and _epoch_age_minutes(last_run_epoch) < STALE_MINUTES:
                name = job.get("name") or job.get("id") or "<unnamed>"
                # Clear execution state to prevent re-trigger
                for clear_key in ("last_run", "lastRun", "lastExecution"):
                    if clear_key in job:
                        job[clear_key] = None
                modified = True
                actions.append(
                    f"Cleared last_run for disabled task '{name}' to prevent re-trigger"
                )

        if modified:
            backup = jobs_path.with_suffix(".json.bak")
            shutil.copy2(jobs_path, backup)
            actions.append(f"Backed up jobs file to {backup}")

            out = jobs_data if isinstance(jobs_data, list) else jobs_data
            with jobs_path.open("w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2)
            actions.append("Wrote updated jobs file")

    if not actions:
        actions.append("No fixable issues detected.")

    return actions


def _color(s: str, code: str) -> str:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def print_human(report: Report) -> None:
    sev_color = {"OK": "32", "WARN": "33", "FAIL": "31"}
    print(f"claw-cron — heartbeat interval collapse detector")
    print(f"  generated:  {report.generated_at}")
    print(f"  jobs file:  {report.jobs_path} ({report.jobs_scanned} tasks)")
    print(f"  heartbeats: {report.heartbeats_scanned} timestamps analyzed")
    print(f"  severity:   {_color(report.severity, sev_color.get(report.severity, '0'))}")
    print()

    for f in report.findings:
        tag = {
            "OK":   _color("[OK]  ", "32"),
            "WARN": _color("[WARN]", "33"),
            "FAIL": _color("[FAIL]", "31"),
        }.get(f["level"], f"[{f['level']}]")
        print(f"  {tag} {f['message']}")
        if f.get("fix"):
            print(f"         Fix: {f['fix']}")

    print()


def determine_exit_code(report: Report) -> int:
    if report.severity == "FAIL":
        return 2
    if report.severity == "WARN":
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="claw-cron",
        description="claw-cron — heartbeat interval collapse detector and fixer",
    )
    p.add_argument(
        "--jobs", default=DEFAULT_JOBS_PATH,
        help=f"Path to cron jobs file (default: {DEFAULT_JOBS_PATH})",
    )
    p.add_argument(
        "--heartbeat", default=DEFAULT_HEARTBEAT_PATH,
        help=f"Path to heartbeat state file (default: {DEFAULT_HEARTBEAT_PATH})",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p.add_argument("--fix", action="store_true", help="Attempt auto-repair of detected issues")
    args = p.parse_args(argv)

    jobs_path = _expand(args.jobs)
    heartbeat_path = _expand(args.heartbeat)

    report = analyze(jobs_path, heartbeat_path)

    if args.fix:
        actions = apply_fix(jobs_path, heartbeat_path)
        if args.json:
            out = asdict(report)
            out["fix_actions"] = actions
            print(json.dumps(out, indent=2))
        else:
            print_human(report)
            print("Fix actions:")
            for a in actions:
                print(f"  -> {a}")
            print()
    elif args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print_human(report)

    return determine_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
