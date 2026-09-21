"""H18 replayed over stored one-minute bars, through the live signal.

Every trade here comes from ``scalp.signal.evaluate`` — the function the live
engine calls — so the two cannot measure different strategies (invariant 59).
A vectorised pass only *proposes* bars; the live function confirms each one.

**What one-minute bars cannot say, and which way each gap is resolved.** The
live entry is a PostOnly limit that lives 20 seconds; a bar lasts 60. So:

* **Entry** at the trigger bar's close (the live limit joins the best bid/ask
  at that moment). It fills only if the *next* bar trades **through** that price
  — touching is not enough, a maker order at the touch may be behind the queue.
  Sixty seconds instead of twenty is generous on fill *rate*; it is also where
  adverse selection shows up, because the fills that happen are the ones price
  ran against.
* **Stop before target** in any bar that reaches both (invariant 40). In the
  fill bar itself the stop may trigger and the target may not: which came first
  inside the bar is unknowable, and the kinder reading is never the one taken.
* **Costs**: maker on the entry and the target (both limits), taker plus
  ``execution.slippage_bps`` on the stop and the time stop (both market).

Portfolio rules mirror the live valves: one position per symbol, at most
``risk.max_positions`` at once, each ``max_capital / max_positions`` notional,
at most ``max_entries_per_minute`` new entries in any minute. The claim the
fast engine may hold on a symbol is not modelled — live will miss some entries
that the backtest takes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tbot.core.config import Config
from tbot.core.models import Side
from tbot.scalp.signal import Intent, evaluate, exit_levels, required_bars

MINUTE_MS = 60_000


@dataclass(slots=True)
class SimTrade:
    symbol: str
    arm: str
    side: Side
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    notional: float
    fees: float
    reason: str  # take_profit | stop | time_stop
    bars_held: int

    @property
    def net_return(self) -> float:
        """Net of every fee, as a fraction of notional."""
        gross = (self.exit / self.entry - 1.0) * self.side.sign
        return gross - self.fees / self.notional

    @property
    def pnl(self) -> float:
        return self.net_return * self.notional


@dataclass
class Result:
    triggers: dict[str, int] = field(default_factory=lambda: {"momentum": 0, "reversion": 0})
    unfilled: dict[str, int] = field(default_factory=lambda: {"momentum": 0, "reversion": 0})
    skipped: dict[str, int] = field(default_factory=dict)
    trades: list[SimTrade] = field(default_factory=list)
    minutes: int = 0


def candidates(closes: np.ndarray, config) -> np.ndarray:
    """Bars where the trigger *might* fire — a superset, confirmed by ``evaluate``.

    Same arithmetic as the live function, vectorised, with a 1% looser
    threshold so floating-point order can never drop a bar the live function
    would take.
    """
    k, w = config.trigger_bars, config.sigma_window
    returns = pd.Series(closes).pct_change()
    sigma = returns.rolling(w).std(ddof=1).to_numpy()
    move = closes / np.roll(closes, k) - 1.0
    threshold = config.trigger_sigma * sigma * math.sqrt(k) * 0.99
    hits = np.abs(move) > threshold
    hits[: required_bars(config) - 1] = False
    return np.flatnonzero(hits)


def _exit_after_fill(
    side: Side,
    entry: float,
    fill_index: int,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    config,
) -> tuple[int, float, str]:
    """(exit bar index, exit price, reason) — stop first, target never in the fill bar."""
    take_profit, stop_loss = exit_levels(side, entry, config)
    last = min(fill_index + config.time_stop_bars - 1, len(closes) - 1)
    for j in range(fill_index, last + 1):
        stopped = lows[j] <= stop_loss if side is Side.BUY else highs[j] >= stop_loss
        if stopped:
            return j, stop_loss, "stop"
        if j == fill_index:
            continue
        hit = highs[j] >= take_profit if side is Side.BUY else lows[j] <= take_profit
        if hit:
            return j, take_profit, "take_profit"
    return last, float(closes[last]), "time_stop"


def run(config: Config, frames: dict[str, pd.DataFrame]) -> Result:
    """Replay H18 over ``{symbol: 1m OHLCV frame}``."""
    scalp = config.scalp
    maker = config.execution.assumed_maker_fee
    taker = config.execution.assumed_taker_fee
    slip = config.execution.slippage_bps / 10_000
    notional = config.risk.max_capital / config.risk.max_positions
    result = Result()

    # Every symbol's proposals, then one chronological pass that applies the
    # portfolio rules across symbols exactly as the live loop would meet them.
    proposals: list[tuple[int, str, Intent, int]] = []
    arrays: dict[str, tuple[np.ndarray, ...]] = {}
    for symbol, frame in frames.items():
        ts = frame["ts"].to_numpy(dtype="int64")
        highs = frame["high"].to_numpy(dtype="float64")
        lows = frame["low"].to_numpy(dtype="float64")
        closes = frame["close"].to_numpy(dtype="float64")
        arrays[symbol] = (ts, highs, lows, closes)
        result.minutes = max(result.minutes, len(ts))
        window = required_bars(scalp)
        for i in candidates(closes, scalp):
            intent = evaluate(symbol, closes[i + 1 - window : i + 1].tolist(), int(ts[i]), scalp)
            if intent is not None:
                proposals.append((int(ts[i]), symbol, intent, int(i)))
    proposals.sort(key=lambda p: (p[0], p[1]))

    busy_until: dict[str, int] = {}  # symbol -> exit ts
    open_exits: list[int] = []
    entries_by_minute: dict[int, int] = {}
    for signal_ts, symbol, intent, i in proposals:
        result.triggers[intent.arm] = result.triggers.get(intent.arm, 0) + 1
        open_exits = [t for t in open_exits if t > signal_ts]
        if busy_until.get(symbol, 0) > signal_ts:
            result.skipped["already_in_position"] = result.skipped.get("already_in_position", 0) + 1
            continue
        if len(open_exits) >= config.risk.max_positions:
            result.skipped["max_positions"] = result.skipped.get("max_positions", 0) + 1
            continue
        if entries_by_minute.get(signal_ts, 0) >= scalp.max_entries_per_minute:
            result.skipped["throttled"] = result.skipped.get("throttled", 0) + 1
            continue

        ts, highs, lows, closes = arrays[symbol]
        fill_index = i + 1
        if fill_index >= len(closes):
            continue
        limit = float(closes[i])
        traded_through = (
            lows[fill_index] < limit if intent.side is Side.BUY else highs[fill_index] > limit
        )
        if not traded_through:
            result.unfilled[intent.arm] = result.unfilled.get(intent.arm, 0) + 1
            continue

        entries_by_minute[signal_ts] = entries_by_minute.get(signal_ts, 0) + 1
        exit_index, exit_price, reason = _exit_after_fill(
            intent.side, limit, fill_index, highs, lows, closes, scalp
        )
        if reason == "take_profit":
            exit_fee = maker
        else:
            exit_fee = taker
            # A market exit pays the slippage it crosses, against us.
            exit_price *= (1 - slip) if intent.side is Side.BUY else (1 + slip)
        quantity = notional / limit
        fees = limit * quantity * maker + exit_price * quantity * exit_fee
        trade = SimTrade(
            symbol=symbol,
            arm=intent.arm,
            side=intent.side,
            signal_ts=signal_ts,
            entry_ts=int(ts[fill_index]),
            exit_ts=int(ts[exit_index]) + MINUTE_MS,
            entry=limit,
            exit=exit_price,
            notional=notional,
            fees=fees,
            reason=reason,
            bars_held=exit_index - fill_index + 1,
        )
        result.trades.append(trade)
        busy_until[symbol] = trade.exit_ts
        open_exits.append(trade.exit_ts)
    return result


def summarise(trades: list[SimTrade], seed: int = 7) -> dict[str, float]:
    """Per-arm statistics, with a bootstrap CI on the per-trade expectancy."""
    if not trades:
        return {"trades": 0}
    returns = np.array([t.net_return for t in trades])
    wins = returns > 0
    gross_win = returns[wins].sum()
    gross_loss = -returns[~wins].sum()
    rng = np.random.default_rng(seed)
    means = rng.choice(returns, size=(2000, len(returns)), replace=True).mean(axis=1)
    reasons = pd.Series([t.reason for t in trades]).value_counts(normalize=True).to_dict()
    return {
        "trades": len(trades),
        "win_rate": float(wins.mean()),
        "expectancy_pct": float(returns.mean() * 100),
        "ci_low_pct": float(np.percentile(means, 2.5) * 100),
        "ci_high_pct": float(np.percentile(means, 97.5) * 100),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "pnl": float(sum(t.pnl for t in trades)),
        "fees": float(sum(t.fees for t in trades)),
        "avg_bars": float(np.mean([t.bars_held for t in trades])),
        "take_profit_share": float(reasons.get("take_profit", 0.0)),
        "stop_share": float(reasons.get("stop", 0.0)),
        "time_stop_share": float(reasons.get("time_stop", 0.0)),
    }
