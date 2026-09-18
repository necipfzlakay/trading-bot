"""Live execution against Bybit through ccxt.

Same interface as the paper broker, so the engine cannot tell them apart. Demo
and real trading differ only in credentials and one flag — ``enable_demo_trading``
points ccxt at ``api-demo.bybit.com`` while market data still comes from
mainnet, which is what makes demo results comparable to live ones.

Two things this layer must get right, because both fail silently otherwise:

* **Precision.** Quantities and prices go through ccxt's ``amount_to_precision``
  and ``price_to_precision``, which return strings. Rounding by hand produces
  orders the venue rejects, or worse, accepts at a size nobody intended.
* **Idempotency.** Every order carries a deterministic ``clientOrderId``. If the
  process dies between sending and recording, the retry is refused by the venue
  as a duplicate instead of doubling the position.
"""

from __future__ import annotations

import time

import ccxt

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger
from tbot.core.models import (
    Fill,
    MarketSpec,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Venue,
)
from tbot.execution.broker import Broker

log = get_logger("execution.ccxt")

_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


class CCXTBroker(Broker):
    """Bybit spot via ccxt, for demo and live alike."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        venue: Venue,
        quote: str = "USDT",
        default_type: str = "spot",
    ):
        self.venue = venue
        self.quote = quote
        self.exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": default_type},
            }
        )
        if venue is Venue.BYBIT_DEMO:
            # Demo lives on the main platform at a separate host. Crucially it
            # is NOT the testnet — mixing the two produces authentication
            # errors that look like credential problems.
            self.exchange.enable_demo_trading(True)
            log.info("demo_trading_enabled", host="api-demo.bybit.com")

        self._markets_loaded = False
        self._specs: dict[str, MarketSpec] = {}

    # -- setup -------------------------------------------------------------

    def load_markets(self) -> None:
        if not self._markets_loaded:
            self.exchange.load_markets()
            self._markets_loaded = True

    def market_spec(self, symbol: str) -> MarketSpec:
        self.load_markets()
        if symbol in self._specs:
            return self._specs[symbol]

        market = self.exchange.markets[symbol]
        spec = MarketSpec(
            symbol=symbol,
            base=market["base"],
            quote=market["quote"],
            min_notional=float(market["limits"]["cost"]["min"] or 0.0),
            min_amount=float(market["limits"]["amount"]["min"] or 0.0),
            amount_step=float(market["precision"]["amount"] or 0.0),
            price_step=float(market["precision"]["price"] or 0.0),
            maker_fee=float(market.get("maker") or 0.001),
            taker_fee=float(market.get("taker") or 0.001),
            active=bool(market.get("active")),
        )
        self._specs[symbol] = spec
        return spec

    def refresh_fees(self, symbols: list[str]) -> None:
        """Pull actual fee tiers from the account.

        Backtests assume a fee. If the real one is worse, every earlier result
        was optimistic, and it is better to find that out at startup than from a
        month of underperformance.
        """
        try:
            fees = self.exchange.fetch_trading_fees()
        except ccxt.BaseError as exc:
            log.warning("fee_fetch_failed", error=str(exc)[:160])
            return

        for symbol in symbols:
            entry = fees.get(symbol)
            if not entry:
                continue
            spec = self.market_spec(symbol)
            actual_maker = float(entry.get("maker") or spec.maker_fee)
            actual_taker = float(entry.get("taker") or spec.taker_fee)
            if actual_taker > spec.taker_fee:
                log.warning(
                    "fee_worse_than_assumed",
                    symbol=symbol,
                    assumed=spec.taker_fee,
                    actual=actual_taker,
                )
            self._specs[symbol] = spec.replace(maker_fee=actual_maker, taker_fee=actual_taker)

    # -- orders ------------------------------------------------------------

    def submit(self, order: Order) -> Order:
        """Send an order, or return the existing one with the same client id."""
        self.load_markets()
        spec = self.market_spec(order.symbol)

        existing = self._find_by_client_id(order.symbol, order.client_id)
        if existing is not None:
            log.info("order_already_exists", client_id=order.client_id)
            return existing

        amount = float(self.exchange.amount_to_precision(order.symbol, order.quantity))
        if amount < spec.min_amount:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"amount {amount} below minimum {spec.min_amount}"
            return order

        params = {"clientOrderId": order.client_id}
        if order.reduce_only:
            params["reduceOnly"] = True

        price = None
        if order.type is OrderType.LIMIT and order.price is not None:
            price = float(self.exchange.price_to_precision(order.symbol, order.price))

        try:
            response = self.exchange.create_order(
                symbol=order.symbol,
                type=order.type.value,
                side=order.side.value,
                amount=amount,
                price=price,
                params=params,
            )
        except ccxt.InsufficientFunds as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"insufficient funds: {str(exc)[:120]}"
            log.warning("order_rejected_funds", symbol=order.symbol, error=str(exc)[:160])
            return order
        except ccxt.BaseError as exc:
            if "170141" in str(exc) or "Duplicate clientOrderId" in str(exc):
                return self._resolve_duplicate(order, amount, price, params)
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)[:200]
            log.error("order_rejected", symbol=order.symbol, error=str(exc)[:200])
            return order

        return self._merge(order, response)

    def _resolve_duplicate(self, order: Order, amount: float, price, params: dict) -> Order:
        """The venue already knows this client id. Find out what became of it.

        Client ids are deterministic so that a crash between sending and
        recording cannot open a second position. The venue remembers them past
        the order's life, so the same id also comes back after the local
        database is wiped — and ``_find_by_client_id`` only searches *open*
        orders, so a terminal one is invisible and the resend is refused.

        What to do depends entirely on the original order's fate:

        * it filled, or is still live -> adopt it. Sending another would be the
          duplicate position the deterministic id exists to prevent.
        * it was cancelled or rejected -> no position came of it, so the intent
          is unfulfilled and may legitimately be re-sent under a fresh id.
        """
        historic = self._find_in_history(order.symbol, order.client_id)
        status = str((historic or {}).get("orderStatus") or "")

        if status in ("New", "PartiallyFilled"):
            log.info("duplicate_order_adopted", client_id=order.client_id, status=status)
            return self._merge(order, self._from_raw_v5(historic))

        if status in ("Filled", "PartiallyFilledCanceled"):
            # A filled order is only evidence of a position we still hold. The
            # venue remembers fills whose inventory has since been sold — by a
            # stop, by hand, or by a reset — and adopting one of those invents a
            # position that does not exist, with negative cash to match.
            executed = float(historic.get("cumExecQty") or 0.0)
            held = self._balances("total").get(order.symbol.split("/")[0], 0.0)
            if held >= executed * 0.95:
                log.info("duplicate_order_adopted", client_id=order.client_id, status=status)
                return self._merge(order, self._from_raw_v5(historic))

            # Inventory is gone, so that fill is spent. Re-sending under a new id
            # might be right, but might also double an entry whose exit we simply
            # have no record of. Skipping one bar is the cheap mistake: the next
            # bar has a different bar_ts, so a fresh id follows on its own.
            order.status = OrderStatus.REJECTED
            order.reject_reason = (
                f"client id already used by a filled order whose inventory is gone "
                f"(executed {executed:g}, held {held:g}) — skipping this bar"
            )
            log.warning(
                "duplicate_order_inventory_gone",
                symbol=order.symbol,
                executed=executed,
                held=held,
                action="entry skipped; the next bar gets a fresh client id",
            )
            return order

        if historic is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "duplicate client id, and the original could not be found"
            log.error("duplicate_order_unresolved", symbol=order.symbol, client_id=order.client_id)
            return order

        # Cancelled or rejected: nothing was opened, so re-issue the intent.
        retry_id = f"{order.client_id[:24]}-r{int(time.time() * 1000) % 100_000}"
        log.info(
            "duplicate_order_reissued",
            symbol=order.symbol,
            original_status=status,
            client_id=retry_id,
        )
        try:
            response = self.exchange.create_order(
                symbol=order.symbol,
                type=order.type.value,
                side=order.side.value,
                amount=amount,
                price=price,
                params={**params, "clientOrderId": retry_id},
            )
        except ccxt.BaseError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)[:200]
            log.error("order_rejected", symbol=order.symbol, error=str(exc)[:200])
            return order

        order.client_id = retry_id
        return self._merge(order, response)

    def _find_in_history(self, symbol: str, client_id: str) -> dict | None:
        """The order's final state, including terminal ones open-order calls hide."""
        try:
            response = self.exchange.privateGetV5OrderHistory(
                {"category": "spot", "symbol": symbol.replace("/", ""), "orderLinkId": client_id}
            )
        except ccxt.BaseError as exc:
            log.warning("order_history_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return None
        for raw in (response.get("result") or {}).get("list") or []:
            if str(raw.get("orderLinkId") or "") == client_id:
                return raw
        return None

    @staticmethod
    def _from_raw_v5(raw: dict) -> dict:
        """Shape a raw v5 order like the ccxt dict ``_merge`` expects."""
        filled = float(raw.get("cumExecQty") or 0.0)
        value = float(raw.get("cumExecValue") or 0.0)
        return {
            "id": raw.get("orderId"),
            "status": "closed" if raw.get("orderStatus") == "Filled" else "open",
            "filled": filled,
            "average": (value / filled) if filled else None,
            "info": raw,
        }

    def cancel(self, client_id: str, symbol: str | None = None) -> bool:
        try:
            self.exchange.cancel_order(id=None, symbol=symbol, params={"clientOrderId": client_id})
            return True
        except ccxt.BaseError as exc:
            log.warning("cancel_failed", client_id=client_id, error=str(exc)[:160])
            return False

    def _balances(self, kind: str) -> dict[str, float]:
        """``free`` (spendable), ``used`` (locked in orders) or ``total``."""
        try:
            balance = self.exchange.fetch_balance()
        except ccxt.BaseError as exc:
            log.error("balance_fetch_failed", error=str(exc)[:200])
            return {}
        return {k: float(v) for k, v in (balance.get(kind) or {}).items() if v}

    def fetch_balance(self) -> dict[str, float]:
        """Spendable balances. Money locked in a resting order cannot be sized against."""
        return self._balances("free")

    def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        try:
            raw = self.exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as exc:
            log.error("open_orders_fetch_failed", error=str(exc)[:200])
            return []
        return [self._from_ccxt(o) for o in raw]

    def fetch_fills(self, symbol: str | None = None, since: int | None = None) -> list[Fill]:
        try:
            raw = self.exchange.fetch_my_trades(symbol, since=since)
        except ccxt.BaseError as exc:
            log.error("fills_fetch_failed", error=str(exc)[:200])
            return []

        fills = []
        for trade in raw:
            fills.append(
                Fill(
                    order_client_id=(trade.get("info") or {}).get("orderLinkId", ""),
                    symbol=trade["symbol"],
                    side=Side(trade["side"]),
                    quantity=float(trade["amount"]),
                    price=float(trade["price"]),
                    fee=float((trade.get("fee") or {}).get("cost") or 0.0),
                    ts=int(trade["timestamp"] or now_ms()),
                    is_maker=trade.get("takerOrMaker") == "maker",
                    exchange_trade_id=str(trade.get("id") or ""),
                )
            )
        return fills

    # -- protective stops ---------------------------------------------------

    def _resting_stops(self, symbol: str) -> list[dict]:
        """Protective stops the venue currently holds for this symbol."""
        try:
            response = self.exchange.privateGetV5OrderRealtime(
                {
                    "category": "spot",
                    "symbol": symbol.replace("/", ""),
                    "orderFilter": "tpslOrder",
                }
            )
            return (response.get("result") or {}).get("list") or []
        except ccxt.BaseError as exc:
            log.warning("stop_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return []

    def _sellable(self, symbol: str, wanted: float) -> float:
        """How much of the base asset can actually be sold, at most ``wanted``.

        On Bybit spot a market buy pays its fee **in the base currency**, so a
        filled order for 65.69 leaves 65.62 in the account. Asking the venue to
        stop out the order quantity is then rejected with "Insufficient
        balance" (170131) — which is how a long position ends up with no
        protective stop at all. Partial fills and dust rounding land in the same
        place, so the clamp is on the balance rather than on the fee.
        """
        base = symbol.split("/")[0]
        try:
            free = float((self.exchange.fetch_balance().get("free") or {}).get(base) or 0.0)
        except ccxt.BaseError as exc:
            # Unknown is not zero: proceed with the requested size and let the
            # venue decide, rather than refusing to protect the position.
            log.warning("sellable_balance_unknown", symbol=symbol, error=str(exc)[:120])
            return wanted

        if free < wanted:
            log.info(
                "stop_quantity_clamped",
                symbol=symbol,
                wanted=wanted,
                free=free,
                reason="spot buy fees are charged in the base asset",
            )
        return min(wanted, free)

    def place_stop(
        self, symbol: str, quantity: float, stop_price: float, client_id: str
    ) -> str | None:
        """Place a reduce-only stop order that lives at the exchange.

        This is the difference between a stop and an intention. A stop held only
        in the bot's memory protects nothing while the process is down — and in
        a market that trades continuously, "down" includes a crash loop, a lost
        network, a sleeping laptop, or an operator stopping the agent.

        Returns the venue order id, or None if the venue refused it. A refusal
        is logged loudly: the position is then running unprotected between bars,
        which the operator needs to know about.
        """
        self.load_markets()

        # Adopt a stop that already protects this holding rather than churning
        # it. Cancelling first and placing second was actively harmful: when the
        # placement then failed, a working stop had been replaced with none.
        for order in self._resting_stops(symbol):
            order_id = str(order.get("orderId") or "")
            try:
                resting_qty = float(order.get("qty") or 0.0)
                resting_trigger = float(order.get("triggerPrice") or 0.0)
            except (TypeError, ValueError):
                continue
            covers = resting_qty >= quantity * 0.995
            same_level = resting_trigger > 0 and abs(resting_trigger - stop_price) / stop_price < 1e-3
            if order_id and covers and same_level:
                log.info("protective_stop_adopted", symbol=symbol, order_id=order_id)
                return order_id

        # Only now is replacing correct: what is resting does not protect this
        # position. Release it so the asset it reserves can back the new stop.
        for order in self._resting_stops(symbol):
            order_id = str(order.get("orderId") or "")
            if order_id and self.cancel_stop(symbol, order_id):
                log.info("stale_stop_cancelled", symbol=symbol, order_id=order_id)

        quantity = self._sellable(symbol, quantity)
        if quantity <= 0:
            log.error(
                "protective_stop_impossible",
                symbol=symbol,
                consequence="nothing of this asset is free to sell",
            )
            return None
        try:
            amount = float(self.exchange.amount_to_precision(symbol, quantity))
            trigger = float(self.exchange.price_to_precision(symbol, stop_price))
            response = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side="sell",
                amount=amount,
                price=None,
                params={
                    "stopLossPrice": trigger,
                    "reduceOnly": True,
                    # Unique per attempt, supplied by the caller: the venue
                    # remembers cancelled ids and refuses them (170141). Two
                    # stops on one holding are prevented by the adopt check
                    # above, not by the id.
                    "clientOrderId": client_id,
                    "triggerDirection": 2,  # trigger on a falling price
                },
            )
        except ccxt.BaseError as exc:
            log.error(
                "protective_stop_rejected",
                symbol=symbol,
                stop=stop_price,
                error=str(exc)[:200],
                consequence="position is unprotected at the venue between bars",
            )
            return None

        order_id = str(response.get("id") or "")
        log.info("protective_stop_placed", symbol=symbol, stop=stop_price, order_id=order_id)
        return order_id

    def amend_stop(self, symbol: str, order_id: str, stop_price: float) -> bool:
        """Move an existing venue stop. Falls back to cancel-and-replace.

        Amending rather than replacing matters: between a cancel and a new
        placement the position has no stop at all, and that gap is exactly when
        a fast move would hurt.
        """
        self.load_markets()
        try:
            trigger = float(self.exchange.price_to_precision(symbol, stop_price))
            self.exchange.edit_order(
                id=order_id,
                symbol=symbol,
                type="market",
                side="sell",
                params={"triggerPrice": trigger, "stopLossPrice": trigger},
            )
            return True
        except ccxt.BaseError as exc:
            log.warning("stop_amend_failed", symbol=symbol, error=str(exc)[:160])
            return False

    def cancel_stop(self, symbol: str, order_id: str) -> bool:
        try:
            self.exchange.cancel_order(id=order_id, symbol=symbol)
            return True
        except ccxt.BaseError as exc:
            log.warning("stop_cancel_failed", symbol=symbol, error=str(exc)[:160])
            return False

    def fetch_positions(self) -> dict[str, float]:
        """Non-quote balances — on spot this is what "a position" means.

        Deliberately ``total`` and not ``free``: a protective stop **locks** the
        base asset it would sell, so a correctly protected position reads as
        ``free ≈ 0``. Reading free here made the engine conclude its own stop
        had closed the position, drop it from the portfolio, and stop managing
        a holding it still owned.
        """
        positions = {}
        for coin, amount in self._balances("total").items():
            if coin == self.quote or amount <= 0:
                continue
            symbol = f"{coin}/{self.quote}"
            # Below the venue's minimum order size there is no way to sell it,
            # so it is not a position — it is a residue no action can clear.
            # Reporting it forever as "unmanaged" only trains the operator to
            # ignore that warning.
            try:
                if amount < self.market_spec(symbol).min_amount:
                    continue
            except Exception:  # noqa: BLE001 — no market for it, report and let the operator see
                pass
            positions[symbol] = amount
        return positions

    def fetch_ticker(self, symbol: str) -> dict:
        try:
            return self.exchange.fetch_ticker(symbol)
        except ccxt.BaseError as exc:
            log.warning("ticker_failed", symbol=symbol, error=str(exc)[:120])
            return {}

    # -- helpers -----------------------------------------------------------

    def _find_by_client_id(self, symbol: str, client_id: str) -> Order | None:
        try:
            raw = self.exchange.fetch_open_orders(symbol)
        except ccxt.BaseError:
            return None
        for order in raw:
            if (order.get("info") or {}).get("orderLinkId") == client_id:
                return self._from_ccxt(order)
        return None

    def _from_ccxt(self, raw: dict) -> Order:
        info = raw.get("info") or {}
        return Order(
            client_id=info.get("orderLinkId", raw.get("clientOrderId", "")),
            symbol=raw["symbol"],
            side=Side(raw["side"]),
            type=OrderType(raw.get("type", "market")),
            quantity=float(raw.get("amount") or 0.0),
            price=float(raw["price"]) if raw.get("price") else None,
            status=_STATUS_MAP.get(raw.get("status", ""), OrderStatus.OPEN),
            exchange_id=str(raw.get("id") or ""),
            filled_quantity=float(raw.get("filled") or 0.0),
            average_price=float(raw.get("average") or 0.0),
            fee_paid=float((raw.get("fee") or {}).get("cost") or 0.0),
            created_ts=int(raw.get("timestamp") or now_ms()),
            updated_ts=int(raw.get("lastUpdateTimestamp") or raw.get("timestamp") or now_ms()),
        )

    def _merge(self, order: Order, response: dict) -> Order:
        order.exchange_id = str(response.get("id") or "")
        order.status = _STATUS_MAP.get(response.get("status", ""), OrderStatus.OPEN)
        order.filled_quantity = float(response.get("filled") or 0.0)
        order.average_price = float(response.get("average") or 0.0)
        order.fee_paid = float((response.get("fee") or {}).get("cost") or 0.0)
        order.updated_ts = int(response.get("timestamp") or now_ms())
        return order

    def top_up_demo(self, amounts: dict[str, str] | None = None) -> bool:
        """Request virtual funds on a demo account.

        Bybit rate-limits this to one call per minute, so it is a maintenance
        action rather than something the engine calls on its own.
        """
        if self.venue is not Venue.BYBIT_DEMO:
            log.warning("top_up_refused_not_demo", venue=self.venue.value)
            return False
        try:
            self.exchange.privatePostV5AccountDemoApplyMoney(
                {
                    "adjustType": 0,
                    "utaDemoApplyMoney": [
                        {"coin": coin, "amountStr": amount}
                        for coin, amount in (amounts or {"USDT": "1000"}).items()
                    ],
                }
            )
            return True
        except ccxt.BaseError as exc:
            log.error("demo_topup_failed", error=str(exc)[:200])
            return False
