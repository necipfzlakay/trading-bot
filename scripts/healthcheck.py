#!/usr/bin/env python3
"""Dead-man switch and backup. Run on a schedule, not by the engine.

An engine that has crashed is loud — launchd restarts it and the logs say so.
An engine that is *wedged* is silent: the process is alive, so nothing restarts
it, and open positions stop being managed while everything looks fine. A process
check cannot see that; a freshness check on ``state.json`` can.

    python scripts/healthcheck.py                # check, alert if stale
    python scripts/healthcheck.py --backup       # also snapshot the database
    python scripts/healthcheck.py --max-age 3600

Exit code 0 healthy, 1 unhealthy — so it also works as a container healthcheck
or a cron guard.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from _bootstrap import setup

from tbot.core.clock import iso, now_ms
from tbot.core.config import load_secrets
from tbot.notify.telegram import TelegramNotifier

# The engine heartbeats state.json every 60s while it waits for the next bar,
# so staleness now means something is actually wrong rather than "the bar is
# long". Ten missed heartbeats is the threshold; it used to be 90 minutes,
# which is how long a wedged engine could leave positions unmanaged.
DEFAULT_MAX_AGE_SECONDS = 600


def check_freshness(cfg, max_age: int) -> tuple[bool, str, dict]:
    path = cfg.state_path
    if not path.exists():
        return False, "state.json does not exist — the engine has never run", {}

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "state.json is unreadable", {}

    age = (now_ms() - int(state.get("updated_ts", 0))) / 1000
    status = state.get("status", "unknown")

    if status == "stopped":
        return False, "engine reports status=stopped", state
    if age > max_age:
        return (
            False,
            f"state has not been updated for {age / 60:.0f} minutes "
            f"(last {iso(int(state.get('updated_ts', 0)))}) — the process may be alive "
            f"but wedged, which nothing else would notice",
            state,
        )
    return True, f"healthy: {status}, updated {age / 60:.1f} minutes ago", state


def check_process(config_path: str) -> tuple[bool, str]:
    """Is *this* engine's process alive — not merely some engine's.

    The pattern includes the config. Matching ``scripts/run.py`` alone answers
    about whichever engine started first, so with two engines running a check
    on the stopped one reported the other one's pid as proof of life. A
    dead-man switch that cannot tell the two apart is not one. ``ops/bot.sh``
    scopes its own process lookups the same way and for the same reason.
    """
    pattern = f"scripts/run.py --config {config_path}"
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not check for the process: {exc}"

    pids = [p for p in result.stdout.split() if p]
    if not pids:
        return False, f"no engine process found for {config_path}"
    return True, f"process alive (pid {', '.join(pids)})"


def backup_database(cfg, keep: int = 14) -> Path | None:
    """Snapshot the database with SQLite's online backup API.

    Copying the file while the engine is writing can capture a torn page; the
    backup API takes a consistent snapshot of a live database instead.
    """
    if not cfg.db_path.exists():
        return None

    backup_dir = cfg.runtime_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    target = backup_dir / f"tbot-{stamp}.db"

    source = sqlite3.connect(str(cfg.db_path))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    snapshots = sorted(backup_dir.glob("tbot-*.db"))
    for stale in snapshots[:-keep]:
        stale.unlink(missing_ok=True)

    return target


def check_disk(cfg, min_free_mb: int = 500) -> tuple[bool, str]:
    """A full disk stops SQLite writes, which stops the bot recording trades."""
    usage = shutil.disk_usage(cfg.runtime_dir)
    free_mb = usage.free / 1e6
    if free_mb < min_free_mb:
        return False, f"only {free_mb:.0f}MB free — SQLite writes will start failing"
    return True, f"{free_mb / 1000:.1f}GB free"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cfg = setup(args.config)
    secrets = load_secrets()

    fresh, fresh_msg, state = check_freshness(cfg, args.max_age)
    alive, alive_msg = check_process(args.config)
    disk_ok, disk_msg = check_disk(cfg)

    checks = [
        ("state freshness", fresh, fresh_msg),
        ("process", alive, alive_msg),
        ("disk", disk_ok, disk_msg),
    ]

    if args.backup:
        target = backup_database(cfg, args.keep)
        checks.append(("backup", target is not None, str(target) if target else "no database yet"))

    healthy = all(ok for _, ok, _ in checks)

    if not args.quiet:
        for name, ok, message in checks:
            print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<18} {message}")
        if state:
            print(
                f"\n  equity {state.get('equity', 0):.2f} · "
                f"drawdown {state.get('drawdown', 0):.1%} · "
                f"positions {len(state.get('open_positions', []))} · "
                f"restarts {state.get('restarts', 0)}"
            )

    if not healthy and cfg.telegram.enabled:
        # Only alert on failure. A healthy heartbeat that pings every fifteen
        # minutes trains the operator to ignore the channel.
        notifier = TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id)
        failures = "\n".join(f"• {name}: {msg}" for name, ok, msg in checks if not ok)
        notifier.notify_error("Health check failed", failures)

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
