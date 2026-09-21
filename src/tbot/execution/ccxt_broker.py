"""Live execution against Bybit through ccxt.

Same interface as the simulated broker, so the engine cannot tell them apart. Demo
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


def _number(raw: dict, key: str) -> float:
    try:
        return float(raw.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _order_state(raw: dict) -> dict[str, float | str]:
    """One v5 order reduced to the things a caller ever acts on."""
    filled = _number(raw, "cumExecQty")
    value = _number(raw, "cumExecValue")
    average = _number(raw, "avgPrice")
    # ``avgPrice`` comes back empty on some terminal orders while the executed
    # value is always present. Deriving it keeps a settled order from reporting
    # a price of zero, which downstream reads as "no fill".
    if average <= 0.0 and filled > 0.0:
        average = value / filled
    status = str(raw.get("orderStatus") or "")
    return {
        "status": status,
        "filled": filled,
        "average_price": average,
        "value": value,
        # Raw, in whatever currency the venue charged it — see ``_fee_in_quote``.
        "fee": _number(raw, "cumExecFee"),
        "side": str(raw.get("side") or ""),
        # Why it ended, in the venue's words. "EC_PostOnlyWillTakeLiquidity" and
        # a TTL cancel are both "no fill", and they are different findings: the
        # first says the book moved away before the order could rest.
        "reason": str(raw.get("rejectReason") or "").replace("EC_NoError", "")
        or str(raw.get("cancelType") or "").replace("UNKNOWN", ""),
        # Bybit spells a cancelled conditional order "Deactivated"; treating
        # that as a fill would invent an exit that never happened.
        "terminal": status
        in ("Filled", "Cancelled", "Deactivated", "Rejected", "PartiallyFilledCanceled"),
        "filled_terminal": status in ("Filled", "PartiallyFilledCanceled"),
    }


def _amend_was_a_no_op(exc: Exception) -> bool:
    """Did the venue refuse the amend because it had nothing to change?

    Bybit files this under the generic ``10001`` alongside genuine parameter
    errors, so the message is the only thing that separates them.
    """
    return "remains unchanged" in str(exc)


class CCXTBroker(Broker):
    """Bybit spot via ccxt, for demo and live alike."""

    # Every v5 endpoint is scoped by category, and the wrong one comes back as
    # an empty list rather than an error — an order lookup then reports that the
    # order never existed. Declared on the class so a subclass trading somewhere
    # else has to say so, and so it survives construction via ``__new__``.
    category = "spot"

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

    def _venue_symbol(self, symbol: str) -> str:
        """``VVV/USDT`` or ``SOL/USDT:USDT`` -> ``VVVUSDT`` / ``SOLUSDT``.

        The v5 endpoints want the bare id. Dropping the settle suffix matters:
        a contract symbol run through a plain ``replace("/", "")`` becomes
        ``SOLUSDT:USDT``, which every lookup answers with an empty list — no
        error, no order found, and the caller concludes the order never existed.
        """
        return symbol.split(":")[0].replace("/", "")

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
            # Bybit answers "Illegal category" for linear on the demo host, and
            # the account fee-rate endpoint returns 10001 there too, so on this
            # venue there is simply no account-level tier to read. The market
            # metadata is the right source in that case and it agrees with what
            # was measured by placing an order (0.06% taker / 0.01% maker), so
            # this is a fact about the venue rather than a problem — logged as
            # such. A warning nothing can clear teaches the channel to be
            # ignored, which is how a real one gets missed.
            log.info(
                "fee_tier_unavailable",
                market=self.exchange.options.get("defaultType"),
                using="market metadata",
                error=str(exc)[:120],
            )
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

        return self._settle(self._merge(order, response))

    # A market order fills at once, but the reply that acknowledges it does not
    # say so. These bound how long the truth is worth waiting for.
    # A just-filled market order is not immediately queryable. Measured on the
    # perpetual venue: four seconds after a fill, both the realtime list and the
    # history came back empty, and the engine recorded H17's first trade at the
    # price it asked for (112.09) rather than the 112.58 it paid — 43.7bps of
    # slippage booked as zero, and a 1R understated by a quarter. Spot settles
    # faster, which is why audit #59 looked complete.
    #
    # The pause grows so the common case still costs one quick lookup.
    _SETTLE_ATTEMPTS = 6
    _SETTLE_PAUSES_S = (0.25, 0.5, 1.0, 1.5, 2.0)

    def _settle(self, order: Order) -> Order:
        """Replace an accepted market order's blanks with what the venue did.

        Bybit's create-order reply carries an id and a status and nothing else:
        no executed quantity, no average price, no fee. ``_merge`` therefore
        leaves ``filled_quantity``, ``average_price`` and ``fee_paid`` at zero,
        and every caller that reads them falls through to what it *asked* for.
        That is how the engine came to record the planned entry as the price
        paid, leave filled entries sitting at ``open`` forever, and hand the
        slippage monitor a drift of exactly zero on every trade — the monitor
        could not have fired whatever the venue charged.

        Only market orders are settled. A limit or stop-entry order is supposed
        to rest unfilled, and polling one would just burn requests.
        """
        if order.type is not OrderType.MARKET:
            return order
        if order.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELED):
            return order
        if not order.exchange_id:
            return order
        if order.filled_quantity > 0.0 and order.average_price > 0.0:
            return order  # the reply was complete for once; nothing to ask

        state: dict[str, float | str] | None = None
        for attempt in range(self._SETTLE_ATTEMPTS):
            state = self.order_state(order.symbol, order.exchange_id)
            if state and float(state.get("filled") or 0.0) > 0.0:
                break
            if state and state.get("terminal"):
                break
            if attempt < len(self._SETTLE_PAUSES_S):
                time.sleep(self._SETTLE_PAUSES_S[attempt])

        if state is None:
            # Unknown is not zero. Leaving the order as the venue accepted it
            # keeps the caller's own fallback in play; claiming a zero fill here
            # would report an order that filled as one that did not.
            log.warning(
                "order_settle_failed",
                symbol=order.symbol,
                client_id=order.client_id,
                consequence="fill price falls back to the requested price",
            )
            return order

        filled = float(state.get("filled") or 0.0)
        average = float(state.get("average_price") or 0.0)
        if filled <= 0.0 or average <= 0.0:
            log.warning(
                "order_settle_empty",
                symbol=order.symbol,
                client_id=order.client_id,
                status=state.get("status"),
                consequence="fill price falls back to the requested price",
            )
            return order

        order.filled_quantity = filled
        order.average_price = average
        order.fee_paid = self._fee_in_quote(order, state)
        if state.get("status") == "Filled":
            order.status = OrderStatus.FILLED
        elif str(state.get("status") or "") == "PartiallyFilled":
            order.status = OrderStatus.PARTIALLY_FILLED
        order.updated_ts = now_ms()
        return order

    def _fee_in_quote(self, order: Order, state: dict[str, float | str]) -> float:
        """``cumExecFee`` converted to the quote currency the ledger counts in.

        Bybit charges a spot **buy** in the base asset — the same fact that
        forces the stop quantity to be clamped (``_sellable``) — and everything
        else in the quote. Recording the raw number would put 0.00131 VVV in a
        column that means dollars.

        Measured on demo: a 1.31 VVV buy reports ``cumExecFee`` 0.00131 against
        ``cumExecValue`` 34.16, i.e. the fee is base-denominated and one tenth
        of a percent of the quantity.
        """
        fee = float(state.get("fee") or 0.0)
        if fee <= 0.0:
            return 0.0
        value = float(state.get("value") or 0.0) or (order.filled_quantity * order.average_price)
        if self.category == "spot" and order.side is Side.BUY:
            fee *= order.average_price

        # A fee larger than a hundredth of the notional means the currency was
        # read wrong, and a wrong fee is worse than a missing one: it lands in
        # realised PnL where nothing else would ever contradict it.
        if value > 0.0 and fee > value * 0.01:
            spec = self.market_spec(order.symbol)
            log.warning(
                "fee_currency_implausible",
                symbol=order.symbol,
                raw_fee=state.get("fee"),
                converted=fee,
                value=value,
                action="using the assumed taker rate instead",
            )
            return value * spec.taker_fee
        return fee

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
            totals = self._balances("total")
            if totals is None:
                # ``_balances`` returns None when the venue could not be read,
                # and this call used to assume a dict — a plain AttributeError
                # on the exact path a network hiccup leads to, since a hiccup
                # is what produces the duplicate id in the first place.
                #
                # The inventory check is the whole safeguard here, and a venue
                # that cannot be read can neither confirm nor deny it. Reading
                # the failure as "holds nothing" would discard a real position;
                # as "holds it" would invent one. Skip the bar, same as below.
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    "client id already used by a filled order, and the balance "
                    "read failed — cannot tell whether that fill is still held"
                )
                log.warning(
                    "duplicate_order_unverifiable",
                    symbol=order.symbol,
                    client_id=order.client_id,
                    status=status,
                    action="entry skipped; the next bar gets a fresh client id",
                )
                return order
            held = totals.get(order.symbol.split("/")[0], 0.0)
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
        # Same empty acknowledgement as any other create call, so the same
        # follow-up question is owed.
        return self._settle(self._merge(order, response))

    def _find_in_history(self, symbol: str, client_id: str) -> dict | None:
        """The order's final state, including terminal ones open-order calls hide."""
        try:
            response = self.exchange.privateGetV5OrderHistory(
                {
                    "category": self.category,
                    "symbol": self._venue_symbol(symbol),
                    "orderLinkId": client_id,
                }
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

    def _balances(self, kind: str) -> dict[str, float] | None:
        """``free`` (spendable), ``used`` (locked in orders) or ``total``.

        **None means the read failed.** It used to return an empty dict, which
        is indistinguishable from "the account holds nothing" — and that is the
        exact reading that makes an engine conclude its positions have been
        sold. It nearly happened: during a four-hour network outage the engine
        logged ``position_inventory_missing`` for a position that was sitting
        untouched at the venue, and was saved only because the stop lookup
        failed in the same breath and pushed it down the "report loudly" branch
        instead of the "close it locally" one. Nothing in the design guaranteed
        that.
        """
        try:
            balance = self.exchange.fetch_balance()
        except ccxt.BaseError as exc:
            log.error("balance_fetch_failed", error=str(exc)[:200])
            return None
        return {k: float(v) for k, v in (balance.get(kind) or {}).items() if v}

    def fetch_balance(self) -> dict[str, float]:
        """Spendable balances. Money locked in a resting order cannot be sized against."""
        return self._balances("free") or {}

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
            # Bounded on BOTH sides. Only the lower bound existed, so any
            # oversized stop was adopted as good enough — and after a partial
            # exit the resting stop covers more than is still held. It would
            # then trigger, try to sell inventory that is no longer there, be
            # refused for insufficient balance, and leave the remainder with no
            # protection at all. The upper bound is what forces a replace.
            # The 0.5% of slack below absorbs the base-asset fee that makes a
            # placed stop very slightly smaller than the position it covers.
            covers = quantity * 0.995 <= resting_qty <= quantity * 1.005
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

    def amend_stop(self, symbol: str, order_id: str, stop_price: float) -> str | None:
        """Move an existing venue stop to a new trigger price.

        Returns **the order id now protecting the position** — the same one when
        the amend succeeded, a new one when the stop had to be replaced, and
        ``None`` when the venue is holding something other than what was asked
        for. A bool could not say "still protected, under a different id", and
        the caller has to store that id or it loses the handle to its own stop
        (audit #37).

        Amending rather than cancel-and-replace matters: between a cancel and a
        new placement the position has no stop at all, and on spot the two
        cannot overlap — a resting stop locks the base asset, so the
        replacement is refused ``170131`` until the old one is gone. That gap is
        exactly when a fast move would hurt.

        The v5 endpoint is called directly rather than through ``edit_order``.
        ccxt builds an amend for the derivatives contract and adds ``triggerBy``
        to it, which Bybit **spot** refuses with ``10001 Request parameter
        error`` — measured, on every single trail. The docstring here used to
        promise a cancel-and-replace fallback that was never written, so a
        failed amend logged one warning and returned ``False``, and nobody read
        the return: the venue kept the *entry* stop for the life of the
        position while the local book showed the trailed one. Measured on the
        demo account: ETH local 2588.06 / venue 2506.63, VVV local 26.45 /
        venue 23.43, ``updatedTime == createdTime`` on both. The gap only
        matters while the process is down, which is the one case a venue-side
        stop exists for (invariant 12).

        The fallback is written now, and it is the second choice on purpose.
        """
        self.load_markets()
        trigger = float(self.exchange.price_to_precision(symbol, stop_price))
        try:
            self.exchange.privatePostV5OrderAmend(
                {
                    "category": self.category,
                    "symbol": self._venue_symbol(symbol),
                    "orderId": order_id,
                    "triggerPrice": str(trigger),
                }
            )
            log.info("stop_amended", symbol=symbol, stop=trigger, order_id=order_id)
            return order_id
        except ccxt.BaseError as exc:
            if _amend_was_a_no_op(exc):
                # "The order remains unchanged as the parameters entered match
                # the existing ones" — the venue already holds this level, so
                # the caller got what it asked for. Bybit reports it under the
                # generic 10001, which is why the message and not the code is
                # what distinguishes it. Reading it as a failure sends the
                # cancel-and-replace path down a route that briefly leaves the
                # position with no stop at all, every single bar, to arrive
                # back where it started. Observed on the first restart after
                # the fix, on all three open positions.
                log.info("stop_already_at_level", symbol=symbol, stop=trigger)
                return order_id
            log.warning("stop_amend_failed", symbol=symbol, error=str(exc)[:160])

        # Cancel-and-replace. The position is unprotected between these two
        # calls, which is why it is not the first choice — but a stop resting
        # at a level the strategy abandoned protects less than a brief gap.
        quantity = self._resting_stop_quantity(symbol, order_id)
        if not self.cancel_stop(symbol, order_id):
            log.error(
                "stop_replace_impossible",
                symbol=symbol,
                order_id=order_id,
                consequence="venue still holds the old stop level",
            )
            return None
        replacement = f"{order_id[-12:]}-{int(time.time()) % 100000}"
        placed = self.place_stop(symbol, quantity, stop_price, f"tb-amend-{replacement}")
        if placed is None:
            log.error(
                "stop_replace_failed",
                symbol=symbol,
                consequence="position has no stop at the venue",
            )
            return None
        log.info("stop_replaced", symbol=symbol, stop=stop_price, order_id=placed)
        return placed

    def _resting_stop_quantity(self, symbol: str, order_id: str) -> float:
        """How much the stop being replaced covers, read from the venue.

        Asking rather than passing the position's quantity keeps the
        replacement the same size as what it replaces: a spot buy pays its fee
        in the base asset, so the resting stop is already slightly smaller than
        the position (invariant 23), and re-deriving it from the local book
        would re-introduce the rejection that clamp exists to avoid.
        """
        for order in self._resting_stops(symbol):
            if str(order.get("orderId") or "") == order_id:
                try:
                    return float(order.get("qty") or 0.0)
                except (TypeError, ValueError):
                    break
        return 0.0

    def cancel_stop(self, symbol: str, order_id: str) -> bool:
        try:
            self.exchange.cancel_order(id=order_id, symbol=symbol)
            return True
        except ccxt.BaseError as exc:
            log.warning("stop_cancel_failed", symbol=symbol, error=str(exc)[:160])
            return False

    def fetch_positions(self) -> dict[str, float] | None:
        """Non-quote balances — on spot this is what "a position" means.

        Deliberately ``total`` and not ``free``: a protective stop **locks** the
        base asset it would sell, so a correctly protected position reads as
        ``free ≈ 0``. Reading free here made the engine conclude its own stop
        had closed the position, drop it from the portfolio, and stop managing
        a holding it still owned.

        Returns **None when the venue could not be read**, which callers must
        treat as "no information" rather than "no positions". An empty dict
        still means the account genuinely holds nothing.
        """
        totals = self._balances("total")
        if totals is None:
            return None
        positions = {}
        for coin, amount in totals.items():
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

    def fetch_balances_detail(self) -> dict[str, dict[str, float]]:
        """Every non-zero holding as free / used / total, in one venue call.

        ``used`` is not a curiosity on spot: a protective stop reserves the base
        asset it would sell, so a correctly protected position reads as fully
        used and zero free. Showing only one of the three numbers is how the
        engine once concluded its own stop had closed a position it still held.
        """
        try:
            raw = self.exchange.fetch_balance()
        except ccxt.BaseError as exc:
            log.error("balance_detail_failed", error=str(exc)[:200])
            return {}

        detail: dict[str, dict[str, float]] = {}
        for kind in ("free", "used", "total"):
            for coin, amount in (raw.get(kind) or {}).items():
                try:
                    value = float(amount)
                except (TypeError, ValueError):
                    continue
                if value:
                    detail.setdefault(coin, {"free": 0.0, "used": 0.0, "total": 0.0})[kind] = value
        return detail

    # -- microstructure ----------------------------------------------------

    def fetch_microstructure(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        """Spread and 24h turnover for each symbol, in a single request.

        Bybit returns every ticker in a category in one call, so this costs the
        same whether the universe is one symbol or a hundred. That matters: the
        reason ``ScanRow.spread_bps`` sat at zero since the beginning was never
        that the data was unavailable, only that nobody wired it up — and a
        config key (``universe.max_spread_bps``) has been quietly deciding
        nothing ever since.

        Both the category and the symbol form are the class's, not literals.
        Hardcoded ``"spot"`` and a plain ``replace("/", "")`` made this method
        fail twice over on the perpetual engine — the wrong ticker list, and
        ``SOLUSDT:USDT`` as the key to look it up by — so all 1213 scan rows of
        a ten-hour run carried a spread of zero. No error, no exception: the
        same silent-zero failure as audit #56 and #62, in the one method those
        fixes did not reach.
        """
        try:
            response = self.exchange.publicGetV5MarketTickers({"category": self.category})
        except ccxt.BaseError as exc:
            log.warning("microstructure_fetch_failed", error=str(exc)[:160])
            return {}

        wanted = {self._venue_symbol(sym): sym for sym in symbols}
        out: dict[str, dict[str, float]] = {}
        for row in (response.get("result") or {}).get("list") or []:
            symbol = wanted.get(str(row.get("symbol") or ""))
            if symbol is None:
                continue
            try:
                bid = float(row.get("bid1Price") or 0.0)
                ask = float(row.get("ask1Price") or 0.0)
                turnover = float(row.get("turnover24h") or 0.0)
            except (TypeError, ValueError):
                continue
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            out[symbol] = {
                "bid": bid,
                "ask": ask,
                # Zero means "not measured", never "perfectly tight". A caller
                # must be able to tell a missing reading from a good one.
                "spread_bps": ((ask - bid) / mid * 10_000) if mid > 0 else 0.0,
                "turnover_24h": turnover,
            }
        return out

    # -- order state -------------------------------------------------------

    def order_state(self, symbol: str, order_id: str) -> dict[str, float | str] | None:
        """What became of one order: status, filled quantity, average price.

        Checked against live orders first and the history second, because a
        triggered stop leaves the live list the moment it fires. Without the
        second lookup a filled stop is indistinguishable from one that was
        cancelled, and the engine would have to guess which of the two emptied
        its position.
        """
        market = self._venue_symbol(symbol)
        # ``orderId`` on the realtime query, because an unfiltered one answers
        # with the *open* orders only — and a market order that just filled is
        # by definition not among them. Asked for by id, the venue returns it
        # (measured), and that is the fastest place a settled fill appears.
        queries: list[dict[str, str]] = [
            {"category": self.category, "symbol": market, "orderId": order_id}
        ]
        if self.category == "spot":
            # Conditional orders are invisible to an unfiltered spot query, and
            # a triggered stop is exactly the order a caller most needs to find.
            # Kept after the lookup by id, which is both cheaper and exact.
            queries += [
                {"category": "spot", "symbol": market, "orderFilter": "tpslOrder"},
                {"category": "spot", "symbol": market, "orderFilter": "StopOrder"},
            ]
        queries.append({"category": self.category, "symbol": market})
        for query in queries:
            try:
                response = self.exchange.privateGetV5OrderRealtime(query)
            except ccxt.BaseError:
                continue
            for raw in (response.get("result") or {}).get("list") or []:
                if str(raw.get("orderId") or "") == order_id:
                    return _order_state(raw)

        try:
            response = self.exchange.privateGetV5OrderHistory(
                {"category": self.category, "symbol": market, "orderId": order_id}
            )
        except ccxt.BaseError as exc:
            log.warning("order_state_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return None
        for raw in (response.get("result") or {}).get("list") or []:
            if str(raw.get("orderId") or "") == order_id:
                return _order_state(raw)
        return None

    # -- armed entries ------------------------------------------------------

    def place_stop_entry(
        self, symbol: str, quantity: float, trigger_price: float, client_id: str
    ) -> str | None:
        """Rest a buy order that fires when price trades up through a level.

        This is the only honest way to act between bar closes: the level is
        computed at the last close, handed to the venue, and the venue does the
        watching. No indicator ever sees an unclosed bar.

        Two venue facts, both measured rather than assumed:

        * Bybit spot **refuses** ``triggerDirection`` outright (``NotSupported``
          from ccxt). Direction is inferred from where the trigger sits relative
          to the last price, which makes the check below load-bearing rather
          than defensive: a trigger that has slipped *below* the market would be
          read as a sell-stop and fire immediately, at the worst possible moment.
        * The order rests under ``orderFilter='StopOrder'``, so cancelling it
          needs that filter too.
        """
        self.load_markets()
        try:
            last = float(self.exchange.fetch_ticker(symbol)["last"])
        except (ccxt.BaseError, KeyError, TypeError) as exc:
            log.warning("armed_entry_no_price", symbol=symbol, error=str(exc)[:120])
            return None

        if trigger_price <= last:
            log.info(
                "armed_entry_skipped",
                symbol=symbol,
                trigger=trigger_price,
                last=last,
                reason="price is already through the level; a resting buy here "
                "would be read as a sell-stop and fire at once",
            )
            return None

        try:
            amount = float(self.exchange.amount_to_precision(symbol, quantity))
            trigger = float(self.exchange.price_to_precision(symbol, trigger_price))
            response = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side="buy",
                amount=amount,
                price=None,
                params={
                    "triggerPrice": trigger,
                    "orderFilter": "StopOrder",
                    "clientOrderId": client_id,
                },
            )
        except ccxt.BaseError as exc:
            log.warning("armed_entry_rejected", symbol=symbol, error=str(exc)[:180])
            return None

        order_id = str(response.get("id") or "")
        log.info("armed_entry_placed", symbol=symbol, trigger=trigger_price, order_id=order_id)
        return order_id

    def cancel_stop_entry(self, symbol: str, order_id: str) -> bool:
        """Withdraw an armed entry. Failure here must be treated as "still armed".

        An armed order that outlives its bar fills at a level the strategy no
        longer believes in, so a cancel that silently failed is worse than one
        that never happened — the caller has to know.
        """
        try:
            self.exchange.cancel_order(
                id=order_id, symbol=symbol, params={"orderFilter": "StopOrder"}
            )
            return True
        except ccxt.BaseError as exc:
            message = str(exc)
            # Already gone is the outcome we wanted, not a failure.
            if "170213" in message or "not exists" in message or "too late" in message:
                return True
            log.warning("armed_entry_cancel_failed", symbol=symbol, error=message[:160])
            return False

    def resting_stop_entries(self, symbol: str) -> list[dict]:
        """Armed entry orders the venue is currently holding for this symbol."""
        try:
            response = self.exchange.privateGetV5OrderRealtime(
                {"category": "spot", "symbol": symbol.replace("/", ""), "orderFilter": "StopOrder"}
            )
            return (response.get("result") or {}).get("list") or []
        except ccxt.BaseError as exc:
            log.warning("armed_entry_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return []

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
            # Carried so a caller can tell the position's own protection from an
            # order the engine placed. On a perpetual the venue reports the stop
            # and the take-profit with no client id at all, which is otherwise
            # indistinguishable from an order nobody recorded.
            meta={"stop_order_type": str(info.get("stopOrderType") or "")},
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


class PerpBroker(CCXTBroker):
    """USDT-margined linear perpetuals.

    A separate class rather than a flag because the differences are not
    cosmetic. On spot a position *is* a balance; here it is a contract the
    venue tracks for you, reported by its own endpoint, and it can be negative.
    Three spot-specific workarounds also stop applying:

    * ``_sellable`` exists because Bybit spot charges the buy fee **in the base
      asset**, leaving slightly less than the order quantity (invariant 23).
      Perpetual fees come out of the margin balance, so the clamp is not just
      unnecessary, it would read a base balance that does not exist and clamp
      every stop to zero.
    * A resting spot stop **locks** the base asset, which is why a second
      reduce-only order beside it is refused and the take-profit had to live in
      the engine (invariant 39). Perpetuals attach both to the position, and
      the venue accepted both in the same call when measured — so here the
      target can be a venue order, and survives the process being down.
    * ``fetch_positions`` on spot reads ``total`` balances. Here it reads the
      position list, so an unmanaged coin balance can never be mistaken for a
      contract.

    What replaces isolated margin as the safety mechanism is leverage. Measured
    on the demo account: the account-level ``ISOLATED_MARGIN`` flag is accepted
    and the position still reports ``tradeMode=0`` (cross) on a Unified
    account, so asserting the flag would refuse to start on a correctly
    configured account. At 1x the position is 100% collateralised and a short
    liquidates 99% away from entry; at 3x that becomes roughly 33%, which a
    weekend gap can reach. So leverage is what is verified, and cross margin is
    only tolerated while leverage is 1.0.
    """

    # Contract orders live under a different v5 category, and every identity
    # lookup — duplicate resolution, order settlement, venue-exit detection —
    # runs through it. Left at "spot" these all answer "no such order".
    category = "linear"


    def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Open orders, minus the position's own stop and take-profit.

        On a perpetual those two are properties of the position, not orders the
        engine placed: the venue reports them with an empty ``orderLinkId``
        because no client id was ever sent. Reconciliation matches on client id,
        so both came back as orphans on every start — two warnings per position,
        for something working exactly as designed, that no action could ever
        clear. Audit #32 is the same lesson: a channel that cries wolf gets
        muted, and then the real wolf goes unheard.
        """
        return [
            order
            for order in super().fetch_open_orders(symbol)
            if not self._is_position_protection(order)
        ]

    # -- maker entries (scalp engine) --------------------------------------

    def best_bid_ask(self, symbol: str) -> tuple[float, float] | None:
        """Top of book, read at the moment of placing — never from a cache."""
        try:
            rows = self.exchange.publicGetV5MarketTickers(
                {"category": self.category, "symbol": self._venue_symbol(symbol)}
            )["result"]["list"]
            bid, ask = float(rows[0]["bid1Price"]), float(rows[0]["ask1Price"])
        except (ccxt.BaseError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("book_read_failed", symbol=symbol, error=str(exc)[:120])
            return None
        return (bid, ask) if 0 < bid < ask else None

    def place_maker_entry(
        self,
        order: Order,
        take_profit: float,
        stop_loss: float,
    ) -> Order:
        """A PostOnly limit entry carrying its own stop and target.

        One call, measured on the demo account (research/scalp_venue_facts.md):
        the venue accepts the entry with a market stop and a limit target
        attached, arms both only on fill, and removes both when the position
        closes. So the position is protected by the venue from the instant it
        exists, whether or not this process is alive (invariant 12), and there
        is no window between a fill and a stop placement.

        Direct v5, not ``create_order``: audit #72 is what ccxt's parameter
        translation cost the last time an order shape mattered this much.

        A PostOnly that would cross the book is accepted and then cancelled by
        the venue (``EC_PostOnlyWillTakeLiquidity``); that arrives here as an
        accepted order and is discovered as "no fill" when its state is read.
        """
        self.load_markets()
        symbol = order.symbol
        spec = self.market_spec(symbol)
        amount = float(self.exchange.amount_to_precision(symbol, order.quantity))
        if amount < spec.min_amount:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"amount {amount} below minimum {spec.min_amount}"
            return order
        price = self.exchange.price_to_precision(symbol, order.price)
        target = self.exchange.price_to_precision(symbol, take_profit)
        try:
            response = self.exchange.privatePostV5OrderCreate(
                {
                    "category": self.category,
                    "symbol": self._venue_symbol(symbol),
                    "side": "Buy" if order.side is Side.BUY else "Sell",
                    "orderType": "Limit",
                    "qty": self.exchange.amount_to_precision(symbol, amount),
                    "price": price,
                    "timeInForce": "PostOnly",
                    "orderLinkId": order.client_id,
                    "positionIdx": 0,
                    "tpslMode": "Partial",
                    "takeProfit": target,
                    "tpOrderType": "Limit",
                    "tpLimitPrice": target,
                    "stopLoss": self.exchange.price_to_precision(symbol, stop_loss),
                    "slOrderType": "Market",
                    "tpTriggerBy": "LastPrice",
                    "slTriggerBy": "LastPrice",
                }
            )
        except ccxt.BaseError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)[:200]
            log.warning("maker_entry_rejected", symbol=symbol, error=str(exc)[:160])
            return order
        order.exchange_id = str((response.get("result") or {}).get("orderId") or "")
        order.quantity = amount
        order.price = float(price)
        order.status = OrderStatus.OPEN
        return order

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Ask the venue to cancel. The answer is *not* confirmation (invariant 41)."""
        try:
            self.exchange.privatePostV5OrderCancel(
                {
                    "category": self.category,
                    "symbol": self._venue_symbol(symbol),
                    "orderId": order_id,
                }
            )
            return True
        except ccxt.BaseError as exc:
            # "order not exists or too late to cancel" means it already filled
            # or was already cancelled — the caller reads the state to find out.
            log.info("cancel_refused", symbol=symbol, order_id=order_id, error=str(exc)[:120])
            return False

    # What the venue reports for the legs it owns. Partial* are the attached
    # legs of an order placed with ``tpslMode=Partial`` (measured: scalp probe).
    _TAKE_PROFIT_KINDS = ("TakeProfit", "PartialTakeProfit")
    _STOP_KINDS = ("StopLoss", "PartialStopLoss", "TrailingStop")

    def recent_reduce_fill(
        self, symbol: str, since_ms: int, closing_side: Side
    ) -> dict[str, float | str] | None:
        """The fill with which the venue closed this position on its own.

        On spot the protective stop is a real order and ``order_state`` finds it
        by id. On a perpetual the stop and the target are properties of the
        position — ``place_stop`` returns ``position:<symbol>``, which is not an
        order id — so that lookup found nothing, and ``_detect_venue_exits``
        could never recognise a perp exit as one. It reported
        ``position_inventory_missing`` at best; at worst the engine's own
        between-bar check got there first, sent a reduce-only close into a
        position that was already flat, was refused ``110017``, and the refusal
        was then booked as a fill. Measured on H17's first trade: the venue took
        profit at 114.34, the engine recorded a sale that never happened.

        Newest reduce-only fill on the closing side since ``since_ms``. The
        result carries ``kind`` so the caller can tell a target from a stop.
        """
        try:
            response = self.exchange.privateGetV5OrderHistory(
                {"category": self.category, "symbol": self._venue_symbol(symbol), "limit": 20}
            )
        except ccxt.BaseError as exc:
            log.warning("exit_fill_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return None

        wanted = "Buy" if closing_side is Side.BUY else "Sell"
        for raw in (response.get("result") or {}).get("list") or []:
            if str(raw.get("side") or "") != wanted:
                continue
            if not raw.get("reduceOnly") and not raw.get("stopOrderType"):
                continue
            if int(raw.get("updatedTime") or 0) < since_ms:
                continue
            state = _order_state(raw)
            if not state["filled_terminal"] or float(state["filled"]) <= 0.0:
                continue
            kind = str(raw.get("stopOrderType") or "")
            state["kind"] = (
                "take_profit"
                if kind in self._TAKE_PROFIT_KINDS
                else "stop"
                if kind in self._STOP_KINDS
                else "order"
            )
            state["ts"] = int(raw.get("updatedTime") or 0)
            return state
        return None

    @staticmethod
    def _is_position_protection(order: Order) -> bool:
        kind = str((order.meta or {}).get("stop_order_type") or "")
        return not order.client_id and kind in (
            "StopLoss",
            "TakeProfit",
            "TrailingStop",
            "PartialStopLoss",
            "PartialTakeProfit",
        )

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        venue: Venue,
        quote: str = "USDT",
        margin_mode: str = "isolated",
        leverage: float = 1.0,
    ):
        super().__init__(api_key, api_secret, venue, quote, default_type="linear")
        self.margin_mode = margin_mode
        self.leverage = leverage

    # -- venue naming ------------------------------------------------------

    def _bare(self, symbol: str) -> str:
        """``SOL/USDT:USDT`` -> ``SOLUSDT``, the form the v5 endpoints want."""
        return self._venue_symbol(symbol)

    def _symbol_from_id(self, venue_id: str) -> str | None:
        """``SOLUSDT`` -> ``SOL/USDT:USDT``, via ccxt's own market table.

        Reconstructed from the loaded markets rather than by string surgery:
        the same id exists on spot, and picking the wrong one would hand the
        engine a symbol whose market has different precision and a different
        minimum.
        """
        self.load_markets()
        for market in self.exchange.markets_by_id.get(venue_id) or []:
            if market.get("linear") and market.get("settle") == self.quote:
                return str(market["symbol"])
        return None

    # -- safety ------------------------------------------------------------

    def assert_venue_ready(self, symbols: list[str]) -> None:
        """Refuse to trade until the venue agrees about leverage.

        Raises rather than warning. An engine that starts with the venue set to
        10x because a previous session left it there would size every position
        correctly and carry ten times the liquidation risk it believes it has,
        and nothing downstream would notice: the sizing maths never reads the
        venue's leverage.

        There is no acknowledgement flag, for the same reason
        ``assert_key_is_safe`` has none.
        """
        self.load_markets()
        problems: list[str] = []

        for symbol in symbols:
            try:
                self.exchange.set_leverage(self.leverage, symbol)
            except ccxt.BaseError as exc:
                # 110043 is "leverage not modified" — it is already at this
                # value, which is the state we wanted, not a failure.
                if "110043" not in str(exc) and "not modified" not in str(exc).lower():
                    problems.append(f"{symbol}: cannot set leverage ({str(exc)[:100]})")
                    continue

            if self.margin_mode == "isolated":
                try:
                    self.exchange.set_margin_mode(
                        "isolated", symbol, {"leverage": self.leverage}
                    )
                except ccxt.BaseError as exc:
                    # Accepted-but-ineffective is the norm on a Unified account,
                    # so this is recorded and not fatal. Leverage is the control
                    # that actually binds; see the class docstring.
                    log.info(
                        "margin_mode_not_set_per_symbol",
                        symbol=symbol,
                        detail=str(exc)[:120],
                    )

        effective = self._venue_leverage(symbols)
        for symbol, value in effective.items():
            if value > self.leverage + 1e-9:
                problems.append(
                    f"{symbol}: venue leverage is {value:g}x, configured ceiling is "
                    f"{self.leverage:g}x"
                )

        if self.leverage > 1.0 and not self._isolated_account():
            problems.append(
                f"leverage {self.leverage:g}x requires isolated margin; the account "
                "reports cross, where a liquidation can reach balances this engine "
                "does not own"
            )

        if problems:
            raise RuntimeError(
                "perpetual venue is not in a safe state:\n  - " + "\n  - ".join(problems)
            )

        log.info(
            "perp_venue_ready",
            leverage=self.leverage,
            margin_mode=self.margin_mode,
            isolated_account=self._isolated_account(),
            symbols=len(symbols),
        )

    def _venue_leverage(self, symbols: list[str]) -> dict[str, float]:
        """Leverage the venue reports per symbol, as it will actually apply it."""
        found: dict[str, float] = {}
        try:
            response = self.exchange.privateGetV5PositionList(
                {"category": "linear", "settleCoin": self.quote}
            )
        except ccxt.BaseError as exc:
            log.warning("position_leverage_unreadable", error=str(exc)[:160])
            return found

        wanted = {self._bare(s) for s in symbols}
        for row in (response.get("result") or {}).get("list") or []:
            venue_id = str(row.get("symbol") or "")
            if venue_id not in wanted:
                continue
            try:
                found[venue_id] = float(row.get("leverage") or 0.0)
            except (TypeError, ValueError):
                continue
        return found

    def _isolated_account(self) -> bool:
        try:
            info = self.exchange.privateGetV5AccountInfo({}).get("result") or {}
        except ccxt.BaseError as exc:
            log.warning("account_info_unreadable", error=str(exc)[:160])
            return False
        return str(info.get("marginMode", "")) == "ISOLATED_MARGIN"

    # -- positions ---------------------------------------------------------

    def fetch_positions(self) -> dict[str, float] | None:
        """Open contracts, keyed by symbol, as **absolute** size.

        Absolute rather than signed so it answers the same question the spot
        implementation answers — "how much of this do we still hold" — which is
        what both callers compare against ``Position.quantity``. The direction
        is the engine's own record; a venue that disagreed about *direction*
        would be a different failure, and one a size comparison cannot express
        anyway.

        None means the venue could not be read (see ``CCXTBroker._balances``).
        """
        try:
            response = self.exchange.privateGetV5PositionList(
                {"category": "linear", "settleCoin": self.quote}
            )
        except ccxt.BaseError as exc:
            log.error("position_fetch_failed", error=str(exc)[:200])
            return None

        positions: dict[str, float] = {}
        for row in (response.get("result") or {}).get("list") or []:
            try:
                size = abs(float(row.get("size") or 0.0))
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            symbol = self._symbol_from_id(str(row.get("symbol") or ""))
            if symbol:
                positions[symbol] = size
        return positions

    def _sellable(self, symbol: str, wanted: float) -> float:
        """No clamp. The spot fee-in-base-asset problem does not exist here.

        Inheriting the spot version would be actively harmful: it reads the
        *base coin balance*, which for a perpetual is zero, and would clamp
        every protective stop to nothing.
        """
        return wanted

    # -- protective orders -------------------------------------------------

    def place_stop(
        self, symbol: str, quantity: float, stop_price: float, client_id: str
    ) -> str | None:
        """Attach a stop-loss to the position itself.

        Not a resting reduce-only order: on perpetuals the venue tracks the
        stop as a property of the position, which means it cannot drift out of
        step with the position's size the way a separate order can. Setting it
        again simply overwrites, so this is idempotent without the adopt or
        replace dance the spot path needs.
        """
        self.load_markets()
        try:
            self.exchange.privatePostV5PositionTradingStop(
                {
                    "category": "linear",
                    "symbol": self._bare(symbol),
                    "stopLoss": self.exchange.price_to_precision(symbol, stop_price),
                    "positionIdx": 0,
                }
            )
        except ccxt.BaseError as exc:
            log.error(
                "protective_stop_rejected",
                symbol=symbol,
                stop=stop_price,
                error=str(exc)[:200],
                consequence="position is unprotected between bars until this succeeds",
            )
            return None

        log.info("protective_stop_set", symbol=symbol, stop=round(stop_price, 8))
        # The venue identifies it by the position, not by an order id. Returning
        # the symbol keeps ``meta["stop_order_id"]`` meaningful for the engine's
        # own bookkeeping without inventing an id the venue would not recognise.
        return f"position:{self._bare(symbol)}"

    def place_take_profit(self, symbol: str, target_price: float) -> bool:
        """Attach a take-profit alongside the stop.

        Impossible on spot — a resting stop locks the base asset and the second
        reduce-only order is refused ``170131``, which is why invariant 39 puts
        the target in the engine. Measured on perpetuals: both legs are accepted
        in the same call. That matters beyond tidiness, because a target held at
        the venue still works while this process is down.
        """
        self.load_markets()
        try:
            self.exchange.privatePostV5PositionTradingStop(
                {
                    "category": "linear",
                    "symbol": self._bare(symbol),
                    "takeProfit": self.exchange.price_to_precision(symbol, target_price),
                    "positionIdx": 0,
                }
            )
        except ccxt.BaseError as exc:
            log.warning("take_profit_rejected", symbol=symbol, error=str(exc)[:160])
            return False
        log.info("take_profit_set", symbol=symbol, target=round(target_price, 8))
        return True

    def amend_stop(self, symbol: str, order_id: str, stop_price: float) -> str | None:
        """Move the position's stop. Overwrite, not edit.

        The base implementation amends a real conditional order. There is no
        such order here: ``place_stop`` returns ``position:<symbol>`` because
        the venue tracks a perpetual's stop as a property of the position.
        Inherited, that call raised on every trail, got caught, and reported
        failure — so the venue kept the *first* stop for the life of the
        position while the local book showed the trailed one. The gap only
        appears when the engine is down, which is the one case a venue-side
        stop exists for (invariant 12).

        ``order_id`` is accepted for interface parity and deliberately unused:
        the position identifies itself.
        """
        del order_id
        return self.place_stop(symbol, 0.0, stop_price, "")

    def cancel_stop(self, symbol: str, order_id: str) -> bool:
        """Clear the position's stop by setting it to zero."""
        try:
            self.exchange.privatePostV5PositionTradingStop(
                {
                    "category": "linear",
                    "symbol": self._bare(symbol),
                    "stopLoss": "0",
                    "positionIdx": 0,
                }
            )
            return True
        except ccxt.BaseError as exc:
            log.warning("stop_cancel_failed", symbol=symbol, error=str(exc)[:160])
            return False
