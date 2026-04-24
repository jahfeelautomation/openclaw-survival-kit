#!/usr/bin/env python3
"""
claw-skills-lint -- Validate OpenClaw workspace skills configuration.

Detects skills that appear enabled in config but aren't loading, skills with
missing dependencies, unregistered skills on disk, and broken SKILL.md files.

Usage:
  python claw_skills_lint.py                          # diagnose (read-only)
  python claw_skills_lint.py --json                   # machine-readable output
  python claw_skills_lint.py --fix                    # create stubs / register missing
  python claw_skills_lint.py --config PATH            # custom config path
  python claw_skills_lint.py --skills-dir PATH        # custom skills directory

Exit codes:
  0 -- all skills OK
  1 -- one or more WARN (UNREGISTERED skills, minor issues)
  2 -- one or more FAIL (MISSING or BROKEN skills)
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


# ---------- constants ----------

VERSION = "1.0"
OPENCLAW_DIR = Path.home() / ".openclaw"
DEFAULT_CONFIG = OPENCLAW_DIR / "openclaw.json"
DEFAULT_SKILLS_DIR = OPENCLAW_DIR / "workspace" / "agents" / "skills"

# Statuses
STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_BROKEN = "BROKEN"
STATUS_UNREGISTERED = "UNREGISTERED"


# ---------- data types ----------

@dataclass
class SkillReport:
    name: str
    status: str
    path: Optional[str] = None
    description: Optional[str] = None
    issues: list[str] = field(default_factory=list)
    fixed: bool = False


@dataclass
class LintReport:
    config_path: str = ""
    skills_dir: str = ""
    total_config: int = 0
    total_disk: int = 0
    skills: list[SkillReport] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        statuses = {s.status for s in self.skills}
        if STATUS_MISSING in statuses or STATUS_BROKEN in statuses:
            return 2
        if STATUS_UNREGISTERED in statuses:
            return 1
        return 0


# ---------- YAML frontmatter parser (stdlib only) ----------

def parse_yaml_frontmatter(text: str) -> dict:
    """
    Parse simple YAML frontmatter delimited by --- lines.
    Handles key: value pairs only (no nested structures).
    Returns empty dict if no frontmatter found.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}

    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}

    result = {}
    for line in lines[1:end]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)", line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            # Strip surrounding quotes
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            # Handle lists in bracket notation: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            result[key] = val
    return result


# ---------- core logic ----------

def load_config_skills(config_path: Path) -> dict:
    """
    Read skills configuration from openclaw.json.
    Expected structure: { "skills": { "enabled": ["skill-name", ...] } }
    or { "skills": { "skill-name": { "enabled": true, ... }, ... } }
    Returns dict mapping skill_name -> config_entry (dict or True).
    """
    if not config_path.exists():
        return {}

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    skills_section = cfg.get("skills", {})
    if not skills_section:
        return {}

    result = {}

    # Format 1: list of enabled skill names
    enabled_list = skills_section.get("enabled", [])
    if isinstance(enabled_list, list):
        for name in enabled_list:
            if isinstance(name, str):
                result[name] = {"enabled": True}

    # Format 2: dict of skill_name -> config
    for key, val in skills_section.items():
        if key == "enabled":
            continue
        if isinstance(val, dict):
            result[key] = val
        elif isinstance(val, bool) and val:
            result[key] = {"enabled": True}

    return result


def scan_skills_dir(skills_dir: Path) -> dict:
    """
    Scan the skills directory for subdirectories containing SKILL.md.
    Returns dict mapping skill_name -> Path to SKILL.md.
    """
    found = {}
    if not skills_dir.exists():
        return found

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            found[entry.name] = skill_md
        else:
            # Directory exists but no SKILL.md -- still counts as a skill dir
            found[entry.name] = None
    return found


def lint_skill_md(skill_md: Path) -> tuple[dict, list[str]]:
    """
    Validate a SKILL.md file. Returns (frontmatter_dict, list_of_issues).
    """
    issues = []

    if not skill_md.exists():
        issues.append("SKILL.md file does not exist")
        return {}, issues

    try:
        readable = os.access(str(skill_md), os.R_OK)
    except OSError:
        readable = False
    if not readable:
        issues.append("SKILL.md is not readable (permission denied)")
        return {}, issues

    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        issues.append(f"Cannot read SKILL.md: {e}")
        return {}, issues

    if not content.strip():
        issues.append("SKILL.md is empty")
        return {}, issues

    if len(content) < 10:
        issues.append(f"SKILL.md is suspiciously short ({len(content)} chars)")

    fm = parse_yaml_frontmatter(content)

    if not fm:
        issues.append("No YAML frontmatter found (expected --- delimited block)")

    if fm and not fm.get("name"):
        issues.append("Frontmatter missing 'name' field")

    if fm and not fm.get("description"):
        issues.append("Frontmatter missing 'description' field")

    # Check for required_tools / dependencies
    deps = fm.get("required_tools") or fm.get("dependencies") or []
    if isinstance(deps, str):
        deps = [deps]
    if isinstance(deps, list):
        for dep in deps:
            if isinstance(dep, str) and dep.strip():
                # Check if the dependency looks like a file path and verify it
                dep_path = skill_md.parent / dep
                if "/" in dep or "\\" in dep or dep.endswith((".py", ".sh", ".js")):
                    if not dep_path.exists():
                        issues.append(f"Required dependency not found: {dep}")

    return fm, issues


