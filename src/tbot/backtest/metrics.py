"""Performance metrics.

Everything is reported net of costs, and the headline comparison is always
against buy-and-hold rather than against zero. A strategy that made money during
a period when everything went up has not demonstrated anything yet.

R-multiples are preferred to currency wherever possible: they stay comparable
across symbols, position sizes and account balances, so a result from a $100
account means the same thing as a result from a $10,000 one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from tbot.core.models import EquityPoint, Trade

PERIODS_PER_YEAR = {"1h": 8760, "4h": 2190, "1d": 365, "1w": 52}


@dataclass(slots=True)
class Metrics:
    trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    avg_win_r: float
    avg_loss_r: float
    avg_bars_held: float
    total_fees: float
    fee_drag: float
    turnover: float
    best_trade_r: float
    worst_trade_r: float
    longest_drawdown_bars: int
    exposure: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    starting_balance: float,
    timeframe: str = "1h",
) -> Metrics:
    """Summarise a completed run."""
    if not equity_curve and not trades:
        return _empty()

    # Trade statistics stand on their own. A run can produce trades without a
    # stored equity curve (an excursion study, a replayed trade log), and
    # dropping the trade metrics in that case would report zeros for numbers we
    # actually have.
    equity = (
        np.array([p.equity for p in equity_curve], dtype="float64")
        if equity_curve
        else np.array([starting_balance, starting_balance + sum(t.pnl for t in trades)])
    )
    returns = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([0.0])
    returns = returns[np.isfinite(returns)]

    periods = PERIODS_PER_YEAR.get(timeframe, 8760)
    years = len(equity) / periods if periods else 1.0

    total_return = equity[-1] / starting_balance - 1.0
    cagr = (
        (equity[-1] / starting_balance) ** (1 / years) - 1.0
        if years > 0 and equity[-1] > 0
        else -1.0
    )

    sharpe = _annualised_sharpe(returns, periods)
    sortino = _annualised_sortino(returns, periods)
    max_dd, longest_dd = _drawdown(equity)

    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    total_fees = sum(t.fees for t in trades)

    exposure = (
        sum(1 for p in equity_curve if p.open_positions > 0) / len(equity_curve)
        if equity_curve
        else 0.0
    )

    return Metrics(
        trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=gross_win / gross_loss
        if gross_loss > 0
        else (float("inf") if gross_win else 0.0),
        expectancy_r=float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
        total_return=float(total_return),
        cagr=float(cagr),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        calmar=float(cagr / max_dd) if max_dd > 0 else 0.0,
        avg_win_r=float(np.mean([t.r_multiple for t in wins])) if wins else 0.0,
        avg_loss_r=float(np.mean([t.r_multiple for t in losses])) if losses else 0.0,
        avg_bars_held=float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
        total_fees=float(total_fees),
        # How much of the gross profit went to the venue. Above ~30% means the
        # strategy is trading too often for the edge it has.
        fee_drag=float(total_fees / gross_win) if gross_win > 0 else float("inf"),
        turnover=float(sum(t.entry_price * t.quantity for t in trades) / starting_balance)
        if starting_balance
        else 0.0,
        best_trade_r=max((t.r_multiple for t in trades), default=0.0),
        worst_trade_r=min((t.r_multiple for t in trades), default=0.0),
        longest_drawdown_bars=longest_dd,
        exposure=float(exposure),
    )


def _empty() -> Metrics:
    return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def _annualised_sharpe(returns: np.ndarray, periods: int) -> float:
    if returns.size < 2:
        return 0.0
    sd = float(np.std(returns, ddof=1))
    return float(np.mean(returns) / sd * np.sqrt(periods)) if sd > 0 else 0.0


def _annualised_sortino(returns: np.ndarray, periods: int) -> float:
    """Sortino punishes only downside deviation, which is the risk that matters."""
    if returns.size < 2:
        return 0.0
    downside = returns[returns < 0]
    if downside.size < 2:
        return float("inf") if np.mean(returns) > 0 else 0.0
    dd = float(np.std(downside, ddof=1))
    return float(np.mean(returns) / dd * np.sqrt(periods)) if dd > 0 else 0.0


def _drawdown(equity: np.ndarray) -> tuple[float, int]:
    """Maximum drawdown and the longest stretch spent below a prior peak.

    Duration matters as much as depth: a 15% drawdown lasting two months and one
    lasting two years are very different things to sit through.
    """
    peaks = np.maximum.accumulate(equity)
    drawdowns = 1.0 - equity / peaks
    max_dd = float(np.max(drawdowns)) if drawdowns.size else 0.0

    longest = current = 0
    for value in drawdowns:
        current = current + 1 if value > 1e-9 else 0
        longest = max(longest, current)

    return max_dd, longest


def buy_and_hold(prices: pd.Series, starting_balance: float, fee: float = 0.001) -> dict:
    """The benchmark the strategy has to beat.

    Beating zero is not the bar. In a period when the asset tripled, a strategy
    that returned 20% underperformed doing nothing.
    """
    if prices.empty:
        return {"total_return": 0.0, "max_drawdown": 0.0, "final_equity": starting_balance}

    units = (starting_balance * (1 - fee)) / float(prices.iloc[0])
    equity = units * prices.to_numpy(dtype="float64")
    final = float(equity[-1] * (1 - fee))
    max_dd, _ = _drawdown(equity)

    return {
        "total_return": final / starting_balance - 1.0,
        "max_drawdown": max_dd,
        "final_equity": final,
    }


def rolling_expectancy(trades: list[Trade], window: int = 50) -> pd.Series:
    """Expectancy over a rolling window of trades.

    The evaluation unit is trades rather than calendar time — this is what the
    risk budget watches to decide whether the edge is still there.
    """
    if not trades:
        return pd.Series(dtype="float64")
    r = pd.Series([t.r_multiple for t in trades])
    return r.rolling(window, min_periods=max(5, window // 5)).mean()


def trade_table(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": t.symbol,
                "setup": t.setup,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "bars": t.bars_held,
                "r": round(t.r_multiple, 3),
                "mfe_r": round(t.mfe_r, 3),
                "mae_r": round(t.mae_r, 3),
                "pnl": round(t.pnl, 4),
                "fees": round(t.fees, 4),
                "exit": t.exit_reason.value,
                "regime": t.regime_at_entry.value,
                "conviction": round(t.conviction_at_entry, 1),
            }
            for t in trades
        ]
    )
