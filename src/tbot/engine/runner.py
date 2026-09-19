"""The live engine loop.

Runs forever until stopped by hand. Every failure path resolves itself — retry,
degrade, or fall back to safe mode — because the operator's requirement was that
the bot decides for itself and never waits for approval.

The loop is deliberately boring: wake on the trigger timeframe's bar close,
reconcile, manage what is open, scan, act at most once, snapshot state, sleep.
Decisions come from the same conviction engine, risk budget and exit manager the
backtester uses, so behaviour matches what was tested.

Ordering inside a bar is not arbitrary. Existing positions are managed *before*
new ones are considered, so a stop that should have fired never competes for
attention with a fresh setup; and state is written last, so an observer reading
``runtime/state.json`` always sees a consistent picture of a completed bar.

Between bar closes the loop does three things and no more: it watches whether
price has reached a level decided at the last close (``_check_intrabar_exits``),
it asks the venue whether a protective stop has fired (``_detect_venue_exits``),
and it writes a heartbeat. None of them computes an indicator on an unclosed
bar — that would make the backtest unable to reproduce the live path, which is
the one property every number in this repo depends on.
"""

from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from tbot.core.clock import (
    is_stale,
    iso,
    last_closed_bar,
    ms_until_next_close,
    now_ms,
    tf_ms,
)
from tbot.core.config import Config, Secrets
from tbot.core.db import Database
from tbot.core.logging import get_logger
from tbot.core.models import (
    Candle,
    ExitReason,
    Fill,
    Mode,
    NoTradeReason,
    OrderStatus,
    OrderType,
    Regime,
    ScanRow,
    Side,
    Venue,
)
from tbot.data.backfill import Backfiller, BybitFetcher
from tbot.data.feed import MarketFeed
from tbot.data.integrity import MIN_BARS_BY_TIMEFRAME
from tbot.data.store import ParquetStore
from tbot.engine.portfolio import Portfolio
from tbot.engine.state import EngineState, RestartTracker, StateWriter
from tbot.engine.watchdog import Watchdog
from tbot.execution.broker import Broker, client_order_id
from tbot.execution.ccxt_broker import CCXTBroker
from tbot.execution.paper_broker import PaperBroker
from tbot.execution.reconcile import apply as apply_reconcile
from tbot.execution.reconcile import reconcile
from tbot.execution.safety import (
    SlippageTracker,
    assert_key_is_safe,
    equity_jump_is_implausible,
)
from tbot.indicators import ta
from tbot.indicators.registry import compute_signals
from tbot.llm.roles import build_roles
from tbot.notify.telegram import TelegramNotifier
from tbot.risk.budget import RiskBudget
from tbot.risk.capital import report as capital_report
from tbot.risk.leverage import LeverageGate
from tbot.risk.sizing import correlation_conflict, size_position
from tbot.strategy.conviction import UNSCORED_REASONS, ConvictionEngine, ScoringContext
from tbot.strategy.exits import ExitManager
from tbot.strategy.regime import classify_series
from tbot.strategy.setups import DEFAULT_SETUPS, detect_all

log = get_logger("engine.runner")
ROOT_DIR = Path(__file__).resolve().parents[3]

BARS_PER_YEAR = {"1h": 8760, "4h": 2190, "1d": 365}


# Frequent enough that a stall is caught in minutes, rare enough that the
# atomic write costs nothing next to a bar that is an hour long.
HEARTBEAT_SECONDS = 60


