"""Simulated execution with explicit, pessimistic cost assumptions.

**This is the backtester's fill engine, not a way to run the bot.** There used
to be a ``paper`` mode that drove the live loop with this broker; it was removed
once the demo venue made it redundant — demo answers the same question against a
real order book, with real rejections, real fees and real partial fills. A
second execution path that only the simulator exercised turned out to be a place
for bugs to hide: audit #66 found every simulated short being dropped on the
floor, in code nobody was running.

Three deliberate pessimism choices, because a simulator that flatters the
strategy is worse than no simulator:

* fills happen at the **next bar's open**, never at the signal bar's close —
  the close is not available to trade at once it is known
* slippage always moves against the order
* fills are checked against the bar's actual range, so a limit price the market
  never reached does not fill
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tbot.core.logging import get_logger
from tbot.core.models import (
    Candle,
    Fill,
    MarketSpec,
    Order,
    OrderStatus,
    OrderType,
    Side,
)
from tbot.execution.broker import Broker

log = get_logger("execution.paper")


@dataclass(slots=True)
class PaperBroker(Broker):
    """In-memory venue. Deterministic given the same bars and orders."""

    specs: dict[str, MarketSpec]
    cash: float
    quote: str = "USDT"
    taker_fee: float = 0.001
    maker_fee: float = 0.001
    slippage_bps: float = 5.0

    balances: dict[str, float] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    pending: list[Order] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.balances.setdefault(self.quote, self.cash)

    # -- Broker interface --------------------------------------------------

    def market_spec(self, symbol: str) -> MarketSpec:
        return self.specs[symbol]

    def submit(self, order: Order) -> Order:
        """Queue an order for the next bar.

        Returning the existing order on a duplicate id mirrors what a real
        exchange does, so the idempotency path is exercised in simulation too.
        """
        existing = self.orders.get(order.client_id)
        if existing is not None:
            return existing

        order.status = OrderStatus.OPEN
        self.orders[order.client_id] = order
        self.pending.append(order)
        return order

    def cancel(self, client_id: str) -> bool:
        order = self.orders.get(client_id)
        if order is None or order.status.is_terminal:
            return False
        order.status = OrderStatus.CANCELED
        self.pending = [o for o in self.pending if o.client_id != client_id]
        return True

    def fetch_balance(self) -> dict[str, float]:
        return dict(self.balances)

    def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if not o.status.is_terminal and (symbol is None or o.symbol == symbol)
        ]

    def fetch_fills(self, symbol: str | None = None, since: int | None = None) -> list[Fill]:
        return [
            f
            for f in self.fills
            if (symbol is None or f.symbol == symbol) and (since is None or f.ts >= since)
        ]

    # -- simulation --------------------------------------------------------

    def process_bar(self, symbol: str, candle: Candle) -> list[Fill]:
        """Execute queued orders against a newly opened bar.

        Called with the bar *after* the one that produced the signal, which is
        what enforces the one-bar execution delay everywhere at once.
        """
        filled: list[Fill] = []
        still_pending: list[Order] = []

        for order in self.pending:
            if order.symbol != symbol:
                still_pending.append(order)
                continue
            fill = self._try_fill(order, candle)
            if fill is None:
                still_pending.append(order)
            else:
                filled.append(fill)

        self.pending = still_pending
        return filled

    def _try_fill(self, order: Order, candle: Candle) -> Fill | None:
        if order.type is OrderType.MARKET:
            price = self._apply_slippage(candle.open, order.side)
            return self._execute(order, price, candle.ts, is_maker=False)

        limit = order.price
        if limit is None:
            return None

        # A limit only fills if the bar actually traded through it. Filling on
        # an untouched price is the classic way a backtest invents entries.
        if order.side is Side.BUY:
            if candle.low <= limit:
                return self._execute(order, min(limit, candle.open), candle.ts, is_maker=True)
        elif candle.high >= limit:
            return self._execute(order, max(limit, candle.open), candle.ts, is_maker=True)
        return None

    def _apply_slippage(self, price: float, side: Side) -> float:
        """Slippage always moves against the order, never in its favour."""
        drift = price * (self.slippage_bps / 10_000.0)
        return price + drift if side is Side.BUY else price - drift

    def _execute(self, order: Order, price: float, ts: int, is_maker: bool) -> Fill:
        spec = self.specs.get(order.symbol)
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        if spec is not None:
            fee_rate = spec.maker_fee if is_maker else spec.taker_fee

        notional = price * order.quantity
        fee = notional * fee_rate

        base = order.symbol.split("/")[0]
        if order.side is Side.BUY:
            self.balances[self.quote] = self.balances.get(self.quote, 0.0) - notional - fee
            self.balances[base] = self.balances.get(base, 0.0) + order.quantity
        else:
            self.balances[self.quote] = self.balances.get(self.quote, 0.0) + notional - fee
            self.balances[base] = self.balances.get(base, 0.0) - order.quantity

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_price = price
        order.fee_paid = fee
        order.updated_ts = ts

        fill = Fill(
            order_client_id=order.client_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            fee=fee,
            ts=ts,
            is_maker=is_maker,
            exchange_trade_id=f"paper-{len(self.fills)}",
        )
        self.fills.append(fill)
        return fill

    def force_fill_at(self, order: Order, price: float, ts: int) -> Fill:
        """Fill at an exact price — used for stop exits triggered intrabar.

        A stop is a resting instruction, not a next-bar decision: when price
        trades through the level the exit happens there. Deferring it to the
        next open would understate losses on exactly the bars that matter.
        """
        self.orders.setdefault(order.client_id, order)
        self.pending = [o for o in self.pending if o.client_id != order.client_id]
        return self._execute(order, self._apply_slippage(price, order.side), ts, is_maker=False)

    def equity(self, marks: dict[str, float]) -> float:
        """Total account value, marking open base balances at ``marks``."""
        total = self.balances.get(self.quote, 0.0)
        for currency, amount in self.balances.items():
            if currency == self.quote or amount == 0:
                continue
            price = marks.get(f"{currency}/{self.quote}")
            if price:
                total += amount * price
        return total
