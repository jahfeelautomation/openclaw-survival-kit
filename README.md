# OpenClaw Survival Kit

**The missing reliability layer for OpenClaw.**

If your gateway crashes when you close a browser tab, if your heartbeat fires every 5 minutes instead of the 90 you configured, if your skills show as "enabled" but aren't actually loaded — this repo is for you.

Each tool in this kit fixes one specific, documented OpenClaw bug. Install what you need. Ignore what you don't. All MIT-licensed, built while getting our own OpenClaw deployment ready for [Agent HQ](https://agenthq.pro) (launching April 30, 2026) — so every fix is tested against a real agent running 24/7 before it ships here.

---

## Why this exists

OpenClaw is powerful, but the gateway is fragile. After months of running it 24/7, we hit the same bugs everyone else hits — and watched the same "fixes" blog posts tell us to just run `openclaw doctor --fix` and pray.

We stopped praying. We started patching. This is the kit.

Every tool in this repo is tied to a real, open GitHub issue on the upstream OpenClaw repo. README sections cite the issue numbers so you can verify the bug before you install the fix.

---

## What's inside (v0.1 roadmap)

| Tool | Fixes | Status |
|---|---|---|
| [`gateway-keeper/`](./gateway-keeper) | Gateway crashes, SIGTERM on webchat disconnect ([#29827](https://github.com/openclaw/openclaw/issues/29827)), 3s handshake timeout ([#47931](https://github.com/openclaw/openclaw/issues/47931)), bonjour infinite restart loop ([#30183](https://github.com/openclaw/openclaw/issues/30183)) | **v0.1 shipping this week** |
| [`claw-cron/`](./claw-cron) | Heartbeat interval collapse ([#27807](https://github.com/openclaw/openclaw/issues/27807)), silent cron failures ([#8414](https://github.com/openclaw/openclaw/issues/8414)), heartbeat stops after 1-2 triggers ([#45772](https://github.com/openclaw/openclaw/issues/45772)) | Planned |
| [`claw-skills-lint/`](./claw-skills-lint) | Workspace skills silently not loading ([#29122](https://github.com/openclaw/openclaw/issues/29122), [#49873](https://github.com/openclaw/openclaw/issues/49873)), skills show enabled but aren't ([#9469](https://github.com/openclaw/openclaw/issues/9469)), v2026.4.10 sandbox break ([#64985](https://github.com/openclaw/openclaw/issues/64985)) | Planned |
| [`claw-session-repair/`](./claw-session-repair) | Orphaned tool_use corrupts JSONL ([#3409](https://github.com/moltbot/moltbot/issues/3409), [#21985](https://github.com/openclaw/openclaw/issues/21985)), malformed tool call errors leaked to users ([#7867](https://github.com/openclaw/openclaw/issues/7867)) | Planned |
| [`claw-channel-watch/`](./claw-channel-watch) | Telegram getUpdates timeout no reconnect ([#4617](https://github.com/openclaw/openclaw/issues/4617)), WhatsApp disconnects ([#22511](https://github.com/openclaw/openclaw/issues/22511)), channel crash on Discord ([#65548](https://github.com/openclaw/openclaw/issues/65548)) | Planned |
| [`claw-pin/`](./claw-pin) | Update treadmill — pin a version, diff breaking changes before upgrading | Planned |

---

## What this kit is NOT

We deliberately don't duplicate existing work. If another project already solved a problem well, we link to it:

- **Token / cost optimization** — see [wassupjay/OpenClaw-Token-Optimization](https://github.com/wassupjay/OpenClaw-Token-Optimization). Drop-in configs that cut token spend 90-97%.
- **SOUL.md / persona tooling** — see [aaronjmars/soul.md](https://github.com/aaronjmars/soul.md). Builder for custom agent personalities.
- **Config drift diagnostics** — see [DanAndBub/Driftwatch](https://github.com/DanAndBub/Driftwatch). Client-side analyzer for SOUL.md truncation, bootstrap budget overrun, cross-file contradictions. Driftwatch was the first tool that made these silent problems visible — this kit picks up where it leaves off.
- **Watchdog inspiration** — the excellent [Yash-Kavaiya/openclaw-watchdog](https://github.com/Yash-Kavaiya/openclaw-watchdog) (Windows), [chrysb/alphaclaw](https://github.com/chrysb/alphaclaw), and [cathrynlavery/openclaw-ops](https://github.com/cathrynlavery/openclaw-ops) influenced our gateway-keeper design. Our version adds cross-platform support, the SIGTERM monkey-patch, and the bonjour-loop workaround baked in.

Shout out to everyone in the ecosystem. If a tool listed above fits your problem, go use it. If you need the reliability bundle — keep reading.

---

## Install

Each tool is a standalone folder. No global install, no framework lock-in.

**Note on status:** This kit is early (v0.1 shipped April 2026). It's honest alpha — the patches are live and tested on our own setup, but nothing here has thousands of users yet. If you hit a rough edge, open an issue. That's how the kit gets better.

```bash
git clone https://github.com/jahfeelautomation/openclaw-survival-kit.git
cd openclaw-survival-kit/gateway-keeper
./install.sh
```

Each tool's own README has the full setup, config, and rollback instructions.

---

## Philosophy

1. **Fix, don't diagnose.** Magnifying glasses are useful; we ship wrenches.
2. **Cite the bug.** Every README says which upstream issue it addresses. Sunlight works.
3. **Small and focused.** One folder = one problem = one install step.
4. **Upstream-friendly.** Where our patches could merge into OpenClaw itself, we open PRs there too. This kit should shrink over time if they accept them.
5. **No framework.** No wrapper CLI, no daemon you have to adopt. Each tool runs on its own.

---

## If you'd rather pay us to run it

We built this kit because we're spinning up Agent HQ on OpenClaw and hit every bug in this repo while getting ready for launch. If you'd rather not maintain patches yourself, [Agent HQ](https://agenthq.pro) is the hosted version — we run the patched OpenClaw, you just use your agent. Launching April 30, 2026.

Either way — this kit stays free, stays open, stays MIT.

---

## Contributing

Found a bug we haven't covered? Open an issue with the upstream GitHub issue link and your repro steps. Want to add a tool? Read [CONTRIBUTING.md](./CONTRIBUTING.md) before sending a PR.

The kit grows as Jeff (our daily-driver agent) hits new bugs. Every fix gets tested against a live workload before it ships here.

---

**License:** MIT
**Maintained by:** [@jahfeelautomation](https://github.com/jahfeelautomation)
**Companion product:** [Agent HQ](https://agenthq.pro)
