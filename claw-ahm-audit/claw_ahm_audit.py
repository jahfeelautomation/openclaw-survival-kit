#!/usr/bin/env python3
"""
claw-ahm-audit — AHM (Agent Headquarters Methodology) Diagnostic

Scans an OpenClaw workspace and flags anti-patterns based on AHM best practices.
Think of it as a linter for your agent setup.

Usage:
    python claw_ahm_audit.py [workspace_path]
    python claw_ahm_audit.py                     # uses current directory
    python claw_ahm_audit.py /path/to/workspace

Output: colored terminal report with PASS/WARN/FAIL for each check.
"""

import os
import sys
import json
import glob
from datetime import datetime, timedelta
from pathlib import Path


# ── Colors ──────────────────────────────────────────────────────────────────

class C:
    PASS = "\033[92m"   # green
    WARN = "\033[93m"   # yellow
    FAIL = "\033[91m"   # red
    INFO = "\033[96m"   # cyan
    BOLD = "\033[1m"
    END = "\033[0m"


def pass_msg(msg):
    return f"  {C.PASS}✓ PASS{C.END}  {msg}"

def warn_msg(msg):
    return f"  {C.WARN}⚠ WARN{C.END}  {msg}"

def fail_msg(msg):
    return f"  {C.FAIL}✗ FAIL{C.END}  {msg}"

def info_msg(msg):
    return f"  {C.INFO}ℹ INFO{C.END}  {msg}"


# ── Checks ──────────────────────────────────────────────────────────────────

def check_bootstrap_files(ws):
    """Check that all required bootstrap files exist and are within budget."""
    results = []
    required = [
        "IDENTITY.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md",
        "BUILD_LOOP.md", "TOOLS.md", "MEMORY.md", "USER.md"
    ]
    recommended = ["BACKLOG.md"]

    total_chars = 0
    for f in required:
        path = ws / f
        if path.exists():
            size = path.stat().st_size
            total_chars += size
            if size > 20000:
                results.append(fail_msg(f"{f} exists but is {size:,} chars (budget: 20,000)"))
            else:
                results.append(pass_msg(f"{f} exists ({size:,} chars)"))
        else:
            results.append(fail_msg(f"{f} is MISSING — required bootstrap file"))

    for f in recommended:
        path = ws / f
        if path.exists():
            results.append(pass_msg(f"{f} exists (recommended)"))
        else:
            results.append(warn_msg(f"{f} not found — agents may go idle without a backlog"))

    if total_chars > 150000:
        results.append(fail_msg(f"Total bootstrap budget: {total_chars:,} chars (limit: 150,000)"))
    else:
        results.append(pass_msg(f"Total bootstrap budget: {total_chars:,} / 150,000 chars"))

    return results


def check_backlog_pattern(ws):
    """Check for BACKLOG.md and proper structure."""
    results = []
    backlog = ws / "BACKLOG.md"

    if not backlog.exists():
        results.append(fail_msg("No BACKLOG.md — agent will idle when BUILD_LOOP is empty"))
        results.append(info_msg("Create BACKLOG.md with recurring daily tasks (content, QA, research)"))
        return results

    content = backlog.read_text(encoding="utf-8", errors="replace")

    if "RECURRING" in content.upper() or "DAILY" in content.upper():
        results.append(pass_msg("BACKLOG.md has recurring/daily section"))
    else:
        results.append(warn_msg("BACKLOG.md has no recurring section — may deplete"))

    if "skill" in content.lower() or "SKILL.md" in content:
        results.append(pass_msg("BACKLOG items link to skills"))
    else:
        results.append(warn_msg("BACKLOG items don't reference skills — agents won't know HOW to do the work"))

    return results


