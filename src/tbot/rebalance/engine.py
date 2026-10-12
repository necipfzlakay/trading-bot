"""The daily-close rebalancing loop: H19 carry, H20 trend, H21 xsmom.

Not the bar Runner: these strategies decide once a day (carry) or once a week
(trend, momentum), hold for weeks, and have no stop and no target — the rule
is the exit. What they need from an engine is small and has to be exact:

1. At a daily close (00:00 UTC, plus a grace period for the venue to publish
   the closed candle and settle funding), fetch closed daily bars — or settled
   funding — over REST, never the forming bar (invariant 1).
2. Ask ``signals`` which names to hold. Exits first, then entries.
3. Every symbol traded is **claimed** first (``engine.claims``). The spot
   wallet is shared with the main engine: a coin two engines hold is a coin
   neither can account for, so one engine per coin (invariant 81).
4. Book every fill on the venue's terms (invariant 60); size each new name to
   an equal share of the allocation once, and hold that quantity.
5. Carry: a spot long and a perp short of the same quantity. If the hedge leg
   fails the spot leg is unwound at once — an unhedged "carry" position is a
   directional bet nobody registered. Funding is read back from the venue's
   own transaction log and banked on the perp position (``book_income``).

A decision picked up more than ``STALE_DECISION_SECONDS`` after its close
manages exits only (invariant 77's rule, at this engine's timescale): the
backtest fills at the next open, and a fill hours later is a different trade.
"""

from __future__ import annotations

import json
import signal as os_signal
import time
from collections import deque
from dataclasses import dataclass, field, replace

from tbot.core.clock import now_ms
from tbot.core.config import Config, Secrets
from tbot.core.db import Database
from tbot.core.logging import get_logger
from tbot.core.models import (
    ExitReason,
    Fill,
    NoTradeReason,
    Order,
    OrderStatus,
    OrderType,
    Regime,
    ScanRow,
    Side,
)
from tbot.engine.claims import SymbolClaims
from tbot.engine.portfolio import Portfolio, restore_position
from tbot.engine.siblings import CarriedBook, carried_book, claim_owner, own_starting_cash
from tbot.engine.state import EngineState, RestartTracker, StateWriter
from tbot.execution.ccxt_broker import CCXTBroker, PerpBroker
from tbot.execution.safety import assert_key_is_safe
from tbot.rebalance.signals import (
    DAY_MS,
    carry_wants,
    funding_sum,
    momentum,
    trend_wants,
    xsmom_wants,
)

log = get_logger("rebalance.engine")

TICK_SECONDS = 5.0
HEARTBEAT_SECONDS = 60  # invariant 37
EQUITY_EVERY_SECONDS = 300
VENUE_CHECK_SECONDS = 300
# The venue publishes the closed daily candle and settles 00:00 funding within
# seconds; a minute and a half leaves room for its own lag.
DECISION_GRACE_MS = 90_000
STALE_DECISION_SECONDS = 6 * 3600
# A decision whose data could not be read is retried this often, not every tick.
RETRY_SECONDS = 600
PACE_SECONDS = 0.15  # between REST calls for one decision (audit #91)


def perp_of(spot_symbol: str) -> str:
    base, quote = spot_symbol.split("/")
    return f"{base}/{quote}:{quote}"


