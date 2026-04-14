# Contributing to OpenClaw Survival Kit

Thanks for wanting to help. Every fix in this kit comes from somebody actually getting bitten, so your pain is welcome.

## Reporting a bug you hit

Open an issue and include:

1. The upstream OpenClaw GitHub issue number if one exists.
2. Your OpenClaw version (`openclaw --version`).
3. OS + arch.
4. What you did, what you expected, what happened.
5. Log output if available.

If there's no upstream issue yet, open one there too and link back — these fixes should also exist where the source of the bug is.

## Adding a new tool

Each tool lives in its own top-level folder and is self-contained. Structure:

```
mytool/
├── README.md              # Problem → fix → install → config → rollback
├── mytool.py              # or .sh / .js — whatever makes sense
├── mytool.example.yaml    # example config
├── install.sh             # optional
└── test/                  # repro scripts for the bug you're fixing
```

Rules:

- **Cite the upstream issue number in the README.** If none exists, link to a forum post or blog describing the bug.
- **Ship a working fix, not a magnifying glass.** Diagnostics belong in the upstream `openclaw doctor`. This repo is for things that change behavior.
- **Non-destructive by default.** Anything that modifies installed files must write a backup, support rollback, and detect when upstream has already fixed the bug.
- **Keep it focused.** One tool fixes one problem. Multi-tool PRs will get split.

## Patch style for node_modules edits

If you're patching `node_modules/openclaw/...`:

1. Find a unique needle in the source.
2. Write a backup (`<file>.gateway-keeper.bak`) before first edit.
3. Detect "already patched" and "already fixed upstream" states, skip cleanly.
4. Include the patch in `patches/<name>.patch` so others can audit it.

See `gateway-keeper/gateway_keeper.py` `apply_patches()` for the pattern.

## Testing

Every tool that modifies runtime behavior must ship with at least one test in `test/` that reproduces the original bug (pre-patch) and confirms the fix (post-patch). We take "battle-tested" seriously and would rather ship slowly than ship broken.

## License

All contributions are MIT-licensed. By opening a PR you agree to release your work under MIT.
