#!/usr/bin/env python3
"""
claw-medic — Emergency CLI for diagnosing and repairing a sick OpenClaw gateway.

Runs a series of end-to-end health checks, reports findings in plain English, and
optionally applies one-shot fixes. No daemon, no config file, no lock-in.

Usage:
  python3 claw_medic.py                           # diagnose (read-only)
  python3 claw_medic.py --fix                     # apply suggested fixes
  python3 claw_medic.py --fix --conservative      # --fix, but skip reinstall-gateway (protects child services)
  python3 claw_medic.py --fix --cleanup-orphans
  python3 claw_medic.py --json                    # machine-readable output
  python3 claw_medic.py --quiet                   # only print FAIL lines
  python3 claw_medic.py --checks gateway,bootstrap

Exit codes:
  0 — all checks OK
  1 — one or more WARN
  2 — one or more FAIL (or unexpected error)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    import psutil  # type: ignore
except ImportError:
    print(
        "claw-medic requires psutil. Install with: python3 -m pip install --user psutil",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------- constants ----------

VERSION = "0.5"
REPO_URL = "https://github.com/jahfeelautomation/openclaw-survival-kit"

DEFAULT_GATEWAY_PORT = 18789  # upstream default — actual port is resolved from config/env/CLI
OPENCLAW_DIR = Path.home() / ".openclaw"
WORKSPACE_DIR = OPENCLAW_DIR / "workspace"
CONFIG_PATH = OPENCLAW_DIR / "openclaw.json"
BOOTSTRAP_CHAR_LIMIT = 20_000   # upstream default agents.defaults.bootstrapMaxChars
BOOTSTRAP_TOTAL_LIMIT = 150_000  # agents.defaults.bootstrapTotalMaxChars

KNOWN_BAD_VERSIONS = {
    "2026.4.10": (
        "Sandbox path check breaks all bundled skills (upstream #64985). "
        "Pin to 2026.4.9 or upgrade to 2026.4.11+ once released."
    ),
}


def resolve_gateway_port(cli_override: Optional[int] = None) -> int:
    """
    Resolve the gateway port using OpenClaw's documented precedence:
        1. --port CLI flag (cli_override here)
        2. OPENCLAW_GATEWAY_PORT environment variable
        3. ~/.openclaw/openclaw.json -> gateway.port
        4. default 18789
    """
    if cli_override:
        return cli_override
    env_port = os.environ.get("OPENCLAW_GATEWAY_PORT") or os.environ.get("OPENCLAW_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            port = cfg.get("gateway", {}).get("port")
            if isinstance(port, int):
                return port
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_GATEWAY_PORT


def resolve_gateway_bind() -> str:
    """Read bind address from config, default loopback."""
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            bind = cfg.get("gateway", {}).get("bind")
            if isinstance(bind, str):
                return bind
        except (OSError, json.JSONDecodeError):
            pass
    return "loopback"


# ---------- data types ----------

class Severity:
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    severity: str
    message: str
    fix: Optional[str] = None
    fix_fn_name: Optional[str] = None  # name of fix function, resolved lazily
    details: dict = field(default_factory=dict)


@dataclass
class Report:
    checks: list[CheckResult] = field(default_factory=list)
    gateway_port: int = DEFAULT_GATEWAY_PORT
    require_session_1: bool = False

    @property
    def exit_code(self) -> int:
        if any(c.severity == Severity.FAIL for c in self.checks):
            return 2
        if any(c.severity == Severity.WARN for c in self.checks):
            return 1
        return 0


# ---------- helpers ----------

def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def _sev_label(severity: str) -> str:
    return {
        Severity.OK: _color(" OK ", "32;1"),
        Severity.WARN: _color("WARN", "33;1"),
        Severity.FAIL: _color("FAIL", "31;1"),
    }[severity]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _find_openclaw_install() -> Optional[Path]:
    try:
        out = subprocess.check_output(
            ["npm", "root", "-g"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        p = Path(out) / "openclaw"
        if p.exists():
            return p
    except Exception:
        pass
    for cand in [
        Path("/usr/lib/node_modules/openclaw"),
        Path("/usr/local/lib/node_modules/openclaw"),
        Path.home() / "node_modules" / "openclaw",
    ]:
        if cand.exists():
            return cand
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            cand = Path(appdata) / "npm" / "node_modules" / "openclaw"
            if cand.exists():
                return cand
    return None


def _port_is_bound(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _http_healthz(port: int, timeout: float = 4.0) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        req = Request(url, headers={"User-Agent": "claw-medic/0.2"})
        with urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300, f"HTTP {resp.status}")
    except URLError as e:
        return False, f"URLError: {e.reason}"
    except (TimeoutError, ConnectionError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def _detect_startup_mechanism() -> dict:
    """
    Report which gateway startup mechanism(s) are present on this host.
    Returns a dict with boolean flags + descriptive notes.
    """
    info = {
        "scheduled_task": False,
        "startup_folder": False,
        "launcher_script": False,
        "systemd_unit": False,
        "launchd_plist": False,
        "notes": [],
    }
    # Windows: scheduled task
    if _is_windows():
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", "OpenClaw Gateway"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                info["scheduled_task"] = True
                if "Disabled" in r.stdout:
                    info["notes"].append("Scheduled Task 'OpenClaw Gateway' is present but DISABLED.")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        startup_cmd = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "OpenClaw Gateway.cmd"
        )
        if startup_cmd.exists():
            info["startup_folder"] = True
        gateway_cmd = OPENCLAW_DIR / "gateway.cmd"
        if gateway_cmd.exists():
            info["launcher_script"] = True
    # Linux: systemd
    elif sys.platform.startswith("linux"):
        for unit_name in ("openclaw-gateway.service", "openclaw.service"):
            for unit_dir in (Path.home() / ".config/systemd/user", Path("/etc/systemd/system")):
                if (unit_dir / unit_name).exists():
                    info["systemd_unit"] = True
                    info["notes"].append(f"systemd unit: {unit_dir / unit_name}")
    # macOS: launchd
    elif sys.platform == "darwin":
        for plist_dir in (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents")):
            for plist in plist_dir.glob("*openclaw*.plist"):
                info["launchd_plist"] = True
                info["notes"].append(f"launchd plist: {plist}")
    # Universal: gateway launcher script
    for candidate in (OPENCLAW_DIR / "gateway.cmd", OPENCLAW_DIR / "gateway.sh"):
        if candidate.exists():
            info["launcher_script"] = True
            break
    return info


def _find_process_by_cmdline_pattern(pattern: re.Pattern) -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmdline = " ".join(p.info["cmdline"] or [])
            if pattern.search(cmdline):
                out.append(p)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return out


def _tail_file(path: Path, n: int = 50) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


# ---------- checks ----------

def check_gateway_process(report: Report) -> None:
    # Match the gateway by cmdline containing both "openclaw" and "gateway" — don't
    # assume a specific port flag, because many users run with --port or env override.
    pattern = re.compile(r"openclaw.*gateway|gateway.*openclaw", re.IGNORECASE)
    procs = _find_process_by_cmdline_pattern(pattern)
    # Filter out claw-medic itself and common false positives
    procs = [p for p in procs if "claw-medic" not in " ".join(p.info.get("cmdline") or []).lower()
             and "claw_medic" not in " ".join(p.info.get("cmdline") or []).lower()]
    if not procs:
        report.checks.append(CheckResult(
            name="gateway_process",
            severity=Severity.FAIL,
            message="No OpenClaw gateway node process running.",
            fix=f'Start the gateway launcher: "{OPENCLAW_DIR / "gateway.cmd"}"' if _is_windows()
                else f'nohup {OPENCLAW_DIR / "gateway.sh"} & (or re-run: openclaw gateway install --force)',
            fix_fn_name="fix_start_gateway",
        ))
        return
    pids = [p.pid for p in procs]
    # Look for a --port flag in the cmdline to report which port this instance is on
    detected_ports = set()
    for p in procs:
        cmdline = " ".join(p.info.get("cmdline") or [])
        m = re.search(r"--port[=\s]+(\d+)", cmdline)
        if m:
            detected_ports.add(int(m.group(1)))
    port_info = f", on port(s) {sorted(detected_ports)}" if detected_ports else ""
    report.checks.append(CheckResult(
        name="gateway_process",
        severity=Severity.OK,
        message=f"Gateway process(es) running (PID(s): {', '.join(str(p) for p in pids)}{port_info}).",
        details={"pids": pids, "detected_ports": sorted(detected_ports)},
    ))


def check_port_bound(report: Report) -> None:
    port = report.gateway_port
    if _port_is_bound(port):
        report.checks.append(CheckResult(
            name="port_bound",
            severity=Severity.OK,
            message=f"Configured port {port} is accepting TCP connections.",
        ))
    else:
        report.checks.append(CheckResult(
            name="port_bound",
            severity=Severity.FAIL,
            message=f"Configured port {port} not bound. Gateway isn't listening on the expected port. (Port source: {'openclaw.json' if CONFIG_PATH.exists() else 'default'})",
            fix="Start the gateway: run the gateway_process fix or `openclaw gateway install --force`.",
            fix_fn_name="fix_start_gateway",
        ))


def check_http_health(report: Report) -> None:
    port = report.gateway_port
    ok, info = _http_healthz(port)
    url = f"http://127.0.0.1:{port}/healthz"
    if ok:
        report.checks.append(CheckResult(
            name="http_health",
            severity=Severity.OK,
            message=f"{url} responded ({info}).",
        ))
    else:
        report.checks.append(CheckResult(
            name="http_health",
            severity=Severity.FAIL,
            message=f"{url} not responding. Detail: {info}",
            fix="Restart the gateway. If the process is alive but the port isn't responding, kill and restart — likely a zombie.",
            fix_fn_name="fix_start_gateway",
        ))


def check_startup_mechanism(report: Report) -> None:
    """Report which startup mechanism(s) the gateway is configured to use."""
    info = _detect_startup_mechanism()
    active_mechanisms = [k for k, v in info.items() if v is True]
    if not active_mechanisms:
        report.checks.append(CheckResult(
            name="startup_mechanism",
            severity=Severity.WARN,
            message=(
                "No gateway startup mechanism detected (no Scheduled Task, no Startup-folder item, "
                "no systemd unit, no launchd plist, no gateway.cmd/sh). Gateway won't auto-start at login."
            ),
            fix="Run: openclaw gateway install --force",
            fix_fn_name="fix_reinstall_gateway",
            details=info,
        ))
        return
    # Flag confusing states
    if info["scheduled_task"] and info["startup_folder"]:
        report.checks.append(CheckResult(
            name="startup_mechanism",
            severity=Severity.WARN,
            message=(
                "Both Scheduled Task AND Startup-folder launcher are present. "
                "OpenClaw falls back to Startup-folder when Scheduled Task creation is denied — "
                "having both can cause duplicate gateway instances. Pick one."
            ),
            details=info,
        ))
        return
    report.checks.append(CheckResult(
        name="startup_mechanism",
        severity=Severity.OK,
        message=f"Startup mechanism detected: {', '.join(active_mechanisms)}." + (" " + " ".join(info["notes"]) if info["notes"] else ""),
        details=info,
    ))


def check_session_1(report: Report) -> None:
    """
    OPT-IN: Only runs when --require-session 1 is passed. Checks that the gateway
    process is running in an interactive user session (Session 1 on typical Windows),
    not Session 0 (service session — no desktop access, no user UI).

    Most users don't need this. It matters if you use the desktop-control skill that
    interacts with the logged-in user's screen.
    """
    if not report.require_session_1 or not _is_windows():
        return
    pattern = re.compile(r"openclaw.*gateway|gateway.*openclaw", re.IGNORECASE)
    procs = _find_process_by_cmdline_pattern(pattern)
    procs = [p for p in procs if "claw-medic" not in " ".join(p.info.get("cmdline") or []).lower()]
    if not procs:
        return  # gateway-process check already flagged this

    # v0.3 fix (#claw-medic-hang): previous version spawned one PowerShell per PID,
    # which deadlocked on multi-gateway setups. Single combined call + short timeout.
    pids = [str(p.pid) for p in procs]
    try:
        ps_cmd = (
            f"$ids = @({','.join(pids)}); "
            "Get-Process -Id $ids -ErrorAction SilentlyContinue | "
            "Select-Object Id, SessionId | ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        report.checks.append(CheckResult(
            name="session_1_required",
            severity=Severity.WARN,
            message=f"Could not query process session IDs ({type(e).__name__}). Skipping check.",
        ))
        return

    if r.returncode != 0 or not r.stdout.strip():
        report.checks.append(CheckResult(
            name="session_1_required",
            severity=Severity.WARN,
            message="PowerShell returned no data for session check. Skipping.",
        ))
        return

    try:
        data = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        data = [data]
    session_0_pids = [d.get("Id") for d in data if d.get("SessionId") == 0]
    if session_0_pids:
        report.checks.append(CheckResult(
            name="session_1_required",
            severity=Severity.FAIL,
            message=(
                f"Gateway PID(s) {session_0_pids} running in Session 0 (service session). "
                "Desktop-control skill will not work. Gateway must be launched from a "
                "user-interactive process to get a user session."
            ),
            fix="Stop gateway + relaunch via Startup-folder gateway.cmd (user session), "
                "not via Scheduled Task 'Run whether user is logged on or not'.",
        ))
    else:
        report.checks.append(CheckResult(
            name="session_1_required",
            severity=Severity.OK,
            message=f"All {len(data)} gateway PID(s) in interactive user session. Desktop skill viable.",
        ))


def check_watchdog_process(report: Report) -> None:
    pattern = re.compile(r"openclaw.watchdog|watchdog.*openclaw", re.IGNORECASE)
    procs = _find_process_by_cmdline_pattern(pattern)
    if procs:
        pids = [p.pid for p in procs]
        report.checks.append(CheckResult(
            name="watchdog_process",
            severity=Severity.OK,
            message=f"Watchdog running (PID(s): {', '.join(str(p) for p in pids)}).",
            details={"pids": pids},
        ))
    else:
        # Could be that the user legitimately isn't using a watchdog. Mark WARN not FAIL.
        report.checks.append(CheckResult(
            name="watchdog_process",
            severity=Severity.WARN,
            message="No watchdog process found. If you expect one, it died or never started.",
            fix="Inspect your Startup folder / scheduled tasks. For our kit, start openclaw-watchdog.ps1 manually.",
        ))


def check_scheduled_tasks_health(report: Report) -> None:
    """Windows only. Find never-run OpenClaw-related scheduled tasks."""
    if not _is_windows():
        return
    try:
        # PowerShell one-liner returning JSON
        ps_cmd = (
            "Get-ScheduledTask | Where-Object { $_.TaskName -match 'OpenClaw|Watchdog|Gateway|Bridge|Cowork|Jeff' } | "
            "ForEach-Object { $info = $_ | Get-ScheduledTaskInfo; "
            "[PSCustomObject]@{ Name=$_.TaskName; State=$_.State.ToString(); "
            "LastRun=$info.LastRunTime.ToString('o'); LastResult=$info.LastTaskResult } } | ConvertTo-Json -Depth 3"
        )
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            text=True, stderr=subprocess.DEVNULL, timeout=20,
        ).strip()
        if not out:
            return
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        orphans = []
        disabled = []
        for task in data:
            last_run = str(task.get("LastRun", ""))
            last_result = task.get("LastResult", 0)
            state = str(task.get("State", ""))
            name = task.get("Name", "?")
            # Windows null date for "never run" is 1899-12-30 — surfaces as 11/30/1999 etc.
            if "1999-" in last_run or "1899-" in last_run or last_result == 267011:
                orphans.append(name)
            if state.lower() == "disabled" and name.lower().startswith("openclaw"):
                disabled.append(name)
        if orphans:
            report.checks.append(CheckResult(
                name="scheduled_task_orphans",
                severity=Severity.WARN,
                message=(
                    f"{len(orphans)} scheduled task(s) have never run successfully "
                    f"(LastResult=267011 / 'Task has not yet run'): {', '.join(orphans)}. "
                    "These are configured but never triggered — likely stale registrations."
                ),
                fix="Review each task. Use --cleanup-orphans to unregister them.",
                fix_fn_name="fix_cleanup_orphan_tasks",
                details={"orphans": orphans},
            ))
        if disabled:
            report.checks.append(CheckResult(
                name="scheduled_task_disabled",
                severity=Severity.WARN,
                message=(
                    f"Disabled OpenClaw-related scheduled task(s): {', '.join(disabled)}. "
                    "If these are legacy (pre-Startup-folder-launcher versions), safe to leave disabled."
                ),
            ))
        if not orphans and not disabled:
            report.checks.append(CheckResult(
                name="scheduled_tasks",
                severity=Severity.OK,
                message=f"No never-run or unexpectedly disabled OpenClaw scheduled tasks detected.",
            ))
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        # Don't fail the whole check on platform quirks
        return


def check_version_known_bugs(report: Report) -> None:
    version: Optional[str] = None
    try:
        out = subprocess.check_output(
            ["openclaw", "--version"], text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            version = m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    if not version:
        report.checks.append(CheckResult(
            name="version",
            severity=Severity.WARN,
            message="Could not detect OpenClaw version — is it installed and on PATH?",
        ))
        return

    if version in KNOWN_BAD_VERSIONS:
        report.checks.append(CheckResult(
            name="version",
            severity=Severity.FAIL,
            message=f"Running known-bad version {version}: {KNOWN_BAD_VERSIONS[version]}",
            fix=f"Pin to a known-good version: npm install -g openclaw@<version>",
        ))
    else:
        report.checks.append(CheckResult(
            name="version",
            severity=Severity.OK,
            message=f"OpenClaw version {version} — no known critical bugs in current kit's registry.",
            details={"version": version},
        ))


def check_bootstrap_budget(report: Report) -> None:
    bootstrap_files = ["SOUL.md", "USER.md", "AGENTS.md", "MEMORY.md", "IDENTITY.md", "PROJECT.md"]
    total_chars = 0
    oversized = []
    for name in bootstrap_files:
        p = WORKSPACE_DIR / name
        if not p.exists():
            continue
        try:
            size = len(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        total_chars += size
        if size > BOOTSTRAP_CHAR_LIMIT:
            oversized.append((name, size))

    if oversized:
        lines = [f"{n}: {s:,} chars (limit {BOOTSTRAP_CHAR_LIMIT:,})" for n, s in oversized]
        report.checks.append(CheckResult(
            name="bootstrap_budget_per_file",
            severity=Severity.WARN,
            message=(
                f"{len(oversized)} bootstrap file(s) exceed the per-file truncation limit; "
                "the middle portion will be silently dropped in the 70/20/10 truncation. "
                f"Details: {'; '.join(lines)}"
            ),
        ))

    if total_chars > BOOTSTRAP_TOTAL_LIMIT:
        report.checks.append(CheckResult(
            name="bootstrap_budget_total",
            severity=Severity.FAIL,
            message=(
                f"Total bootstrap chars = {total_chars:,}, limit = {BOOTSTRAP_TOTAL_LIMIT:,}. "
                "Your system prompt is over budget and every message pays the price."
            ),
            fix="Trim SOUL.md/AGENTS.md. See wassupjay/OpenClaw-Token-Optimization for a proven cleanup.",
        ))
    elif not oversized:
        report.checks.append(CheckResult(
            name="bootstrap_budget",
            severity=Severity.OK,
            message=f"Bootstrap budget healthy: {total_chars:,} / {BOOTSTRAP_TOTAL_LIMIT:,} chars total.",
            details={"total_chars": total_chars},
        ))


def check_recent_log_errors(report: Report) -> None:
    """
    v0.3 fix: only consider log entries from the last 24h. Before this, the check
    flagged stale March-23-style content from months ago — noisy false positive.
    """
    log = OPENCLAW_DIR / "gateway.log"
    if not log.exists():
        report.checks.append(CheckResult(
            name="gateway_log",
            severity=Severity.WARN,
            message=f"Gateway log not found at {log}.",
        ))
        return
    lines = _tail_file(log, n=500)
    if not lines:
        return

    # Match OpenClaw's ISO-8601 timestamp prefix: 2026-04-13T10:20:30.123-07:00
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
    now = time.time()
    cutoff = now - 24 * 3600
    recent: list[str] = []
    for ln in lines:
        m = ts_re.match(ln)
        if not m:
            continue
        try:
            ts_struct = time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            # Treat as local time — log format is ambiguous but close enough for this heuristic
            ts = time.mktime(ts_struct)
        except ValueError:
            continue
        if ts >= cutoff:
            recent.append(ln)

    if not recent:
        report.checks.append(CheckResult(
            name="gateway_log",
            severity=Severity.OK,
            message="No gateway log entries in the last 24h (or timestamps don't parse).",
        ))
        return

    flags = {
        "rate_limit": 0,
        "SIGTERM": 0,
        "truncating": 0,
        "error": 0,
        "failed": 0,
        "ECONNREFUSED": 0,
    }
    for ln in recent:
        lo = ln.lower()
        for key in flags:
            if key.lower() in lo:
                flags[key] += 1
    interesting = {k: v for k, v in flags.items() if v > 0}
    if interesting:
        pretty = ", ".join(f"{k}={v}" for k, v in interesting.items())
        sev = Severity.WARN if (flags["error"] + flags["failed"] + flags["SIGTERM"]) < 10 else Severity.FAIL
        report.checks.append(CheckResult(
            name="gateway_log",
            severity=sev,
            message=f"Gateway log (last 24h, {len(recent)} entries) contains: {pretty}.",
            details={**interesting, "recent_entry_count": len(recent)},
        ))
    else:
        report.checks.append(CheckResult(
            name="gateway_log",
            severity=Severity.OK,
            message=f"Gateway log tail (last 24h, {len(recent)} entries) looks clean.",
        ))


# ---------- fixes ----------

def fix_start_gateway(verbose: bool = True, conservative: bool = False) -> Optional[bool]:
    """
    Returns True on success, False on failure, None when deferred under --conservative.
    """
    gateway_cmd = OPENCLAW_DIR / ("gateway.cmd" if _is_windows() else "gateway.sh")
    if not gateway_cmd.exists():
        if conservative:
            # Caller (apply_fixes) handles the user-facing "SKIPPED" line; we stay quiet
            # so we don't duplicate the message.
            return None
        print(f"  gateway launcher missing at {gateway_cmd}; running reinstall first")
        if not fix_reinstall_gateway(verbose):
            return False
    try:
        if _is_windows():
            subprocess.Popen(
                ["cmd.exe", "/c", str(gateway_cmd)],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
            )
        else:
            subprocess.Popen(["/bin/sh", str(gateway_cmd)], start_new_session=True)
        time.sleep(6)
        if _port_is_bound(GATEWAY_PORT):
            if verbose:
                print(f"  gateway started and bound to port {GATEWAY_PORT}")
            return True
        else:
            if verbose:
                print("  gateway launcher ran but port not bound yet; wait and re-run claw-medic")
            return False
    except OSError as e:
        print(f"  failed to launch gateway: {e}")
        return False


def fix_reinstall_gateway(verbose: bool = True) -> bool:
    if not shutil.which("openclaw"):
        print("  openclaw CLI not on PATH; cannot reinstall automatically")
        return False
    try:
        result = subprocess.run(
            ["openclaw", "gateway", "install", "--force"],
            capture_output=True, text=True, timeout=60,
        )
        if verbose:
            if result.stdout.strip():
                print("  " + result.stdout.strip().replace("\n", "\n  "))
            if result.stderr.strip():
                print("  (stderr) " + result.stderr.strip().replace("\n", "\n  "))
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  openclaw gateway install --force timed out after 60s")
        return False


def fix_cleanup_orphan_tasks(orphan_names: list[str], verbose: bool = True) -> bool:
    """
    Windows only. Requires admin privileges for system-created tasks.
    If we can't delete (Access Denied), we print the exact elevated-shell command
    the user needs to run manually instead of silently succeeding.
    """
    if not _is_windows():
        return False
    any_failed = False
    denied = []
    for name in orphan_names:
        try:
            r = subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                if verbose:
                    print(f"  removed scheduled task: {name}")
            else:
                any_failed = True
                err_text = (r.stderr.strip() or r.stdout.strip() or "").lower()
                if "access is denied" in err_text or "access denied" in err_text:
                    denied.append(name)
                if verbose:
                    print(f"  FAILED to remove {name}: {r.stderr.strip() or r.stdout.strip()}")
        except subprocess.TimeoutExpired:
            any_failed = True
            if verbose:
                print(f"  timeout removing {name}")
    if denied and verbose:
        print("")
        print("  Some tasks require admin. In an ELEVATED PowerShell (Run as Administrator), paste:")
        for name in denied:
            print(f'    schtasks /Delete /TN "{name}" /F')
    return not any_failed


# ---------- runner ----------

CHECK_REGISTRY: dict[str, Callable[[Report], None]] = {
    "gateway_process": check_gateway_process,
    "port_bound": check_port_bound,
    "http_health": check_http_health,
    "startup_mechanism": check_startup_mechanism,
    "session_1_required": check_session_1,  # opt-in via --require-session 1
    "watchdog_process": check_watchdog_process,
    "scheduled_tasks": check_scheduled_tasks_health,
    "version": check_version_known_bugs,
    "bootstrap_budget": check_bootstrap_budget,
    "gateway_log": check_recent_log_errors,
}

# Grouping for --checks categories
CHECK_GROUPS = {
    "gateway": ["gateway_process", "port_bound", "http_health"],
    "startup": ["startup_mechanism", "scheduled_tasks"],
    "watchdog": ["watchdog_process"],
    "bootstrap": ["bootstrap_budget"],
    "version": ["version"],
    "logs": ["gateway_log"],
    "session": ["session_1_required"],
}


def run_checks(
    selected: Optional[list[str]] = None,
    port_override: Optional[int] = None,
    require_session_1: bool = False,
) -> Report:
    report = Report()
    report.gateway_port = resolve_gateway_port(port_override)
    report.require_session_1 = require_session_1

    names: list[str]
    if not selected:
        names = list(CHECK_REGISTRY.keys())
    else:
        names = []
        for s in selected:
            if s in CHECK_GROUPS:
                names.extend(CHECK_GROUPS[s])
            elif s in CHECK_REGISTRY:
                names.append(s)
        # dedupe preserve order
        seen = set()
        names = [n for n in names if not (n in seen or seen.add(n))]
    for n in names:
        try:
            CHECK_REGISTRY[n](report)
        except Exception as e:
            report.checks.append(CheckResult(
                name=n,
                severity=Severity.FAIL,
                message=f"Check raised unexpected error: {e}",
            ))
    return report


def print_report(report: Report, quiet: bool = False) -> None:
    for c in report.checks:
        if quiet and c.severity == Severity.OK:
            continue
        header = f"[{_sev_label(c.severity)}] {c.name}"
        print(header)
        print(f"       {c.message}")
        if c.fix and c.severity != Severity.OK:
            print(f"       Suggested fix: {c.fix}")
        print()
    totals = {Severity.OK: 0, Severity.WARN: 0, Severity.FAIL: 0}
    for c in report.checks:
        totals[c.severity] += 1
    print(
        f"Summary: {_color(str(totals[Severity.OK]), '32;1')} ok, "
        f"{_color(str(totals[Severity.WARN]), '33;1')} warn, "
        f"{_color(str(totals[Severity.FAIL]), '31;1')} fail"
    )


def apply_fixes(report: Report, cleanup_orphans: bool, conservative: bool = False) -> None:
    """
    --conservative behavior (v0.4):
    If set, any fix whose fix_fn is `fix_reinstall_gateway` is SKIPPED and printed
    as a manual step instead. Rationale: `openclaw gateway install --force` kills
    whatever is bound to the gateway port so the new install can take it over.
    Child services spawned by the gateway (e.g. Jeff HQ on port 3333) may die
    with it and not auto-respawn. Conservative users want to stop, decide, and
    restart their child services themselves.
    `fix_start_gateway` also respects --conservative: if it would fall through
    to reinstall (because gateway.cmd/gateway.sh is missing), it prints the
    manual command instead of running --force itself.
    """
    fixes_applied = 0
    fixes_deferred = 0
    for c in report.checks:
        if c.severity == Severity.OK or not c.fix_fn_name:
            continue
        if c.fix_fn_name == "fix_cleanup_orphan_tasks" and not cleanup_orphans:
            continue
        print(f"\nApplying fix for {c.name}:")
        print(f"  {c.message}")

        if conservative and c.fix_fn_name == "fix_reinstall_gateway":
            print(f"  -> {_color('SKIPPED (--conservative)', '33;1')}: "
                  f"`openclaw gateway install --force` may kill child services "
                  f"(e.g. HQ servers, watchdog children) that were launched by the gateway.")
            print(f"  Run manually when you're ready to handle any child-service restarts:")
            print(f"    openclaw gateway install --force")
            fixes_deferred += 1
            continue

        ok: Optional[bool] = False
        if c.fix_fn_name == "fix_start_gateway":
            ok = fix_start_gateway(conservative=conservative)
        elif c.fix_fn_name == "fix_reinstall_gateway":
            ok = fix_reinstall_gateway()
        elif c.fix_fn_name == "fix_cleanup_orphan_tasks":
            orphans = c.details.get("orphans", [])
            ok = fix_cleanup_orphan_tasks(orphans)
        if ok is None:
            # Conservative deferral from fix_start_gateway (gateway.cmd missing, would have fallen
            # through to `openclaw gateway install --force`). Treat like the explicit skip above.
            print(f"  -> {_color('SKIPPED (--conservative)', '33;1')}: "
                  f"gateway launcher missing; would have run "
                  f"`openclaw gateway install --force` to recreate it.")
            print(f"  Run manually when you're ready to handle any child-service restarts:")
            print(f"    openclaw gateway install --force")
            fixes_deferred += 1
        elif ok:
            fixes_applied += 1
            print(f"  -> {_color('fixed', '32;1')}")
        else:
            print(f"  -> {_color('failed, manual intervention may be needed', '31;1')}")
    summary = f"\nFixes applied: {fixes_applied}"
    if fixes_deferred:
        summary += f"   Deferred (--conservative): {fixes_deferred}"
    print(summary)


# ---------- v0.5: community feedback reports ----------

# PII scrubbers. These are heuristics, not a guarantee — users always review the
# saved JSON before filing. The point is to catch the common cases so the
# default is "safe to share."

_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Unix home directories: /home/foo, /Users/foo
    (re.compile(r"/(home|Users)/[^/\s\"']+"), "~"),
    # Windows user profile: C:\Users\Foo (case-insensitive, either slash)
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+"), "%USERPROFILE%"),
    # IPv4 addresses (except loopback — loopback is useful to keep)
    (re.compile(r"(?<!\d)(?!127\.)(?!0\.)"
                r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?!\d)"), "[ip-redacted]"),
    # Email addresses
    (re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "[email-redacted]"),
]


def _scrub_text(s: str) -> str:
    """Strip likely-PII patterns from a string.

    First pass: substitute the user's actual home directory literally (handles
    non-standard layouts like containers, Cowork sandboxes, corporate-managed
    Windows profiles). Second pass: regex patterns for anything the literal
    substitution missed.
    """
    if not isinstance(s, str):
        return s
    home = str(Path.home())
    if home and home != "/" and home in s:
        s = s.replace(home, "~")
    # Also handle uppercase Windows drive letter variants (C:\Users\... vs c:\users\...)
    if _is_windows() and home:
        s = s.replace(home.replace("\\", "/"), "~")
    for pat, repl in _SCRUB_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _scrub_value(v):
    """Recursively scrub a JSON-ish value (dict / list / str / scalar)."""
    if isinstance(v, str):
        return _scrub_text(v)
    if isinstance(v, dict):
        return {k: _scrub_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_scrub_value(x) for x in v]
    return v


def build_report_payload(report: Report, fix_name: Optional[str], outcome: Optional[str]) -> dict:
    """
    Build the scrubbed diagnostic payload that ships with a community fix-failure report.
    Everything user-identifying is replaced in-place; the shape is stable so the
    5am triage task can parse it mechanically.
    """
    import platform
    return {
        "claw_medic_version": VERSION,
        "reported_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": platform.system(),
        "os_release": platform.release(),
        "python": sys.version.split()[0],
        "gateway_port": report.gateway_port,
        "require_session_1": report.require_session_1,
        "fix_name": fix_name,
        "outcome": outcome,
        "checks": [_scrub_value(asdict(c)) for c in report.checks],
    }


def emit_report(report: Report, fix_name: Optional[str], outcome: Optional[str]) -> Path:
    """
    Save a scrubbed diagnostic report to disk and print the pre-filled GitHub
    issue URL. Returns the path to the saved JSON so callers can reference it.
    Never posts anything — the user reviews and files themselves.
    """
    payload = build_report_payload(report, fix_name, outcome)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path.cwd() / f"claw-medic-report-{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))

    # Build the pre-filled issue URL. GitHub issue forms accept template field
    # pre-population via query string. We only prefill short fields here; the
    # full scrubbed JSON is attached by the user manually after review.
    from urllib.parse import urlencode, quote
    failure_summary = (
        f"{fix_name or 'unknown-fix'}: {outcome or 'fix did not resolve the issue'}"
    )
    qs = urlencode({
        "template": "fix-failure.yml",
        "title": f"[fix-failure] {failure_summary}",
        "claw-medic-version": VERSION,
        "os": payload["os"],
        "fix-name": fix_name or "",
        "outcome": outcome or "",
    }, quote_via=quote)
    issue_url = f"{REPO_URL}/issues/new?{qs}"

    print()
    print(_color("=== claw-medic community report ===", "36;1"))
    print(f"Scrubbed diagnostic saved: {out_path}")
    print(f"Size: {out_path.stat().st_size} bytes   (review before sharing)")
    print()
    print("Steps to file:")
    print(f"  1. Open: {issue_url}")
    print(f"  2. Scroll to 'Full diagnostic JSON' — attach {out_path.name}")
    print(f"  3. Review the pre-filled fields, edit anything wrong, submit")
    print()
    print("Scrubber replaces: home paths (~), Windows user profiles (%USERPROFILE%),")
    print("public IPs ([ip-redacted]), email addresses ([email-redacted]). Loopback")
    print("IPs (127.*) and ports are kept. If you see anything sensitive still in the")
    print("JSON, delete those fields before attaching — the scrubber is a heuristic,")
    print("not a guarantee.")
    return out_path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="claw-medic", description=__doc__)
    parser.add_argument("--fix", action="store_true", help="apply suggested fixes")
    parser.add_argument("--conservative", action="store_true",
                        help="with --fix: skip `openclaw gateway install --force` (which may kill "
                             "child services) and print it as a manual step instead. Safer on hosts "
                             "that run HQ servers or watchdog children spawned by the gateway.")
    parser.add_argument("--cleanup-orphans", action="store_true",
                        help="with --fix, also remove never-run scheduled tasks")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="only print FAIL/WARN lines")
    parser.add_argument("--checks", default="", help="comma-separated check or group names")
    parser.add_argument("--port", type=int, default=None,
                        help="override gateway port (default: read from openclaw.json / OPENCLAW_GATEWAY_PORT env / 18789)")
    parser.add_argument("--require-session", default=None,
                        help="on Windows: require gateway to run in session N (usually 1, for desktop-skill users). "
                             "Default: off (most users don't need this).")
    parser.add_argument("--report", action="store_true",
                        help="after diagnostic, save a PII-scrubbed JSON report and print a pre-filled "
                             "GitHub issue URL so you can file a fix-failure report against the kit. "
                             "Nothing is posted — you review and submit manually.")
    parser.add_argument("--fix-name", default=None,
                        help="with --report: the check/fix that failed (e.g. 'gateway_process', "
                             "'startup_mechanism'). Prefills the issue title and body.")
    parser.add_argument("--outcome", default=None,
                        help="with --report: one-line description of what happened "
                             "(e.g. 'fix ran but gateway still not bound to 18789').")
    parser.add_argument("--version", action="version", version=f"claw-medic {VERSION}")
    args = parser.parse_args(argv)

    selected = [s.strip() for s in args.checks.split(",") if s.strip()] or None
    require_session_1 = args.require_session == "1"
    report = run_checks(selected, port_override=args.port, require_session_1=require_session_1)

    if args.json:
        out = {
            "gateway_port": report.gateway_port,
            "require_session_1": report.require_session_1,
            "checks": [asdict(c) for c in report.checks],
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"Gateway port (resolved): {report.gateway_port}"
              + (f"   [session-1 required]" if report.require_session_1 else ""))
        print()
        print_report(report, quiet=args.quiet)

    if args.fix:
        apply_fixes(report, cleanup_orphans=args.cleanup_orphans, conservative=args.conservative)
        # Re-run the checks after fixing to give the user an up-to-date picture
        print("\n--- re-checking after fixes ---\n")
        post = run_checks(selected, port_override=args.port, require_session_1=require_session_1)
        print_report(post, quiet=args.quiet)
        # Use the post-fix report for the --report payload — that's what the user
        # wants to share ("here's what still broke after I applied your fix").
        if args.report:
            emit_report(post, args.fix_name, args.outcome)
        return post.exit_code

    if args.report:
        emit_report(report, args.fix_name, args.outcome)

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
