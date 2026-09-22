"""Runtime state snapshot.

``runtime/state.json`` is a deliberate interface, not a debug dump. It is what a
person — or Claude in a later session — reads first to answer "what is the bot
doing and is it healthy?" without attaching to the process or parsing logs.

Written atomically (temp file plus rename) so a reader never catches a half
written file, which would otherwise happen exactly when something is going wrong
and someone is watching closely.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tbot.core.clock import iso, now_ms
from tbot.core.logging import get_logger

log = get_logger("engine.state")


@dataclass(slots=True)
class EngineState:
    """Everything an observer needs to judge the bot's health at a glance."""

    mode: str = "live"
    venue: str = "bybit-demo"
    status: str = "starting"  # starting | running | degraded | safe_mode | stopped
    started_ts: int = 0
    updated_ts: int = 0
    last_bar_ts: int = 0
    bars_processed: int = 0
    restarts: int = 0
    equity: float = 0.0
    # The price each open position was valued at inside ``equity``. A restart
    # takes the positions back out of that equity at these same prices; at any
    # other price the unrealised P&L lands in cash and is then counted again.
    equity_marks: dict[str, float] = field(default_factory=dict)
    cash: float = 0.0
    peak_equity: float = 0.0
    # What the account was worth when this run began. In live mode that is the
    # venue balance, not a config number, so every percentage the dashboard
    # shows is measured against money that actually existed.
    starting_equity: float = 0.0
    drawdown: float = 0.0
    risk_multiplier: float = 1.0
    threshold: float = 0.0
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    scan_top: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None
    last_error_ts: int = 0
    degraded_reason: str | None = None
    leverage_allowed: float = 1.0
    llm_enabled: bool = False
    universe_size: int = 0
    # Measured execution cost against the backtest assumption. When this drifts
    # worse, earlier results were optimistic.
    slippage: dict[str, Any] = field(default_factory=dict)
    # Every currency the account holds, as free / used / total plus what it is
    # worth in the quote currency. ``used`` is not decoration: a protective stop
    # reserves the base asset, so a properly protected position shows as fully
    # used and zero free, and an observer who only sees "free" concludes the
    # position is gone.
    balances: list[dict[str, Any]] = field(default_factory=list)
    # Between-bar price stream: connected, stale, how many symbols, how old the
    # last message is. Empty dict means there is no feed in this mode.
    feed: dict[str, Any] = field(default_factory=dict)

    @property
    def uptime_seconds(self) -> float:
        return (now_ms() - self.started_ts) / 1000 if self.started_ts else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uptime_seconds"] = round(self.uptime_seconds, 1)
        data["uptime_human"] = _human_duration(self.uptime_seconds)
        data["updated_iso"] = iso(self.updated_ts) if self.updated_ts else None
        data["last_bar_iso"] = iso(self.last_bar_ts) if self.last_bar_ts else None
        return data


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


class StateWriter:
    """Persists the state snapshot atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, state: EngineState) -> None:
        state.updated_ts = now_ms()
        payload = json.dumps(state.to_dict(), indent=2, default=str)

        # Write-then-rename: a reader polling this file never sees a partial
        # document, which is exactly when they would be reading it.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state_file_unreadable", path=str(self.path))
            return None


class RestartTracker:
    """Counts recent restarts to detect a crash loop.

    Beyond the threshold the engine drops to safe mode — holding existing
    positions, opening nothing new — instead of thrashing. Restarting forever is
    the requirement; restarting forever *while trading* is not.
    """

    def __init__(self, path: Path, window_seconds: int = 3600):
        self.path = Path(path)
        self.window_ms = window_seconds * 1000

    def record(self) -> int:
        stamps = self._load()
        now = now_ms()
        stamps = [t for t in stamps if now - t < self.window_ms]
        stamps.append(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(stamps), encoding="utf-8")
        return len(stamps)

    def count(self) -> int:
        now = now_ms()
        return len([t for t in self._load() if now - t < self.window_ms])

    def _load(self) -> list[int]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
