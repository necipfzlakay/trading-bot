"""The one-minute maker loop (H18).

Not the bar Runner: that loop refreshes a parquet archive over REST on every
close and takes ~3.5 minutes over 79 symbols. This one keeps closed minutes in
memory from a websocket (``scalp.stream``), asks one pure function what to do
(``scalp.signal``), and lets the venue hold every protective order.

The lifecycle of one trade:

1. A minute closes; the signal fires and names an arm and a side.
2. The symbol is **claimed** on the shared table before anything is sent —
   the fast engine trades the same perpetuals and one-way mode would net us.
3. A **PostOnly** limit joins the best bid (buy) or ask (sell), with the stop
   (market) and target (limit) **attached**: the venue arms both on fill, so
   the position is never unprotected, even for a moment, even with this
   process dead (invariant 12).
4. Unfilled after ``entry_ttl_seconds`` → cancelled, the cancel confirmed from
   the venue's own history (invariant 41), the claim released.
5. Filled → booked at the venue's price and fee (invariant 60), never ours.
6. The venue takes profit or stops out on its own; the engine learns of it by
   polling and books the venue's closing fill — it never sends a close of its
   own for a level the venue holds (audit #82: that race booked a refused
   order as a sale). The one exit it does send is the time stop.
7. Flat at the venue → the claim is released.

Every decision about *whether* to trade is the signal's; every limit on *how
much* is a pre-registered valve in ``ScalpConfig``. None of them is a parameter
to be tuned against the result.
"""

from __future__ import annotations

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
from tbot.engine.siblings import CarriedBook, carried_book, own_starting_cash
from tbot.engine.state import EngineState, RestartTracker, StateWriter
from tbot.execution.ccxt_broker import PerpBroker
from tbot.execution.safety import assert_key_is_safe
from tbot.scalp.signal import Intent, evaluate, exit_levels, required_bars
from tbot.scalp.stream import MINUTE_MS, KlineStream

log = get_logger("scalp.engine")

TICK_SECONDS = 0.5
HEARTBEAT_SECONDS = 60  # invariant 37: the dead-man switch is only as sharp as this
# A PostOnly the venue has not reported yet is still in flight. Its history lags
# — measured, a cancelled order took over six seconds to appear — so "not
# found" is not "no fill" until well past the TTL.
UNKNOWN_ORDER_GRACE_SECONDS = 90.0
# Flat at the venue with no closing fill on record yet: keep asking this long
# before booking the exit at the last price, flagged. Measured: at 00:00 UTC, the
# funding settlement, a TAO stop that filled at 318.79 was still missing from the
# order history 15 s later — five 3 s polls — and was booked at 319.37, a -1.1R
# stop written as -0.49R. Waiting costs nothing, the venue is already flat; a
# guess is a permanent wrong row. Elapsed time, not a poll count (invariant 46).
EXIT_LOOKUP_SECONDS = 60.0


# Contracts the account may not trade until an agreement is signed in the web
# UI. One code per product family, measured live: 110123 XAU ("Trading Terms"),
# 110125 CL ("Crude Oil Trading Terms"), 110126 SOXL ("sign the required
# agreement"). Matching only the first let the other two feed the engine-wide
# refusal streak. The message is checked as well, for the next family's code.
_AGREEMENT_CODES = ("110123", "110125", "110126")
_AGREEMENT_PHRASES = ("trading terms", "required agreement")


def _needs_agreement(reason: str) -> bool:
    text = reason.lower()
    return any(code in reason for code in _AGREEMENT_CODES) or any(
        phrase in text for phrase in _AGREEMENT_PHRASES
    )


@dataclass
class PendingEntry:
    intent: Intent
    order: Order
    take_profit: float
    stop_loss: float
    placed: float  # time.monotonic()
    placed_ms: int = 0  # wall clock just before submission
    cancel_sent: bool = False
    cancel_attempts: int = 0


