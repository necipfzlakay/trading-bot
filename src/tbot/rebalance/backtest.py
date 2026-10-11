"""Replay of the three rebalancing strategies on daily data.

The same ``signals`` functions the live engine calls, on the same closed daily
bars: the decision is taken on day *t*'s close and filled at day *t+1*'s open
(invariant 1). Costs are the venue's measured rates plus slippage on every leg.

Deliberately small. These strategies trade a few times a month, so the event
loop is a day loop, and the only state is cash and a set of fixed-quantity
holdings — exactly the live engine's model (``signals`` module docstring).

Carry is replayed as what it is when both legs fill: the spot long and the
perp short cancel each other's price move, and what is left is the funding
the short receives minus four legs of cost. The basis — the perp drifting
against spot while held — is not in the archive and is therefore not in this
number; the forward test is where it gets measured.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from tbot.rebalance.signals import DAY_MS, carry_wants, funding_sum, trend_wants, xsmom_wants

MONDAY = 0


@dataclass(slots=True)
class Bars:
    """Aligned daily bars: one row per day, one column per symbol."""

    days: list[int]  # day open, UTC ms
    open: dict[str, list[float]]
    close: dict[str, list[float]]


@dataclass(slots=True)
class Result:
    equity: list[tuple[int, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        return self.equity[-1][1] / self.equity[0][1] - 1.0 if self.equity else 0.0

    @property
    def max_drawdown(self) -> float:
        peak, worst = 0.0, 0.0
        for _, value in self.equity:
            peak = max(peak, value)
            worst = max(worst, 1.0 - value / peak if peak > 0 else 0.0)
        return worst

    @property
    def sharpe(self) -> float:
        """Annualised daily Sharpe (365 days: crypto does not close)."""
        values = [v for _, v in self.equity]
        rets = [b / a - 1.0 for a, b in zip(values, values[1:], strict=False) if a > 0]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return mean / math.sqrt(var) * math.sqrt(365) if var > 0 else 0.0

    @property
    def years(self) -> float:
        return (self.equity[-1][0] - self.equity[0][0]) / (365 * DAY_MS) if self.equity else 0.0

    @property
    def cagr(self) -> float:
        if self.years <= 0:
            return 0.0
        return (1.0 + self.total_return) ** (1.0 / self.years) - 1.0

    def summary(self) -> dict:
        rets = [t["return"] for t in self.trades]
        wins = [r for r in rets if r > 0]
        return {
            "trades": len(rets),
            "win_rate": len(wins) / len(rets) if rets else 0.0,
            "mean_trade": sum(rets) / len(rets) if rets else 0.0,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
        }


def _weekday(day_ms: int) -> int:
    return ((day_ms // DAY_MS) + 3) % 7  # 1970-01-01 was a Thursday


def _closes_on(weekday: int) -> Callable[[int], bool]:
    """Decide on the bar whose *close* falls on ``weekday`` at 00:00 UTC.

    The live engine decides at an instant — Monday 00:00 UTC is the venue's
    weekly boundary (invariant 17) — which is the close of Sunday's daily bar.
    """
    return lambda day: _weekday(day + DAY_MS) == weekday


def replay_holdings(
    bars: Bars,
    decide: Callable[[int, set[str]], list[str] | None],
    *,
    slots: int,
    cost: float,
    start_cash: float = 1_000.0,
    decide_on: Callable[[int], bool] = lambda _day: True,
) -> Result:
    """Long-only fixed-quantity holdings, one equal slot per name.

    ``decide(i, held)`` sees bars up to and including day ``i`` and returns the
    names to hold (``None`` = no opinion, keep everything). Orders fill at day
    ``i + 1``'s open. A new name gets ``equity / slots`` of cash, capped by the
    cash there is.
    """
    cash = start_cash
    held: dict[str, tuple[float, float, int]] = {}  # symbol -> (qty, entry, entry_day)
    result = Result()
    n = len(bars.days)

    def mark(i: int) -> float:
        value = cash
        for symbol, (qty, entry, _) in held.items():
            price = bars.close[symbol][i]
            value += qty * (price if math.isfinite(price) else entry)
        return value

    for i in range(n - 1):
        result.equity.append((bars.days[i], mark(i)))
        if not decide_on(bars.days[i]):
            continue
        wanted = decide(i, set(held))
        if wanted is None:
            continue
        fill = i + 1
        for symbol in [s for s in held if s not in wanted]:
            price = bars.open[symbol][fill]
            if not math.isfinite(price):
                continue
            qty, entry, entered = held.pop(symbol)
            proceeds = qty * price * (1.0 - cost)
            cash += proceeds
            paid = qty * entry * (1.0 + cost)
            result.trades.append({
                "symbol": symbol, "entry_day": entered, "exit_day": bars.days[fill],
                "return": proceeds / paid - 1.0,
            })
        equity = mark(i)
        for symbol in wanted:
            if symbol in held:
                continue
            price = bars.open[symbol][fill]
            budget = min(equity / slots, cash)
            if not math.isfinite(price) or budget <= 5.0:
                continue
            qty = budget / (price * (1.0 + cost))
            cash -= qty * price * (1.0 + cost)
            held[symbol] = (qty, price, bars.days[fill])
    result.equity.append((bars.days[-1], mark(n - 1)))
    return result


def trend(
    bars: Bars, symbols: Sequence[str], *, sma_days: int, cost: float, weekday: int = MONDAY
) -> Result:
    def decide(i: int, held: set[str]) -> list[str] | None:
        wanted = []
        for symbol in symbols:
            view = bars.close[symbol][: i + 1]
            verdict = trend_wants(view, sma_days)
            if verdict is None:
                if symbol in held:
                    wanted.append(symbol)  # no opinion is not a sale
            elif verdict:
                wanted.append(symbol)
        return wanted

    return replay_holdings(
        bars, decide, slots=len(symbols), cost=cost, decide_on=_closes_on(weekday),
    )


def xsmom(
    bars: Bars, *, lookback_days: int, top_k: int, hold_rank: int, cost: float,
    weekday: int = MONDAY,
) -> Result:
    def decide(i: int, held: set[str]) -> list[str]:
        view = {s: c[: i + 1] for s, c in bars.close.items()}
        return xsmom_wants(
            view, held, lookback_days=lookback_days, top_k=top_k, hold_rank=hold_rank
        )

    return replay_holdings(
        bars, decide, slots=top_k, cost=cost, decide_on=_closes_on(weekday),
    )


def buy_and_hold(bars: Bars, symbols: Sequence[str], *, cost: float) -> Result:
    return replay_holdings(
        bars, lambda _i, _held: list(symbols), slots=len(symbols), cost=cost,
        decide_on=lambda day: day == bars.days[0],
    )


def carry(
    funding: Mapping[str, Sequence[tuple[int, float]]],
    days: Sequence[int],
    *,
    window_days: int,
    enter_sum: float,
    exit_sum: float,
    max_pairs: int,
    pair_cost: float,
    start_cash: float = 1_000.0,
) -> Result:
    """Funding received on each held pair, minus the round trip of both legs.

    Decided at each day's close (00:00 UTC, right after a settlement), filled
    at once; a pair earns every settlement after its entry and before its exit.
    ``pair_cost`` is one side of both legs as a fraction of one leg's notional
    (spot + perp), paid at entry and again at exit.
    """
    equity = start_cash
    held: dict[str, tuple[float, int, float]] = {}  # symbol -> (notional, entry_day, earned)
    result = Result()
    for day_index, day in enumerate(days):
        close_ts = day + DAY_MS  # the decision is taken at this day's close
        if day_index > 0:
            prev_close = days[day_index - 1] + DAY_MS
            for symbol, (notional, entered, earned) in list(held.items()):
                got = sum(
                    rate * notional for ts, rate in funding[symbol] if prev_close < ts <= close_ts
                )
                equity += got
                held[symbol] = (notional, entered, earned + got)
        result.equity.append((close_ts, equity))

        sums = {s: funding_sum(f, close_ts, window_days) for s, f in funding.items()}
        wanted = carry_wants(
            sums, set(held), enter_sum=enter_sum, exit_sum=exit_sum, max_pairs=max_pairs
        )
        for symbol in [s for s in held if s not in wanted]:
            notional, entered, earned = held.pop(symbol)
            equity -= notional * pair_cost
            result.trades.append({
                "symbol": symbol, "entry_day": entered, "exit_day": close_ts,
                "return": (earned - 2 * notional * pair_cost) / notional,
            })
        for symbol in wanted:
            if symbol in held:
                continue
            # Each pair ties up two legs of notional: spot paid in full, perp
            # margined at 1x.
            notional = equity / (2 * max_pairs)
            equity -= notional * pair_cost
            held[symbol] = (notional, close_ts, 0.0)
    return result
