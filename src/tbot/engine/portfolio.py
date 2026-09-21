"""Portfolio state: open positions, realised trades, equity.

Deliberately venue-agnostic. The same object backs a backtest and a live run, so
the numbers the dashboard shows are produced by the same code that produced the
backtest — no second implementation to drift.
"""

from __future__ import annotations

import json
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
    # Scratch for close_position: what the venue hands back, which differs by
    # side. Declared because the class uses __slots__.
    _returned: float = 0.0

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.starting_cash
        self.peak_equity = max(self.peak_equity, self.starting_cash)

    def seed_from_venue(self, cash: float, marks: dict[str, float] | None = None) -> None:
        """Adopt the account's real balance as the starting point.

        In live and demo mode ``starting_balance`` from config is fiction — the
        account holds whatever it holds. Without this the portfolio carries the
        configured number instead: sizing reads a cash limit that does not
        exist, and the first bar's equity contradicts the balance read at
        startup, which the implausible-jump guard correctly flags as corrupt
        data. Call it once, after reconciliation, before the first bar.
        """
        self.cash = cash
        equity = self.equity(marks or {})
        self.starting_cash = equity
        self.peak_equity = equity
        log.info("portfolio_seeded_from_venue", cash=round(cash, 2), equity=round(equity, 2))

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
            # ``original_quantity`` is what R is measured against for the rest
            # of this position's life. Without it a scaled-out position would
            # compute R against whatever is left, and half a position closing
            # at +2R would report +2R on half the money.
            meta={"regime_at_entry": regime.value, "original_quantity": fill.quantity},
        )
        self.positions[fill.symbol] = position
        self.cash -= fill.notional + fill.fee
        return position

    def scale_out(
        self, symbol: str, price: float, fraction: float, fee: float = 0.0
    ) -> float | None:
        """Sell part of a position, keeping the rest open. Returns the quantity sold.

        The fraction is of the **original** quantity, not what remains, so this
        cannot compound into a full exit if it is ever called twice.

        No ``Trade`` row is written here. A trade in this system is one
        position's whole life — its MAE, MFE, bars held and R all describe the
        position, not a slice of it — and emitting a row per slice would
        silently change what every per-trade statistic means. The realised
        money is banked on the position instead and folded into the Trade when
        it finally closes.
        """
        position = self.positions.get(symbol)
        if position is None:
            return None

        original = float(position.meta.get("original_quantity", position.quantity))
        quantity = min(original * fraction, position.quantity)
        if quantity <= 0:
            return None

        gross = (price - position.entry_price) * quantity * position.side.sign
        position.quantity -= quantity
        position.fees_paid += fee
        position.meta["scaled_out"] = True
        position.meta["realised_pnl"] = float(position.meta.get("realised_pnl", 0.0)) + gross
        position.meta["scaled_quantity"] = (
            float(position.meta.get("scaled_quantity", 0.0)) + quantity
        )
        # Same asymmetry as a full close: a long banks the sale, a short
        # releases the slice of margin it posted plus that slice's result.
        if position.side is Side.BUY:
            self.cash += price * quantity - fee
        else:
            self.cash += position.entry_price * quantity + gross - fee
        return quantity

    def close_position(
        self, symbol: str, exit_price: float, exit_ts: int, reason: ExitReason, fee: float = 0.0
    ) -> Trade | None:
        position = self.positions.pop(symbol, None)
        if position is None:
            return None

        gross = (exit_price - position.entry_price) * position.quantity * position.side.sign
        # Anything already taken off at a scale-out level is part of this
        # trade's result, not a separate one.
        realised_earlier = float(position.meta.get("realised_pnl", 0.0))
        total_fees = position.fees_paid + fee
        pnl = gross + realised_earlier - fee

        # Measured against the risk the position actually carried — original
        # quantity times the initial stop distance — so R stays comparable
        # whether or not part of it was sold early. With no scale-out this is
        # algebraically the old formula.
        r_unit = position.r_unit
        original = float(position.meta.get("original_quantity", position.quantity))
        risk_at_entry = original * r_unit
        r_multiple = (gross + realised_earlier) / risk_at_entry if risk_at_entry > 0 else 0.0

        # A long returns the proceeds of the sale. A short returns the margin
        # it posted and settles the difference — paying out ``exit_price *
        # quantity`` would hand back money the position never held.
        if position.side is Side.BUY:
            self._returned = exit_price * position.quantity - fee
        else:
            self._returned = position.entry_price * position.quantity + gross - fee

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
        self.cash += self._returned
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
        """Cash plus what the open positions are worth.

        The two sides are not symmetric and cannot share a line. A long *spent*
        its cash and holds an asset, so the asset's market value is the entry.
        A short posted **margin** — ``entry_price * quantity`` at 1x, which
        ``open_position`` deducted from cash — and owes a contract. Its
        contribution is therefore that margin coming back, plus or minus the
        move.

        Counting only ``unrealized_pnl`` for a short, as this did while nothing
        could open one, loses the margin entirely: a short opened at 100 on a
        300 account reported equity 200 before the price had moved at all.
        """
        total = self.cash
        for symbol, position in self.positions.items():
            price = marks.get(symbol, position.entry_price)
            if position.side is Side.BUY:
                total += price * position.quantity
            else:
                total += position.entry_price * position.quantity
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


def restore_position(raw: dict) -> Position:
    """Rebuild a position from its database row.

    Restoring ``meta`` is not cosmetic: it carries ``stop_order_id``, and
    dropping it left the engine unable to move the venue stop it had itself
    placed — every restart lost the trail (audit #37). Both engine loops load
    positions through here so neither can quietly drop a field the other keeps.
    """
    return Position(
        symbol=raw["symbol"],
        side=Side(raw["side"]),
        quantity=raw["quantity"],
        entry_price=raw["entry_price"],
        entry_ts=raw["entry_ts"],
        initial_stop=raw["initial_stop"],
        current_stop=raw["current_stop"],
        setup=raw["setup"],
        risk_amount=raw["risk_amount"],
        leverage=raw["leverage"],
        fees_paid=raw["fees_paid"],
        max_favorable_price=raw["max_favorable_price"],
        max_adverse_price=raw["max_adverse_price"],
        bars_held=raw["bars_held"],
        conviction_at_entry=raw["conviction_at_entry"],
        meta=json.loads(raw["meta"]) if raw.get("meta") else {},
    )