def check_cron_skill_pattern(ws):
    """Check that crons follow cron+skill pattern (no inline logic > 20 lines)."""
    results = []

    # Check OpenClaw native crons
    cron_path = Path.home() / ".openclaw" / "cron" / "jobs.json"
    if cron_path.exists():
        try:
            with open(cron_path, "r") as f:
                jobs = json.load(f)
            if isinstance(jobs, list):
                for job in jobs:
                    prompt = job.get("prompt", "")
                    name = job.get("name", job.get("id", "unknown"))
                    lines = prompt.strip().split("\n")
                    if len(lines) > 20:
                        results.append(fail_msg(f"Cron '{name}' has {len(lines)} lines of inline logic (max: 20)"))
                    elif "skill" not in prompt.lower() and "SKILL.md" not in prompt:
                        results.append(warn_msg(f"Cron '{name}' doesn't reference a skill file"))
                    else:
                        results.append(pass_msg(f"Cron '{name}' follows cron+skill pattern"))
        except (json.JSONDecodeError, KeyError):
            results.append(warn_msg("Could not parse cron/jobs.json"))
    else:
        results.append(info_msg("No native cron jobs found (may be using BullMQ or external scheduler)"))

    # Check for BullMQ queue files
    queue_dir = ws / "scripts" / "queue"
    if queue_dir.exists():
        results.append(pass_msg("BullMQ queue directory found"))

    return results


def check_watchdogs(ws):
    """Check that critical crons have watchdog verification."""
    results = []

    # Look for watchdog-related files
    watchdog_skill = None
    for pattern in ["**/watchdog/SKILL.md", "**/watchdog*.md"]:
        matches = list(ws.glob(pattern))
        if matches:
            watchdog_skill = matches[0]
            break

    if watchdog_skill:
        results.append(pass_msg(f"Watchdog skill found: {watchdog_skill.relative_to(ws)}"))
    else:
        results.append(warn_msg("No watchdog skill found — critical crons may fail silently"))

    # Check for watchdog data/alerts
    alerts_file = ws / "data" / "watchdog-alerts.jsonl"
    if alerts_file.exists():
        results.append(pass_msg("Watchdog alerts log exists"))
    else:
        results.append(info_msg("No watchdog-alerts.jsonl — watchdog may not be wired yet"))

    return results