def close_weekday(close_ms: int) -> int:
    """0 = Monday, for the instant ``close_ms`` (1970-01-01 was a Thursday)."""
    return ((close_ms // DAY_MS) + 3) % 7


@dataclass
class RebalanceEngine:
    config: Config
    secrets: Secrets
    db: Database
    broker: CCXTBroker  # spot
    symbols: list[str]
    perp: PerpBroker | None = None  # carry's short leg

    claims: SymbolClaims = field(init=False)
    portfolio: Portfolio = field(init=False)
    writer: StateWriter = field(init=False)
    state: EngineState = field(init=False)
    recent_scans: deque = field(default_factory=lambda: deque(maxlen=40), init=False)
    _running: bool = field(default=True, init=False)
    _paused: bool = field(default=False, init=False)
    _last_decision: int = field(default=0, init=False)
    _decision_retry_at: float = field(default=0.0, init=False)
    _exit_retry_at: float = field(default=0.0, init=False)
    _marks: dict[str, float] = field(default_factory=dict, init=False)
    _missing: dict[str, int] = field(default_factory=dict, init=False)
    _pending_exits: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.rb.strategy == "carry" and self.perp is None:
            raise ValueError("the carry strategy needs a perpetual broker for its short leg")
        self.claims = SymbolClaims(self.config.engine.claims_path, claim_owner(self.config))
        self.portfolio = Portfolio(starting_cash=self.config.risk.max_capital)
        self.writer = StateWriter(self.config.state_path)
        self.state = EngineState(
            mode=self.config.mode.value,
            venue=self.config.venue.value,
            status="starting",
            started_ts=now_ms(),
            universe_size=len(self.symbols),
            leverage_allowed=1.0,
        )
        os_signal.signal(os_signal.SIGTERM, self._stop)
        os_signal.signal(os_signal.SIGINT, self._stop)

    def _stop(self, signum, _frame) -> None:
        log.info("shutdown_signal", signal=signum)
        self._running = False

    @property
    def rb(self):
        return self.config.rebalance

    @property
    def slots(self) -> int:
        if self.rb.strategy == "trend":
            return len(self.symbols)
        if self.rb.strategy == "xsmom":
            return self.rb.xsmom_top_k
        return self.rb.carry_max_pairs

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.state.restarts = RestartTracker(self.config.runtime_dir / "restarts.json").record()
        if self.state.restarts > self.config.engine.max_restarts_per_hour:
            self.state.status = "safe_mode"
            log.error("restart_storm", restarts=self.state.restarts)
        self.db.start_run(
            mode=self.config.mode.value,
            venue=self.config.venue.value,
            config_hash=str(hash(self.config.model_dump_json()))[:16],
        )
        assert_key_is_safe(
            self.broker.exchange,
            ip_whitelist_acknowledged=self.config.security.ip_whitelist_acknowledged,
        )
        self.broker.load_markets()
        if self.perp is not None:
            self.perp.load_markets()

        # Read before anything writes a snapshot (invariant 79).
        previous = self.writer.read() or {}
        carried = carried_book(previous)
        self._last_decision = int(previous.get("last_bar_ts") or 0)
        self.state.bars_processed = int(previous.get("bars_processed") or 0)
        realised = self._reconcile(carried.marks)
        self._seed_capital(carried, realised_since=realised)

        if self.state.status != "safe_mode":
            self.state.status = "running"
        self.state.last_bar_ts = self._last_decision
        self._refresh_marks()
        self._snapshot()
        log.info(
            "rebalance_engine_started",
            strategy=self.rb.strategy,
            symbols=len(self.symbols),
            equity=round(self._equity(), 2),
            positions=len(self.portfolio.positions),
            last_decision=self._last_decision or None,
        )
        try:
            self._loop()
        finally:
            self._shutdown()

    def _loop(self) -> None:
        now = time.monotonic()
        next_heartbeat = now
        next_equity = now
        next_venue = now + VENUE_CHECK_SECONDS
        next_funding = now
        while self._running:
            self._poll_commands()
            self._maybe_decide()
            now = time.monotonic()
            if self._pending_exits and now >= self._exit_retry_at:
                self._retry_exits()
            if now >= next_heartbeat:
                self._refresh_marks()
                self._snapshot()
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            if now >= next_equity:
                self._record_equity()
                next_equity = time.monotonic() + EQUITY_EVERY_SECONDS
            if now >= next_venue:
                self._check_venue()
                next_venue = time.monotonic() + VENUE_CHECK_SECONDS
            if self.rb.strategy == "carry" and now >= next_funding:
                self._book_funding()
                next_funding = time.monotonic() + self.rb.carry_funding_poll_seconds
            time.sleep(TICK_SECONDS)

    def _shutdown(self) -> None:
        """Nothing rests at the venue to withdraw: every order here is a market order."""
        self.state.status = "stopped"
        self._snapshot()
        self.claims.close()
        log.info("rebalance_engine_stopped", open_positions=len(self.portfolio.positions))

    # -- startup -----------------------------------------------------------

    def _venue_holdings(self) -> dict[str, float] | None:
        """Every symbol's venue quantity, spot and perp, or None if unreadable."""
        spot = self.broker.fetch_positions()
        if spot is None:
            return None
        held = dict(spot)
        if self.perp is not None:
            perp = self.perp.fetch_positions()
            if perp is None:
                return None
            held.update(perp)
        return held

    def _reconcile(self, marks: dict[str, float] | None = None) -> float:
        """Local positions against the venue; returns what it booked (invariant 80).

        These strategies leave nothing resting at the venue — no stop, no
        target — so a position the venue no longer holds was not closed by
        anything of ours. It is booked out at the last price and reported
        loudly; keeping it would be a phantom the engine sizes against forever.
        """
        for raw in self.db.load_positions():
            self.portfolio.positions[raw["symbol"]] = restore_position(raw)
        held = self._venue_holdings()
        if held is None:
            log.error("reconcile_skipped", reason="venue holdings unreadable")
            return 0.0
        valued_before = self.portfolio.equity(marks or {})
        for symbol, position in list(self.portfolio.positions.items()):
            self.claims.claim(symbol, reason="held at start")
            if abs(float(held.get(symbol, 0.0))) < position.quantity * 0.05:
                self._book_vanished(symbol)
        for symbol in self.claims.mine() - set(self.portfolio.positions):
            if abs(float(held.get(symbol, 0.0))) <= 0.0:
                self.claims.release(symbol)
            else:
                log.error("claimed_position_not_in_book", symbol=symbol,
                          venue_quantity=held.get(symbol),
                          consequence="kept claimed so no other engine trades into it")
        realised = self.portfolio.equity(marks or {}) - valued_before
        log.info("reconcile_complete", positions=len(self.portfolio.positions),
                 realised_while_down=round(realised, 6))
        return realised

    def _seed_capital(self, last: CarriedBook, *, realised_since: float = 0.0) -> None:
        if last.equity is not None and realised_since:
            last = replace(last, equity=last.equity + realised_since)
        free = self.broker.fetch_balance().get(self.config.quote_currency, 0.0)
        cash = own_starting_cash(
            venue_free=free,
            positions_value=self.portfolio.equity(last.marks) - self.portfolio.cash,
            cap=self.config.risk.max_capital,
            last_equity=last.equity,
            reserved_for_siblings=0.0,
        )
        self.portfolio.seed_from_venue(cash, last.marks)
        self.portfolio.carry_over(peak=last.peak, starting=last.starting)
        self.state.starting_equity = self.portfolio.starting_cash
        self.state.peak_equity = self.portfolio.peak_equity
        log.info("capital_seeded", basis="capped allocation", venue_free=round(free, 2),
                 cash=round(cash, 2), last_equity=last.equity, last_peak=last.peak)

    # -- the decision ------------------------------------------------------

    def _due_close(self, now: int) -> int | None:
        """The daily close this engine owes a decision for, if any."""
        close = now - now % DAY_MS
        if close <= self._last_decision or now - close < DECISION_GRACE_MS:
            return None
        if self.rb.strategy != "carry" and close_weekday(close) != self.rb.decision_weekday:
            return None
        return close

    def _maybe_decide(self) -> None:
        if time.monotonic() < self._decision_retry_at:
            return
        now = now_ms()
        close = self._due_close(now)
        if close is None:
            return
        stale = (now - close) / 1000 > STALE_DECISION_SECONDS
        if not self._decide(close, stale=stale):
            self._decision_retry_at = time.monotonic() + RETRY_SECONDS
            return
        self._last_decision = close
        self.state.last_bar_ts = close
        self.state.bars_processed += 1
        for position in self.portfolio.positions.values():
            position.bars_held += 1
            self.db.upsert_position(position)
        self._snapshot()

    def _decide(self, close: int, *, stale: bool) -> bool:
        """One rebalance. False when the data to decide on could not be read."""
        wanted, scores = self._wanted(close)
        if wanted is None:
            log.warning("decision_data_unavailable", close=close,
                        retry_in_seconds=RETRY_SECONDS)
            return False
        held = self._held_names()
        exits = [n for n in held if n not in wanted]
        entries = [n for n in wanted if n not in held]
        log.info("rebalance_decision", strategy=self.rb.strategy, close=close, stale=stale,
                 held=sorted(held), wanted=wanted, exits=exits, entries=entries)

        for name in exits:
            self._exit(name, close)

        reasons: dict[str, NoTradeReason | None] = {}
        blocked = self._entry_block(stale)
        for name in entries:
            if blocked is not None:
                reasons[name] = blocked
                continue
            reasons[name] = self._enter(name, close, scores.get(name, 0.0))
        self._record_scans(close, scores, wanted, reasons)
        return True

    def _entry_block(self, stale: bool) -> NoTradeReason | None:
        if stale:
            return NoTradeReason.STALE_BAR
        if self._paused or self.state.status == "safe_mode":
            return NoTradeReason.RISK_BUDGET_EXHAUSTED
        if self.rb.freeze_equity > 0 and self._equity() <= self.rb.freeze_equity:
            log.warning("freeze_equity_reached", equity=round(self._equity(), 2),
                        freeze=self.rb.freeze_equity)
            return NoTradeReason.RISK_BUDGET_EXHAUSTED
        return None

    def _held_names(self) -> set[str]:
        """Strategy-level names held: spot symbols (a carry pair by its spot leg)."""
        return {s for s in self.portfolio.positions if ":" not in s}

    def _wanted(self, close: int) -> tuple[list[str] | None, dict[str, float]]:
        held = self._held_names()
        if self.rb.strategy == "carry":
            sums: dict[str, float | None] = {}
            for symbol in self.symbols:
                sums[symbol] = self._funding_sum(symbol, close)
                time.sleep(PACE_SECONDS)
            if all(v is None for v in sums.values()):
                return None, {}
            wanted = carry_wants(
                sums, held, enter_sum=self.rb.carry_enter_sum,
                exit_sum=self.rb.carry_exit_sum, max_pairs=self.rb.carry_max_pairs,
            )
            return wanted, {s: v for s, v in sums.items() if v is not None}

        closes: dict[str, list[float]] = {}
        for symbol in self.symbols:
            series = self._daily_closes(symbol, close)
            if series is not None:
                closes[symbol] = series
            time.sleep(PACE_SECONDS)
        if not closes:
            return None, {}

        if self.rb.strategy == "trend":
            wanted, scores = [], {}
            for symbol in self.symbols:
                series = closes.get(symbol)
                verdict = trend_wants(series, self.rb.trend_sma_days) if series else None
                if series and len(series) >= self.rb.trend_sma_days:
                    sma = sum(series[-self.rb.trend_sma_days:]) / self.rb.trend_sma_days
                    scores[symbol] = series[-1] / sma - 1.0
                if verdict is None:
                    if symbol in held:
                        wanted.append(symbol)  # no opinion is not a sale (invariant 10)
                elif verdict:
                    wanted.append(symbol)
            return wanted, scores

        scores = {
            s: m for s in closes
            if (m := momentum(closes[s], self.rb.xsmom_lookback_days)) is not None
        }
        # Held names whose data failed keep their place: unscored, not sold.
        view = {s: closes.get(s, []) for s in set(closes) | held}
        wanted = xsmom_wants(
            view, held, lookback_days=self.rb.xsmom_lookback_days,
            top_k=self.rb.xsmom_top_k, hold_rank=self.rb.xsmom_hold_rank,
        )
        return wanted, scores

    def _daily_closes(self, symbol: str, close: int) -> list[float] | None:
        """Closed daily closes up to ``close``, or None if the venue is behind.

        The last bar must be the one that closes at ``close``; a series that
        ends earlier is stale and deciding on it would be deciding on the past.
        """
        since = close - (self.rb.history_days + 2) * DAY_MS
        try:
            candles = self.broker.exchange.fetch_ohlcv(
                symbol, "1d", since=since, limit=self.rb.history_days + 5
            )
        except Exception as exc:  # noqa: BLE001 — one symbol's failure is that symbol's
            log.warning("daily_fetch_failed", symbol=symbol, error=str(exc)[:160])
            return None
        closed = [c for c in candles if int(c[0]) + DAY_MS <= close]  # never the forming bar
        if not closed or int(closed[-1][0]) != close - DAY_MS:
            log.warning("daily_series_stale", symbol=symbol,
                        last=int(closed[-1][0]) if closed else None, want=close - DAY_MS)
            return None
        return [float(c[4]) for c in closed]

    def _funding_sum(self, symbol: str, close: int) -> float | None:
        assert self.perp is not None
        window = self.rb.carry_window_days
        try:
            rows = self.perp.exchange.fetch_funding_rate_history(
                perp_of(symbol), since=close - (window + 1) * DAY_MS, limit=200
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("funding_fetch_failed", symbol=symbol, error=str(exc)[:160])
            return None
        settled = [(int(r["timestamp"]), float(r["fundingRate"])) for r in rows
                   if r.get("timestamp") and r.get("fundingRate") is not None]
        return funding_sum(settled, close, window)

    # -- entries -----------------------------------------------------------

    def _budget(self) -> float:
        equity = self._equity()
        per_slot = equity / self.slots
        if self.rb.strategy == "carry":
            per_slot = equity / (2 * self.slots)  # two legs of notional per pair
        return min(per_slot, self.portfolio.cash * (0.5 if self.rb.strategy == "carry" else 1.0))

    def _enter(self, name: str, close: int, score: float) -> NoTradeReason | None:
        if self.rb.strategy == "carry":
            return self._enter_pair(name, close, score)
        if not self.claims.claim(name, reason=self.rb.strategy):
            return NoTradeReason.CLAIMED_BY_OTHER_ENGINE
        price = self._price(name)
        budget = self._budget()
        if price is None or budget <= 0:
            self.claims.release(name)
            return NoTradeReason.STALE_DATA if price is None else NoTradeReason.INSUFFICIENT_BALANCE
        order = self._order(name, close, "entry", Side.BUY, budget / price)
        result = self._fill(self.broker, order, price)
        if result is None:
            self.claims.release(name)
            return NoTradeReason.INSUFFICIENT_BALANCE
        self._open(result, name, score, price)
        return NoTradeReason.ENTERED

    def _enter_pair(self, name: str, close: int, score: float) -> NoTradeReason | None:
        """Spot long, then perp short of what the spot leg actually holds."""
        assert self.perp is not None
        short = perp_of(name)
        if not self.claims.claim(name, reason="carry"):
            return NoTradeReason.CLAIMED_BY_OTHER_ENGINE
        if not self.claims.claim(short, reason="carry"):
            self.claims.release(name)
            return NoTradeReason.CLAIMED_BY_OTHER_ENGINE
        price = self._price(name)
        budget = self._budget()
        if price is None or budget <= 0:
            self.claims.release(name)
            self.claims.release(short)
            return NoTradeReason.STALE_DATA if price is None else NoTradeReason.INSUFFICIENT_BALANCE

        # Sized to what both venues can express: the perp's step is coarser.
        quantity = float(self.perp.exchange.amount_to_precision(short, budget / price))
        spot = self._fill(self.broker, self._order(name, close, "entry", Side.BUY, quantity), price)
        if spot is None:
            self.claims.release(name)
            self.claims.release(short)
            return NoTradeReason.INSUFFICIENT_BALANCE
        # A spot buy pays its fee in the base asset (invariant 23): hedge what
        # is actually held, not what was ordered.
        fee_in_base = (spot.fee_paid or 0.0) / spot.average_price if spot.average_price > 0 else 0.0
        hedge = float(self.perp.exchange.amount_to_precision(short, spot.filled_quantity - fee_in_base))
        perp = self._fill(self.perp, self._order(short, close, "entry", Side.SELL, hedge), price)
        position = self._open(spot, name, score, price)
        position.meta["pair"] = short
        self.db.upsert_position(position)
        if perp is None:
            # Unhedged carry is a directional bet nobody registered. Unwind.
            log.error("carry_hedge_failed", symbol=name,
                      action="spot leg sold back at once; the pair is not held")
            self.db.record_event("carry_hedge_failed", f"{name}: perp short refused, spot unwound",
                                 level="error")
            self._exit_leg(name, close, action="unwind")
            self.claims.release(short)
            return NoTradeReason.INSUFFICIENT_BALANCE
        short_position = self._open(perp, short, score, price)
        short_position.meta["pair"] = name
        short_position.meta["funding_through_ms"] = int(short_position.meta.get("opened_ms") or 0)
        self.db.upsert_position(short_position)
        return NoTradeReason.ENTERED

    def _order(self, symbol: str, close: int, action: str, side: Side, quantity: float,
               reduce_only: bool = False) -> Order:
        broker = self.perp if ":" in symbol and self.perp is not None else self.broker
        return broker.build_order(
            namespace=self.config.engine.namespace,
            strategy=self.rb.strategy,
            symbol=symbol,
            bar_ts=close,
            action=action,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reduce_only=reduce_only,
        )

    def _fill(self, broker: CCXTBroker, order: Order, planned: float) -> Order | None:
        """Send a market order; the order back with the venue's price, size and fee."""
        submitted = broker.submit(order)
        self.db.upsert_order(submitted)
        if submitted.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELED):
            log.warning("order_refused", symbol=order.symbol, side=order.side.value,
                        reason=(submitted.reject_reason or "")[:160])
            return None
        if submitted.average_price <= 0 or submitted.filled_quantity <= 0:
            log.warning("fill_terms_unknown", symbol=order.symbol,
                        consequence="recorded at the requested price and size")
            submitted.average_price = submitted.average_price or planned
            submitted.filled_quantity = submitted.filled_quantity or order.quantity
        return submitted

    def _open(self, order: Order, symbol: str, score: float, planned: float):
        price, quantity, side = order.average_price, order.filled_quantity, order.side
        fee, fee_estimated = self._fee_split(symbol, float(order.fee_paid or 0.0), price, quantity)
        fill = Fill(
            order_client_id=order.client_id, symbol=symbol, side=side, quantity=quantity,
            price=price, fee=fee, ts=now_ms(), fee_estimated=fee_estimated,
        )
        self.db.record_fill(fill)
        # No stop: the rule is the exit. 1R is the whole notional, so R reads as
        # the trade's return on what it put to work.
        stop = 0.0 if side is Side.BUY else 2.0 * price
        position = self.portfolio.open_position(
            fill=fill, stop_price=stop, setup=self.rb.strategy,
            risk_amount=quantity * price, conviction=score, regime=Regime.UNKNOWN,
        )
        position.meta.update({"opened_ms": now_ms(), "planned_entry": planned, "no_stop": True})
        self.db.upsert_position(position)
        self._marks[symbol] = price
        log.info("position_opened", symbol=symbol, side=side.value, price=price,
                 quantity=quantity, fee=fee, strategy=self.rb.strategy)
        return position

    # -- exits -------------------------------------------------------------

    def _exit(self, name: str, close: int) -> None:
        legs = [name]
        pair = (self.portfolio.positions.get(name).meta or {}).get("pair") \
            if name in self.portfolio.positions else None
        if pair:
            legs.insert(0, pair)  # the short first: an unhedged long is the lesser risk
        for leg in legs:
            self._exit_leg(leg, close)

    def _exit_leg(self, symbol: str, close: int, action: str = "exit") -> bool:
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return True
        is_perp = ":" in symbol
        broker = self.perp if is_perp and self.perp is not None else self.broker
        quantity = position.quantity if is_perp else self.broker._sellable(symbol, position.quantity)
        order = self._order(symbol, close, action, position.side.opposite, quantity,
                            reduce_only=is_perp)
        result = self._fill(broker, order, self._price(symbol) or position.entry_price)
        if result is None:
            # Kept, and retried: a refused exit is not a closed position (invariant 72).
            self._pending_exits.add(symbol)
            self._exit_retry_at = time.monotonic() + RETRY_SECONDS
            log.error("exit_refused", symbol=symbol, retry_in_seconds=RETRY_SECONDS)
            return False
        self._pending_exits.discard(symbol)
        self._close(symbol, result.average_price, float(result.fee_paid or 0.0),
                    ExitReason.SIGNAL_EXIT)
        return True

    def _retry_exits(self) -> None:
        bucket = now_ms() - now_ms() % 60_000  # a fresh client id per retry minute
        for symbol in list(self._pending_exits):
            self._exit_leg(symbol, bucket, action="retry")

    def _close(self, symbol: str, price: float, fee: float, reason: ExitReason) -> None:
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return
        fee, fee_estimated = self._fee_split(symbol, fee, price, position.quantity)
        # The funding a perp leg earned and the leg it hedged leave with the
        # position unless the trade carries them: H19's kill rule compares a
        # pair's basis loss against exactly this income.
        kept = {k: position.meta[k] for k in ("income", "pair") if k in position.meta}
        trade = self.portfolio.close_position(
            symbol, price, now_ms(), reason, fee, fee_estimated=fee_estimated
        )
        if trade is None:
            return
        trade.meta.update(kept)
        self.db.record_trade(trade)
        self.db.delete_position(symbol)
        self.claims.release(symbol)
        self._marks.pop(symbol, None)
        log.info("position_closed", symbol=symbol, pnl=round(trade.pnl, 4),
                 r=round(trade.r_multiple, 4), reason=reason.value, days=trade.bars_held)
        self._snapshot()

    def _book_vanished(self, symbol: str) -> None:
        """A position the venue no longer holds, with nothing of ours to explain it."""
        price = self._price(symbol) or self.portfolio.positions[symbol].entry_price
        log.error("position_missing_at_venue", symbol=symbol, booked_at=price,
                  consequence="booked out at the last price — nothing of this engine "
                              "rests at the venue, so this was not our exit")
        self.db.record_event("position_missing_at_venue",
                             f"{symbol}: gone at the venue, booked at {price}", level="error")
        self._close(symbol, price, 0.0, ExitReason.MANUAL_FLATTEN)

    def _fee_split(self, symbol: str, reported: float, price: float, quantity: float
                   ) -> tuple[float, float]:
        """Only what the venue reported enters the ledger (invariant 76)."""
        if reported > 0.0:
            return reported, 0.0
        broker = self.perp if ":" in symbol and self.perp is not None else self.broker
        estimate = price * quantity * broker.market_spec(symbol).taker_fee
        log.warning("fee_unreported", symbol=symbol, estimated=round(estimate, 6))
        return 0.0, estimate

    # -- carry income ------------------------------------------------------

    def _book_funding(self) -> None:
        if self.perp is None:
            return
        for symbol, position in list(self.portfolio.positions.items()):
            if ":" not in symbol:
                continue
            since = int(position.meta.get("funding_through_ms") or position.meta.get("opened_ms") or 0)
            rows = self.perp.funding_settlements(symbol, since)
            if rows is None or not rows:
                continue
            for ts, amount, _id in rows:
                self.portfolio.book_income(symbol, amount)
                position.meta["funding_through_ms"] = max(ts, since)
            self.db.upsert_position(position)
            total = float(position.meta.get("income", 0.0))
            log.info("funding_booked", symbol=symbol, settlements=len(rows),
                     amount=round(sum(a for _, a, _ in rows), 6), total=round(total, 6))
        self._snapshot()

    # -- watching ----------------------------------------------------------

    def _price(self, symbol: str) -> float | None:
        broker = self.perp if ":" in symbol and self.perp is not None else self.broker
        ticker = broker.fetch_ticker(symbol) or {}
        price = ticker.get("last") or ticker.get("close")
        return float(price) if price else None

    def _refresh_marks(self) -> None:
        for symbol, position in self.portfolio.positions.items():
            price = self._price(symbol)
            if price:
                self._marks[symbol] = price
                position.update_excursion(price, price)

    def _check_venue(self) -> None:
        """Two readings before a vanished position is booked (invariant 44's rule)."""
        if not self.portfolio.positions:
            return
        held = self._venue_holdings()
        if held is None:
            return
        for symbol, position in list(self.portfolio.positions.items()):
            if abs(float(held.get(symbol, 0.0))) >= position.quantity * 0.05:
                self._missing.pop(symbol, None)
                continue
            self._missing[symbol] = self._missing.get(symbol, 0) + 1
            if self._missing[symbol] >= 2:
                self._missing.pop(symbol, None)
                self._book_vanished(symbol)

    def _poll_commands(self) -> None:
        path = self.config.runtime_dir / "command.json"
        if not path.exists():
            return
        try:
            action = str(json.loads(path.read_text(encoding="utf-8")).get("action") or "")
        except (OSError, ValueError):
            action = ""
        path.unlink(missing_ok=True)
        if action == "pause":
            self._paused = True
            self.state.degraded_reason = "paused by operator — rules still exit, nothing new opens"
        elif action == "resume":
            self._paused = False
            self.state.degraded_reason = None
        elif action == "flatten":
            self._paused = True
            self.state.degraded_reason = "flattened by operator — paused"
            bucket = now_ms() - now_ms() % 60_000
            for name in sorted(self._held_names()):
                self._exit(name, bucket)
        if action:
            log.info("operator_command", action=action)
            self._snapshot()

    # -- reporting ---------------------------------------------------------

    def _equity(self) -> float:
        return self.portfolio.equity(self._marks)

    def _record_equity(self) -> None:
        point = self.portfolio.record_equity(now_ms() - now_ms() % 60_000, self._marks)
        self.db.record_equity(point)

    def _record_scans(self, close: int, scores: dict[str, float], wanted: list[str],
                      reasons: dict[str, NoTradeReason | None]) -> None:
        """One row per symbol per decision, each with its reason (invariant 34)."""
        threshold = {"trend": 0.0, "xsmom": 0.0, "carry": self.rb.carry_enter_sum}[self.rb.strategy]
        held = self._held_names()
        rows = []
        for symbol in self.symbols:
            score = scores.get(symbol)
            if symbol in reasons:
                reason = reasons[symbol]
            elif symbol in held:
                reason = NoTradeReason.ALREADY_IN_POSITION
            elif score is None:
                reason = NoTradeReason.STALE_DATA
            elif symbol in wanted:
                reason = NoTradeReason.MAX_POSITIONS
            else:
                reason = NoTradeReason.BELOW_THRESHOLD
            regime = Regime.UNKNOWN
            if self.rb.strategy == "trend" and score is not None:
                regime = Regime.TREND_UP if score > 0 else Regime.TREND_DOWN
            row = ScanRow(
                symbol=symbol, bar_ts=close, regime=regime, best_side=Side.BUY,
                score=round((score or 0.0) * 100, 4), threshold=threshold * 100,
                no_trade_reason=reason,
            )
            rows.append(row)
            self.recent_scans.append({
                "symbol": symbol, "score": round(row.score, 2), "threshold": round(row.threshold, 2),
                "regime": regime.value, "reason": reason.value if reason else "entered",
            })
        self.db.record_scans(rows)

    def _positions_view(self) -> list[dict]:
        rows = []
        for p in self.portfolio.positions.values():
            mark = self._marks.get(p.symbol)
            rows.append({
                "symbol": p.symbol,
                "side": p.side.value,
                "quantity": p.quantity,
                "entry": p.entry_price,
                "stop": None,
                "target": None,
                "setup": p.setup,
                "bars_held": p.bars_held,
                "mfe_r": round(p.mfe_r, 4),
                "mae_r": round(p.mae_r, 4),
                "r_multiple": round(p.unrealized_r(mark), 4) if mark else None,
                "conviction": round(p.conviction_at_entry, 4),
                "price": round(mark, 8) if mark else None,
                "fees_paid": round(p.fees_paid, 6),
                "fees_estimated": round(p.fees_estimated, 6),
                "income": round(float(p.meta.get("income", 0.0)), 6),
            })
        return rows

    def _snapshot(self) -> None:
        equity = self._equity()
        self.state.equity = equity
        self.state.equity_marks = dict(self._marks)
        self.state.cash = self.portfolio.cash
        self.state.peak_equity = max(self.state.peak_equity, equity)
        self.state.drawdown = (
            1.0 - equity / self.state.peak_equity if self.state.peak_equity > 0 else 0.0
        )
        self.state.open_positions = self._positions_view()
        self.state.scan_top = list(self.recent_scans)[::-1][: len(self.symbols)]
        self.writer.write(self.state)