@dataclass
class ScalpEngine:
    config: Config
    secrets: Secrets
    db: Database
    broker: PerpBroker
    symbols: list[str]

    stream: KlineStream = field(init=False)
    claims: SymbolClaims = field(init=False)
    portfolio: Portfolio = field(init=False)
    writer: StateWriter = field(init=False)
    state: EngineState = field(init=False)
    pending: dict[str, PendingEntry] = field(default_factory=dict, init=False)
    recent_scans: deque = field(default_factory=lambda: deque(maxlen=20), init=False)
    _orders: deque = field(default_factory=deque, init=False)
    _entries: deque = field(default_factory=deque, init=False)
    _reject_streak: int = field(default=0, init=False)
    _cooldown_until: float = field(default=0.0, init=False)
    _flat_since: dict[str, float] = field(default_factory=dict, init=False)  # monotonic
    _orphans_reported: set[str] = field(default_factory=set, init=False)
    _symbol_rejections: dict[str, int] = field(default_factory=dict, init=False)
    _dropped: set[str] = field(default_factory=set, init=False)
    _killed: bool = field(default=False, init=False)
    _running: bool = field(default=True, init=False)
    _minutes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.stream = KlineStream(list(self.symbols))
        self.claims = SymbolClaims(self.config.engine.claims_path, self.config.engine.namespace)
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
    def scalp(self):
        return self.config.scalp

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
        self._restrict_to_tradeable_books()
        self._cancel_leftover_entries()
        # Before reconciliation: closing a position the venue exited while we
        # were down writes a snapshot, and that snapshot — the unseeded book —
        # would then be read back as the previous run's. Measured: 567.46 of a
        # $1000 allocation came back as 1317.73 with its peak reset, the $500
        # kill criterion pushed out of reach.
        carried = carried_book(self.writer.read())
        realised = self._reconcile(carried.marks)
        self._seed_capital(carried, realised_since=realised)
        for symbol in self.symbols:
            self._load_history(symbol)
        if not self.stream.start():
            raise RuntimeError("the scalp engine cannot run without its websocket stream")

        self.state.status = self.state.status if self.state.status == "safe_mode" else "running"
        self._snapshot()
        log.info(
            "scalp_engine_started",
            symbols=len(self.symbols),
            equity=round(self._equity(), 2),
            arms=self.scalp.arms,
            restarts=self.state.restarts,
        )
        try:
            self._loop()
        finally:
            self._shutdown()

    def _loop(self) -> None:
        now = time.monotonic()
        next_poll = now
        next_heartbeat = now + HEARTBEAT_SECONDS
        while self._running:
            for symbol in self.stream.take_gaps():
                self._load_history(symbol)
            for symbol in self.stream.drain():
                self._on_closed_bar(symbol)
            now = time.monotonic()
            if now >= next_poll:
                self._poll_venue()
                next_poll = time.monotonic() + self.scalp.poll_seconds
            if now >= next_heartbeat:
                self._record_equity()
                self._snapshot()
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            time.sleep(TICK_SECONDS)

    def _shutdown(self) -> None:
        """Resting entries are withdrawn; open positions stay, protected at the venue."""
        for symbol in list(self.pending):
            self._cancel_pending(symbol, reason="shutdown")
        self.stream.stop()
        self.state.status = "stopped"
        self._snapshot()
        self.claims.close()
        log.info("scalp_engine_stopped", open_positions=len(self.portfolio.positions))

    # -- startup -----------------------------------------------------------

    def _restrict_to_tradeable_books(self) -> None:
        """Keep the symbols whose book the maker entry can actually work in.

        ``universe.max_spread_bps`` is read here, once, against the live book:
        a spread wider than the target is a trade that starts at a loss.
        """
        limit = self.config.universe.max_spread_bps
        books = self.broker.fetch_microstructure(self.symbols)  # one request for all
        keep, dropped = [], {}
        for symbol in self.symbols:
            spread = (books.get(symbol) or {}).get("spread_bps", 0.0)
            if spread <= 0:
                dropped[symbol] = "no book"  # zero is "not measured", never "tight"
            elif spread > limit:
                dropped[symbol] = f"{spread:.1f}bps"
            else:
                keep.append(symbol)
        if dropped:
            log.info("universe_spread_filter", kept=len(keep), dropped=dropped, limit_bps=limit)
        self.symbols = keep
        self.stream.symbols = list(keep)
        self.state.universe_size = len(keep)

    def _cancel_leftover_entries(self) -> None:
        """Entries a previous run left resting are withdrawn, not adopted.

        Their signal belonged to a minute that is long gone. Only this engine's
        own ids are touched — the namespace prefix is what separates them.
        """
        prefix = f"tb{self.config.engine.namespace[:2]}-"
        for order in self.broker.fetch_open_orders():
            if order.client_id.startswith(prefix) and not order.reduce_only:
                self.broker.cancel_order(order.symbol, order.exchange_id)
                log.info("leftover_entry_cancelled", symbol=order.symbol, client_id=order.client_id)

    def _reconcile(self, marks: dict[str, float] | None = None) -> float:
        """Local positions against the venue, before anything new is opened.

        Returns what the exits booked here changed the book by, measured from
        the prices the previous run's equity valued them at (``marks``). That
        equity is where seeding restarts from, and it knew nothing of a stop
        that filled after the process went down: without the difference the
        trade's P&L reaches ``trades`` but not the book, and equity minus the
        allocation stops equalling the sum of the trades (invariant 76).
        """
        for raw in self.db.load_positions():
            self.portfolio.positions[raw["symbol"]] = restore_position(raw)
        held = self.broker.fetch_positions()
        if held is None:
            log.error("reconcile_skipped", reason="venue positions unreadable")
            return 0.0
        valued_before = self.portfolio.equity(marks or {})
        for symbol, position in list(self.portfolio.positions.items()):
            self.claims.claim(symbol, reason="held at start")
            if float(held.get(symbol, 0.0)) < position.quantity * 0.05:
                # Closed by its own stop or target while this process was down.
                self._book_venue_exit(symbol, force=True)
        mine = self.claims.mine()
        for symbol in mine - set(self.portfolio.positions):
            if float(held.get(symbol, 0.0)) <= 0.0:
                self.claims.release(symbol)
            else:
                log.error(
                    "claimed_position_not_in_book",
                    symbol=symbol,
                    venue_quantity=held.get(symbol),
                    consequence="kept claimed so no other engine nets into it",
                )
        realised = self.portfolio.equity(marks or {}) - valued_before
        log.info("reconcile_complete", positions=len(self.portfolio.positions),
                 realised_while_down=round(realised, 6))
        return realised

    def _seed_capital(
        self, last: CarriedBook | None = None, *, realised_since: float = 0.0
    ) -> None:
        last = carried_book(self.writer.read()) if last is None else last
        if last.equity is not None and realised_since:
            last = replace(last, equity=last.equity + realised_since)
        free = self.broker.fetch_balance().get(self.config.quote_currency, 0.0)
        cash = own_starting_cash(
            venue_free=free,
            # At the prices inside ``last.equity`` — see ``carried_book``.
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

    def _load_history(self, symbol: str) -> None:
        """Closed minutes over REST — at startup, and to refill a gap."""
        need = required_bars(self.scalp) + 5
        since = now_ms() - (need + 2) * MINUTE_MS
        try:
            candles = self.broker.exchange.fetch_ohlcv(symbol, "1m", since=since, limit=need + 2)
        except Exception as exc:  # noqa: BLE001 — a symbol without history just waits
            log.warning("history_fetch_failed", symbol=symbol, error=str(exc)[:120])
            return
        current_minute = now_ms() - now_ms() % MINUTE_MS
        closed = [c for c in candles if c[0] < current_minute]  # never the forming bar
        self.stream.seed(symbol, closed)

    # -- the minute --------------------------------------------------------

    def _on_closed_bar(self, symbol: str) -> None:
        self._minutes += 1
        bar = (self.stream.bars(symbol) or [[0, 0, 0, 0, 0, 0]])[-1]
        bar_ts, high, low = int(bar[0]), bar[2], bar[3]
        self.state.last_bar_ts = max(self.state.last_bar_ts, bar_ts)

        position = self.portfolio.positions.get(symbol)
        if position is not None:
            position.bars_held += 1
            position.update_excursion(high, low)
            self.db.upsert_position(position)
            if position.bars_held >= self.scalp.time_stop_bars:
                self._time_stop(symbol, bar_ts)
            return

        if symbol in self._dropped:
            return
        intent = evaluate(symbol, self.stream.closes(symbol), bar_ts, self.scalp)
        if intent is None:
            return
        reason = self._try_enter(intent)
        row = ScanRow(
            symbol=symbol,
            bar_ts=bar_ts,
            regime=Regime.UNKNOWN,
            best_side=intent.side,
            score=abs(intent.move) * 10_000,  # the move in bps
            threshold=intent.threshold * 10_000,
            no_trade_reason=reason,
        )
        self.db.record_scans([row])
        self.recent_scans.append(
            {
                "symbol": symbol,
                "score": round(row.score, 1),
                "threshold": round(row.threshold, 1),
                "regime": intent.arm,
                "reason": reason.value if reason else "entered",
            }
        )

    def _try_enter(self, intent: Intent) -> NoTradeReason | None:
        """Every reason not to trade, checked before anything is sent."""
        symbol, now = intent.symbol, time.monotonic()
        if self._killed or self.state.status == "safe_mode":
            return NoTradeReason.RISK_BUDGET_EXHAUSTED
        if self.stream.is_stale:
            return NoTradeReason.STALE_DATA
        if symbol in self.portfolio.positions or symbol in self.pending:
            return NoTradeReason.ALREADY_IN_POSITION
        if len(self.portfolio.positions) + len(self.pending) >= self.config.risk.max_positions:
            return NoTradeReason.MAX_POSITIONS
        if now < self._cooldown_until or not self._within_rate(now):
            return NoTradeReason.THROTTLED

        book = self.broker.best_bid_ask(symbol)
        if book is None:
            return NoTradeReason.STALE_DATA
        bid, ask = book
        if (ask - bid) / ((ask + bid) / 2) * 10_000 > self.config.universe.max_spread_bps:
            return NoTradeReason.SPREAD_TOO_WIDE
        # Join the best level on our own side. PostOnly means it rests there or
        # is cancelled by the venue — it can never take liquidity.
        price = bid if intent.side is Side.BUY else ask

        spec = self.broker.market_spec(symbol)
        committed = sum(p.order.quantity * p.order.price for p in self.pending.values())
        notional = min(
            self.config.risk.max_capital / self.config.risk.max_positions,
            self.portfolio.cash - committed,
        )
        quantity = notional / price if price > 0 else 0.0
        if notional < spec.min_notional * 1.05 or quantity < spec.min_amount:
            return NoTradeReason.MIN_NOTIONAL

        if not self.claims.claim(symbol, reason=intent.setup):
            return NoTradeReason.CLAIMED_BY_OTHER_ENGINE

        take_profit, stop_loss = exit_levels(intent.side, price, self.scalp)
        order = self.broker.build_order(
            namespace=self.config.engine.namespace,
            strategy=intent.setup,
            symbol=symbol,
            bar_ts=intent.bar_ts,
            action="entry",
            side=intent.side,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            price=price,
        )
        if self.db.order_exists(order.client_id):
            self.claims.release(symbol)
            return NoTradeReason.COOLDOWN_SAME_BAR  # idempotency (invariant 6)
        order.meta = {
            "arm": intent.arm,
            "setup": intent.setup,
            "stop": stop_loss,
            "take_profit": take_profit,
            "move_bps": round(intent.move * 10_000, 2),
            "threshold_bps": round(intent.threshold * 10_000, 2),
        }

        placed_ms = now_ms()
        placed = self.broker.place_maker_entry(order, take_profit, stop_loss)
        self._orders.append(now)
        self.db.upsert_order(placed)
        if placed.status is OrderStatus.REJECTED:
            self.claims.release(symbol)
            self._note_rejection(symbol, placed.reject_reason or "")
            return NoTradeReason.INSUFFICIENT_BALANCE

        self._reject_streak = 0
        self._entries.append(now)
        self.pending[symbol] = PendingEntry(
            intent, placed, take_profit, stop_loss, now, placed_ms=placed_ms
        )
        log.info(
            "maker_entry_placed",
            symbol=symbol,
            arm=intent.arm,
            side=intent.side.value,
            price=placed.price,
            quantity=placed.quantity,
            take_profit=round(take_profit, 8),
            stop_loss=round(stop_loss, 8),
        )
        return None

    def _within_rate(self, now: float) -> bool:
        for window in (self._orders, self._entries):
            while window and now - window[0] > 60.0:
                window.popleft()
        return (
            len(self._orders) < self.scalp.max_orders_per_minute
            and len(self._entries) < self.scalp.max_entries_per_minute
        )

    def _note_rejection(self, symbol: str, reason: str) -> None:
        """Count a refusal against the symbol, and against the venue as a whole.

        The two mean different things. A symbol the account may not trade —
        measured: Bybit's TradFi perpetuals (XAU, ...) answer ``110123 You must
        agree to the Trading Terms`` until the terms are accepted in the web UI —
        will refuse every time, and counting it toward the engine-wide streak
        put the whole engine into cool-down over one untradeable contract.
        Such a symbol is dropped for the run instead; a symbol that merely keeps
        failing is dropped after three. The engine-wide streak is kept for what
        it was for: the venue refusing everything.
        """
        count = self._symbol_rejections.get(symbol, 0) + 1
        self._symbol_rejections[symbol] = count
        structural = _needs_agreement(reason)
        if structural or count >= 3:
            self._drop_symbol(symbol, reason)
            if structural:
                return
        self._reject_streak += 1
        log.warning("entry_rejected", symbol=symbol, streak=self._reject_streak, reason=reason[:160])
        if self._reject_streak >= self.scalp.reject_streak_limit:
            self._cooldown_until = time.monotonic() + self.scalp.cooldown_seconds
            self._reject_streak = 0
            log.error("rejection_cooldown", seconds=self.scalp.cooldown_seconds, last=reason[:160])
            self.db.record_event(
                "rejection_cooldown",
                f"{self.scalp.reject_streak_limit} refusals in a row; "
                f"no new entries for {self.scalp.cooldown_seconds}s. Last: {reason[:120]}",
                level="error",
            )

    def _drop_symbol(self, symbol: str, reason: str) -> None:
        if symbol in self._dropped:
            return
        self._dropped.add(symbol)
        self.state.universe_size = len(self.symbols) - len(self._dropped)
        log.warning("symbol_dropped", symbol=symbol, reason=reason[:160],
                    consequence="no new entries on it this run; an open position is still managed")
        self.db.record_event(
            "symbol_dropped", f"{symbol}: {reason[:160]}", level="warning"
        )

    # -- the venue ---------------------------------------------------------

    def _poll_venue(self) -> None:
        self._check_kill()
        for symbol in list(self.pending):
            self._check_pending(symbol)
        stale_claims = self.claims.mine() - set(self.portfolio.positions) - set(self.pending)
        if not self.portfolio.positions and not stale_claims:
            return
        held = self.broker.fetch_positions()
        if held is None:
            return  # unreadable is not flat
        self._sweep_claims(stale_claims, held)
        for symbol, position in list(self.portfolio.positions.items()):
            if float(held.get(symbol, 0.0)) < position.quantity * 0.05:
                self._book_venue_exit(symbol)
            else:
                self._flat_since.pop(symbol, None)

    def _check_kill(self) -> None:
        """The pre-registered H18 kill: at or below the floor, nothing new opens.

        Checked on every poll, unconditionally. It first lived after the early
        return for "no open positions" — so once the losses were realised and
        every position closed, which is exactly when the floor is reached, the
        check never ran.
        """
        if self._killed:
            return
        equity = self._equity()
        if equity > self.scalp.kill_equity:
            return
        self._killed = True
        self.state.status = "killed"
        log.error("kill_equity_reached", equity=round(equity, 2), floor=self.scalp.kill_equity)
        self.db.record_event(
            "kill_equity_reached",
            f"equity {equity:.2f} <= {self.scalp.kill_equity:.2f}: no new entries "
            "(pre-registered H18 kill criterion)",
            level="error",
        )

    def _sweep_claims(self, stale: set[str], held: dict[str, float]) -> None:
        """Release claims this engine holds without a position or a live entry.

        An entry whose state never came back keeps its claim (it may have
        filled). Once the venue shows the symbol flat, it is handed back; while
        it carries size, it stays claimed and is reported — once.
        """
        for symbol in stale:
            if float(held.get(symbol, 0.0)) <= 0.0:
                self.claims.release(symbol)
                self._orphans_reported.discard(symbol)
            elif symbol not in self._orphans_reported:
                self._orphans_reported.add(symbol)
                log.error(
                    "claimed_position_not_in_book",
                    symbol=symbol,
                    venue_quantity=held.get(symbol),
                    consequence="kept claimed so no other engine nets into it; "
                    "its venue stop and target still protect it",
                )
                self.db.record_event(
                    "claimed_position_not_in_book",
                    f"{symbol}: {held.get(symbol)} at the venue, not in the scalp book",
                    level="error",
                )

    def _check_pending(self, symbol: str) -> None:
        entry = self.pending[symbol]
        age = time.monotonic() - entry.placed
        state = self.broker.order_state(symbol, entry.order.exchange_id)

        if state is None:
            if age > self.scalp.entry_ttl_seconds + UNKNOWN_ORDER_GRACE_SECONDS:
                log.error(
                    "entry_state_unknown",
                    symbol=symbol,
                    order_id=entry.order.exchange_id,
                    consequence="claim kept; the claim sweep releases it once the venue is flat",
                )
                self.pending.pop(symbol, None)
            elif age > self.scalp.entry_ttl_seconds and not entry.cancel_sent:
                self._send_cancel(entry)
            return

        filled = float(state.get("filled") or 0.0)
        if state.get("terminal"):
            self.pending.pop(symbol, None)
            if filled > 0:
                self._book_entry_fill(entry, state)
            else:
                self._record_unfilled(entry, state)
                self.claims.release(symbol)
                log.info("maker_entry_expired", symbol=symbol, status=state.get("status"),
                         reason=state.get("reason") or "", after_seconds=round(age, 1))
            return

        if age > self.scalp.entry_ttl_seconds and entry.cancel_attempts < 3:
            self._send_cancel(entry)
        # Still not terminal: keep it pending. A cancel the venue has not
        # confirmed is not a cancel (invariant 41), and releasing the claim now
        # could hand the symbol on under a live order.

    def _send_cancel(self, entry: PendingEntry) -> None:
        entry.cancel_sent = True
        entry.cancel_attempts += 1
        self.broker.cancel_order(entry.order.symbol, entry.order.exchange_id)
        self._orders.append(time.monotonic())

    def _cancel_pending(self, symbol: str, reason: str) -> None:
        entry = self.pending.get(symbol)
        if entry is None:
            return
        self._send_cancel(entry)
        for _ in range(8):
            state = self.broker.order_state(symbol, entry.order.exchange_id)
            if state and state.get("terminal"):
                self.pending.pop(symbol, None)
                if float(state.get("filled") or 0.0) > 0:
                    self._book_entry_fill(entry, state)
                else:
                    self._record_unfilled(entry, state)
                    self.claims.release(symbol)
                return
            time.sleep(0.75)
        log.error("cancel_unconfirmed", symbol=symbol, reason=reason,
                  consequence="claim kept; next start cancels leftovers")

    def _record_unfilled(self, entry: PendingEntry, state: dict) -> None:
        """The order ended without a fill; the book must say so, and why.

        Left at ``open`` it reads as a live order forever. And the reason is
        data: a PostOnly the venue cancelled for crossing (the book moved away
        between reading it and placing) is a fill-rate fact the backtest cannot
        see on one-minute bars, where a TTL expiry is not.
        """
        entry.order.status = OrderStatus.CANCELED
        entry.order.reject_reason = str(state.get("reason") or state.get("status") or "")
        self.db.upsert_order(entry.order)

    def _book_entry_fill(self, entry: PendingEntry, state: dict) -> None:
        """Open the position on the terms the venue reports (invariant 60)."""
        symbol = entry.order.symbol
        price = float(state.get("average_price") or 0.0) or float(entry.order.price or 0.0)
        quantity = float(state["filled"])
        fee, fee_estimated = self._fee_split(
            symbol, float(state.get("fee") or 0.0), price, quantity, "entry", maker=True
        )
        fill = Fill(
            order_client_id=entry.order.client_id,
            symbol=symbol,
            side=entry.intent.side,
            quantity=quantity,
            price=price,
            fee=fee,
            ts=now_ms(),
            is_maker=True,
            fee_estimated=fee_estimated,
        )
        entry.order.status = OrderStatus.FILLED
        entry.order.filled_quantity, entry.order.average_price = quantity, price
        entry.order.fee_paid = fee
        self.db.upsert_order(entry.order)
        self.db.record_fill(fill)
        position = self.portfolio.open_position(
            fill=fill,
            stop_price=entry.stop_loss,
            setup=entry.intent.setup,
            risk_amount=quantity * abs(price - entry.stop_loss),
            conviction=0.0,
            regime=Regime.UNKNOWN,
        )
        # When the venue filled it, not when this process noticed. The target
        # and stop are attached to the entry and live from the fill on; a poll
        # (3 s) is slower than a one-minute target, and a closing fill stamped
        # before ``opened_ms`` is filtered out of ``recent_reduce_fill`` as a
        # previous position's. Measured: three targets filled 1-2 s after
        # their entries and were booked as stops at a guessed price, one of
        # them a +0.39 win written as a -0.66 loss. The submission time is the
        # fallback: nothing this order opened can have closed before it. Not
        # ``order.created_ts`` — that is the signal bar, which can precede the
        # previous position's close on this symbol.
        opened_ms = int(state.get("updated_ms") or 0) or entry.placed_ms or now_ms()
        position.meta.update(
            {
                "opened_ms": opened_ms,
                "tp_price": entry.take_profit,
                "arm": entry.intent.arm,
                "entry_bar_ts": entry.intent.bar_ts,
                # The venue holds both legs; there is no separate stop order.
                "stop_order_id": f"attached:{entry.order.exchange_id}",
                "stop_at_venue": entry.stop_loss,
            }
        )
        self.db.upsert_position(position)
        log.info("position_opened", symbol=symbol, arm=entry.intent.arm,
                 side=entry.intent.side.value, price=price, quantity=quantity, fee=fee)
        self._snapshot()

    def _time_stop(self, symbol: str, bar_ts: int) -> None:
        """The one exit the engine sends itself. Reduce-only market."""
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return
        order = self.broker.build_order(
            namespace=self.config.engine.namespace,
            strategy=position.setup,
            symbol=symbol,
            bar_ts=bar_ts,
            action="exit",
            side=position.side.opposite,
            quantity=position.quantity,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )
        submitted = self.broker.submit(order)
        self._orders.append(time.monotonic())
        self.db.upsert_order(submitted)
        if submitted.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED):
            # Most likely the venue's stop or target got there first; the next
            # poll books the venue's own fill. Never book a refused order.
            log.info("time_stop_refused", symbol=symbol, reason=(submitted.reject_reason or "")[:120])
            return
        if submitted.filled_quantity <= 0 or submitted.average_price <= 0:
            log.warning("time_stop_terms_unknown", symbol=symbol,
                        consequence="booked from the venue on the next poll")
            return
        # ``fee_paid`` is zero when the venue did not report it; ``_close``
        # estimates that case beside the ledger rather than booking it free.
        self._close(symbol, submitted.average_price, submitted.fee_paid, ExitReason.TIME_STOP)

    def _book_venue_exit(self, symbol: str, force: bool = False) -> None:
        """The venue closed it (target or stop). Book the venue's fill, not a guess."""
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return
        since = int(position.meta.get("opened_ms") or position.entry_ts or 0)
        state = self.broker.recent_reduce_fill(symbol, since, position.side.opposite)
        if state:
            reason = (
                ExitReason.TAKE_PROFIT
                if state.get("kind") == "take_profit"
                else ExitReason.INITIAL_STOP
            )
            self._close(symbol, float(state["average_price"]), float(state.get("fee") or 0.0), reason)
            return
        first_seen = self._flat_since.setdefault(symbol, time.monotonic())
        if time.monotonic() - first_seen < EXIT_LOOKUP_SECONDS and not force:
            return  # the venue's history lags; ask again next poll
        price = self.stream.last_price(symbol) or position.current_stop
        log.error("exit_terms_unknown", symbol=symbol, booked_at=price,
                  consequence="flat at the venue but no closing fill on record")
        # No fill, so no reported fee: ``_close`` estimates it beside the ledger.
        self._close(symbol, price, 0.0, ExitReason.INITIAL_STOP)

    def _fee_split(
        self,
        symbol: str,
        reported: float,
        price: float,
        quantity: float,
        stage: str,
        *,
        maker: bool = False,
    ) -> tuple[float, float]:
        """``(reported, estimated)`` — exactly one of them carries the fee.

        Only what the venue reported enters the ledger (invariant 76). A leg it
        did not report on is estimated at the configured rate and kept beside
        it: out of cash and ``pnl``, shown in the panel's own box. Recording it
        as zero, as the entry and the time stop did, understated the cost; the
        exit fallback wrote the guess into the ledger as if charged.
        """
        if reported > 0.0:
            return reported, 0.0
        spec = self.broker.market_spec(symbol)
        estimate = price * quantity * (spec.maker_fee if maker else spec.taker_fee)
        log.warning("fee_unreported", symbol=symbol, stage=stage, estimated=round(estimate, 6),
                    consequence="estimate kept beside the ledger; not in pnl, fees or cash")
        return 0.0, estimate

    def _close(self, symbol: str, price: float, fee: float, reason: ExitReason) -> None:
        position = self.portfolio.positions.get(symbol)
        fee_estimated = 0.0
        if position is not None:
            fee, fee_estimated = self._fee_split(symbol, fee, price, position.quantity, "exit")
        trade = self.portfolio.close_position(
            symbol, price, now_ms(), reason, fee, fee_estimated=fee_estimated
        )
        self._flat_since.pop(symbol, None)
        if trade is None:
            return
        self.db.record_trade(trade)
        self.db.delete_position(symbol)
        self.claims.release(symbol)
        self.db.record_event(
            "trade_context",
            f"{trade.symbol} {trade.side.value} {trade.r_multiple:+.2f}R",
            payload={
                "symbol": trade.symbol,
                "side": trade.side.value,
                "setup": trade.setup,
                "r_multiple": round(trade.r_multiple, 4),
                "pnl": round(trade.pnl, 6),
                "bars_held": trade.bars_held,
                "exit_reason": trade.exit_reason.value,
            },
        )
        log.info("position_closed", symbol=symbol, setup=trade.setup, r=round(trade.r_multiple, 3),
                 pnl=round(trade.pnl, 4), reason=reason.value, bars=trade.bars_held)
        self._check_kill()
        self._snapshot()

    # -- reporting ---------------------------------------------------------

    def _marks(self) -> dict[str, float]:
        marks = {}
        for symbol in self.portfolio.positions:
            price = self.stream.last_price(symbol)
            if price:
                marks[symbol] = price
        return marks

    def _equity(self) -> float:
        return self.portfolio.equity(self._marks())

    def _record_equity(self) -> None:
        point = self.portfolio.record_equity(now_ms() - now_ms() % MINUTE_MS, self._marks())
        self.db.record_equity(point)

    def _positions_view(self) -> list[dict]:
        rows = []
        for p in self.portfolio.positions.values():
            mark = self.stream.last_price(p.symbol)
            rows.append(
                {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry": p.entry_price,
                    "stop": p.current_stop,
                    "target": p.meta.get("tp_price"),
                    "setup": p.setup,
                    "bars_held": p.bars_held,
                    "mfe_r": round(p.mfe_r, 2),
                    "mae_r": round(p.mae_r, 2),
                    "r_multiple": round(p.unrealized_r(mark), 2) if mark else None,
                    "conviction": 0.0,
                    "price": round(mark, 8) if mark else None,
                    "fees_paid": round(p.fees_paid, 6),
                    "fees_estimated": round(p.fees_estimated, 6),
                }
            )
        return rows

    def _snapshot(self) -> None:
        marks = self._marks()
        equity = self.portfolio.equity(marks)
        self.state.equity = equity
        self.state.equity_marks = marks  # see ``carried_book``
        self.state.cash = self.portfolio.cash
        self.state.peak_equity = max(self.state.peak_equity, equity)
        self.state.drawdown = (
            1.0 - equity / self.state.peak_equity if self.state.peak_equity > 0 else 0.0
        )
        self.state.bars_processed = self._minutes
        self.state.open_positions = self._positions_view()
        self.state.scan_top = list(self.recent_scans)[::-1][:10]
        self.state.feed = self.stream.status()
        self.writer.write(self.state)
