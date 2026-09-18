"""Broker interface shared by backtest, paper and live.

One interface, three implementations, and the strategy cannot tell them apart.
That is the property that makes backtest results mean something: if simulated
and live execution diverge, it is a bug in a broker rather than a difference in
strategy code.

The idempotency contract lives here. ``client_order_id`` is derived from
(strategy, symbol, bar, action), so a crash between sending an order and
recording the acknowledgement cannot produce a duplicate on restart — the
exchange rejects the second one.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from tbot.core.models import Fill, MarketSpec, Order, OrderType, Side


def client_order_id(strategy: str, symbol: str, bar_ts: int, action: str) -> str:
    """Deterministic order id — the same inputs always produce the same id.

    Bybit caps client order ids at 36 characters, so the readable prefix is
    combined with a hash of the full tuple rather than the tuple itself.
    """
    raw = f"{strategy}:{symbol}:{bar_ts}:{action}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    prefix = symbol.split("/")[0][:6].lower()
    return f"tb-{prefix}-{action[:4]}-{digest}"


class Broker(ABC):
    """Everything the engine needs from a venue."""

    @abstractmethod
    def market_spec(self, symbol: str) -> MarketSpec:
        """Exchange constraints: minimums, step sizes, fees."""

    @abstractmethod
    def submit(self, order: Order) -> Order:
        """Send an order. Returns it updated with venue state.

        Must be safe to call twice with the same ``client_id``: the second call
        returns the existing order rather than creating another.
        """

    @abstractmethod
    def cancel(self, client_id: str) -> bool:
        """Cancel a resting order. False when it was already terminal."""

    @abstractmethod
    def fetch_balance(self) -> dict[str, float]:
        """Free balance per currency."""

    @abstractmethod
    def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Orders the venue considers live."""

    @abstractmethod
    def fetch_fills(self, symbol: str | None = None, since: int | None = None) -> list[Fill]:
        """Executions, for reconciling local state against the venue."""

    def build_order(
        self,
        strategy: str,
        symbol: str,
        bar_ts: int,
        action: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        reduce_only: bool = False,
    ) -> Order:
        """Construct an order with a deterministic id."""
        return Order(
            client_id=client_order_id(strategy, symbol, bar_ts, action),
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            created_ts=bar_ts,
            updated_ts=bar_ts,
        )
