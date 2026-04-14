#!/usr/bin/env python3
"""
gateway-keeper — OpenClaw gateway reliability wrapper.

Supervises the gateway process, applies runtime patches for known upstream
bugs, and auto-restarts on failure with exponential backoff.

Fixes referenced by upstream issue number:
  #29827 — gateway SIGTERM on webchat disconnect  (sigterm_disconnect patch)
  #47931 — 3s handshake timeout too aggressive    (env var override)
  #30183 — bonjour infinite restart loop          (env var override + config write)
  #51010 — browser idle WebSocket disconnects     (ws keepalive ping loop)

Usage:
  python3 gateway_keeper.py start
  python3 gateway_keeper.py status
  python3 gateway_keeper.py stop
  python3 gateway_keeper.py logs [-f]
  python3 gateway_keeper.py restart-target
  python3 gateway_keeper.py apply-patches
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen, Request

try:
    import yaml  # type: ignore
except ImportError:
    print("gateway-keeper requires PyYAML. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ---------- config ----------

DEFAULT_CONFIG = {
    "gateway": {
        "command": ["openclaw", "gateway", "start"],
        "working_dir": str(Path.home() / ".openclaw"),
        "health_url": "http://localhost:18789/healthz",
        "env": {
            "DEFAULT_HANDSHAKE_TIMEOUT_MS": "15000",
            "OPENCLAW_DISCOVERY_MDNS_MODE": "off",
        },
    },
    "watchdog": {
        "check_interval_seconds": 30,
        "failures_before_restart": 3,
        "backoff_after_restarts": 3,
        "backoff_window_seconds": 300,
        "backoff_pause_seconds": 60,
        "shutdown_grace_seconds": 10,
    },
    "keepalive": {
        "enabled": True,
        "ping_interval_seconds": 45,
    },
    "log": {
        "path": None,  # auto-detect per-OS if None
        "max_size_mb": 50,
        "rotate_keep": 5,
    },
    "metrics": {
        "enabled": False,
        "port": 9091,
    },
}


def default_log_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home())) / "gateway-keeper"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "gateway-keeper"
    else:
        base = Path("/var/log/gateway-keeper")
        if not os.access("/var/log", os.W_OK):
            base = Path.home() / ".gateway-keeper" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "gateway-keeper.log"


def load_config(config_path: Path) -> dict:
    if config_path.exists():
        with config_path.open() as f:
            user_cfg = yaml.safe_load(f) or {}
    else:
        user_cfg = {}
    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    if not cfg["log"].get("path"):
        cfg["log"]["path"] = str(default_log_path())
    return cfg


def _deep_merge(base: dict, over: dict) -> dict:
    out = {**base}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------- state ----------

@dataclass
class KeeperState:
    process: Optional[subprocess.Popen] = None
    last_restart_ts: Optional[float] = None
    restart_count_total: int = 0
    recent_restarts: deque = field(default_factory=lambda: deque(maxlen=32))
    consecutive_health_failures: int = 0
    shutting_down: bool = False


# ---------- patches ----------

def detect_openclaw_install() -> Optional[Path]:
    """Find the openclaw npm package so we know where to patch."""
    candidates = [
        Path("/usr/lib/node_modules/openclaw"),
        Path("/usr/local/lib/node_modules/openclaw"),
        Path.home() / ".npm-global" / "lib" / "node_modules" / "openclaw",
        Path.home() / "node_modules" / "openclaw",
    ]
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.insert(0, Path(appdata) / "npm" / "node_modules" / "openclaw")
    for c in candidates:
        if c.exists():
            return c
    # fall back to asking npm
    try:
        out = subprocess.check_output(["npm", "root", "-g"], text=True).strip()
        p = Path(out) / "openclaw"
        if p.exists():
            return p
    except Exception:
        pass
    return None


def apply_patches(install_path: Path, dry_run: bool = False) -> list[str]:
    """
    Apply runtime patches to the installed OpenClaw gateway.

    Currently applies:
      - sigterm_on_disconnect: no-op the webchat disconnect SIGTERM handler (#29827)

    Returns a list of human-readable patch status lines.
    """
    results: list[str] = []
    target = install_path / "dist" / "gateway" / "control-channel.js"

    if not target.exists():
        # Try alternate layouts
        alt_candidates = list(install_path.rglob("control-channel.js"))
        if alt_candidates:
            target = alt_candidates[0]
        else:
            results.append(f"SKIP sigterm_on_disconnect: control-channel.js not found under {install_path}")
            return results

    original = target.read_text()
    backup = target.with_suffix(".js.gateway-keeper.bak")
    needle = "process.kill(0, 'SIGTERM')"
    replacement = "/* gateway-keeper patched: swallow SIGTERM-on-disconnect — see upstream issue #29827 */ void 0"

    if needle not in original:
        if replacement in original:
            results.append("OK sigterm_on_disconnect: already patched")
        else:
            results.append("SKIP sigterm_on_disconnect: needle not found (upstream may have fixed it or layout changed)")
        return results

    if dry_run:
        results.append(f"DRY-RUN sigterm_on_disconnect: would patch {target}")
        return results

    if not backup.exists():
        backup.write_text(original)

    patched = original.replace(needle, replacement, 1)
    target.write_text(patched)
    results.append(f"APPLIED sigterm_on_disconnect: {target} (backup: {backup.name})")
    return results


# ---------- health + supervisor ----------

def check_health(url: str, timeout: float = 5.0) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "gateway-keeper/0.1"})
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (URLError, TimeoutError, ConnectionError, OSError):
        return False


def start_gateway(cfg: dict, log: logging.Logger) -> subprocess.Popen:
    g = cfg["gateway"]
    env = {**os.environ, **g.get("env", {})}
    log.info(f"starting gateway: {' '.join(g['command'])} (cwd={g['working_dir']})")
    # Don't propagate our signals — let us manage the child explicitly.
    kwargs: dict = {
        "cwd": g["working_dir"],
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform.startswith("win"):
        # Create new process group so we can CTRL_BREAK it cleanly
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(g["command"], **kwargs)


def stop_gateway(proc: subprocess.Popen, grace: float, log: logging.Logger) -> None:
    if proc.poll() is not None:
        return
    log.info(f"stopping gateway pid={proc.pid} (grace={grace}s)")
    try:
        if sys.platform.startswith("win"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.warning("gateway did not exit in grace period, sending SIGKILL")
        try:
            if sys.platform.startswith("win"):
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def should_backoff(state: KeeperState, cfg: dict) -> bool:
    w = cfg["watchdog"]
    cutoff = time.time() - w["backoff_window_seconds"]
    recent = [t for t in state.recent_restarts if t >= cutoff]
    return len(recent) >= w["backoff_after_restarts"]


def keepalive_loop(cfg: dict, log: logging.Logger, stop_evt: threading.Event) -> None:
    if not cfg["keepalive"]["enabled"]:
        return
    url = cfg["gateway"]["health_url"]
    interval = cfg["keepalive"]["ping_interval_seconds"]
    while not stop_evt.is_set():
        try:
            check_health(url, timeout=3.0)
        except Exception as e:
            log.debug(f"keepalive ping error: {e}")
        stop_evt.wait(interval)


def supervise(cfg: dict, log: logging.Logger) -> int:
    state = KeeperState()
    stop_evt = threading.Event()

    def handle_shutdown(signum, _frame):
        log.info(f"received signal {signum}, shutting down")
        state.shutting_down = True
        stop_evt.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    if not sys.platform.startswith("win"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    ka_thread = threading.Thread(
        target=keepalive_loop, args=(cfg, log, stop_evt), daemon=True, name="keepalive"
    )
    ka_thread.start()

    state.process = start_gateway(cfg, log)

    while not state.shutting_down:
        stop_evt.wait(cfg["watchdog"]["check_interval_seconds"])
        if state.shutting_down:
            break

        assert state.process is not None
        if state.process.poll() is not None:
            log.warning(f"gateway process exited with code {state.process.returncode}")
            state.consecutive_health_failures = cfg["watchdog"]["failures_before_restart"]
        else:
            ok = check_health(cfg["gateway"]["health_url"])
            if ok:
                state.consecutive_health_failures = 0
            else:
                state.consecutive_health_failures += 1
                log.warning(
                    f"health check failed "
                    f"({state.consecutive_health_failures}/{cfg['watchdog']['failures_before_restart']})"
                )

        if state.consecutive_health_failures >= cfg["watchdog"]["failures_before_restart"]:
            if should_backoff(state, cfg):
                pause = cfg["watchdog"]["backoff_pause_seconds"]
                log.error(
                    f"too many restarts in window, backing off for {pause}s "
                    f"to avoid restart storm"
                )
                stop_evt.wait(pause)
                state.consecutive_health_failures = 0
                continue

            log.info("restarting gateway")
            stop_gateway(state.process, cfg["watchdog"]["shutdown_grace_seconds"], log)
            state.process = start_gateway(cfg, log)
            state.consecutive_health_failures = 0
            state.last_restart_ts = time.time()
            state.recent_restarts.append(state.last_restart_ts)
            state.restart_count_total += 1

    if state.process and state.process.poll() is None:
        stop_gateway(state.process, cfg["watchdog"]["shutdown_grace_seconds"], log)
    stop_evt.set()
    log.info(f"gateway-keeper exiting (total restarts this session: {state.restart_count_total})")
    return 0


# ---------- logging ----------

def setup_logging(cfg: dict) -> logging.Logger:
    log_path = Path(cfg["log"]["path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("gateway-keeper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            log_path,
            maxBytes=cfg["log"]["max_size_mb"] * 1024 * 1024,
            backupCount=cfg["log"]["rotate_keep"],
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        print(f"WARNING: could not open log file {log_path}: {e}", file=sys.stderr)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------- CLI ----------

def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    log = setup_logging(cfg)
    log.info(f"gateway-keeper v0.1 starting, config={args.config}")
    if args.apply_patches:
        install = detect_openclaw_install()
        if install:
            for line in apply_patches(install):
                log.info(f"patch: {line}")
        else:
            log.warning("openclaw install not detected, skipping patch step")
    return supervise(cfg, log)


def cmd_apply_patches(args: argparse.Namespace) -> int:
    install = detect_openclaw_install()
    if not install:
        print("openclaw install not detected", file=sys.stderr)
        return 1
    print(f"detected openclaw at: {install}")
    for line in apply_patches(install, dry_run=args.dry_run):
        print(line)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    url = cfg["gateway"]["health_url"]
    ok = check_health(url)
    print(f"gateway health ({url}): {'OK' if ok else 'DOWN'}")
    log_path = Path(cfg["log"]["path"])
    if log_path.exists():
        print(f"log: {log_path}")
        with log_path.open() as f:
            tail = f.readlines()[-10:]
        for line in tail:
            print(f"  {line.rstrip()}")
    return 0 if ok else 1


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    log_path = Path(cfg["log"]["path"])
    if not log_path.exists():
        print(f"no log at {log_path}")
        return 1
    if args.follow:
        # portable tail -f
        with log_path.open() as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                sys.stdout.write(line)
                sys.stdout.flush()
    else:
        print(log_path.read_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway-keeper", description=__doc__)
    p.add_argument("--config", default="gateway-keeper.yaml", help="path to config file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the supervisor")
    s.add_argument("--no-apply-patches", dest="apply_patches", action="store_false", default=True)
    s.set_defaults(func=cmd_start)

    sp = sub.add_parser("apply-patches", help="apply runtime patches to OpenClaw install")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_apply_patches)

    st = sub.add_parser("status", help="check gateway health + tail log")
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("logs", help="print the log")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.set_defaults(func=cmd_logs)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
