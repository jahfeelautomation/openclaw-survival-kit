# claw-ahm-audit

**AHM Diagnostic — lint your OpenClaw agent setup for best practices.**

Part of the [OpenClaw Survival Kit](https://github.com/jahfeel/openclaw-survival-kit).

## What It Does

Scans an OpenClaw workspace and grades it against AHM (Agent Headquarters Methodology) best practices. Think of it as a linter for your agent setup — it catches anti-patterns before they cause problems.

## Usage

```bash
# Audit the current workspace
python claw_ahm_audit.py /path/to/workspace

# Audit from workspace root
cd ~/.openclaw/workspace && python projects/openclaw-survival-kit/claw-ahm-audit/claw_ahm_audit.py .
```

## What It Checks

| Check | What It Looks For |
|-------|------------------|
| **Bootstrap Files** | All 9 files exist, each under 20K chars, total under 150K |
| **BACKLOG Pattern** | BACKLOG.md exists with recurring items linked to skills |
| **Cron+Skill Pattern** | Crons point to skills, no inline logic > 20 lines |
| **Watchdog Verification** | Critical crons have watchdog verification |
| **Memory Health** | MEMORY.md under 500 lines, daily files exist, maintenance scheduled |
| **Security Rules** | Prompt injection defense in SOUL.md |
| **Model Selection** | Not using expensive models for heartbeats |
| **Escalation Path** | Agent knows what to do when stuck |
| **Workspace Hygiene** | No temp files at root, projects have CONTEXT.md |
| **Skills** | Has reusable skills for content, QA, memory maintenance |

## Output

Color-coded terminal report:
- **✓ PASS** — follows AHM best practice
- **⚠ WARN** — works but could be better
- **✗ FAIL** — anti-pattern that needs fixing

Ends with an AHM Score (percentage) and letter grade (A-F).

## Requirements

- Python 3.8+
- No external dependencies

## Anti-Patterns It Catches

1. Inline cron logic (>20 lines without skill reference)
2. No BACKLOG.md (agent idles when out of tasks)
3. Bloated bootstrap files (>20K each or >150K total)
4. Expensive heartbeat models (Opus/GPT-4 for routine checks)
5. No watchdogs on critical crons
6. Unpruned MEMORY.md (>500 lines)
7. Missing daily memory files
8. No security/injection defense rules
9. No escalation path for stuck agents
10. Temp files cluttering workspace root
11. Projects missing CONTEXT.md
12. No reusable skills

## License

MIT — same as the rest of the Survival Kit.
