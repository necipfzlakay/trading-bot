"""Portfolio state: open positions, realised trades, equity.

Deliberately venue-agnostic. The same object backs a backtest and a live run, so
the numbers the dashboard shows are produced by the same code that produced the
backtest — no second implementation to drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tbot.core.logging import get_logger
from tbot.core.models import (
    EquityPoint,
    ExitReason,
    Fill,
    Position,
    Regime,
    Side,
    Trade,
)

log = get_logger("engine.portfolio")


@dataclass(slots=True)
class Portfolio:
    """Positions, closed trades and the equity curve."""

    starting_cash: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    peak_equity: float = 0.0

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.starting_cash
        self.peak_equity = max(self.peak_equity, self.starting_cash)

    # -- positions ---------------------------------------------------------

    def open_position(
        self,
        fill: Fill,
        stop_price: float,
        setup: str,
        risk_amount: float,
        conviction: float,
        regime: Regime,
        leverage: float = 1.0,
        conviction_id: int | None = None,
    ) -> Position:
        position = Position(
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            entry_price=fill.price,
            entry_ts=fill.ts,
            initial_stop=stop_price,
            current_stop=stop_price,
            setup=setup,
            risk_amount=risk_amount,
            leverage=leverage,
            fees_paid=fill.fee,
            conviction_at_entry=conviction,
            entry_conviction_id=conviction_id,
            meta={"regime_at_entry": regime.value},
        )
        self.positions[fill.symbol] = position
        self.cash -= fill.notional + fill.fee
        return position

    def close_position(
        self, symbol: str, exit_price: float, exit_ts: int, reason: ExitReason, fee: float = 0.0
    ) -> Trade | None:
        position = self.positions.pop(symbol, None)
        if position is None:
            return None

        gross = (exit_price - position.entry_price) * position.quantity * position.side.sign
        total_fees = position.fees_paid + fee
        pnl = gross - fee

        r_unit = position.r_unit
        r_multiple = (
            (exit_price - position.entry_price) * position.side.sign / r_unit if r_unit > 0 else 0.0
        )

        trade = Trade(
            symbol=symbol,
            side=position.side,
            setup=position.setup,
            entry_ts=position.entry_ts,
            exit_ts=exit_ts,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl=pnl,
            fees=total_fees,
            r_multiple=r_multiple,
            mfe_r=position.mfe_r,
            mae_r=position.mae_r,
            bars_held=position.bars_held,
            exit_reason=reason,
            regime_at_entry=Regime(position.meta.get("regime_at_entry", Regime.UNKNOWN.value)),
            conviction_at_entry=position.conviction_at_entry,
            leverage=position.leverage,
        )
        self.trades.append(trade)
        self.cash += exit_price * position.quantity - fee
        return trade

    def update_bar(self, symbol: str, high: float, low: float) -> None:
        """Advance a position's excursion tracking and bar count."""
        position = self.positions.get(symbol)
        if position is None:
            return
        position.update_excursion(high, low)
        position.bars_held += 1

    # -- valuation ---------------------------------------------------------

    def equity(self, marks: dict[str, float]) -> float:
        total = self.cash
        for symbol, position in self.positions.items():
            price = marks.get(symbol, position.entry_price)
            total += price * position.quantity if position.side is Side.BUY else 0.0
            if position.side is Side.SELL:
                total += position.unrealized_pnl(price)
        return total

    def unrealized(self, marks: dict[str, float]) -> float:
        return sum(p.unrealized_pnl(marks.get(s, p.entry_price)) for s, p in self.positions.items())

    def record_equity(self, ts: int, marks: dict[str, float]) -> EquityPoint:
        equity = self.equity(marks)
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 1.0 - equity / self.peak_equity if self.peak_equity > 0 else 0.0
        point = EquityPoint(
            ts=ts,
            equity=equity,
            cash=self.cash,
            unrealized=self.unrealized(marks),
            open_positions=len(self.positions),
            drawdown=max(0.0, drawdown),
        )
        self.equity_curve.append(point)
        return point

    # -- queries -----------------------------------------------------------

    @property
    def open_symbols(self) -> list[str]:
        return list(self.positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def summary(self) -> dict:
        wins = [t for t in self.trades if t.is_win]
        losses = [t for t in self.trades if not t.is_win]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        return {
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.trades) if self.trades else 0.0,
            "total_pnl": self.total_pnl(),
            "total_fees": sum(t.fees for t in self.trades),
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "expectancy_r": (
                sum(t.r_multiple for t in self.trades) / len(self.trades) if self.trades else 0.0
            ),
            "open_positions": len(self.positions),
            "cash": self.cash,
        }