def check_memory_health(ws):
    """Check memory tier structure and MEMORY.md size."""
    results = []

    memory_md = ws / "MEMORY.md"
    if memory_md.exists():
        lines = memory_md.read_text(encoding="utf-8", errors="replace").split("\n")
        if len(lines) > 500:
            results.append(fail_msg(f"MEMORY.md is {len(lines)} lines (hard cap: 500)"))
        elif len(lines) > 400:
            results.append(warn_msg(f"MEMORY.md is {len(lines)} lines — approaching 500-line cap"))
        else:
            results.append(pass_msg(f"MEMORY.md is {len(lines)} lines (under 500 cap)"))
    else:
        results.append(fail_msg("No MEMORY.md — agent has no persistent memory"))

    # Check memory directory structure
    memory_dir = ws / "memory"
    if memory_dir.exists():
        has_decisions = (memory_dir / "decisions").exists()
        has_daily = any(memory_dir.glob("202*.md"))

        if has_decisions:
            results.append(pass_msg("memory/decisions/ exists"))
        else:
            results.append(warn_msg("No memory/decisions/ — one-time decisions will get lost"))

        if has_daily:
            # Check for recent daily files
            today = datetime.now().strftime("%Y-%m-%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            today_file = memory_dir / f"{today}.md"
            yesterday_file = memory_dir / f"{yesterday}.md"

            if today_file.exists():
                results.append(pass_msg(f"Today's daily memory exists ({today}.md)"))
            else:
                results.append(warn_msg(f"No daily memory for today ({today}.md)"))

            if yesterday_file.exists():
                results.append(pass_msg(f"Yesterday's daily memory exists ({yesterday}.md)"))
        else:
            results.append(warn_msg("No daily memory files found"))
    else:
        results.append(fail_msg("No memory/ directory — no structured memory system"))

    # Check for memory maintenance
    maint_skill = list(ws.glob("**/memory-maintenance/SKILL.md"))
    if maint_skill:
        results.append(pass_msg("Memory maintenance skill exists"))
    else:
        results.append(warn_msg("No memory maintenance skill — memory will bloat over time"))

    return results


def check_security(ws):
    """Check that security rules exist in SOUL.md."""
    results = []

    soul = ws / "SOUL.md"
    if not soul.exists():
        results.append(fail_msg("No SOUL.md — cannot check security rules"))
        return results

    content = soul.read_text(encoding="utf-8", errors="replace").upper()

    if "SECURITY" in content or "INJECTION" in content:
        results.append(pass_msg("Security/injection defense rules found in SOUL.md"))
    else:
        results.append(fail_msg("No security rules in SOUL.md — agent is vulnerable to prompt injection"))

    if "UNTRUSTED" in content:
        results.append(pass_msg("SOUL.md mentions untrusted data handling"))
    else:
        results.append(warn_msg("SOUL.md doesn't mention untrusted data — agent may follow injected instructions"))

    return results


def check_model_selection(ws):
    """Check openclaw.json for model selection patterns."""
    results = []

    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if not config_path.exists():
        results.append(info_msg("No openclaw.json found — skipping model check"))
        return results

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except json.JSONDecodeError:
        results.append(warn_msg("openclaw.json is not valid JSON"))
        return results

    # Check heartbeat model
    heartbeat = config.get("agents", {}).get("defaults", {}).get("heartbeat", {})
    hb_model = heartbeat.get("model", "unknown")
    hb_interval = heartbeat.get("every", "unknown")

    results.append(info_msg(f"Heartbeat: model={hb_model}, interval={hb_interval}"))

    if "opus" in hb_model.lower() or "gpt-4" in hb_model.lower():
        results.append(warn_msg(f"Heartbeat uses expensive model ({hb_model}) — consider a cheaper one"))
    else:
        results.append(pass_msg(f"Heartbeat model is appropriate ({hb_model})"))

    # Check compaction settings
    compaction = config.get("agents", {}).get("defaults", {}).get("compaction", {})
    if compaction:
        results.append(pass_msg(f"Compaction configured: mode={compaction.get('mode', '?')}"))
    else:
        results.append(warn_msg("No compaction settings — sessions may bloat"))

    return results


def check_escalation_path(ws):
    """Check that escalation rules are defined."""
    results = []

    agents = ws / "AGENTS.md"
    if agents.exists():
        content = agents.read_text(encoding="utf-8", errors="replace").upper()
        if "ESCALAT" in content:
            results.append(pass_msg("Escalation rules found in AGENTS.md"))
        else:
            results.append(warn_msg("No escalation rules in AGENTS.md — agent has nowhere to go when stuck"))

    soul = ws / "SOUL.md"
    if soul.exists():
        content = soul.read_text(encoding="utf-8", errors="replace").upper()
        if "STUCK" in content or "ESCALAT" in content:
            results.append(pass_msg("SOUL.md mentions stuck/escalation handling"))

    return results


def check_workspace_hygiene(ws):
    """Check workspace organization."""
    results = []

    # Check for temp files at root
    root_files = [f.name for f in ws.iterdir() if f.is_file()]
    temp_patterns = [".tmp", ".bak", ".log", ".png"]
    temp_files = [f for f in root_files if any(f.endswith(ext) for ext in temp_patterns)]

    if temp_files:
        results.append(warn_msg(f"Temp files at workspace root: {', '.join(temp_files[:5])}"))
    else:
        results.append(pass_msg("No temp files at workspace root"))

    # Check for CONTEXT.md in project directories
    projects_dir = ws / "projects"
    if projects_dir.exists():
        project_dirs = [d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        missing_context = [d.name for d in project_dirs if not (d / "CONTEXT.md").exists()]

        if missing_context:
            results.append(warn_msg(f"Projects missing CONTEXT.md: {', '.join(missing_context[:5])}"))
        else:
            results.append(pass_msg(f"All {len(project_dirs)} projects have CONTEXT.md"))

    return results


def check_skills(ws):
    """Check skill directory structure."""
    results = []

    # Find all skill directories
    skill_dirs = []
    for skills_root in [ws / "agents" / "skills", ws / "skills", ws / "claude" / "skills"]:
        if skills_root.exists():
            for d in skills_root.iterdir():
                if d.is_dir() and (d / "SKILL.md").exists():
                    skill_dirs.append(d)

    if skill_dirs:
        results.append(pass_msg(f"Found {len(skill_dirs)} skills"))

        # Check for essential skills
        skill_names = [d.name for d in skill_dirs]
        essential = {
            "content-related": any("content" in n for n in skill_names),
            "qa-related": any("qa" in n or "audit" in n for n in skill_names),
            "memory-maintenance": any("memory" in n for n in skill_names),
        }

        for category, found in essential.items():
            if found:
                results.append(pass_msg(f"Has {category} skill"))
            else:
                results.append(info_msg(f"No {category} skill found (recommended)"))
    else:
        results.append(warn_msg("No skills found — agent has no reusable methodology"))

    return results


# ── Main ────────────────────────────────────────────────────────────────────

def run_audit(workspace_path):
    ws = Path(workspace_path).resolve()

    if not ws.exists():
        print(f"{C.FAIL}Error: workspace path does not exist: {ws}{C.END}")
        sys.exit(1)

    print(f"\n{C.BOLD}{'=' * 60}{C.END}")
    print(f"{C.BOLD}  AHM DIAGNOSTIC — Agent Headquarters Methodology Audit{C.END}")
    print(f"{C.BOLD}{'=' * 60}{C.END}")
    print(f"  Workspace: {ws}")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()

    checks = [
        ("Bootstrap Files", check_bootstrap_files),
        ("BACKLOG Pattern", check_backlog_pattern),
        ("Cron+Skill Pattern", check_cron_skill_pattern),
        ("Watchdog Verification", check_watchdogs),
        ("Memory Health", check_memory_health),
        ("Security Rules", check_security),
        ("Model Selection", check_model_selection),
        ("Escalation Path", check_escalation_path),
        ("Workspace Hygiene", check_workspace_hygiene),
        ("Skills", check_skills),
    ]

    total_pass = 0
    total_warn = 0
    total_fail = 0

    for name, check_fn in checks:
        print(f"{C.BOLD}─── {name} ───{C.END}")
        try:
            results = check_fn(ws)
            for r in results:
                print(r)
                if "PASS" in r:
                    total_pass += 1
                elif "WARN" in r:
                    total_warn += 1
                elif "FAIL" in r:
                    total_fail += 1
        except Exception as e:
            print(fail_msg(f"Check crashed: {e}"))
            total_fail += 1
        print()

    # Summary
    print(f"{C.BOLD}{'=' * 60}{C.END}")
    total = total_pass + total_warn + total_fail
    score = int((total_pass / total) * 100) if total > 0 else 0

    if score >= 80:
        grade_color = C.PASS
        grade = "A" if score >= 90 else "B"
    elif score >= 60:
        grade_color = C.WARN
        grade = "C"
    else:
        grade_color = C.FAIL
        grade = "D" if score >= 40 else "F"

    print(f"  {C.PASS}✓ {total_pass} passed{C.END}  |  {C.WARN}⚠ {total_warn} warnings{C.END}  |  {C.FAIL}✗ {total_fail} failures{C.END}")
    print(f"  {C.BOLD}AHM Score: {grade_color}{score}% (Grade: {grade}){C.END}")
    print()

    if total_fail > 0:
        print(f"  {C.FAIL}Fix the failures above to reach AHM compliance.{C.END}")
        print(f"  Reference: AHM Playbook at claude/docs/AHM-PLAYBOOK.md")
    elif total_warn > 0:
        print(f"  {C.WARN}Good foundation — address warnings for full AHM compliance.{C.END}")
    else:
        print(f"  {C.PASS}Full AHM compliance! Your agent setup follows all best practices.{C.END}")

    print(f"{C.BOLD}{'=' * 60}{C.END}\n")

    return total_fail


if __name__ == "__main__":
    workspace = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    failures = run_audit(workspace)
    sys.exit(1 if failures > 0 else 0)
