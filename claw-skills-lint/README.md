# claw-skills-lint

Validate that OpenClaw workspace skills are properly loaded and configured.

## What it checks

- Skills listed in `openclaw.json` but missing from disk (**MISSING**)
- Skills on disk but not registered in config (**UNREGISTERED**)
- Empty or malformed `SKILL.md` files (**BROKEN**)
- Missing YAML frontmatter fields (name, description)
- Unresolvable dependencies listed in frontmatter

## Usage

```bash
# Read-only diagnostic
python claw_skills_lint.py

# JSON output for automation
python claw_skills_lint.py --json

# Auto-fix: create stubs for MISSING, register UNREGISTERED
python claw_skills_lint.py --fix

# Custom paths
python claw_skills_lint.py --config /path/to/openclaw.json --skills-dir /path/to/skills/
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All skills OK |
| 1 | WARN — unregistered skills found |
| 2 | FAIL — missing or broken skills |

## Requirements

Python 3.8+, stdlib only. No external dependencies.
