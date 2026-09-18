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
from tbot.strategy.conviction import ConvictionEngine, ScoringContext
from tbot.strategy.exits import ExitManager
from tbot.strategy.regime import classify_series
from tbot.strategy.setups import DEFAULT_SETUPS, detect_all

log = get_logger("engine.runner")
ROOT_DIR = Path(__file__).resolve().parents[3]

BARS_PER_YEAR = {"1h": 8760, "4h": 2190, "1d": 365}


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
            assert_key_is_safe(self.broker.exchange)

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

        try:
            self._loop()
        finally:
            self.state.status = "stopped"
            self.writer.write(self.state)
            self.db.stop_run("clean" if self._running is False else "error")
            log.info("engine_stopped", bars=self.state.bars_processed)

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
                sleep_ms = ms_until_next_close(timeframe) + 2000
                slept = 0.0
                while slept < sleep_ms / 1000 and self._running:
                    self._poll_commands()
                    self._apply_command_file()
                    step = min(self.config.engine.loop_interval_seconds, sleep_ms / 1000 - slept)
                    time.sleep(max(0.1, step))
                    slept += step

            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                self._record_error(exc)
                time.sleep(min(60, self.config.engine.loop_interval_seconds * 4))

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

        # Orders submitted on the previous bar fill against this bar's open.
        # Without this the paper simulator queues orders and never executes
        # them: the portfolio would show positions the broker never opened, with
        # no slippage and no fees — the exact opposite of what paper mode is for.
        self._drain_paper_fills(frames, bar_ts)
        self._manage_positions(frames, bar_ts)
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
                "total_return": self.state.equity / self.config.starting_balance - 1.0,
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
            entry_atr = float(position.meta.get("entry_atr", atr_now) or atr_now)
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
                conviction_now=None,
                collapse_threshold=self.config.conviction.collapse_threshold,
            )

            if decision.should_exit:
                self._close(symbol, decision.exit_price or close, bar_ts, decision.reason)
            elif decision.new_stop is not None:
                position.current_stop = decision.new_stop
                self.db.upsert_position(position)
                self._sync_protective_stop(position)
            elif not position.meta.get("stop_order_id"):
                # No venue stop on this position: either the venue refused it at
                # entry, or the process restarted before one was placed. Retry
                # every bar — a single refusal used to leave the position
                # unprotected for its entire life.
                self._place_protective_stop(position, bar_ts)

    def _scan(self, frames: dict, bar_ts: int) -> list[ScanRow]:
        correlations = self._correlations(frames)
        rows: list[ScanRow] = []
        enabled = self.config.indicators.enabled or None

        drawdown_bump = self.risk.state.threshold_bump
        streak_bump = self.risk.threshold_bump() - drawdown_bump

        for symbol, data in frames.items():
            trigger, setup, permission = data["trigger"], data["setup"], data["permission"]
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
        returns = data["trigger"]["close"].pct_change().tail(30).dropna().to_numpy()
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