def run_lint(config_path: Path, skills_dir: Path) -> LintReport:
    """Run the full lint pass and return a report."""
    report = LintReport(
        config_path=str(config_path),
        skills_dir=str(skills_dir),
    )

    config_skills = load_config_skills(config_path)
    disk_skills = scan_skills_dir(skills_dir)

    report.total_config = len(config_skills)
    report.total_disk = len(disk_skills)

    all_names = sorted(set(list(config_skills.keys()) + list(disk_skills.keys())))

    for name in all_names:
        in_config = name in config_skills
        in_disk = name in disk_skills

        if in_config and not in_disk:
            # MISSING: configured but not on disk
            report.skills.append(SkillReport(
                name=name,
                status=STATUS_MISSING,
                issues=["Skill is in config but has no directory on disk"],
            ))
            continue

        if in_disk and not in_config:
            # UNREGISTERED: on disk but not in config
            skill_md = disk_skills[name]
            sr = SkillReport(
                name=name,
                status=STATUS_UNREGISTERED,
                issues=["Skill directory exists on disk but is not listed in config"],
            )
            if skill_md and skill_md.exists():
                sr.path = str(skill_md)
                fm, extra_issues = lint_skill_md(skill_md)
                sr.description = fm.get("description")
                sr.issues.extend(extra_issues)
            report.skills.append(sr)
            continue

        # Both in config and on disk
        skill_md = disk_skills[name]
        sr = SkillReport(name=name, status=STATUS_OK)

        if skill_md is None:
            # Directory exists but no SKILL.md
            sr.status = STATUS_BROKEN
            sr.issues.append("Skill directory exists but SKILL.md is missing")
            sr.path = str(skills_dir / name)
        else:
            sr.path = str(skill_md)
            fm, issues = lint_skill_md(skill_md)
            sr.description = fm.get("description")
            if issues:
                # Determine severity
                critical = any(
                    "empty" in i.lower() or "not readable" in i.lower() or "cannot read" in i.lower()
                    for i in issues
                )
                sr.status = STATUS_BROKEN if critical else STATUS_OK
                sr.issues = issues

        report.skills.append(sr)

    return report


# ---------- fix mode ----------

def apply_fixes(report: LintReport, config_path: Path, skills_dir: Path) -> int:
    """
    Fix mode:
    - MISSING: create skill directory + stub SKILL.md
    - UNREGISTERED: add to config's skills.enabled list
    Returns count of fixes applied.
    """
    fixes = 0

    for skill in report.skills:
        if skill.status == STATUS_MISSING:
            # Create stub SKILL.md
            skill_dir = skills_dir / skill.name
            skill_dir.mkdir(parents=True, exist_ok=True)
            stub = skill_dir / "SKILL.md"
            stub.write_text(
                f"---\n"
                f"name: {skill.name}\n"
                f"description: Auto-generated stub by claw-skills-lint\n"
                f"---\n\n"
                f"# {skill.name}\n\n"
                f"TODO: Add skill documentation here.\n",
                encoding="utf-8",
            )
            skill.fixed = True
            skill.issues.append(f"FIX: Created stub SKILL.md at {stub}")
            fixes += 1

        elif skill.status == STATUS_UNREGISTERED:
            # Register in config
            try:
                if config_path.exists():
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                else:
                    cfg = {}
            except (json.JSONDecodeError, OSError):
                cfg = {}

            skills_section = cfg.setdefault("skills", {})
            enabled = skills_section.setdefault("enabled", [])
            if isinstance(enabled, list) and skill.name not in enabled:
                enabled.append(skill.name)
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                skill.fixed = True
                skill.issues.append(f"FIX: Added '{skill.name}' to config skills.enabled")
                fixes += 1

    return fixes


# ---------- output ----------

def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


STATUS_COLORS = {
    STATUS_OK: "32;1",
    STATUS_MISSING: "31;1",
    STATUS_BROKEN: "31;1",
    STATUS_UNREGISTERED: "33;1",
}


def print_report(report: LintReport) -> None:
    print(f"Config:     {report.config_path}")
    print(f"Skills dir: {report.skills_dir}")
    print(f"In config:  {report.total_config}   On disk: {report.total_disk}")
    print()

    if not report.skills:
        print("No skills found in config or on disk.")
        return

    for s in report.skills:
        color = STATUS_COLORS.get(s.status, "0")
        label = _color(f"[{s.status:>12s}]", color)
        print(f"{label} {s.name}")
        if s.description:
            print(f"               {s.description}")
        for issue in s.issues:
            print(f"               - {issue}")
        if s.fixed:
            print(f"               {_color('(fixed)', '32;1')}")
        print()

    counts = {}
    for s in report.skills:
        counts[s.status] = counts.get(s.status, 0) + 1
    parts = []
    for status in [STATUS_OK, STATUS_UNREGISTERED, STATUS_MISSING, STATUS_BROKEN]:
        if status in counts:
            parts.append(f"{_color(str(counts[status]), STATUS_COLORS[status])} {status.lower()}")
    print(f"Summary: {', '.join(parts)}")


# ---------- CLI ----------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claw-skills-lint",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"path to openclaw.json (default: {DEFAULT_CONFIG})")
    parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR),
                        help=f"path to skills directory (default: {DEFAULT_SKILLS_DIR})")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--fix", action="store_true",
                        help="create stubs for MISSING skills, register UNREGISTERED ones")
    parser.add_argument("--version", action="version", version=f"claw-skills-lint {VERSION}")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    skills_dir = Path(args.skills_dir)

    report = run_lint(config_path, skills_dir)

    if args.fix:
        fixes = apply_fixes(report, config_path, skills_dir)
        if not args.json:
            print(f"Applied {fixes} fix(es).\n")
        # Re-lint after fixes
        report = run_lint(config_path, skills_dir)

    if args.json:
        out = {
            "config_path": report.config_path,
            "skills_dir": report.skills_dir,
            "total_config": report.total_config,
            "total_disk": report.total_disk,
            "skills": [asdict(s) for s in report.skills],
            "exit_code": report.exit_code,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(report)

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
