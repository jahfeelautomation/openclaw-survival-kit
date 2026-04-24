#!/usr/bin/env python3
"""
claw-reaper — pre-flight config validator and runtime watchdog for OpenClaw
compaction + subagent RAM-bomb patterns.

This is v0.1 alpha. Scope:
  - `check` subcommand: static analysis of openclaw.json, grade A..F
  - `start` subcommand: runtime loop that projects transcript growth and
    optionally force-compacts when projection breaches the budget
  - `reap-now` subcommand: one-shot checkpoint sweep respecting TTL
  - `status` / `logs` / `stop`: standard service affordances

Citations for the three RAM-bomb patterns are in README.md. This file
implements the detection logic for those patterns, not a reimplementation
of OpenClaw's compaction engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML, required
except ImportError:
    sys.stderr.write("claw-reaper: missing dependency 'PyYAML'. pip install pyyaml\n")
    sys.exit(2)

LOG = logging.getLogger("claw-reaper")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "target": {
        "openclaw_json": "~/.openclaw/openclaw.json",
        "sessions_dir": "~/.openclaw/sessions",
    },
    "budget": {
        "ram_bytes_per_agent": 16 * 1024 * 1024,
        "projection_window_minutes": 240,
    },
    "watchdog": {
        "check_interval_seconds": 60,
        "trigger_compaction_on_projection_breach": False,
        "backoff_after_forced_compactions": 3,
        "backoff_window_seconds": 600,
        "backoff_pause_seconds": 300,
    },
    "reaper": {
        "enabled": True,
        "checkpoint_ttl_days": 7,
        "run_interval_hours": 6,
        "respect_compaction_mode": False,
    },
    "log": {"max_size_mb": 50, "rotate_keep": 5},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> dict:
    if not path:
        return dict(DEFAULT_CONFIG)
    p = Path(os.path.expanduser(path))
    if not p.exists():
        LOG.warning("reaper config not found at %s, using defaults", p)
        return dict(DEFAULT_CONFIG)
    with p.open("r", encoding="utf-8") as fh:
        user = yaml.safe_load(fh) or {}
    return _deep_merge(DEFAULT_CONFIG, user)


def load_openclaw_json(path: str) -> dict:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        raise FileNotFoundError(f"openclaw.json not found at {p}")
    # utf-8-sig tolerates a BOM if a PowerShell writer left one behind.
    with p.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Static config check — the "grade" pipeline
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    level: str   # "OK" | "WARN" | "FAIL"
    code: str    # short slug
    message: str
    fix: str | None = None


@dataclass
class CheckReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, *args, **kw): self.findings.append(Finding(*args, **kw))

    def grade(self) -> str:
        fails = sum(1 for f in self.findings if f.level == "FAIL")
        warns = sum(1 for f in self.findings if f.level == "WARN")
        if fails >= 2: return "F"
        if fails == 1: return "D"
        if warns >= 2: return "C"
        if warns == 1: return "B"
        return "A"


def check_compaction_defaults(cfg: dict, report: CheckReport) -> None:
    """Pattern 1 — upstream #54102. truncateAfterCompaction=false keeps
    the full pre-compaction transcript in memory, and without a matching
    forceFlushTranscriptBytes cap it will grow until OOM."""
    defaults = cfg.get("agents", {}).get("defaults", {}).get("compaction", {})
    truncate = defaults.get("truncateAfterCompaction", True)
    flush_bytes = (
        defaults.get("memoryFlush", {}).get("forceFlushTranscriptBytes")
    )
    if truncate is False and (flush_bytes is None or flush_bytes > 16 * 1024 * 1024):
        report.add(
            "WARN",
            "compaction_ram_bomb",
            "agents.defaults.compaction.truncateAfterCompaction = false"
            + " with no adequate forceFlushTranscriptBytes cap",
            fix="Set truncateAfterCompaction=true OR set"
                " memoryFlush.forceFlushTranscriptBytes ≤ 8388608 (8 MiB).",
        )
    else:
        report.add("OK", "compaction_defaults",
                   "compaction defaults do not match the #54102 RAM-bomb shape")


def check_subagent_spawn_mode(cfg: dict, report: CheckReport) -> None:
    """Pattern 2 — upstream #59823. Subagents in spawnMode=session are
    exempt from the parent's compaction, so their transcript can grow
    indefinitely while the parent happily compacts around them."""
    agents = cfg.get("agents", {})
    for name, spec in agents.items():
        if name == "defaults":
            continue
        if not isinstance(spec, dict):
            continue
        if spec.get("spawnMode") == "session":
            own_truncate = (
                spec.get("compaction", {}).get("truncateAfterCompaction")
            )
            if own_truncate is not True:
                report.add(
                    "WARN",
                    "subagent_session_leak",
                    f"agents.{name}.spawnMode = 'session' with no"
                    " truncateAfterCompaction override",
                    fix=f"Either change agents.{name}.spawnMode to 'isolated'"
                        f" OR set agents.{name}.compaction.truncateAfterCompaction = true",
                )


def check_cron_pollution(cfg: dict, report: CheckReport) -> None:
    """Bonus check — cron jobs with sessionTarget='main' pollute the
    main transcript with scheduled noise. Not a RAM bomb per se but
    compounds the RAM-bomb patterns above."""
    offenders = []
    cron_jobs = cfg.get("cron", {}).get("jobs", []) or []
    for job in cron_jobs:
        if job.get("sessionTarget") == "main":
            offenders.append(job.get("name", "<unnamed>"))
    if offenders:
        report.add(
            "WARN",
            "cron_main_pollution",
            f"cron jobs writing to main session: {', '.join(offenders)}",
            fix="Set sessionTarget='isolated' on these cron jobs.",
        )
    else:
        report.add("OK", "cron_sessiontarget",
                   "all cron jobs target isolated sessions")


def check_checkpoint_mode(cfg: dict, report: CheckReport) -> None:
    """Pattern 3 — upstream #61447. Checkpoint rotation doesn't run
    under compaction mode 'safeguard' unless respect_compaction_mode
    is explicitly overridden."""
    mode = cfg.get("agents", {}).get("defaults", {}).get("compaction", {}).get("mode")
    if mode == "safeguard":
        report.add(
            "WARN",
            "safeguard_mode_checkpoint_stall",
            "compaction.mode = 'safeguard' — checkpoint rotation will"
            " not run (upstream #61447). claw-reaper's reaper runs"
            " independently, but confirm reaper.respect_compaction_mode=false",
        )


def run_check(openclaw_cfg_path: str) -> CheckReport:
    cfg = load_openclaw_json(openclaw_cfg_path)
    report = CheckReport()
    check_compaction_defaults(cfg, report)
    check_subagent_spawn_mode(cfg, report)
    check_cron_pollution(cfg, report)
    check_checkpoint_mode(cfg, report)
    return report


def print_report(report: CheckReport, path: str) -> None:
    print(f"claw-reaper v0.1 — config check for {path}\n")
    for f in report.findings:
        tag = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}[f.level]
        print(f"{tag} {f.message}")
        if f.fix:
            print(f"       Fix: {f.fix}")
    print()
    print(f"Config grade: {report.grade()}")


# ---------------------------------------------------------------------------
# Runtime watchdog — projection math
# ---------------------------------------------------------------------------

@dataclass
class Projection:
    agent: str
    current_bytes: int
    append_rate_bps: float      # bytes/sec in the last sampling window
    seconds_to_next_compaction: float
    projected_bytes: float

    def exceeds(self, budget: int) -> bool:
        return self.projected_bytes > budget


def project_session(session_path: Path, sample_seconds: int = 300) -> Projection | None:
    """Approximate append velocity from mtime + size. A proper implementation
    would tail the JSONL and timestamp each append, but mtime is adequate for
    the RAM-bomb signal at the coarse resolution we need."""
    try:
        stat = session_path.stat()
    except FileNotFoundError:
        return None
    size = stat.st_size
    mtime = stat.st_mtime
    age = max(time.time() - mtime, 1.0)
    # crude: assume append_rate ≈ size / age for now; v0.2 will keep a real sampler
    append_rate = size / age
    # default: assume next compaction is 1 hour away unless we learn otherwise
    seconds_to_next = 3600.0
    projected = size + append_rate * seconds_to_next
    return Projection(
        agent=session_path.stem,
        current_bytes=size,
        append_rate_bps=append_rate,
        seconds_to_next_compaction=seconds_to_next,
        projected_bytes=projected,
    )


def run_watchdog(cfg: dict) -> None:
    interval = cfg["watchdog"]["check_interval_seconds"]
    budget = cfg["budget"]["ram_bytes_per_agent"]
    sessions_dir = Path(os.path.expanduser(cfg["target"]["sessions_dir"]))
    LOG.info("claw-reaper runtime started; budget=%s bytes; interval=%ss",
             budget, interval)

    def _handle_sigterm(*_):
        LOG.info("SIGTERM received, exiting cleanly")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    while True:
        for jsonl in sessions_dir.rglob("*.jsonl"):
            p = project_session(jsonl)
            if p is None:
                continue
            if p.exceeds(budget):
                LOG.warning(
                    "RAM-bomb projection: agent=%s current=%d projected=%.0f budget=%d",
                    p.agent, p.current_bytes, p.projected_bytes, budget,
                )
                if cfg["watchdog"]["trigger_compaction_on_projection_breach"]:
                    LOG.info("would force compaction on %s (stub in v0.1)", p.agent)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Checkpoint reaper
# ---------------------------------------------------------------------------

def reap_now(cfg: dict) -> int:
    ttl_days = cfg["reaper"]["checkpoint_ttl_days"]
    sessions_dir = Path(os.path.expanduser(cfg["target"]["sessions_dir"]))
    cutoff = time.time() - (ttl_days * 86400)
    removed = 0
    for cp in sessions_dir.rglob("*.checkpoint"):
        try:
            if cp.stat().st_mtime < cutoff:
                cp.unlink()
                removed += 1
        except OSError as e:
            LOG.warning("could not reap %s: %s", cp, e)
    LOG.info("reaper removed %d checkpoints older than %d days", removed, ttl_days)
    return removed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="claw-reaper")
    ap.add_argument("--config", default=None, help="path to claw-reaper.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="static config check")
    p_check.add_argument("--openclaw-json", default=None)

    sub.add_parser("start", help="run the watchdog loop")
    sub.add_parser("reap-now", help="one-shot checkpoint sweep")
    sub.add_parser("status", help="print current projections and reaper status")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "check":
        target = args.openclaw_json or cfg["target"]["openclaw_json"]
        target = os.path.expanduser(target)
        report = run_check(target)
        print_report(report, target)
        return 0 if report.grade() in ("A", "B") else 1

    if args.cmd == "start":
        run_watchdog(cfg)
        return 0

    if args.cmd == "reap-now":
        reap_now(cfg)
        return 0

    if args.cmd == "status":
        sessions = Path(os.path.expanduser(cfg["target"]["sessions_dir"]))
        for jsonl in sessions.rglob("*.jsonl"):
            p = project_session(jsonl)
            if p:
                print(f"{p.agent:24s} cur={p.current_bytes:>10d} "
                      f"proj={p.projected_bytes:>12.0f} "
                      f"rate={p.append_rate_bps:>8.1f} B/s")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
