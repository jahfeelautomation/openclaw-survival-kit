#!/usr/bin/env python3
"""
claw-drift — CLI scanner for bootstrap-file truncation, budget overrun,
cross-file contradictions, and drift over time.

Inspired by DanAndBub/Driftwatch (https://github.com/DanAndBub/Driftwatch), which
was the first tool to make these silent problems visible. Their tool runs
entirely client-side in a browser — great for ad-hoc human review. This is
the CLI twin: runs anywhere Python does, outputs JSON, composes with cron,
piped into watchdogs, or checked into CI.

Usage:
  python3 claw_drift.py                       # scan, print report to stdout
  python3 claw_drift.py --json                # machine-readable JSON
  python3 claw_drift.py --snapshot            # persist snapshot for drift tracking
  python3 claw_drift.py --workspace /path     # custom workspace root
  python3 claw_drift.py --quiet               # only print WARN/FAIL lines
  python3 claw_drift.py --exit-nonzero        # exit 1 on WARN, 2 on FAIL

Exit codes:
  0 — all bootstrap files healthy
  1 — at least one at-risk or bloated file, or budget >90%
  2 — at least one truncated file, or budget over limit, or hard contradiction

Original concept, checks list, and 70/20/10 truncation preview pattern © DanAndBub
(https://github.com/DanAndBub/Driftwatch) — MIT. We implement them independently
here for CLI/automation use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

BOOTSTRAP_FILES = [
    "SOUL.md", "AGENTS.md", "MEMORY.md", "IDENTITY.md",
    "TOOLS.md", "USER.md", "HEARTBEAT.md", "BOOTSTRAP.md",
]
PER_FILE_LIMIT = 20_000
TOTAL_BUDGET = 150_000
SNAPSHOT_FILE = ".claw-drift-snapshots.json"

# Cross-file contradiction patterns — edit to taste for your ops.
CONTRADICTIONS = [
    {
        "label": "Model primary contradiction",
        "a": re.compile(r"primary\s*[:=]\s*['\"]?minimax", re.I),
        "b": re.compile(r"primary\s*[:=]\s*['\"]?(claude|opus|anthropic)", re.I),
        "note": "Two files name different primary models.",
    },
    {
        "label": "Fork vs no-fork ambiguity",
        "a": re.compile(r"\bNO\s+FORK|don'?t\s+fork", re.I),
        "b": re.compile(r"we will fork|let'?s fork|plan to fork", re.I),
        "note": "Fork intent is ambiguous across files.",
    },
    {
        "label": "Naming drift for customer UI",
        "a": re.compile(r"customer[- ]facing\s+(\"|')?Heartbeat", re.I),
        "b": re.compile(r"Heartbeat\s+→\s+(Pulse|Hide)", re.I),
        "note": "Heartbeat naming policy not consistent.",
    },
    {
        "label": "Time estimate leak",
        "a": re.compile(r"no\s+time\s+estimates", re.I),
        "b": re.compile(r"by\s+(April|May|June|July|\d+\s+(days|weeks|months))", re.I),
        "note": "Rule forbids time estimates but one file contains one.",
    },
]


@dataclass
class FileReport:
    name: str
    path: str
    exists: bool
    size: int
    status: str                         # healthy / missing / bloated / at-risk / truncated
    percent_of_limit: int
    headings: dict = field(default_factory=dict)
    truncation_preview: Optional[dict] = None


@dataclass
class Report:
    generated_at: str
    total_chars: int
    budget_percent: int
    budget_status: str                  # healthy / bloated / at-risk / over-budget
    per_file: list
    contradictions: list
    drift: Optional[list]
    since_snapshot: Optional[str]


def analyze(workspace: Path, snapshots_path: Path) -> Report:
    from datetime import datetime, timezone

    per_file = []
    total = 0

    for name in BOOTSTRAP_FILES:
        p = workspace / name
        content = None
        size = 0
        exists = False
        try:
            if p.exists():
                content = p.read_text(encoding="utf-8", errors="replace")
                size = len(content)
                exists = True
                total += size
        except Exception:
            pass

        headings = {"h1": 0, "h2": 0, "h3": 0}
        if content:
            for line in content.splitlines():
                if line.startswith("# "):
                    headings["h1"] += 1
                elif line.startswith("## "):
                    headings["h2"] += 1
                elif line.startswith("### "):
                    headings["h3"] += 1

        preview = None
        if size > PER_FILE_LIMIT and content:
            head_size = int(PER_FILE_LIMIT * 0.7)
            tail_size = int(PER_FILE_LIMIT * 0.2)
            preview = {
                "head_bytes": head_size,
                "tail_bytes": tail_size,
                "lost_bytes": size - (head_size + tail_size),
                "lost_percent": round((size - (head_size + tail_size)) / size * 100),
            }

        if not exists:
            status = "missing"
        elif size > PER_FILE_LIMIT:
            status = "truncated"
        elif size > PER_FILE_LIMIT * 0.9:
            status = "at-risk"
        elif size > PER_FILE_LIMIT * 0.75:
            status = "bloated"
        else:
            status = "healthy"

        per_file.append(FileReport(
            name=name, path=str(p), exists=exists, size=size,
            status=status,
            percent_of_limit=round(size / PER_FILE_LIMIT * 100) if size else 0,
            headings=headings, truncation_preview=preview,
        ))

    budget_pct = round(total / TOTAL_BUDGET * 100)
    if total > TOTAL_BUDGET:
        budget_status = "over-budget"
    elif total > TOTAL_BUDGET * 0.9:
        budget_status = "at-risk"
    elif total > TOTAL_BUDGET * 0.75:
        budget_status = "bloated"
    else:
        budget_status = "healthy"

    # Contradictions — read full text of each file once
    texts = {}
    for f in per_file:
        if f.exists:
            try:
                texts[f.name] = Path(f.path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    contradictions = []
    for rule in CONTRADICTIONS:
        a_hits = [n for n, t in texts.items() if rule["a"].search(t)]
        b_hits = [n for n, t in texts.items() if rule["b"].search(t)]
        if a_hits and b_hits:
            contradictions.append({
                "label": rule["label"],
                "note": rule["note"],
                "side_a_files": a_hits,
                "side_b_files": b_hits,
            })

    # Drift — compare against last snapshot
    drift = None
    since = None
    if snapshots_path.exists():
        try:
            snaps = json.loads(snapshots_path.read_text())
            history = snaps.get("history", [])
            if history:
                last = history[-1]
                since = last.get("generated_at")
                last_files = {f["name"]: f for f in last.get("per_file", [])}
                drift = []
                for f in per_file:
                    prior = last_files.get(f.name)
                    if not prior:
                        drift.append({"name": f.name, "new_file": True})
                        continue
                    drift.append({
                        "name": f.name,
                        "size_before": prior["size"],
                        "size_after": f.size,
                        "delta": f.size - prior["size"],
                        "headings_changed": prior.get("headings") != f.headings,
                    })
        except Exception:
            pass

    return Report(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_chars=total,
        budget_percent=budget_pct,
        budget_status=budget_status,
        per_file=[asdict(f) for f in per_file],
        contradictions=contradictions,
        drift=drift,
        since_snapshot=since,
    )


def save_snapshot(snapshots_path: Path, report: Report) -> None:
    data = {"history": []}
    if snapshots_path.exists():
        try:
            data = json.loads(snapshots_path.read_text())
        except Exception:
            pass
    data.setdefault("history", []).append({
        "generated_at": report.generated_at,
        "total_chars": report.total_chars,
        "per_file": [
            {"name": f["name"], "size": f["size"], "headings": f["headings"], "status": f["status"]}
            for f in report.per_file
        ],
    })
    data["history"] = data["history"][-20:]
    snapshots_path.write_text(json.dumps(data, indent=2))


def _color(s: str, code: str) -> str:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def print_human(report: Report, quiet: bool) -> None:
    def dot(status: str) -> str:
        return {
            "healthy": _color("●", "32"),
            "bloated": _color("●", "33"),
            "at-risk": _color("●", "33"),
            "truncated": _color("●", "31"),
            "missing": _color("○", "90"),
        }.get(status, "·")

    if not quiet:
        print("claw-drift — scanned", len(report.per_file), "bootstrap files")
        print(f"  inspired by DanAndBub/Driftwatch (github.com/DanAndBub/Driftwatch)")
        print(f"  generated:  {report.generated_at}")
        print(f"  total bytes: {report.total_chars:,} / {TOTAL_BUDGET:,}  "
              f"({report.budget_percent}% — {report.budget_status})")
        print()

    for f in report.per_file:
        status = f["status"]
        if quiet and status in ("healthy", "missing"):
            continue
        line = f" {dot(status)}  {f['name']:<14} {f['size']:>7,}  ({f['percent_of_limit']:>3}%)  {status}"
        if f.get("truncation_preview"):
            line += f"  → LOSES {f['truncation_preview']['lost_percent']}% of content"
        print(line)

    if report.contradictions:
        print()
        print("Contradictions found:")
        for c in report.contradictions:
            print(f"  ⚠ {c['label']} — {c['note']}")
            print(f"    side A: {', '.join(c['side_a_files'])}")
            print(f"    side B: {', '.join(c['side_b_files'])}")

    if report.drift:
        deltas = [d for d in report.drift if d.get("delta") and abs(d["delta"]) > 100]
        if deltas:
            print()
            print(f"Drift since {report.since_snapshot}:")
            for d in deltas:
                sign = "+" if d["delta"] > 0 else ""
                print(f"  {d['name']}: {sign}{d['delta']} bytes")


def determine_exit_code(report: Report) -> int:
    if any(f["status"] == "truncated" for f in report.per_file):
        return 2
    if report.budget_status == "over-budget":
        return 2
    if report.contradictions:
        return 2
    if any(f["status"] in ("bloated", "at-risk") for f in report.per_file):
        return 1
    if report.budget_status in ("bloated", "at-risk"):
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="claw-drift — bootstrap file sanity scanner (inspired by DanAndBub/Driftwatch)")
    p.add_argument("--workspace", default=os.getcwd(), help="Workspace directory containing bootstrap files")
    p.add_argument("--snapshots", default=None, help="Path to snapshots JSON (default: <workspace>/.claw-drift-snapshots.json)")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p.add_argument("--snapshot", action="store_true", help="Persist snapshot for drift tracking")
    p.add_argument("--quiet", action="store_true", help="Only print warning / fail lines")
    p.add_argument("--exit-nonzero", action="store_true", help="Exit 1/2 on issues (for CI)")
    args = p.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    snapshots_path = Path(args.snapshots) if args.snapshots else workspace / SNAPSHOT_FILE

    report = analyze(workspace, snapshots_path)
    if args.snapshot:
        save_snapshot(snapshots_path, report)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print_human(report, quiet=args.quiet)

    return determine_exit_code(report) if args.exit_nonzero else 0


if __name__ == "__main__":
    sys.exit(main())