@dataclass(slots=True)
class Runner:
    """Owns the live loop and everything it touches."""

    config: Config
    secrets: Secrets
    db: Database
    store: ParquetStore
    broker: Broker
    symbols: list[str]
    block_lookup: dict[str, int] = field(default_factory=dict)
    selected_by_regime: dict[str, list[str]] = field(default_factory=dict)
    edge_decay_bars: dict[str, int] = field(default_factory=dict)

    conviction: ConvictionEngine = field(init=False)
    exits: ExitManager = field(init=False)
    risk: RiskBudget = field(init=False)
    portfolio: Portfolio = field(init=False)
    watchdog: Watchdog = field(default_factory=Watchdog)
    state: EngineState = field(init=False)
    writer: StateWriter = field(init=False)
    notifier: TelegramNotifier | None = None
    roles: dict = field(default_factory=dict)
    fetcher: BybitFetcher = field(init=False)
    _running: bool = True
    _paused: bool = False
    _llm_veto: bool = False
    _last_risk_multiplier: float = 1.0
    _last_summary_day: int = 0
    _veto_prices: dict = field(default_factory=dict)
    _veto_horizon_bars: int = 24
    _veto_prices_max: int = 2000
    # Symbols the venue keeps refusing. Dropped for the session rather than
    # retried every bar: a persistent rejection is a property of the symbol
    # (delisted, suspended, precision changed), not bad luck.
    _rejections: dict = field(default_factory=dict)
    _rejection_limit: int = 3
    slippage: SlippageTracker = field(init=False)
    # Between-bar price stream. None in paper mode and whenever ccxt.pro is not
    # importable — every read of it is guarded, because the engine has to work
    # exactly as before when it is absent.
    feed: MarketFeed | None = None
    # Live spread and 24h turnover, refreshed once per bar. Empty until the
    # first successful read; zero always means "not measured".
    _microstructure: dict = field(default_factory=dict)
    # The one armed entry order, if any: {"symbol", "order_id", "client_id",
    # "trigger", "bar_ts", "stop", "quantity", "setup", "conviction", "regime",
    # "risk_amount"}. At most one — see EntryConfig.armed_max_orders.
    _armed: dict | None = None
    # Symbols whose venue inventory has gone missing, and how many consecutive
    # polls have said so. One reading is not evidence — a balance fetched
    # seconds after an entry can lag — so nothing is acted on until it repeats.
    _missing_inventory: dict = field(default_factory=dict)
    # Last close per symbol, refreshed each bar. Lets the balances view price a
    # holding without a network call.
    _marks: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.conviction = ConvictionEngine(
            config=self.config.conviction,
            block_lookup=self.block_lookup,
            selected_by_regime=self.selected_by_regime,
        )
        self.exits = ExitManager(self.config.exits, self.edge_decay_bars)
        self.risk = RiskBudget(self.config.risk)
        self.portfolio = Portfolio(starting_cash=self.config.starting_balance)
        self.writer = StateWriter(self.config.state_path)
        self.fetcher = BybitFetcher(self.config.data.rest_rate_limit_ms)
        self.slippage = SlippageTracker(assumed_bps=self.config.execution.slippage_bps)

        gate = LeverageGate(self.config.promotion_path)
        allowed = gate.allowed_leverage(self.config.risk.max_leverage)

        self.state = EngineState(
            mode=self.config.mode.value,
            venue=self.config.venue.value,
            status="starting",
            started_ts=now_ms(),
            universe_size=len(self.symbols),
            leverage_allowed=allowed,
            llm_enabled=self.config.llm.enabled,
        )
        log.info(
            "leverage_status",
            allowed=allowed,
            explanation=gate.explain(self.config.risk.max_leverage),
        )

        if self.config.telegram.enabled:
            self.notifier = TelegramNotifier(
                self.secrets.telegram_bot_token, self.secrets.telegram_chat_id
            )
        self.roles = build_roles(self.config, self.secrets, self.db)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        log.info("shutdown_signal", signal=signum)
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        tracker = RestartTracker(self.config.runtime_dir / "restarts.json")
        restarts = tracker.record()
        self.state.restarts = restarts

        if restarts > self.config.engine.max_restarts_per_hour:
            # Restarting forever is the requirement; restarting forever *while
            # trading* is not. Hold what is open and stop opening new positions.
            self.state.status = "safe_mode"
            self.risk.degrade(f"{restarts} restarts in the last hour")
            log.error("restart_storm", restarts=restarts)
            if self.notifier:
                self.notifier.notify_error(
                    "Restart storm",
                    f"{restarts} restarts in an hour — safe mode: existing positions "
                    f"still managed, no new entries.",
                )

        self.db.start_run(
            mode=self.config.mode.value,
            venue=self.config.venue.value,
            config_hash=str(hash(self.config.model_dump_json()))[:16],
        )

        if isinstance(self.broker, CCXTBroker):
            # Refuse to trade with a key that can move funds off the exchange.
            assert_key_is_safe(
                self.broker.exchange,
                ip_whitelist_acknowledged=self.config.security.ip_whitelist_acknowledged,
            )

        if self.config.engine.reconcile_on_start:
            self._reconcile()

        balance = self._equity_now()
        if self.config.mode is Mode.LIVE:
            # Invariant: in live mode the balance comes from the venue, never
            # from config. Seeding here keeps the portfolio, the sizing cash
            # limit and the equity curve on the same real number.
            self.portfolio.seed_from_venue(balance)
            balance = self.portfolio.equity({})
            self._check_balance_against_last_run(balance)
        self.risk.update_equity(balance)
        self.state.equity = balance
        self.state.cash = self.portfolio.cash
        self.state.starting_equity = self.portfolio.starting_cash
        self.state.open_positions = self._positions_view()
        self.state.balances = self._balances_view()
        self.state.status = "running" if self.state.status != "safe_mode" else "safe_mode"
        self.writer.write(self.state)

        diagnostic = capital_report(self.config, balance)
        if not diagnostic.usable and self.notifier:
            self.notifier.notify_error("Account size", diagnostic.note)

        log.info(
            "engine_started",
            mode=self.config.mode.value,
            venue=self.config.venue.value,
            symbols=len(self.symbols),
            equity=round(balance, 2),
            restarts=restarts,
        )
        if self.notifier:
            self.notifier.notify_startup(
                self.config.mode.value, self.config.venue.value, balance, len(self.symbols)
            )

        self._start_feed()

        try:
            self._loop()
        finally:
            # An armed entry that outlives the engine fills into an account
            # nobody is managing: no trailing stop, no exit check, and no
            # protective stop at the venue either. The protective stop is the
            # opposite case and stays exactly where it is — it is *for* the time
            # the engine is down.
            self._withdraw_armed_on_shutdown()
            if self.feed is not None:
                self.feed.stop()
            self.state.status = "stopped"
            self.writer.write(self.state)
            self.db.stop_run("clean" if self._running is False else "error")
            log.info("engine_stopped", bars=self.state.bars_processed)

    def _withdraw_armed_on_shutdown(self) -> None:
        armed = self._armed
        if armed is None or not isinstance(self.broker, CCXTBroker):
            return
        if self.broker.cancel_stop_entry(armed["symbol"], armed["order_id"]):
            log.info("armed_entry_withdrawn", symbol=armed["symbol"], reason="engine stopping")
            self._armed = None
        else:
            log.error(
                "armed_entry_left_resting",
                symbol=armed["symbol"],
                order_id=armed["order_id"],
                consequence="it can fill while the engine is down, unmanaged and unprotected",
            )
            self.db.record_event(
                "armed_entry_left_resting",
                f"{armed['symbol']} armed order {armed['order_id']} could not be cancelled",
                level="error",
            )

    def _start_feed(self) -> None:
        """Open the between-bar price stream, if this mode has one.

        Paper mode deliberately gets no feed: paper exists to walk the same path
        as the backtest, and a paper run that reacted to prices the backtest
        cannot see would stop being a comparison.
        """
        if self.config.mode is not Mode.LIVE:
            return
        feed = MarketFeed(list(self.symbols))
        self.feed = feed if feed.start() else None
        if self.feed is None:
            log.warning(
                "feed_disabled",
                consequence="exits are evaluated at bar close only, as before; "
                "the venue stop is unaffected",
            )

    def _loop(self) -> None:
        timeframe = self.config.timeframes.trigger
        last_processed = 0

        while self._running:
            try:
                bar_ts = last_closed_bar(timeframe)
                if bar_ts > last_processed:
                    self._process_bar(bar_ts)
                    last_processed = bar_ts

                # Wake shortly after the next close rather than polling hot,
                # but keep checking for controls while waiting. Bars can be an
                # hour apart, and an emergency flatten that waits for the next
                # bar close is not an emergency control.
                self._wait_for_next_bar(timeframe)

            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                self._record_error(exc)
                time.sleep(min(60, self.config.engine.loop_interval_seconds * 4))

    def _wait_for_next_bar(self, timeframe: str) -> None:
        """Idle until the next bar closes, staying useful the whole time.

        **Every deadline here is measured against ``time.monotonic()``, never
        accumulated.** The previous version added the *intended* step to a
        counter and compared that to the target, so any work inside the loop
        was invisible to it. Telegram's ``getUpdates`` costs 3-8 seconds from
        this machine and was being called on every 5-second tick, so each
        iteration really took about nine seconds while counting as five. The
        loop overshot by roughly 80% and bars were processed **7 to 38 minutes
        after they closed** — entries priced off a close that was half an hour
        stale, and no log line anywhere saying so.

        A clock that measures intentions instead of elapsed time is wrong in
        exactly the way that is hardest to see: nothing errors, nothing retries,
        the bot simply runs late and keeps reporting that it is fine.

        Three cadences, all independent:

        * every tick (``loop_interval_seconds``) — the between-bar exit check,
          which only reads memory and so costs nothing
        * ``execution.venue_poll_seconds`` — ask the venue whether a stop or an
          armed entry has filled, and refresh the balances the panel shows
        * ``telegram.poll_seconds`` — commands, deliberately in the slow lane
        """
        now = time.monotonic()
        deadline = now + (ms_until_next_close(timeframe) + 2000) / 1000
        next_tick = now
        next_command_poll = now
        next_venue_poll = now + self.config.execution.venue_poll_seconds
        next_heartbeat = now + HEARTBEAT_SECONDS
        interval = self.config.engine.loop_interval_seconds

        while self._running:
            now = time.monotonic()
            if now >= deadline:
                return

            if now >= next_command_poll:
                # Scheduled from *after* the call returns: a poll that takes
                # four seconds must not immediately become due again.
                self._poll_commands()
                now = time.monotonic()  # that call can cost seconds; do not
                next_command_poll = now + self.config.telegram.poll_seconds
                # judge the deadlines below against the clock, not against when
                # this iteration happened to begin.

            self._apply_command_file()

            # Cheap: reads the feed's in-memory quotes, no venue call. This is
            # what closes the gap the operator asked about — a symbol that runs
            # mid-bar is seen in seconds rather than at the next close.
            self._check_intrabar_exits()

            if now >= next_venue_poll:
                # Expensive enough to rate-limit. Without it the engine learns
                # its own stop filled only at the next close, and spends the gap
                # with a wrong portfolio, a wrong equity, and a position slot
                # locked against a holding that is gone.
                self._detect_venue_exits()
                self._detect_armed_fill()
                # Same trip to the venue, so the holdings the panel shows are
                # never an hour out of date.
                self.state.balances = self._balances_view()
                next_venue_poll = time.monotonic() + self.config.execution.venue_poll_seconds

            if now >= next_heartbeat:
                # Without this, state.json is only touched on bar close, so on
                # an hourly timeframe a *wedged* engine looks identical to a
                # waiting one for up to an hour — and the dead-man switch has to
                # be slackened to match, which is the whole thing it exists to
                # catch. Positions go unmanaged for that window.
                self.state.open_positions = self._positions_view()
                self.state.feed = self.feed.status() if self.feed else {}
                self.writer.write(self.state)
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

            # Sleep only what is left of this tick. When the work above already
            # overran the interval there is nothing to wait for, and pausing
            # anyway is how the drift got in.
            next_tick = max(next_tick + interval, time.monotonic())
            time.sleep(max(0.0, min(next_tick, deadline) - time.monotonic()))

    # -- per bar -----------------------------------------------------------

    def _process_bar(self, bar_ts: int) -> None:
        frames = self._refresh_data()
        if not frames:
            self.risk.degrade("no market data available")
            self.state.status = "degraded"
            self.writer.write(self.state)
            return

        self.risk.recover()
        self._apply_command_file()
        self._measure_veto_outcomes(frames, bar_ts)
        self._check_event_guard(frames)

        marks = {s: float(f["trigger"]["close"].iloc[-1]) for s, f in frames.items()}
        self._marks.update(marks)

        # A new bar has begun, so the running extremes the feed tracks belong to
        # it and not to the bar that just closed. Without this the intra-bar
        # check would keep comparing against a high set hours ago.
        if self.feed is not None:
            self.feed.reset_extremes()

        # Orders submitted on the previous bar fill against this bar's open.
        # Without this the paper simulator queues orders and never executes
        # them: the portfolio would show positions the broker never opened, with
        # no slippage and no fees — the exact opposite of what paper mode is for.
        self._drain_paper_fills(frames, bar_ts)
        # Before anything reads the portfolio: an armed order may have filled
        # since the last close, and that fill is a position the rest of this bar
        # has to know about.
        self._resolve_armed(bar_ts)
        self._manage_positions(frames, bar_ts)
        self._refresh_microstructure(list(frames))
        scans = self._scan(frames, bar_ts)
        if self.state.equity < self.config.risk.min_equity:
            if self.state.status != "below_min_equity":
                self.state.status = "below_min_equity"
                log.error(
                    "below_minimum_equity",
                    equity=round(self.state.equity, 2),
                    minimum=self.config.risk.min_equity,
                    action="no new entries; open positions still managed",
                    reason="wide-stop setups cannot be sized, so the bot would "
                    "silently trade a different universe than the one tested",
                )
                self.db.record_event(
                    "below_min_equity",
                    f"equity {self.state.equity:.2f} < minimum {self.config.risk.min_equity:.2f}",
                    level="error",
                )
                if self.notifier:
                    self.notifier.notify_error(
                        "Below minimum equity",
                        f"{self.state.equity:.2f} < {self.config.risk.min_equity:.2f}. "
                        f"No new entries — below this the bot would trade a quieter "
                        f"subset of the universe than the one it was tested on. "
                        f"Open positions are still managed.",
                    )
        elif self.risk.frozen:
            if self.state.status != "frozen":
                self.state.status = "frozen"
                log.error(
                    "capital_freeze",
                    drawdown=round(self.risk.state.drawdown, 4),
                    threshold=self.config.risk.freeze_drawdown,
                    action="no new entries; open positions still managed",
                )
                self.db.record_event(
                    "capital_freeze",
                    f"drawdown {self.risk.state.drawdown:.1%} past the "
                    f"{self.config.risk.freeze_drawdown:.0%} floor",
                    level="error",
                )
                if self.notifier:
                    self.notifier.notify_error(
                        "Capital freeze",
                        f"Drawdown {self.risk.state.drawdown:.1%}. No new entries. "
                        f"Open positions are still managed and protected.",
                    )
        elif not self._paused and not self.risk.state.degraded and self.state.status != "safe_mode":
            self._maybe_enter(frames, scans, bar_ts)
            self._maybe_arm(frames, scans, bar_ts)

        point = self.portfolio.record_equity(bar_ts, marks)
        if equity_jump_is_implausible(self.state.equity, point.equity):
            # Almost always bad data rather than a real move. Sizing off it
            # would turn a data error into a real loss.
            self.risk.degrade(
                f"implausible equity move {self.state.equity:.2f} -> {point.equity:.2f}"
            )
            self.db.record_event(
                "implausible_equity", self.risk.state.degraded_reason or "", level="error"
            )
            if self.notifier:
                self.notifier.notify_degraded(self.risk.state.degraded_reason or "")
        self.risk.update_equity(point.equity, bar_ts)
        self.db.record_equity(point)
        self.db.record_scans(scans)

        self.conviction.note_bar(entered=any(s.no_trade_reason is None for s in scans))
        self._snapshot(bar_ts, scans, point)

        # Both of these were written, documented in the config, and never
        # called — the same shape as audit #7, back again. A notification path
        # that exists but is unreachable is worse than none: the operator reads
        # the config, believes they will be told, and is not.
        self._notify_risk_change()
        self._maybe_daily_summary(bar_ts)

        log.info(
            "bar_processed",
            bar=iso(bar_ts),
            equity=round(point.equity, 2),
            drawdown=round(point.drawdown, 4),
            positions=len(self.portfolio.positions),
            scanned=len(scans),
            risk_mult=self.risk.risk_multiplier,
        )

    def _refresh_data(self) -> dict[str, dict[str, pd.DataFrame]]:
        """Top up the archive and load the frames each symbol needs.

        Covers the scan universe *plus* every symbol currently holding a
        position. A symbol can leave the universe between runs — liquidity
        drops, the list is refreshed — and without this its open position would
        never be managed again: the stop would stop trailing and no exit would
        ever fire. It would simply sit there.
        """
        backfiller = Backfiller(self.fetcher, self.store)
        frames: dict[str, dict[str, pd.DataFrame]] = {}
        tf = self.config.timeframes

        held = set(self.portfolio.positions)
        orphaned = held - set(self.symbols)
        if orphaned:
            log.info(
                "loading_data_for_orphaned_positions",
                symbols=sorted(orphaned),
                reason="symbol left the scan universe but the position is still open",
            )

        for symbol in list(dict.fromkeys([*self.symbols, *sorted(held)])):
            ok = True
            for timeframe in tf.all:
                result = self.watchdog.call(
                    f"backfill:{timeframe}",
                    lambda s=symbol, t=timeframe: backfiller.backfill_symbol(s, t, years=0.1),
                )
                if result is None:
                    ok = False
            if not ok:
                continue

            trigger = self.store.read_with_derivatives(symbol, tf.trigger)
            setup = self.store.read_with_derivatives(symbol, tf.setup)
            permission = self.store.read_ohlcv(symbol, tf.permission)
            if not all(
                len(frame) >= MIN_BARS_BY_TIMEFRAME.get(name, 200)
                for name, frame in (
                    (tf.trigger, trigger),
                    (tf.setup, setup),
                    (tf.permission, permission),
                )
            ):
                continue

            if is_stale(
                int(trigger["ts"].iloc[-1]),
                tf.trigger,
                tolerance=self.config.engine.stale_data_tolerance_bars,
            ):
                log.warning("stale_data", symbol=symbol, last_bar=iso(int(trigger["ts"].iloc[-1])))
                continue

            frames[symbol] = {"trigger": trigger, "setup": setup, "permission": permission}

        return frames

    def _drain_paper_fills(self, frames: dict, bar_ts: int) -> None:
        """Execute queued simulated orders against the newly opened bar.

        Only meaningful for :class:`PaperBroker`; a live venue fills on its own.
        """
        if not isinstance(self.broker, PaperBroker):
            return

        for symbol, data in frames.items():
            row = data["trigger"].iloc[-1]
            candle = Candle(
                ts=bar_ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            for fill in self.broker.process_bar(symbol, candle):
                self.db.record_fill(fill)
                order = self.broker.orders.get(fill.order_client_id)
                if order is not None:
                    self.db.upsert_order(order)
                self._apply_paper_fill(fill, order, data, bar_ts)

    def _apply_paper_fill(self, fill, order, data: dict, bar_ts: int) -> None:
        """Turn a simulated fill into portfolio state.

        Entry plans travel on the order's ``meta`` rather than being recomputed,
        so the position that opens is the one that was scored and sized.
        """
        if order is None or fill.side is not Side.BUY or not order.meta:
            return
        if self.portfolio.has_position(fill.symbol):
            return

        planned_entry = float(order.meta.get("planned_entry", fill.price))
        planned_stop = float(order.meta["stop"])
        stop = fill.price - (planned_entry - planned_stop)

        position = self.portfolio.open_position(
            fill=fill,
            stop_price=stop,
            setup=str(order.meta.get("setup", "unknown")),
            risk_amount=float(order.meta.get("risk_amount", 0.0)),
            conviction=float(order.meta.get("conviction", 0.0)),
            regime=Regime(order.meta.get("regime", Regime.UNKNOWN.value)),
        )
        position.meta["entry_atr"] = self._atr_for(data, self.config.exits.trail_timeframe)
        self.db.upsert_position(position)

        log.info(
            "position_opened",
            symbol=fill.symbol,
            price=fill.price,
            quantity=fill.quantity,
            stop=stop,
            conviction=position.conviction_at_entry,
            setup=position.setup,
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.notify_entry(
                fill.symbol,
                "buy",
                fill.price,
                fill.quantity,
                stop,
                position.conviction_at_entry,
                position.setup,
            )

    def _atr_for(self, data: dict, timeframe_role: str) -> float:
        frame = data[timeframe_role]
        series = ta.atr(frame["high"], frame["low"], frame["close"], self.config.exits.atr_period)
        return float(series.iloc[-1]) if not series.empty else 0.0

    def _apply_command_file(self) -> None:
        """Pick up pause/resume/flatten written by the dashboard.

        A file rather than a socket: the engine stays single-threaded, and a
        crashed dashboard cannot leave the bot in a strange state. Polled
        between bars as well as on them, so a flatten takes effect in seconds.
        """
        path = self.config.runtime_dir / "command.json"
        if not path.exists():
            return
        try:
            action = json.loads(path.read_text(encoding="utf-8")).get("action")
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
            return
        path.unlink(missing_ok=True)

        if action == "pause":
            self._paused = True
        elif action == "resume":
            self._paused = False
        elif action == "flatten":
            for symbol in list(self.portfolio.positions):
                data = self.store.read_ohlcv(symbol, self.config.timeframes.trigger)
                price = float(data["close"].iloc[-1]) if not data.empty else 0.0
                self._close(symbol, price, now_ms(), ExitReason.MANUAL_FLATTEN)
        else:
            return

        log.info("dashboard_command", action=action)
        self.db.record_event("dashboard_command", f"action={action}")

    def _notify_risk_change(self) -> None:
        """Tell the operator when the exposure tier moves.

        Config advertises ``notify_risk_changes``; without this it was silent.
        """
        multiplier = self.risk.risk_multiplier
        if multiplier == self._last_risk_multiplier:
            return
        self._last_risk_multiplier = multiplier
        if self.notifier and self.config.telegram.notify_risk_changes:
            self.notifier.notify_risk_change(
                self.risk.state.drawdown, multiplier, self.risk.threshold_bump()
            )
        self.db.record_event(
            "risk_tier_change",
            f"drawdown {self.risk.state.drawdown:.1%} -> multiplier {multiplier:.2f}x",
            level="warning",
            payload={"multiplier": multiplier, "drawdown": self.risk.state.drawdown},
        )

    def _maybe_daily_summary(self, bar_ts: int) -> None:
        """Send one summary per day, at the configured hour."""
        if self.notifier is None:
            return
        day = bar_ts // 86_400_000
        hour = (bar_ts % 86_400_000) // 3_600_000
        if day == self._last_summary_day or hour != self.config.telegram.daily_summary_hour_utc:
            return
        self._last_summary_day = day

        summary = self.portfolio.summary()
        self.notifier.daily_summary(
            {
                "equity": self.state.equity,
                # Invariant 33: against what the account was actually worth when
                # this run began, never a config number. In live mode
                # starting_balance is whatever the yaml happens to say, which is
                # how the dashboard once reported a 1014 USDT account at +914%.
                "total_return": (self.state.equity / self.state.starting_equity - 1.0)
                if self.state.starting_equity
                else 0.0,
                "drawdown": self.risk.state.drawdown,
                "risk_multiplier": self.risk.risk_multiplier,
                "trades": summary["trades"],
                "expectancy_r": summary["expectancy_r"],
                "open_positions": len(self.portfolio.positions),
                "scanned": len(self.symbols),
            }
        )

        writer = self.roles.get("report_writer")
        if writer is not None:
            # Numbers are computed here and passed in; the model only narrates.
            result = writer.write(
                {
                    "return_pct": round(
                        (self.state.equity / self.config.starting_balance - 1.0) * 100, 2
                    ),
                    "drawdown_pct": round(self.risk.state.drawdown * 100, 2),
                    "trades": summary["trades"],
                    "expectancy_r": round(summary["expectancy_r"], 3),
                    "win_rate": round(summary["win_rate"], 3),
                }
            )
            if result.ok:
                self.notifier.send(
                    f"📝 <b>{result.data.get('headline', '')}</b>\n{result.data.get('body', '')}"
                )

        post_mortem = self.roles.get("post_mortem")
        if post_mortem is not None and summary["trades"] >= 10:
            self._run_post_mortem()

    def _run_post_mortem(self) -> None:
        """Ask for hypotheses about recent trades. Written to research/, never applied."""
        role = self.roles.get("post_mortem")
        if role is None:
            return

        recent = self.db.recent_trades(60)
        if len(recent) < 10:
            return

        # R units and counts only — no currency, no balances (see llm/redact.py).
        payload = {
            "trades": [
                {
                    "setup": t["setup"],
                    "r": round(t["r_multiple"], 3),
                    "mfe_r": round(t["mfe_r"], 3),
                    "mae_r": round(t["mae_r"], 3),
                    "bars": t["bars_held"],
                    "exit": t["exit_reason"],
                    "regime": t["regime_at_entry"],
                    "conviction": t["conviction_at_entry"],
                }
                for t in recent
            ]
        }
        result = role.analyse(payload)
        if not result.ok:
            return

        path = ROOT_DIR / "research" / "hypotheses.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"\n## Otomatik analiz — {iso(now_ms())}\n"]
        for observation in result.data.get("observations", [])[:5]:
            lines.append(f"- {observation}")
        for hypothesis in result.data.get("hypotheses", [])[:5]:
            lines.append(
                f"- **{hypothesis.get('claim', '')}** — test: {hypothesis.get('test', '')} "
                f"(güven: {hypothesis.get('confidence', '?')})"
            )
        lines.append("\n_Otomatik üretildi. Backtest + CPCV'den geçmeden koda girmez._\n")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        log.info("post_mortem_written", hypotheses=len(result.data.get("hypotheses", [])))

    def _manage_positions(self, frames: dict, bar_ts: int) -> None:
        for symbol in list(self.portfolio.positions):
            data = frames.get(symbol)
            if data is None:
                # An open position the bot cannot see is the worst of both
                # worlds: exposed, and unmanaged. Say so loudly every bar.
                log.error(
                    "position_without_data",
                    symbol=symbol,
                    consequence="stop is not trailing and exits cannot fire",
                )
                self.db.record_event(
                    "position_without_data",
                    f"{symbol}: no market data; position unmanaged this bar",
                    level="error",
                )
                self.risk.degrade(f"no data for open position {symbol}")
                continue

            trigger = data["trigger"]
            trail_frame = data[
                {"trigger": "trigger", "setup": "setup", "permission": "permission"}[
                    self.config.exits.trail_timeframe
                ]
            ]
            row = trigger.iloc[-1]
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])

            self.portfolio.update_bar(symbol, high, low)
            position = self.portfolio.positions[symbol]

            atr_series = ta.atr(
                trail_frame["high"],
                trail_frame["low"],
                trail_frame["close"],
                self.config.exits.atr_period,
            )
            atr_now = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
            entry_atr = self._entry_atr_for(position, atr_now)
            regime = classify_series(
                data["permission"], timeframe=self.config.timeframes.permission
            ).iloc[-1]

            decision = self.exits.evaluate(
                position=position,
                high=high,
                low=low,
                close=close,
                atr=atr_now,
                entry_atr=entry_atr,
                regime=Regime(regime),
                conviction_now=self._conviction_for_open(symbol, data, Regime(regime), bar_ts),
                collapse_threshold=self.config.conviction.collapse_threshold,
            )

            if decision.should_exit:
                self._close(symbol, decision.exit_price or close, bar_ts, decision.reason)
                continue

            # Only reached when no exit fired, so the stop has already had its
            # say on this bar (invariant D40).
            self._maybe_scale_out(position, high, low, bar_ts)

            if decision.new_stop is not None:
                position.current_stop = decision.new_stop
                self.db.upsert_position(position)
                self._sync_protective_stop(position)
            elif not position.meta.get("stop_order_id"):
                # No venue stop on this position: either the venue refused it at
                # entry, or the process restarted before one was placed. Retry
                # every bar — a single refusal used to leave the position
                # unprotected for its entire life.
                self._place_protective_stop(position, bar_ts)

    # -- between bars ------------------------------------------------------

    def _check_intrabar_exits(self) -> None:
        """Act on a level reached since the last close, without waiting for it.

        This is the answer to "a coin ran 20% and the bar had not closed yet".
        It reads only the feed's running high and low — the same two numbers the
        backtester reads off a finished bar — and hands them to the same
        ``ExitManager``. Nothing here computes an indicator, so the forming bar
        never influences a decision and the backtest can still reproduce this
        path exactly (invariant 1).

        Silent no-op whenever the feed is absent or stale. Reaction speed is an
        improvement; the protective stop at the venue is the protection.
        """
        if self.feed is None or not self.portfolio.positions or self._paused:
            return

        timeframe = self.config.timeframes.trigger
        # The exit belongs to the bar now forming, not the one that closed. A
        # distinct bar_ts keeps its client order id from colliding with an exit
        # the bar-close path might build for the same position.
        forming_bar = last_closed_bar(timeframe) + tf_ms(timeframe)

        for symbol in list(self.portfolio.positions):
            quote = self.feed.quote(symbol)
            if quote is None:
                continue
            position = self.portfolio.positions.get(symbol)
            if position is None:
                continue

            decision = self.exits.evaluate_intrabar(position, quote.high, quote.low)
            if not decision.should_exit:
                # The stop did not fire, so a scale-out level reached since the
                # last close can be acted on now rather than at the next one.
                self._maybe_scale_out(position, quote.high, quote.low, forming_bar)
                continue

            log.info(
                "intrabar_exit",
                symbol=symbol,
                reason=decision.reason.value if decision.reason else "unknown",
                detail=decision.detail,
                last=quote.last,
                note="acted between bar closes; the bar-close path would have "
                "reached the same decision up to one bar later",
            )
            self._close(symbol, decision.exit_price or quote.last, forming_bar, decision.reason)
            self.state.open_positions = self._positions_view()
            self.writer.write(self.state)

    def _detect_venue_exits(self) -> None:
        """Notice that the venue's own stop has fired, without waiting for the close.

        The protective stop lives at the exchange precisely so it works when the
        engine does not. The cost was that the engine did not find out: until
        the next close it carried a position that no longer existed, reported an
        equity that was wrong, held a slot against nothing, and would send a
        second sell for inventory already gone.

        Two readings are required before acting. A balance fetched seconds after
        an entry can still lag, and inventing an exit is worse than noticing one
        late.
        """
        if not isinstance(self.broker, CCXTBroker) or not self.portfolio.positions:
            return

        held = self.watchdog.call("fetch_positions", self.broker.fetch_positions)
        if held is None:
            return  # a failed read is not evidence of anything

        for symbol in list(self.portfolio.positions):
            position = self.portfolio.positions.get(symbol)
            if position is None:
                continue

            remaining = float(held.get(symbol, 0.0))
            if remaining >= position.quantity * 0.95:
                self._missing_inventory.pop(symbol, None)
                continue

            seen = self._missing_inventory.get(symbol, 0) + 1
            self._missing_inventory[symbol] = seen
            if seen < 2:
                continue

            order_id = position.meta.get("stop_order_id")
            state = (
                self.broker.order_state(symbol, str(order_id))
                if order_id and isinstance(self.broker, CCXTBroker)
                else None
            )

            if state and state.get("filled_terminal") and float(state.get("filled") or 0) > 0:
                fill_price = float(state.get("average_price") or 0.0) or position.current_stop
                log.info(
                    "venue_stop_filled",
                    symbol=symbol,
                    price=fill_price,
                    quantity=state.get("filled"),
                    note="detected between bars; the position is closed locally "
                    "at the price the venue actually got",
                )
                self._close_from_venue_fill(symbol, fill_price)
                self._missing_inventory.pop(symbol, None)
                continue

            # Inventory is gone but no stop of ours explains it. That is a
            # disagreement with the venue, not a decision to make here: say so
            # once and let the next bar's reconciliation settle it.
            if seen == 2:
                log.error(
                    "position_inventory_missing",
                    symbol=symbol,
                    expected=position.quantity,
                    held=remaining,
                    stop_order=str(order_id or "none"),
                    stop_status=str((state or {}).get("status") or "unknown"),
                    consequence="the engine and the venue disagree about what is held",
                )
                self.db.record_event(
                    "position_inventory_missing",
                    f"{symbol}: engine holds {position.quantity:g}, venue shows {remaining:g}",
                    level="error",
                )
                if self.notifier:
                    self.notifier.notify_error(
                        "Position missing at venue",
                        f"{symbol}: the bot believes it holds {position.quantity:g} but the "
                        f"venue shows {remaining:g}, and no stop of ours accounts for it.",
                    )

    def _close_from_venue_fill(self, symbol: str, price: float) -> None:
        """Record an exit the venue performed on its own.

        No order is sent: the sale already happened. Sending one would try to
        sell inventory that is gone, which is the exact failure this detection
        exists to prevent.
        """
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return

        spec = self.broker.market_spec(symbol)
        fee = price * position.quantity * spec.taker_fee
        reason = (
            ExitReason.INITIAL_STOP
            if position.current_stop <= position.initial_stop
            else ExitReason.TRAILING_STOP
        )
        trade = self.portfolio.close_position(symbol, price, now_ms(), reason, fee)
        if trade is None:
            return

        self.db.record_trade(trade)
        self.db.delete_position(symbol)
        self.risk.record_trade(trade)
        self.state.open_positions = self._positions_view()
        self.writer.write(self.state)

        log.info(
            "position_closed",
            symbol=symbol,
            r=round(trade.r_multiple, 3),
            pnl=round(trade.pnl, 4),
            reason=trade.exit_reason.value,
            source="venue stop",
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.notify_exit(trade)

    def _maybe_scale_out(self, position, high: float, low: float, bar_ts: int) -> None:
        """Take part of a position off at its level, and re-protect the rest.

        Three things have to happen together or the position ends up worse off
        than before:

        1. sell the slice at the venue,
        2. reduce the tracked quantity by what actually filled,
        3. **resize the protective stop.**

        Step 3 is not housekeeping. The resting stop still covers the original
        size, so when it triggers it asks to sell inventory that has already
        been sold, is refused for insufficient balance, and leaves the
        remainder unprotected — a partial profit bought with the whole
        position's safety. ``place_stop`` now refuses to adopt an oversized
        stop, so re-placing here genuinely replaces it.

        Order matters too: sell first, then re-place. Cancelling the stop
        before the sale would open a gap with no protection at all, which is
        the lesson from audit #27.
        """
        plan = self.exits.scale_out(position, high, low)
        if plan is None:
            return

        original = float(position.meta.get("original_quantity", position.quantity))
        wanted = min(original * plan.fraction, position.quantity)
        if wanted <= 0:
            return

        spec = self.broker.market_spec(position.symbol)
        remainder = position.quantity - wanted
        if wanted < spec.min_amount or (remainder > 0 and remainder < spec.min_amount):
            # Either slice would be unsellable. Taking a partial profit that
            # strands an unsellable remnant is worse than not taking it.
            log.info(
                "scale_out_skipped",
                symbol=position.symbol,
                wanted=wanted,
                remainder=remainder,
                min_amount=spec.min_amount,
                reason="one of the two pieces would be below the venue minimum",
            )
            position.meta["scaled_out"] = True  # do not retry every bar
            self.db.upsert_position(position)
            return

        order = self.broker.build_order(
            strategy=position.setup,
            symbol=position.symbol,
            bar_ts=bar_ts,
            action="scaleout",
            side=Side.SELL,
            quantity=wanted,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )

        if isinstance(self.broker, PaperBroker):
            fill = self.broker.force_fill_at(order, plan.price, bar_ts)
        else:
            submitted = self.watchdog.call("submit_scale_out", lambda: self.broker.submit(order))
            if submitted is None or submitted.status.value in ("rejected", "expired"):
                log.warning(
                    "scale_out_rejected",
                    symbol=position.symbol,
                    reason=getattr(submitted, "reject_reason", "no response"),
                )
                return
            filled = submitted.filled_quantity or wanted
            fill = Fill(
                order_client_id=submitted.client_id,
                symbol=position.symbol,
                side=Side.SELL,
                quantity=filled,
                price=submitted.average_price or plan.price,
                fee=submitted.fee_paid,
                ts=bar_ts,
            )
            self.db.upsert_order(submitted)

        self.db.record_fill(fill)
        sold = self.portfolio.scale_out(
            position.symbol, fill.price, fill.quantity / max(original, 1e-12), fill.fee
        )
        if not sold:
            return
        self.db.upsert_position(position)

        # Re-protect what is left, at the same level, at the new size.
        if isinstance(self.broker, CCXTBroker):
            position.meta.pop("stop_order_id", None)
            self._place_protective_stop(position, bar_ts)

        log.info(
            "scaled_out",
            symbol=position.symbol,
            sold=sold,
            price=fill.price,
            remaining=position.quantity,
            detail=plan.detail,
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.send(
                f"💰 <b>PARTIAL {position.symbol}</b>\n"
                f"{sold:.6f} @ {fill.price:.6f} ({plan.detail})\n"
                f"kalan {position.quantity:.6f}, stop {position.current_stop:.6f}"
            )

    def _entry_atr_for(self, position, atr_now: float) -> float:
        """The ATR this position was opened against, backfilled if it is missing.

        A position can reach the portfolio without one: reconciliation adopts
        holdings the engine did not open, and rows written before ``meta`` was
        persisted (audit #37) came back with only their stop fields.

        Missing, it used to fall back to ``atr_now`` **silently**, which quietly
        killed two exit layers at once:

        * the volatility-shock check becomes ``atr_now / atr_now == 1.0`` and can
          never reach its ratio, so that layer simply never fires;
        * ``ExitManager.target_price`` returns None, so a configured profit
          target would not exist for this position either.

        Both failures are invisible — the position looks managed and is missing
        exits. Backfilling from the current ATR is an approximation and said to
        be one; it is the difference between an approximate exit and no exit.
        """
        stored = float(position.meta.get("entry_atr", 0.0) or 0.0)
        if stored > 0:
            return stored
        if atr_now <= 0:
            return 0.0
        position.meta["entry_atr"] = atr_now
        self.db.upsert_position(position)
        log.warning(
            "entry_atr_backfilled",
            symbol=position.symbol,
            value=atr_now,
            reason="position carried no entry ATR (adopted, or written before "
            "meta was persisted)",
            consequence="volatility-shock and take-profit exits were inert for it "
            "until now; the backfilled value is today's ATR, not the one at entry",
        )
        self.db.record_event(
            "entry_atr_backfilled",
            f"{position.symbol}: entry_atr missing, backfilled with {atr_now:.6f}",
            level="warning",
        )
        return atr_now

    def _conviction_for_open(
        self, symbol: str, data: dict, regime: Regime, bar_ts: int
    ) -> float | None:
        """Re-score an open position's thesis, or None when it cannot be judged.

        This feeds the conviction-collapse exit: the entry reasons are gone even
        though price has not yet moved against us. Without it the position holds
        on to a dead thesis until the stop, which is the expensive way to find
        out.

        Two things make this subtle.

        **Invariant 10 applies.** A score that could not be computed is "no
        opinion", never zero — and zero sits below every collapse threshold, so
        returning it would exit every position on its first bar.

        **Portfolio-state blockers are deliberately neutralised.** ``already_open``,
        ``portfolio_full`` and ``correlation_conflict`` answer "should I open
        this?"; the question here is "is the reason I opened it still true?".
        Leaving them set would zero the score for the tautological reason that
        the position exists.
        """
        try:
            enabled = self.config.indicators.enabled or None
            trigger, setup, permission = data["trigger"], data["setup"], data["permission"]
            ctx = ScoringContext(
                symbol=symbol,
                bar_ts=bar_ts,
                regime=regime,
                trigger_signals=_last_row(compute_signals(trigger, enabled)),
                setup_signals=_last_row(compute_signals(setup, enabled)),
                permission_signals=_last_row(compute_signals(permission, enabled)),
                price=float(trigger["close"].iloc[-1]),
                atr=float(
                    ta.atr(
                        setup["high"], setup["low"], setup["close"], self.config.exits.atr_period
                    ).iloc[-1]
                ),
                already_open=False,
                portfolio_full=False,
                correlation_conflict=False,
                llm_veto=self._llm_veto,
            )
            held = self.portfolio.positions.get(symbol)
            if held is None:
                return None
            scored = self.conviction.score(ctx, held.side)
        except Exception as exc:  # noqa: BLE001 — an exit check must never crash the bar
            log.warning("conviction_rescore_failed", symbol=symbol, error=str(exc)[:160])
            return None

        # Only a *blocked* verdict is "no opinion". ``below_threshold`` is a
        # real score that did not clear the entry bar — and a thesis that has
        # collapsed is below the entry bar by definition, so discarding those
        # was discarding the entire point of this method.
        if scored.no_trade_reason in UNSCORED_REASONS:
            return None
        return scored.total

    def _refresh_microstructure(self, symbols: list[str]) -> None:
        """Read live spread and 24h turnover for the whole universe, once.

        This closes a gap that had been open since the beginning: ``ScanRow``
        has carried ``spread_bps`` and ``volume_24h`` fields all along, the
        conviction engine has had penalty branches keyed on them, and
        ``NoTradeReason.SPREAD_TOO_WIDE`` has existed as an enum member — but
        nothing ever supplied a number, so every one of those was dead. A
        setting that decides nothing while looking like it decides something is
        worse than no setting.

        One request covers every spot symbol, so the cost does not grow with the
        universe. Collection is separate from *acting* on it — see
        ``execution.microstructure_penalties``.
        """
        if not isinstance(self.broker, CCXTBroker):
            return
        if not self.config.execution.microstructure_enabled:
            return
        data = self.watchdog.call(
            "fetch_microstructure", lambda: self.broker.fetch_microstructure(symbols)
        )
        if data:
            self._microstructure = data

    def _scan(self, frames: dict, bar_ts: int) -> list[ScanRow]:
        correlations = self._correlations(frames)
        rows: list[ScanRow] = []
        enabled = self.config.indicators.enabled or None

        drawdown_bump = self.risk.state.threshold_bump
        streak_bump = self.risk.threshold_bump() - drawdown_bump

        penalties_on = self.config.execution.microstructure_penalties

        for symbol, data in frames.items():
            trigger, setup, permission = data["trigger"], data["setup"], data["permission"]
            micro = self._microstructure.get(symbol, {})
            regime = Regime(
                classify_series(permission, timeframe=self.config.timeframes.permission).iloc[-1]
            )

            ctx = ScoringContext(
                symbol=symbol,
                bar_ts=bar_ts,
                regime=regime,
                trigger_signals=_last_row(compute_signals(trigger, enabled)),
                setup_signals=_last_row(compute_signals(setup, enabled)),
                permission_signals=_last_row(compute_signals(permission, enabled)),
                price=float(trigger["close"].iloc[-1]),
                atr=float(
                    ta.atr(
                        setup["high"], setup["low"], setup["close"], self.config.exits.atr_period
                    ).iloc[-1]
                ),
                # Penalties stay off until the collection phase has been
                # looked at. Switching them on changes which trades happen, so
                # it is a decision with evidence behind it, not a side effect of
                # wiring up the data. Measured on the live universe: spreads run
                # 0.01-2.7bps against a 10bps penalty floor, so today this would
                # change nothing — which is itself the finding.
                spread_bps=micro.get("spread_bps", 0.0) if penalties_on else 0.0,
                volume_24h=micro.get("turnover_24h", 0.0) if penalties_on else 0.0,
                already_open=self.portfolio.has_position(symbol),
                portfolio_full=len(self.portfolio.positions) >= self.config.risk.max_positions,
                correlation_conflict=correlation_conflict(
                    symbol,
                    self.portfolio.open_symbols,
                    correlations,
                    self.config.risk.max_correlation,
                ),
                llm_veto=self._llm_veto,
            )

            conviction = self.conviction.score(ctx, Side.BUY, drawdown_bump, streak_bump)
            self.db.record_conviction(conviction)

            reason = conviction.no_trade_reason
            if reason is None and not detect_all(
                setup, regime, self.config.exits.initial_stop_atr_mult, DEFAULT_SETUPS
            ):
                reason = NoTradeReason.NO_SETUP

            rows.append(
                ScanRow(
                    symbol=symbol,
                    bar_ts=bar_ts,
                    regime=regime,
                    best_side=Side.BUY,
                    score=conviction.total,
                    threshold=conviction.threshold,
                    no_trade_reason=reason,
                    # Always recorded, whether or not it is allowed to decide
                    # anything. Measuring first and acting second is the only
                    # way to know later whether acting would have helped.
                    spread_bps=micro.get("spread_bps", 0.0),
                    volume_24h=micro.get("turnover_24h", 0.0),
                )
            )

        return rows

    def _maybe_enter(self, frames: dict, scans: list[ScanRow], bar_ts: int) -> None:
        """Try candidates best-first, and leave a reason on every one of them.

        Every row here ends the bar with a ``no_trade_reason`` set. A row left
        at ``None`` reads as "eligible" in the scan table, which is a lie about
        a symbol that was considered and passed over — and that table exists
        precisely so "why did it not trade?" is answered by query, not guess.
        """
        candidates = sorted((r for r in scans if r.no_trade_reason is None), key=lambda r: -r.score)
        for index, row in enumerate(candidates):
            if len(self.portfolio.positions) >= self.config.risk.max_positions:
                for remaining in candidates[index:]:
                    remaining.no_trade_reason = NoTradeReason.MAX_POSITIONS
                return
            if self._enter(frames[row.symbol], row, bar_ts):
                row.no_trade_reason = NoTradeReason.ENTERED
                # One entry per bar keeps concentration under control. The ones
                # not reached were not rejected — say so rather than implying it.
                for remaining in candidates[index + 1 :]:
                    remaining.no_trade_reason = NoTradeReason.COOLDOWN_SAME_BAR
                return

    def _enter(self, data: dict, row: ScanRow, bar_ts: int) -> bool:
        setup_frame = data["setup"]
        setups = detect_all(
            setup_frame, row.regime, self.config.exits.initial_stop_atr_mult, DEFAULT_SETUPS
        )
        if not setups:
            # Scored well enough, but no entry trigger fired on this bar.
            row.no_trade_reason = NoTradeReason.NO_SETUP
            return False

        candidate = setups[0]
        price = float(data["trigger"]["close"].iloc[-1])
        stop = price - candidate.stop_distance
        if stop >= price:
            row.no_trade_reason = NoTradeReason.NO_SETUP
            return False

        spec = self.broker.market_spec(row.symbol)
        returns = (
            data["trigger"]["close"]
            .pct_change()
            .tail(self.config.risk.vol_lookback_bars)
            .dropna()
            .to_numpy()
        )
        gate = LeverageGate(self.config.promotion_path)

        sizing = size_position(
            equity=self.risk.state.equity,
            risk_amount=self.risk.risk_amount(self.risk.state.equity),
            entry_price=price,
            stop_price=stop,
            spec=spec,
            config=self.config.risk,
            recent_returns=returns,
            bars_per_year=BARS_PER_YEAR.get(self.config.timeframes.trigger, 8760),
            available_cash=self.portfolio.cash,
            leverage=gate.allowed_leverage(self.config.risk.max_leverage),
            min_notional_buffer=self.config.execution.min_notional_buffer,
        )
        if not sizing.ok:
            row.no_trade_reason = sizing.rejected or NoTradeReason.MIN_NOTIONAL
            log.info("entry_skipped", symbol=row.symbol, reason=sizing.detail)
            return False

        order = self.broker.build_order(
            strategy=candidate.name,
            symbol=row.symbol,
            bar_ts=bar_ts,
            action="entry",
            side=Side.BUY,
            quantity=sizing.quantity,
            order_type=OrderType.MARKET,
        )
        if self.db.order_exists(order.client_id):
            # A crash between sending and recording would otherwise double the
            # position on restart.
            log.info("duplicate_order_suppressed", client_id=order.client_id)
            row.no_trade_reason = NoTradeReason.COOLDOWN_SAME_BAR
            return False

        order.meta = {
            "stop": stop,
            "setup": candidate.name,
            "conviction": row.score,
            "regime": row.regime.value,
            "risk_amount": sizing.risk_amount,
        }
        order.meta["planned_entry"] = price
        submitted = self.watchdog.call("submit_order", lambda: self.broker.submit(order))
        if submitted is None or submitted.status.value in ("rejected", "expired"):
            self._record_rejection(row.symbol, submitted)
            row.no_trade_reason = NoTradeReason.INSUFFICIENT_BALANCE
            return False
        self._rejections.pop(row.symbol, None)
        self.db.upsert_order(submitted)

        if isinstance(self.broker, PaperBroker):
            # Queued; it executes against the next bar's open in
            # _drain_paper_fills. Opening the position here would skip slippage
            # and fees and break parity with the backtest.
            log.info(
                "entry_queued",
                symbol=row.symbol,
                conviction=row.score,
                setup=candidate.name,
            )
            return True

        fill_price = submitted.average_price or price
        filled = submitted.filled_quantity or sizing.quantity

        # A market order on a thin book can come back partially filled. Record
        # what actually filled, not what was asked for — carrying the requested
        # size would make the bot believe it holds more than it does, and every
        # exit would then try to sell inventory that is not there.
        if submitted.status is OrderStatus.PARTIALLY_FILLED or (
            filled > 0 and filled < sizing.quantity * 0.999
        ):
            log.warning(
                "partial_fill",
                symbol=row.symbol,
                requested=sizing.quantity,
                filled=filled,
                action="position opened at the filled size; remainder cancelled",
            )
            self.watchdog.call("cancel_remainder", lambda: self.broker.cancel(submitted.client_id))
            if filled <= 0:
                row.no_trade_reason = NoTradeReason.MIN_NOTIONAL
                return False

        fill = Fill(
            order_client_id=submitted.client_id,
            symbol=row.symbol,
            side=Side.BUY,
            quantity=filled,
            price=fill_price,
            fee=submitted.fee_paid,
            ts=bar_ts,
        )
        self.db.record_fill(fill)

        drift = self.slippage.record(price, fill_price, side_sign=1)
        if self.slippage.exceeds_assumption():
            log.warning(
                "slippage_worse_than_assumed",
                median_bps=round(self.slippage.median_bps, 2),
                assumed_bps=self.config.execution.slippage_bps,
                advice="the backtest that justified going live was optimistic",
            )
        del drift

        # Preserve the stop *distance*: the fill happened at a different price
        # than the one the stop was computed against, and the risk the position
        # carries must stay the risk that was sized.
        actual_stop = fill_price - (price - stop)
        position = self.portfolio.open_position(
            fill=fill,
            stop_price=actual_stop,
            setup=candidate.name,
            risk_amount=sizing.risk_amount,
            conviction=row.score,
            regime=row.regime,
        )
        position.meta["entry_atr"] = self._atr_for(data, self.config.exits.trail_timeframe)
        self.db.upsert_position(position)

        # A stop that exists only in this process protects nothing while the
        # process is down, and crypto trades around the clock.
        self._place_protective_stop(position, bar_ts)

        log.info(
            "position_opened",
            symbol=row.symbol,
            price=fill_price,
            quantity=fill.quantity,
            stop=actual_stop,
            conviction=row.score,
            setup=candidate.name,
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.notify_entry(
                row.symbol,
                "buy",
                fill_price,
                fill.quantity,
                actual_stop,
                row.score,
                candidate.name,
            )
        return True

    # -- armed entries ------------------------------------------------------

    def _maybe_arm(self, frames: dict, scans: list[ScanRow], bar_ts: int) -> None:
        """Leave one resting buy order at a breakout level the strategy already likes.

        This is what lets the bot act on a move that happens *between* closes.
        The level comes from closed bars only; the venue does the watching.

        **Exactly one at a time, deliberately.** The measured edge is
        cross-sectional: every bar ranks the symbols against each other and the
        highest takes the slot. Arming several would hand that choice to the
        market — whichever level price happens to touch first, not whichever
        symbol scored best — quietly replacing the selection mechanism that was
        actually tested with a race.
        """
        if not self.config.entries.armed_breakout:
            return
        if not isinstance(self.broker, CCXTBroker) or self._armed is not None:
            return
        # An armed order is a position the bot has already committed to, so it
        # occupies a slot. Otherwise a fill could push the book past its cap.
        if len(self.portfolio.positions) >= self.config.risk.max_positions:
            return

        # Only symbols that cleared the score but had no trigger this bar: those
        # are the ones whose breakout has not happened *yet*.
        waiting = sorted(
            (r for r in scans if r.no_trade_reason is NoTradeReason.NO_SETUP),
            key=lambda r: -r.score,
        )
        for row in waiting:
            if row.score < row.threshold or self.portfolio.has_position(row.symbol):
                continue
            data = frames.get(row.symbol)
            if data is None:
                continue
            candidate = self._armable_candidate(data, row)
            if candidate is None:
                continue
            if self._arm_entry(data, row, candidate, bar_ts):
                return

    def _armable_candidate(self, data: dict, row: ScanRow):
        """The breakout level for this symbol, if one is close enough to matter."""
        for setup in DEFAULT_SETUPS:
            arm = getattr(setup, "arm", None)
            if arm is None:
                continue
            candidate = arm(
                data["setup"],
                row.regime,
                self.config.exits.initial_stop_atr_mult,
                self.config.entries.armed_max_distance_atr,
            )
            if candidate is not None and candidate.stop_distance > 0:
                return candidate
        return None

    def _arm_entry(self, data: dict, row: ScanRow, candidate, bar_ts: int) -> bool:
        """Size the trade against the trigger level and rest the order."""
        spec = self.broker.market_spec(row.symbol)
        returns = (
            data["trigger"]["close"]
            .pct_change()
            .tail(self.config.risk.vol_lookback_bars)
            .dropna()
            .to_numpy()
        )
        gate = LeverageGate(self.config.promotion_path)

        # Sized against the *trigger* price, not the close. The fill happens at
        # the level, so that is the price the risk has to be measured from.
        sizing = size_position(
            equity=self.risk.state.equity,
            risk_amount=self.risk.risk_amount(self.risk.state.equity),
            entry_price=candidate.entry_price,
            stop_price=candidate.stop_price,
            spec=spec,
            config=self.config.risk,
            recent_returns=returns,
            bars_per_year=BARS_PER_YEAR.get(self.config.timeframes.trigger, 8760),
            available_cash=self.portfolio.cash,
            leverage=gate.allowed_leverage(self.config.risk.max_leverage),
            min_notional_buffer=self.config.execution.min_notional_buffer,
        )
        if not sizing.ok:
            return False

        client_id = client_order_id(candidate.name, row.symbol, bar_ts, "arm")
        order_id = self.watchdog.call(
            "place_stop_entry",
            lambda: self.broker.place_stop_entry(
                row.symbol, sizing.quantity, candidate.entry_price, client_id
            ),
        )
        if not order_id:
            return False

        self._armed = {
            "symbol": row.symbol,
            "order_id": order_id,
            "client_id": client_id,
            "trigger": candidate.entry_price,
            "stop": candidate.stop_price,
            "quantity": sizing.quantity,
            "setup": candidate.name,
            "conviction": row.score,
            "regime": row.regime.value,
            "risk_amount": sizing.risk_amount,
            "bar_ts": bar_ts,
            "entry_atr": candidate.atr,
        }
        row.no_trade_reason = NoTradeReason.COOLDOWN_SAME_BAR
        log.info(
            "entry_armed",
            symbol=row.symbol,
            trigger=candidate.entry_price,
            stop=candidate.stop_price,
            quantity=sizing.quantity,
            detail=candidate.trigger_detail,
        )
        return True

    def _resolve_armed(self, bar_ts: int) -> None:
        """Settle last bar's armed order: adopt a fill, otherwise withdraw it.

        **An armed order lives exactly one bar** (invariant D41). The level was
        computed from a channel that has since moved; leaving the order there
        means filling, three bars later, at a price the strategy no longer has
        an opinion about. So it is cancelled unless it filled — and the cancel
        is *verified*, because a cancel that silently failed leaves a live order
        the engine has stopped tracking, which is strictly worse than never
        having cancelled.
        """
        armed = self._armed
        if armed is None or not isinstance(self.broker, CCXTBroker):
            return

        state = self.broker.order_state(armed["symbol"], armed["order_id"])
        if state and state.get("filled_terminal") and float(state.get("filled") or 0) > 0:
            self._open_from_armed_fill(armed, state, bar_ts)
            self._armed = None
            return

        if state and not state.get("terminal"):
            if not self.broker.cancel_stop_entry(armed["symbol"], armed["order_id"]):
                # Still live and we failed to withdraw it. Keep tracking it
                # rather than forgetting it: next bar tries again, and until
                # then the slot stays reserved.
                log.error(
                    "armed_entry_still_live",
                    symbol=armed["symbol"],
                    order_id=armed["order_id"],
                    consequence="an order the engine no longer wants is resting at the venue",
                )
                return
            log.info("armed_entry_expired", symbol=armed["symbol"], trigger=armed["trigger"])

        self._armed = None

    def _detect_armed_fill(self) -> None:
        """Notice an armed order filling between closes, not an hour later.

        Same blind spot as the protective stop, same fix: the venue acted, so
        the engine has to ask. A position that exists at the venue but not in
        the portfolio is unmanaged — no trailing stop, no exit check, and no
        protective stop at the venue either.
        """
        armed = self._armed
        if armed is None or not isinstance(self.broker, CCXTBroker):
            return
        state = self.broker.order_state(armed["symbol"], armed["order_id"])
        if state and state.get("filled_terminal") and float(state.get("filled") or 0) > 0:
            self._open_from_armed_fill(armed, state, armed["bar_ts"])
            self._armed = None
            self.state.open_positions = self._positions_view()
            self.writer.write(self.state)

    def _open_from_armed_fill(self, armed: dict, state: dict, bar_ts: int) -> None:
        """Record the position an armed order opened at the venue."""
        symbol = armed["symbol"]
        if self.portfolio.has_position(symbol):
            return

        filled = float(state.get("filled") or 0.0)
        price = float(state.get("average_price") or 0.0) or float(armed["trigger"])
        spec = self.broker.market_spec(symbol)
        fee = price * filled * spec.taker_fee

        fill = Fill(
            order_client_id=str(armed["client_id"]),
            symbol=symbol,
            side=Side.BUY,
            quantity=filled,
            price=price,
            fee=fee,
            ts=now_ms(),
        )
        self.db.record_fill(fill)

        # Preserve the stop *distance* the trade was sized with: the fill landed
        # somewhere other than the trigger, and the risk carried must stay the
        # risk that was budgeted.
        actual_stop = price - (float(armed["trigger"]) - float(armed["stop"]))
        position = self.portfolio.open_position(
            fill=fill,
            stop_price=actual_stop,
            setup=str(armed["setup"]),
            risk_amount=float(armed["risk_amount"]),
            conviction=float(armed["conviction"]),
            regime=Regime(armed["regime"]),
        )
        position.meta["entry_atr"] = float(armed.get("entry_atr") or 0.0)
        self.db.upsert_position(position)
        self._place_protective_stop(position, bar_ts)

        log.info(
            "position_opened",
            symbol=symbol,
            price=price,
            quantity=filled,
            stop=actual_stop,
            conviction=armed["conviction"],
            setup=armed["setup"],
            source="armed entry",
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.notify_entry(
                symbol,
                "buy",
                price,
                filled,
                actual_stop,
                float(armed["conviction"]),
                str(armed["setup"]),
            )

    def _record_rejection(self, symbol: str, order) -> None:
        """Count venue refusals and stop retrying a symbol that always fails."""
        count = self._rejections.get(symbol, 0) + 1
        self._rejections[symbol] = count
        reason = getattr(order, "reject_reason", None) or "no response"

        log.warning("order_rejected", symbol=symbol, count=count, reason=str(reason)[:160])

        if count >= self._rejection_limit and symbol in self.symbols:
            self.symbols = [s for s in self.symbols if s != symbol]
            log.error(
                "symbol_dropped_from_universe",
                symbol=symbol,
                rejections=count,
                reason=str(reason)[:160],
                note="likely delisted, suspended, or precision changed",
            )
            self.db.record_event(
                "symbol_dropped",
                f"{symbol} dropped after {count} rejections: {str(reason)[:120]}",
                level="error",
            )
            if self.notifier:
                self.notifier.notify_error(
                    "Symbol dropped", f"{symbol} refused {count} times — removed from the scan."
                )

    def _place_protective_stop(self, position, bar_ts: int) -> None:
        """Mirror the in-memory stop as a real order at the venue."""
        if not isinstance(self.broker, CCXTBroker):
            return

        # Unique per attempt: a stop that is replaced cannot reuse the id the
        # venue already refused. Duplicate stops are prevented by place_stop
        # adopting an existing one, not by the id being stable.
        client_id = client_order_id(
            position.setup, position.symbol, bar_ts, f"stop-{int(now_ms() / 1000)}"
        )
        order_id = self.watchdog.call(
            "place_stop",
            lambda: self.broker.place_stop(
                position.symbol, position.quantity, position.current_stop, client_id
            ),
        )
        if order_id:
            position.meta["stop_order_id"] = order_id
            # Remembered so reconciliation recognises the bot's own stop instead
            # of reporting it as an orphan on every start — a warning nothing
            # can clear is a warning that gets ignored.
            position.meta["stop_client_id"] = client_id
            self.db.upsert_position(position)
        else:
            # The position is now protected only while this process is alive.
            self.db.record_event(
                "unprotected_position",
                f"{position.symbol}: venue refused the protective stop",
                level="error",
            )
            if self.notifier:
                self.notifier.notify_error(
                    "Stop not placed at venue",
                    f"{position.symbol} is protected only while the bot is running.",
                )

    def _sync_protective_stop(self, position) -> None:
        """Move the venue stop when the trail ratchets up."""
        if not isinstance(self.broker, CCXTBroker):
            return
        order_id = position.meta.get("stop_order_id")
        if not order_id:
            return
        self.watchdog.call(
            "amend_stop",
            lambda: self.broker.amend_stop(position.symbol, order_id, position.current_stop),
        )

    def _cancel_protective_stop(self, symbol: str) -> None:
        """Remove the venue stop before closing, so it cannot fire afterwards.

        A stale reduce-only stop left behind would sit there until it triggered
        on a position that no longer exists.
        """
        if not isinstance(self.broker, CCXTBroker):
            return
        position = self.portfolio.positions.get(symbol)
        order_id = position.meta.get("stop_order_id") if position else None
        if order_id:
            self.watchdog.call("cancel_stop", lambda: self.broker.cancel_stop(symbol, order_id))

    def _close(self, symbol: str, price: float, bar_ts: int, reason: ExitReason | None) -> None:
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return

        order = self.broker.build_order(
            strategy=position.setup,
            symbol=symbol,
            bar_ts=bar_ts,
            action="exit",
            side=Side.SELL,
            quantity=position.quantity,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )
        if isinstance(self.broker, PaperBroker):
            # A stop is a resting instruction: it executes where it triggers.
            # Deferring it to the next open understates losses on exactly the
            # bars that produce them.
            fill = self.broker.force_fill_at(order, price, bar_ts)
            self.db.record_fill(fill)
            self.db.upsert_order(order)
            price = fill.price
        else:
            self._cancel_protective_stop(symbol)
            submitted = self.watchdog.call("submit_exit", lambda: self.broker.submit(order))
            if submitted is not None:
                self.db.upsert_order(submitted)
                price = submitted.average_price or price

        spec = self.broker.market_spec(symbol)
        fee = price * position.quantity * spec.taker_fee
        trade = self.portfolio.close_position(
            symbol, price, bar_ts, reason or ExitReason.TRAILING_STOP, fee
        )
        if trade is None:
            return

        self.db.record_trade(trade)
        self.db.delete_position(symbol)
        self.risk.record_trade(trade)

        log.info(
            "position_closed",
            symbol=symbol,
            r=round(trade.r_multiple, 3),
            pnl=round(trade.pnl, 4),
            reason=trade.exit_reason.value,
            bars=trade.bars_held,
        )
        if self.notifier and self.config.telegram.notify_fills:
            self.notifier.notify_exit(trade)

    # -- support -----------------------------------------------------------

    def _audit_config(self, config_hash: str) -> None:
        """Sanity-check the configuration when it has changed since last start.

        Advisory only — it warns, it cannot block. Runs on change rather than
        every start so a stable setup does not burn the daily call budget
        re-reviewing itself.
        """
        auditor = self.roles.get("config_auditor")
        previous = self.db.get_meta("config_hash")
        self.db.set_meta("config_hash", config_hash)
        if auditor is None or previous == config_hash:
            return

        payload = {
            "risk_per_trade": self.config.risk.risk_per_trade,
            "max_positions": self.config.risk.max_positions,
            "max_leverage": self.config.risk.max_leverage,
            "initial_stop_atr_mult": self.config.exits.initial_stop_atr_mult,
            "trailing_atr_mult": self.config.exits.trailing_atr_mult,
            "conviction_threshold": self.config.conviction.base_threshold,
            "timeframes": self.config.timeframes.all,
            "mode": self.config.mode.value,
        }
        result = auditor.audit(payload)
        if not result.ok:
            return

        concerns = result.data.get("concerns", [])
        verdict = result.data.get("verdict", "ok")
        if verdict != "ok" and concerns:
            log.warning("config_audit", verdict=verdict, concerns=concerns)
            self.db.record_event(
                "config_audit", f"{verdict}: {'; '.join(concerns[:3])}", level="warning"
            )
            if self.notifier:
                self.notifier.send(
                    f"⚙️ <b>Config audit: {verdict}</b>\n"
                    + "\n".join(f"• {c}" for c in concerns[:4])
                )

    def _reconcile(self) -> None:
        stored = {p["symbol"]: p for p in self.db.load_positions()}
        for symbol, raw in stored.items():
            from tbot.core.models import Position

            self.portfolio.positions[symbol] = Position(
                symbol=symbol,
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
                # Restoring meta is not cosmetic: it carries stop_order_id, so
                # dropping it left the engine unable to move the venue stop it
                # had itself placed. Every restart then lost the trail.
                meta=json.loads(raw["meta"]) if raw.get("meta") else {},
            )

        if self.config.mode is not Mode.LIVE:
            return

        venue_positions = (
            self.watchdog.call("fetch_positions", lambda: self.broker.fetch_positions(), default={})
            or {}
        )
        open_orders = (
            self.watchdog.call(
                "fetch_open_orders", lambda: self.broker.fetch_open_orders(), default=[]
            )
            or []
        )
        known = {o["client_id"] for o in self.db.query("SELECT client_id FROM orders")}
        # Protective stops never reach the orders table; they are ours all the
        # same. Matched by venue order id as well as client id, because a stop
        # placed by an older build recorded only the former — and a warning that
        # nothing can clear is a warning that gets ignored.
        own_stops = {
            str(p.meta.get("stop_order_id") or "") for p in self.portfolio.positions.values()
        }
        known |= {
            p.meta["stop_client_id"]
            for p in self.portfolio.positions.values()
            if p.meta.get("stop_client_id")
        }
        open_orders = [
            o for o in open_orders if not (o.exchange_id and str(o.exchange_id) in own_stops)
        ]

        report = reconcile(self.portfolio.positions, venue_positions, open_orders, known)
        self.portfolio.positions = apply_reconcile(
            report, self.portfolio.positions, venue_positions
        )

        # Dropping it from memory is not enough: the row is reloaded on the next
        # start, dropped again, and warns again — for ever. Worse, it keeps the
        # symbol's stale entry price alive to be inherited by the next position
        # opened there.
        for symbol in report.missing_remotely:
            self.db.delete_position(symbol)

        self.db.record_event(
            "reconcile",
            report.summary(),
            level="info" if report.clean else "warning",
            payload={
                "missing_locally": report.missing_locally,
                "missing_remotely": report.missing_remotely,
            },
        )
        if not report.clean and self.notifier:
            self.notifier.notify_error("Reconciliation mismatch", report.summary())

    def _check_balance_against_last_run(self, balance: float) -> None:
        """Say something when the account is not the size we last left it.

        The implausible-jump guard only compares bar to bar **within** a run, so
        a balance that changed while the engine was down slid in silently as the
        new normal — a 97% drop was adopted without a word. Between runs the
        cause is never a market move: it is a withdrawal, a manual trade, the
        wrong account, or a reset. None of those should be quiet.
        """
        previous = self.writer.read() or {}
        last = float(previous.get("equity") or 0.0)
        if last <= 0 or balance <= 0:
            return

        change = (balance - last) / last
        if abs(change) < 0.25:
            return

        message = (
            f"account was {last:.2f} when the engine last ran and is {balance:.2f} now "
            f"({change:+.1%}) — no market move explains that across a restart"
        )
        log.warning("balance_changed_while_down", previous=last, current=balance, change=change)
        self.db.record_event("balance_changed_while_down", message, level="error")
        if self.notifier:
            self.notifier.notify_error("Account size changed", message)

    def _equity_now(self) -> float:
        """Account equity, read from the venue in live mode.

        A failed read is "unknown", not ``starting_balance``. Substituting the
        configured number for a real balance is how a transient API error turns
        into sizing against money the account does not have — and, on the next
        bar, into an equity series that jumps between two unrelated figures.
        """
        if isinstance(self.broker, PaperBroker):
            return self.broker.equity({})

        balance = self.watchdog.call(
            "fetch_balance", lambda: self.broker.fetch_balance(), default=None
        )
        if balance:
            return float(balance.get(self.config.quote_currency, 0.0))

        # Fall back to the last figure the venue actually gave us. On the very
        # first read there is none, and refusing is the only honest answer.
        if self.state.equity > 0:
            log.warning("balance_read_failed", using_last_known=round(self.state.equity, 2))
            return float(self.state.equity)
        raise RuntimeError(
            "Could not read the account balance from the venue, and there is no "
            "previous reading to fall back on. Refusing to start rather than size "
            "positions against a number from the config file."
        )

    def _correlations(self, frames: dict) -> dict[tuple[str, str], float]:
        series = {
            s: d["trigger"]["close"].pct_change().tail(300).reset_index(drop=True)
            for s, d in frames.items()
        }
        if len(series) < 2:
            return {}
        table = pd.DataFrame(series).dropna()
        if len(table) < 50:
            return {}
        matrix = table.corr()
        out = {}
        names = list(matrix.columns)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                value = matrix.loc[a, b]
                if pd.notna(value):
                    out[(a, b) if a < b else (b, a)] = float(value)
        return out

    def _measure_veto_outcomes(self, frames: dict, bar_ts: int) -> None:
        """Score past vetoes against what the market actually did.

        The event guard is the one role that can affect trading, so it is the
        one that has to earn its place. Every veto is paired with the return the
        blocked entry would have produced, measured in R against the same
        ATR-based stop the trade would have used. ``scripts/llm_impact.py``
        turns those pairs into a keep-or-remove decision.
        """
        horizon_ms = tf_ms(self.config.timeframes.trigger) * self._veto_horizon_bars
        for pending in self.db.pending_counterfactuals(bar_ts - horizon_ms):
            symbol = pending.get("symbol")
            record = self._veto_prices.pop((symbol, pending["id"]), None)
            data = frames.get(symbol)
            if record is None or data is None:
                continue

            entry_price, stop_distance = record
            if stop_distance <= 0:
                continue
            now_price = float(data["trigger"]["close"].iloc[-1])
            self.db.set_counterfactual(pending["id"], (now_price - entry_price) / stop_distance)

    def _check_event_guard(self, frames: dict) -> None:
        """Ask the veto role whether to pause new entries. Advisory, veto-only."""
        guard = self.roles.get("event_guard")
        if guard is None:
            self._llm_veto = False
            return

        context = {
            "symbols": list(frames)[:10],
            "timeframe": self.config.timeframes.trigger,
            "recent_moves": {
                s: round(float(d["trigger"]["close"].pct_change().tail(24).sum()), 4)
                for s, d in list(frames.items())[:10]
            },
        }
        result = guard.check(context)
        self._llm_veto = bool(result.data.get("block_new_entries"))

        if self._llm_veto and guard.last_call_id is not None:
            # Snapshot what the blocked entries would have paid, so the veto can
            # be scored later rather than taken on trust.
            for symbol, data in frames.items():
                atr = self._atr_for(data, self.config.exits.trail_timeframe)
                if atr <= 0:
                    continue
                self._veto_prices[(symbol, guard.last_call_id)] = (
                    float(data["trigger"]["close"].iloc[-1]),
                    atr * self.config.exits.initial_stop_atr_mult,
                )

        # Entries are normally consumed when their outcome is measured, but a
        # disabled role or a missing row would leak them. Cap the dict so a
        # long-running process cannot grow without bound.
        if len(self._veto_prices) > self._veto_prices_max:
            for key in list(self._veto_prices)[: len(self._veto_prices) - self._veto_prices_max]:
                self._veto_prices.pop(key, None)

    def _poll_commands(self) -> None:
        if self.notifier is None:
            return
        for command in self.notifier.poll_commands():
            if command == "pause":
                self._paused = True
                self.notifier.send("⏸ Paused. Open positions are still managed.")
            elif command == "resume":
                self._paused = False
                self.notifier.send("▶️ Resumed.")
            elif command == "flatten":
                for symbol in list(self.portfolio.positions):
                    data = self.store.read_ohlcv(symbol, self.config.timeframes.trigger)
                    price = float(data["close"].iloc[-1]) if not data.empty else 0.0
                    self._close(symbol, price, now_ms(), ExitReason.MANUAL_FLATTEN)
                self.notifier.send("🔻 All positions closed.")
            elif command in ("status", "pnl", "positions"):
                self.notifier.send(f"<pre>{self._status_text()}</pre>")
            elif command == "stop":
                self.notifier.send("🛑 Stopping. Restart with ./ops/bot.sh start <mode>.")
                self._running = False

    def _status_text(self) -> str:
        summary = self.portfolio.summary()
        return (
            f"status      {self.state.status}\n"
            f"equity      {self.state.equity:.2f}\n"
            f"drawdown    {self.risk.state.drawdown:.1%}\n"
            f"risk mult   {self.risk.risk_multiplier:.2f}x\n"
            f"threshold   {self.conviction.current_threshold(self.risk.threshold_bump()):.0f}\n"
            f"positions   {len(self.portfolio.positions)}\n"
            f"trades      {summary['trades']} (exp {summary['expectancy_r']:+.3f}R)\n"
            f"uptime      {self.state.uptime_seconds / 3600:.1f}h\n"
            f"restarts    {self.state.restarts}"
        )

    def _positions_view(self) -> list[dict]:
        """What the dashboard and ``bot.sh status`` read as "open positions"."""
        return [
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "quantity": p.quantity,
                "entry": p.entry_price,
                "stop": p.current_stop,
                "setup": p.setup,
                "bars_held": p.bars_held,
                "mfe_r": round(p.mfe_r, 2),
                "mae_r": round(p.mae_r, 2),
                "conviction": p.conviction_at_entry,
            }
            for p in self.portfolio.positions.values()
        ]

    def _balances_view(self) -> list[dict]:
        """Every currency the account holds, valued in the quote currency.

        The operator's question was "how much of each do I actually have?", and
        the honest answer has three parts, not one. ``free`` is what can be
        spent or sold right now; ``used`` is reserved by a resting order — on
        spot a protective stop locks the whole holding, so a properly protected
        position reads as zero free; ``total`` is what is owned. Showing only
        free would report a protected position as no position at all.

        In paper mode the venue is a simulation, so the same three columns are
        derived from the simulated cash and holdings rather than fetched.
        """
        quote = self.config.quote_currency
        rows: list[dict] = []

        if isinstance(self.broker, CCXTBroker):
            detail = self.watchdog.call("fetch_balances_detail", self.broker.fetch_balances_detail)
            if detail is None:
                return self.state.balances  # keep the last good picture, do not blank it
        else:
            detail = {quote: {"free": self.portfolio.cash, "used": 0.0, "total": self.portfolio.cash}}
            for symbol, position in self.portfolio.positions.items():
                detail[symbol.split("/")[0]] = {
                    "free": 0.0,
                    "used": position.quantity,
                    "total": position.quantity,
                }

        for coin, amounts in detail.items():
            total = float(amounts.get("total") or 0.0)
            if total <= 0:
                continue
            price = 1.0 if coin == quote else self._mark_for(f"{coin}/{quote}")
            rows.append(
                {
                    "coin": coin,
                    "free": round(float(amounts.get("free") or 0.0), 8),
                    "used": round(float(amounts.get("used") or 0.0), 8),
                    "total": round(total, 8),
                    # None, not zero: an unknown price must not be displayed as a
                    # holding worth nothing.
                    "price": round(price, 8) if price else None,
                    "value": round(total * price, 4) if price else None,
                }
            )

        rows.sort(key=lambda r: -(r["value"] or 0.0))
        return rows

    def _mark_for(self, symbol: str) -> float:
        """Last price known for a symbol, preferring data already in hand.

        Order matters only for cost: the feed is free, the cached bar close is
        free, and asking the venue is a network call, so it is the last resort.
        """
        if self.feed is not None:
            quote = self.feed.quote(symbol)
            if quote is not None and quote.last > 0:
                return float(quote.last)
        cached = self._marks.get(symbol)
        if cached:
            return float(cached)
        if isinstance(self.broker, CCXTBroker):
            ticker = self.broker.fetch_ticker(symbol)
            try:
                return float(ticker.get("last") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _snapshot(self, bar_ts: int, scans: list[ScanRow], point) -> None:
        self.state.last_bar_ts = bar_ts
        self.state.bars_processed += 1
        self.state.equity = point.equity
        self.state.cash = point.cash
        self.state.peak_equity = self.portfolio.peak_equity
        self.state.starting_equity = self.portfolio.starting_cash
        self.state.drawdown = point.drawdown
        self.state.risk_multiplier = self.risk.risk_multiplier
        self.state.threshold = self.conviction.current_threshold(self.risk.threshold_bump())
        self.state.degraded_reason = self.risk.state.degraded_reason
        self.state.slippage = self.slippage.summary()
        if self.state.status not in ("safe_mode", "stopped"):
            self.state.status = "degraded" if self.risk.state.degraded else "running"

        self.state.open_positions = self._positions_view()
        self.state.balances = self._balances_view()
        self.state.feed = self.feed.status() if self.feed else {}
        self.state.scan_top = [
            {
                "symbol": r.symbol,
                "score": round(r.score, 1),
                "threshold": round(r.threshold, 1),
                "regime": r.regime.value,
                "reason": r.no_trade_reason.value if r.no_trade_reason else "eligible",
            }
            for r in sorted(scans, key=lambda r: -r.score)[:10]
        ]
        self.writer.write(self.state)

    def _record_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self.state.last_error = message[:500]
        self.state.last_error_ts = now_ms()
        log.error("loop_error", error=message[:500], exc_info=True)
        self.db.record_event("loop_error", message[:500], level="error")
        self.writer.write(self.state)

        if self.notifier and self.config.telegram.notify_errors:
            self.notifier.notify_error("Loop error", message)

        triage = self.roles.get("anomaly_triage")
        if triage is not None:
            result = triage.triage({"error": message[:500], "status": self.state.status})
            if result.ok and self.notifier:
                self.notifier.send(f"🔎 {result.data.get('summary', '')}")


def _last_row(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {}
    row = frame.iloc[-1]
    return {str(k): float(v) if v is not None else np.nan for k, v in row.items()}


def build_broker(config: Config, secrets: Secrets, symbols: list[str]) -> Broker:
    """Paper mode gets the simulator; live mode gets the venue."""
    if config.mode is not Mode.LIVE:
        fetcher = BybitFetcher(config.data.rest_rate_limit_ms)
        specs = {}
        for symbol in symbols:
            raw = fetcher.market_spec(symbol)
            from tbot.core.models import MarketSpec

            specs[symbol] = MarketSpec(**raw)
        return PaperBroker(
            specs=specs,
            cash=config.starting_balance,
            quote=config.quote_currency,
            taker_fee=config.execution.assumed_taker_fee,
            maker_fee=config.execution.assumed_maker_fee,
            slippage_bps=config.execution.slippage_bps,
        )

    key, secret = secrets.credentials_for(config.venue)
    broker = CCXTBroker(key, secret, config.venue, config.quote_currency)
    broker.load_markets()
    broker.refresh_fees(symbols)
    return broker


def venue_label(venue: Venue) -> str:
    return "DEMO (virtual funds)" if venue is Venue.BYBIT_DEMO else "LIVE (real money)"
