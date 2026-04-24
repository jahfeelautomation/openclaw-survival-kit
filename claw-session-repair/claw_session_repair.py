#!/usr/bin/env python3
"""
claw-session-repair -- Clean up orphaned tool_result entries, malformed session
data, and stale transcripts that accumulate during OpenClaw crashes.

Scans session directories for context/transcript files, detects corruption and
bloat, and optionally repairs them.

Usage:
  python claw_session_repair.py                       # diagnose (read-only)
  python claw_session_repair.py --json                # machine-readable output
  python claw_session_repair.py --fix                 # apply repairs
  python claw_session_repair.py --max-age 14          # flag sessions older than 14 days
  python claw_session_repair.py --sessions-dir PATH   # custom sessions path

Exit codes:
  0 -- all sessions healthy
  1 -- one or more WARN (stale sessions, minor issues)
  2 -- one or more FAIL (orphaned entries, malformed data, critical size)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------- constants ----------

VERSION = "1.0"
OPENCLAW_DIR = Path.home() / ".openclaw"
DEFAULT_SESSIONS_DIR = OPENCLAW_DIR / "sessions"

SIZE_WARN_BYTES = 5 * 1024 * 1024    # 5 MB
SIZE_CRIT_BYTES = 20 * 1024 * 1024   # 20 MB
DEFAULT_MAX_AGE_DAYS = 7

# Severity
SEV_OK = "OK"
SEV_WARN = "WARN"
SEV_FAIL = "FAIL"


# ---------- data types ----------

@dataclass
class FileIssue:
    file: str
    issue_type: str  # orphaned_tool_result, malformed_json, oversized, duplicate
    severity: str
    detail: str
    line_numbers: list[int] = field(default_factory=list)
    fixed: bool = False


@dataclass
class SessionReport:
    session_id: str
    path: str
    status: str = SEV_OK
    last_activity: Optional[str] = None
    last_activity_epoch: Optional[float] = None
    total_size_bytes: int = 0
    file_count: int = 0
    issues: list[FileIssue] = field(default_factory=list)
    stale: bool = False
    archived: bool = False

    def worst_severity(self) -> str:
        if any(i.severity == SEV_FAIL for i in self.issues):
            return SEV_FAIL
        if any(i.severity == SEV_WARN for i in self.issues) or self.stale:
            return SEV_WARN
        return SEV_OK


@dataclass
class RepairReport:
    sessions_dir: str = ""
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    sessions: list[SessionReport] = field(default_factory=list)
    total_issues: int = 0
    total_fixed: int = 0

    @property
    def exit_code(self) -> int:
        for s in self.sessions:
            if s.worst_severity() == SEV_FAIL:
                return 2
        for s in self.sessions:
            if s.worst_severity() == SEV_WARN:
                return 1
        return 0


# ---------- analysis ----------

def analyze_jsonl(filepath: Path) -> list[FileIssue]:
    """Analyze a JSONL transcript file for issues."""
    issues = []
    file_str = str(filepath)

    try:
        content = filepath.read_bytes()
    except OSError as e:
        issues.append(FileIssue(
            file=file_str, issue_type="read_error", severity=SEV_FAIL,
            detail=f"Cannot read file: {e}",
        ))
        return issues

    # Size checks
    size = len(content)
    if size > SIZE_CRIT_BYTES:
        issues.append(FileIssue(
            file=file_str, issue_type="oversized", severity=SEV_FAIL,
            detail=f"File is {size / 1024 / 1024:.1f} MB (critical threshold: {SIZE_CRIT_BYTES / 1024 / 1024:.0f} MB)",
        ))
    elif size > SIZE_WARN_BYTES:
        issues.append(FileIssue(
            file=file_str, issue_type="oversized", severity=SEV_WARN,
            detail=f"File is {size / 1024 / 1024:.1f} MB (warning threshold: {SIZE_WARN_BYTES / 1024 / 1024:.0f} MB)",
        ))

    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        text = content.decode("latin-1")

    lines = text.splitlines()

    # Parse each line, track tool_use / tool_result pairing
    parsed_entries = []
    malformed_lines = []
    tool_use_ids = set()
    tool_result_ids = set()
    seen_hashes = {}
    duplicate_lines = []

    for i, line in enumerate(lines, start=1):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        try:
            entry = json.loads(line_stripped)
            parsed_entries.append((i, entry))
        except json.JSONDecodeError:
            malformed_lines.append(i)
            continue

        if not isinstance(entry, dict):
            continue

        # Track tool_use / tool_result
        entry_type = entry.get("type") or entry.get("role")
        if entry_type == "tool_use" or (
            isinstance(entry.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in entry.get("content", [])
            )
        ):
            # Extract tool_use id(s)
            if "id" in entry:
                tool_use_ids.add(entry["id"])
            for block in entry.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use" and "id" in block:
                    tool_use_ids.add(block["id"])

        if entry_type == "tool_result" or (
            isinstance(entry.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in entry.get("content", [])
            )
        ):
            tid = entry.get("tool_use_id")
            if tid:
                tool_result_ids.add(tid)
            for block in entry.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        tool_result_ids.add(tid)

        # Duplicate detection: same timestamp + content hash
        ts = entry.get("timestamp") or entry.get("ts")
        content_key = entry.get("content")
        if ts is not None and content_key is not None:
            dedup_key = f"{ts}:{hash(str(content_key))}"
            if dedup_key in seen_hashes:
                duplicate_lines.append(i)
            else:
                seen_hashes[dedup_key] = i

    if malformed_lines:
        issues.append(FileIssue(
            file=file_str, issue_type="malformed_json", severity=SEV_FAIL,
            detail=f"{len(malformed_lines)} malformed JSON line(s)",
            line_numbers=malformed_lines[:20],  # cap reported lines
        ))

    # Orphaned tool_results: have a tool_use_id that doesn't match any tool_use
    orphaned = tool_result_ids - tool_use_ids
    if orphaned:
        issues.append(FileIssue(
            file=file_str, issue_type="orphaned_tool_result", severity=SEV_FAIL,
            detail=f"{len(orphaned)} tool_result(s) without matching tool_use",
        ))

    if duplicate_lines:
        issues.append(FileIssue(
            file=file_str, issue_type="duplicate", severity=SEV_WARN,
            detail=f"{len(duplicate_lines)} duplicate entries (same timestamp + content)",
            line_numbers=duplicate_lines[:20],
        ))

    return issues


def analyze_json_file(filepath: Path) -> list[FileIssue]:
    """Analyze a JSON (non-JSONL) session file."""
    issues = []
    file_str = str(filepath)

    try:
        content = filepath.read_bytes()
    except OSError as e:
        issues.append(FileIssue(
            file=file_str, issue_type="read_error", severity=SEV_FAIL,
            detail=f"Cannot read file: {e}",
        ))
        return issues

    size = len(content)
    if size > SIZE_CRIT_BYTES:
        issues.append(FileIssue(
            file=file_str, issue_type="oversized", severity=SEV_FAIL,
            detail=f"File is {size / 1024 / 1024:.1f} MB (critical threshold: {SIZE_CRIT_BYTES / 1024 / 1024:.0f} MB)",
        ))
    elif size > SIZE_WARN_BYTES:
        issues.append(FileIssue(
            file=file_str, issue_type="oversized", severity=SEV_WARN,
            detail=f"File is {size / 1024 / 1024:.1f} MB (warning threshold: {SIZE_WARN_BYTES / 1024 / 1024:.0f} MB)",
        ))

    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        issues.append(FileIssue(
            file=file_str, issue_type="malformed_json", severity=SEV_FAIL,
            detail=f"Invalid JSON: {e}",
        ))

    return issues


def scan_session(session_dir: Path, max_age_days: int) -> SessionReport:
    """Analyze a single session directory."""
    sr = SessionReport(
        session_id=session_dir.name,
        path=str(session_dir),
    )

    # Gather all relevant files
    data_files = []
    for ext in ("*.jsonl", "*.json"):
        data_files.extend(session_dir.glob(ext))
    # Also check subdirectories one level deep
    for sub in session_dir.iterdir():
        if sub.is_dir():
            for ext in ("*.jsonl", "*.json"):
                data_files.extend(sub.glob(ext))

    sr.file_count = len(data_files)

    # Track last modification time
    latest_mtime = 0.0
    for f in data_files:
        try:
            st = f.stat()
            sr.total_size_bytes += st.st_size
            if st.st_mtime > latest_mtime:
                latest_mtime = st.st_mtime
        except OSError:
            continue

    if latest_mtime > 0:
        sr.last_activity_epoch = latest_mtime
        sr.last_activity = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))

        age_days = (time.time() - latest_mtime) / 86400
        if age_days > max_age_days:
            sr.stale = True

    # Analyze each file
    for f in data_files:
        if f.suffix == ".jsonl":
            sr.issues.extend(analyze_jsonl(f))
        elif f.suffix == ".json":
            sr.issues.extend(analyze_json_file(f))

    sr.status = sr.worst_severity()
    return sr


def scan_all_sessions(sessions_dir: Path, max_age_days: int) -> RepairReport:
    """Scan all session directories."""
    report = RepairReport(
        sessions_dir=str(sessions_dir),
        max_age_days=max_age_days,
    )

    if not sessions_dir.exists():
        return report

    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip hidden directories
        if entry.name.startswith("."):
            continue
        sr = scan_session(entry, max_age_days)
        report.sessions.append(sr)

    report.total_issues = sum(len(s.issues) for s in report.sessions)
    return report


# ---------- fix mode ----------

def fix_jsonl(filepath: Path) -> tuple[int, int]:
    """
    Repair a JSONL file:
    - Remove malformed lines
    - Remove orphaned tool_result entries
    - Remove exact duplicates
    Returns (lines_removed, lines_kept).
    """
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0

    lines = text.splitlines()
    if not lines:
        return 0, 0

    # First pass: parse and identify tool_use IDs
    parsed = []
    tool_use_ids = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
            parsed.append((stripped, entry))
        except json.JSONDecodeError:
            parsed.append((stripped, None))  # malformed
            continue

        if isinstance(entry, dict):
            if "id" in entry and (entry.get("type") == "tool_use"):
                tool_use_ids.add(entry["id"])
            for block in entry.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use" and "id" in block:
                    tool_use_ids.add(block["id"])

    # Second pass: filter
    kept = []
    seen_hashes = set()
    removed = 0

    for raw, entry in parsed:
        # Skip malformed
        if entry is None:
            removed += 1
            continue

        # Skip orphaned tool_results
        if isinstance(entry, dict):
            tid = entry.get("tool_use_id")
            is_tool_result = entry.get("type") == "tool_result" or any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in entry.get("content", [])
            )
            if is_tool_result and tid and tid not in tool_use_ids:
                removed += 1
                continue

        # Skip duplicates
        line_hash = hash(raw)
        ts = entry.get("timestamp") or entry.get("ts") if isinstance(entry, dict) else None
        dedup_key = f"{ts}:{line_hash}" if ts else str(line_hash)
        if dedup_key in seen_hashes:
            removed += 1
            continue
        seen_hashes.add(dedup_key)

        kept.append(raw)

    if removed > 0:
        # Write backup
        backup = filepath.with_suffix(filepath.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(filepath, backup)
        filepath.write_text("\n".join(kept) + "\n", encoding="utf-8")

    return removed, len(kept)


def archive_session(session_dir: Path, sessions_dir: Path) -> bool:
    """Move a stale session to an _archive subdirectory."""
    archive_dir = sessions_dir / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / session_dir.name
    if dest.exists():
        # Add timestamp suffix to avoid collision
        dest = archive_dir / f"{session_dir.name}_{int(time.time())}"
    try:
        shutil.move(str(session_dir), str(dest))
        return True
    except OSError:
        return False


def apply_fixes(report: RepairReport) -> int:
    """Apply fixes to all sessions with issues. Returns count of fixes."""
    fixes = 0
    sessions_dir = Path(report.sessions_dir)

    for sr in report.sessions:
        # Fix JSONL files with issues
        for issue in sr.issues:
            if issue.issue_type in ("malformed_json", "orphaned_tool_result", "duplicate"):
                fp = Path(issue.file)
                if fp.suffix == ".jsonl" and fp.exists():
                    removed, kept = fix_jsonl(fp)
                    if removed > 0:
                        issue.fixed = True
                        issue.detail += f" [FIXED: removed {removed}, kept {kept}]"
                        fixes += 1

        # Archive stale sessions
        if sr.stale:
            session_path = Path(sr.path)
            if session_path.exists():
                if archive_session(session_path, sessions_dir):
                    sr.archived = True
                    fixes += 1

    report.total_fixed = fixes
    return fixes


# ---------- output ----------

def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


SEV_COLORS = {SEV_OK: "32;1", SEV_WARN: "33;1", SEV_FAIL: "31;1"}


def print_report(report: RepairReport) -> None:
    print(f"Sessions dir: {report.sessions_dir}")
    print(f"Max age:      {report.max_age_days} days")
    print(f"Sessions:     {len(report.sessions)}")
    print()

    if not report.sessions:
        print("No sessions found.")
        return

    for sr in report.sessions:
        sev = sr.worst_severity()
        color = SEV_COLORS[sev]
        label = _color(f"[{sev:>4s}]", color)
        size_mb = sr.total_size_bytes / 1024 / 1024
        stale_tag = _color(" [STALE]", "33;1") if sr.stale else ""
        archived_tag = _color(" [ARCHIVED]", "36;1") if sr.archived else ""

        print(f"{label} {sr.session_id}{stale_tag}{archived_tag}")
        print(f"       Files: {sr.file_count}   Size: {size_mb:.2f} MB   Last activity: {sr.last_activity or 'unknown'}")

        for issue in sr.issues:
            issue_color = SEV_COLORS[issue.severity]
            fixed_tag = _color(" (fixed)", "32;1") if issue.fixed else ""
            print(f"       - [{_color(issue.severity, issue_color)}] {issue.issue_type}: {issue.detail}{fixed_tag}")
            if issue.line_numbers:
                lines_str = ", ".join(str(n) for n in issue.line_numbers[:10])
                if len(issue.line_numbers) > 10:
                    lines_str += f" ... (+{len(issue.line_numbers) - 10} more)"
                print(f"         Lines: {lines_str}")
        print()

    # Summary
    counts = {SEV_OK: 0, SEV_WARN: 0, SEV_FAIL: 0}
    for sr in report.sessions:
        counts[sr.worst_severity()] += 1
    stale_count = sum(1 for s in report.sessions if s.stale)

    print(
        f"Summary: {_color(str(counts[SEV_OK]), '32;1')} ok, "
        f"{_color(str(counts[SEV_WARN]), '33;1')} warn, "
        f"{_color(str(counts[SEV_FAIL]), '31;1')} fail"
        + (f"   ({stale_count} stale)" if stale_count else "")
    )
    if report.total_issues:
        print(f"Total issues: {report.total_issues}", end="")
        if report.total_fixed:
            print(f"   Fixed: {report.total_fixed}", end="")
        print()


# ---------- CLI ----------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claw-session-repair",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR),
                        help=f"path to sessions directory (default: {DEFAULT_SESSIONS_DIR})")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--fix", action="store_true",
                        help="remove orphaned entries, fix malformed JSON, archive stale sessions")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"flag sessions with no activity in N days (default: {DEFAULT_MAX_AGE_DAYS})")
    parser.add_argument("--version", action="version", version=f"claw-session-repair {VERSION}")
    args = parser.parse_args(argv)

    sessions_dir = Path(args.sessions_dir)
    report = scan_all_sessions(sessions_dir, args.max_age)

    if args.fix:
        fixes = apply_fixes(report)
        if not args.json:
            print(f"Applied {fixes} fix(es).\n")

    if args.json:
        out = {
            "sessions_dir": report.sessions_dir,
            "max_age_days": report.max_age_days,
            "total_sessions": len(report.sessions),
            "total_issues": report.total_issues,
            "total_fixed": report.total_fixed,
            "sessions": [asdict(s) for s in report.sessions],
            "exit_code": report.exit_code,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(report)

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
