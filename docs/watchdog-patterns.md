# 7 Watchdog Patterns — Extracted from openclaw-watchdog.ps1

These patterns emerged from running an OpenClaw gateway 24/7 for months.
They're the things that keep breaking and the solutions that actually work.

---

## Pattern 1: PID-Targeted Process Kill

**Problem:** `taskkill /IM node.exe` kills everything — Claude Desktop, MCP bridges, your IDE.

**Solution:** Find the specific PID holding port 18789, kill only that.
```
Get-NetTCPConnection -LocalPort 18789 | Select OwningProcess
Stop-Process -Id $pid -Force
```

**Why:** OpenClaw runs as a Node process, but so do 5-10 other things on your machine.

## Pattern 2: Session 1 Awareness

**Problem:** `openclaw gateway start` uses Windows Task Scheduler, which launches in Session 0 (invisible desktop). The gateway runs but you can't see or interact with it.

**Solution:** Always start via the gateway.cmd batch file directly, which inherits the current session (Session 1 = interactive desktop).

**Why:** Debugging a gateway you can't see is impossible. Session 0 processes also have different permissions.

## Pattern 3: Orphan Process Cleanup Before Restart

**Problem:** Gateway crash leaves behind browser processes (Puppeteer/Chrome) and subagent Node processes that hold file locks and eat RAM.

**Solution:** Before restarting, kill orphan child processes by walking the process tree from the last known gateway PID.

**Why:** Without cleanup, RAM usage climbs with every restart cycle until the machine becomes unusable.

## Pattern 4: Exponential Backoff with Cap

**Problem:** If the gateway keeps crashing, rapid restarts create log spam and resource churn.

**Solution:** Start at 30 seconds, double on each failure, cap at 5 minutes. Reset to 30 seconds on successful startup.

**Why:** Gives the underlying issue time to resolve (disk full, port conflict, Windows Update) while never giving up permanently.

## Pattern 5: Health Check Beyond Port Check

**Problem:** Gateway process can be running (port 18789 open) but not actually responding to requests — zombie state.

**Solution:** Three-tier health check:
1. Is the port open? (TCP connection test)
2. Does HTTP /health respond within 5 seconds?
3. Is the response valid JSON with expected fields?

Only pass if all three succeed.

**Why:** A port being open just means the process is alive. The gateway can be stuck in a boot loop, deadlocked, or partially crashed.

## Pattern 6: Bridge Alerting (Agent-to-Agent)

**Problem:** When the gateway is down, there's no way for the watchdog to alert through the normal agent communication channels (which go through the gateway).

**Solution:** Write directly to the bridge messages.json file and send a Telegram message via curl/Invoke-RestMethod as a side-channel alert.

**Why:** Your monitoring system shouldn't depend on the system it's monitoring.

## Pattern 7: Memory Usage Tracking

**Problem:** OpenClaw's transcript accumulation can cause memory to grow unbounded, eventually triggering OOM or swap thrashing.

**Solution:** Track RSS memory of the gateway process on each health check. If it exceeds a threshold (default 2GB), force a restart. Log memory trends for debugging.

**Why:** Gradual memory leaks are invisible until they cause a crash. Proactive monitoring catches them before they cause downtime.

---

## Using These Patterns

These patterns are now embedded in the OpenClaw Survival Kit tools:

- **Pattern 1** → `gateway-keeper` (PID-targeted restarts)
- **Pattern 2** → `gateway-keeper` (Session 1 launch)
- **Pattern 3** → `gateway-keeper` (orphan cleanup)
- **Pattern 4** → `gateway-keeper` (backoff algorithm)
- **Pattern 5** → `claw-medic` (three-tier health check)
- **Pattern 6** → Implemented in watchdog, available as reference
- **Pattern 7** → `claw-reaper` (memory monitoring + transcript reaping)
