"""Event-driven backtester.

Runs the same decision code the live engine runs. What differs is only where
bars come from and which broker receives the orders, so a divergence between
backtest and live is a bug in one of those two places rather than a strategy
that behaves differently in testing.

Three structural guarantees, each with a test that fails if it breaks:

* **No lookahead.** Indicators are causal (verified by truncation tests), so
  they can be precomputed over the whole series for speed without leaking.
  Cross-timeframe reads go through ``align.align_slow_to_fast``, which maps on
  close times rather than containment.
* **Execution delay.** A signal on bar *t* submits an order that fills at bar
  *t+1*'s open. The close of a bar is not a price anyone could have traded at
  once they knew it was the close.
* **Stops fill where they trigger.** If a bar's range covers the stop, the exit
  happens at the stop price during that bar, not at the next open. Deferring it
  would understate losses on exactly the bars that produce them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tbot.backtest.align import align_slow_to_fast, project_series
from tbot.core.config import Config
from tbot.core.logging import get_logger
from tbot.core.models import (
    Candle,
    ExitReason,
    MarketSpec,
    NoTradeReason,
    OrderType,
    Regime,
    ScanRow,
    Side,
)
from tbot.data.integrity import MIN_BARS_BY_TIMEFRAME
from tbot.engine.portfolio import Portfolio
from tbot.execution.paper_broker import PaperBroker
from tbot.indicators import ta
from tbot.indicators.registry import compute_signals
from tbot.risk.budget import RiskBudget
from tbot.risk.sizing import correlation_conflict, size_position
from tbot.strategy.conviction import UNSCORED_REASONS, ConvictionEngine, ScoringContext
from tbot.strategy.exits import ExitManager
from tbot.strategy.ranking import CrossSectionalRanker
from tbot.strategy.regime import classify_series
from tbot.strategy.setups import (
    DEFAULT_SETUPS,
    detect_all,
    fallback_candidate,
    setups_for,
)

log = get_logger("backtest.engine")

BARS_PER_YEAR = {"1h": 8760, "4h": 2190, "1d": 365, "15m": 35_040, "1w": 52}


def _has_enough_history(frames: dict[str, pd.DataFrame]) -> bool:
    """Whether every timeframe has enough bars for its indicators to warm up."""
    return all(len(frame) >= MIN_BARS_BY_TIMEFRAME.get(tf, 200) for tf, frame in frames.items())


@dataclass(slots=True)
class SymbolData:
    """Everything precomputed for one symbol, aligned to the trigger timeframe."""

    symbol: str
    frame: pd.DataFrame  # trigger-timeframe OHLCV
    trigger_signals: pd.DataFrame
    setup_signals: pd.DataFrame  # projected onto trigger bars
    permission_signals: pd.DataFrame  # projected onto trigger bars
    regime: np.ndarray
    atr: np.ndarray  # trigger-timeframe ATR, for entry stop placement
    exit_atr: np.ndarray  # setup-timeframe ATR, for trailing the position
    setup_frame: pd.DataFrame
    setup_map: np.ndarray
    spec: MarketSpec


@dataclass(slots=True)
class BacktestResult:
    portfolio: Portfolio
    scans: list[ScanRow] = field(default_factory=list)
    config_summary: dict = field(default_factory=dict)
    bars_processed: int = 0
    # Where entry candidates are lost, stage by stage. A strategy producing too
    # few trades to measure is a solvable problem only once you know which gate
    # is doing the filtering.
    funnel: dict = field(default_factory=dict)

    @property
    def trades(self):
        return self.portfolio.trades

    @property
    def equity_curve(self):
        return self.portfolio.equity_curve


class Backtester:
    """Replays stored bars through the live decision path."""

    def __init__(
        self,
        config: Config,
        block_lookup: dict[str, int] | None = None,
        selected_by_regime: dict[str, list[str]] | None = None,
        edge_decay_bars: dict[str, int] | None = None,
    ):
        self.config = config
        self.conviction = ConvictionEngine(
            config=config.conviction,
            block_lookup=block_lookup or {},
            selected_by_regime=selected_by_regime or {},
        )
        self.exits = ExitManager(config.exits, edge_decay_bars)
        self.risk = RiskBudget(config.risk)
        self._funnel: Counter = Counter()
        # The one armed entry order, mirroring the live engine. Off unless
        # ``entries.armed_breakout`` says otherwise, so the default backtest is
        # the same one that has been measured all along.
        self._armed: dict | None = None
        self.ranker = CrossSectionalRanker(
            top_percentile=config.conviction.top_percentile,
            min_universe=config.conviction.min_universe_for_ranking,
            min_absolute_score=config.conviction.rank_absolute_floor,
        )

    # -- preparation -------------------------------------------------------

    def prepare(self, store, symbols: list[str]) -> dict[str, SymbolData]:
        """Load and precompute per symbol, aligned to the trigger timeframe."""
        tf = self.config.timeframes
        prepared: dict[str, SymbolData] = {}

        for symbol in symbols:
            trigger = store.read_with_derivatives(symbol, tf.trigger)
            setup = store.read_with_derivatives(symbol, tf.setup)
            permission = store.read_ohlcv(symbol, tf.permission)
            # Minimums are per timeframe, not a flat number: three years of
            # weekly data is 156 bars by definition, so a hardcoded 220 would
            # reject every weekly series no matter how complete it is.
            if not _has_enough_history(
                {tf.trigger: trigger, tf.setup: setup, tf.permission: permission}
            ):
                log.debug(
                    "skip_short_history",
                    symbol=symbol,
                    bars={
                        tf.trigger: len(trigger),
                        tf.setup: len(setup),
                        tf.permission: len(permission),
                    },
                )
                continue

            enabled = self.config.indicators.enabled or None
            trigger_signals = compute_signals(trigger, enabled)
            setup_signals = compute_signals(setup, enabled)
            permission_signals = compute_signals(permission, enabled)
            regime_labels = classify_series(permission, timeframe=tf.permission)

            setup_map = align_slow_to_fast(trigger["ts"], setup["ts"], tf.trigger, tf.setup)
            permission_map = align_slow_to_fast(
                trigger["ts"], permission["ts"], tf.trigger, tf.permission
            )

            projected_setup = pd.DataFrame(
                {c: project_series(setup_signals[c], setup_map) for c in setup_signals.columns}
            )
            projected_permission = pd.DataFrame(
                {
                    c: project_series(permission_signals[c], permission_map)
                    for c in permission_signals.columns
                }
            )
            projected_regime = project_series(
                pd.Series(regime_labels.to_numpy()), permission_map, fill=Regime.UNKNOWN.value
            )

            atr_series = ta.atr(
                trigger["high"], trigger["low"], trigger["close"], self.config.exits.atr_period
            )
            # The trail must be measured on the timeframe that matches the
            # intended holding period. Trailing a multi-week position with a
            # 1h ATR stops it out within hours — the position never survives
            # long enough to capture the move the tail analysis identified.
            trail_source = {
                "trigger": (trigger, None),
                "setup": (setup, setup_map),
                "permission": (permission, permission_map),
            }[self.config.exits.trail_timeframe]
            trail_frame, trail_map = trail_source
            exit_atr_series = ta.atr(
                trail_frame["high"],
                trail_frame["low"],
                trail_frame["close"],
                self.config.exits.atr_period,
            )
            projected_exit_atr = (
                exit_atr_series.to_numpy(dtype="float64")
                if trail_map is None
                else project_series(exit_atr_series, trail_map)
            )

            prepared[symbol] = SymbolData(
                symbol=symbol,
                frame=trigger,
                trigger_signals=trigger_signals,
                setup_signals=projected_setup,
                permission_signals=projected_permission,
                regime=np.asarray(projected_regime, dtype=object),
                atr=atr_series.to_numpy(dtype="float64"),
                exit_atr=np.asarray(projected_exit_atr, dtype="float64"),
                setup_frame=setup,
                setup_map=setup_map,
                spec=self._spec_for(symbol, store),
            )

        return prepared

    def _spec_for(self, symbol: str, store) -> MarketSpec:
        """Exchange constraints. Uses stored values when available, else config.

        Backtesting against optimistic fees produces a strategy that only works
        on paper, so the fallbacks here are the config's *assumed* costs, which
        are set pessimistically.
        """
        return MarketSpec(
            symbol=symbol,
            base=symbol.split("/")[0],
            quote=symbol.split("/")[1],
            min_notional=5.0,
            min_amount=0.0,
            amount_step=0.0,
            price_step=0.0,
            maker_fee=self.config.execution.assumed_maker_fee,
            taker_fee=self.config.execution.assumed_taker_fee,
        )

    # -- main loop ---------------------------------------------------------

    def run(
        self, store, symbols: list[str], start_ts: int | None = None, end_ts: int | None = None
    ) -> BacktestResult:
        data = self.prepare(store, symbols)
        if not data:
            raise RuntimeError("no symbol had enough history to backtest")

        broker = PaperBroker(
            specs={s: d.spec for s, d in data.items()},
            cash=self.config.starting_balance,
            quote=self.config.quote_currency,
            taker_fee=self.config.execution.assumed_taker_fee,
            maker_fee=self.config.execution.assumed_maker_fee,
            slippage_bps=self.config.execution.slippage_bps,
        )
        portfolio = Portfolio(starting_cash=self.config.starting_balance)
        self.risk.update_equity(self.config.starting_balance)

        timeline = self._build_timeline(data, start_ts, end_ts)
        correlations = self._correlations(data)
        scans: list[ScanRow] = []
        cursors = dict.fromkeys(data, 0)
        bars = 0

        for bar_ts in timeline:
            marks: dict[str, float] = {}
            entries_this_bar = 0

            # -- advance each symbol's cursor to this timestamp
            for symbol, d in data.items():
                idx = cursors[symbol]
                ts_values = d.frame["ts"].to_numpy()
                while idx < len(ts_values) and ts_values[idx] < bar_ts:
                    idx += 1
                cursors[symbol] = idx
                if idx < len(ts_values) and ts_values[idx] == bar_ts:
                    marks[symbol] = float(d.frame["close"].iloc[idx])

            # -- fills from orders submitted on the previous bar
            for symbol, d in data.items():
                idx = cursors[symbol]
                ts_values = d.frame["ts"].to_numpy()
                if idx >= len(ts_values) or ts_values[idx] != bar_ts:
                    continue
                row = d.frame.iloc[idx]
                candle = Candle(
                    ts=bar_ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                for fill in broker.process_bar(symbol, candle):
                    self._apply_fill(portfolio, broker, fill, d, idx, bar_ts)

            # -- an armed order may have filled on this bar, and that fill is a
            #    position everything below has to know about
            self._resolve_armed(portfolio, broker, data, cursors, bar_ts)

            # -- manage open positions before considering new ones
            for symbol in list(portfolio.positions):
                d = data.get(symbol)
                idx = cursors.get(symbol, 0)
                if d is None or idx >= len(d.frame) or d.frame["ts"].iloc[idx] != bar_ts:
                    continue
                self._manage_position(portfolio, broker, d, idx, bar_ts)

            # -- scan every symbol, then decide which qualify
            bar_rows: list[tuple[ScanRow, SymbolData, int]] = []
            for symbol, d in data.items():
                idx = cursors[symbol]
                if idx >= len(d.frame) or d.frame["ts"].iloc[idx] != bar_ts:
                    continue
                row = self._scan_symbol(portfolio, d, idx, bar_ts, correlations)
                bar_rows.append((row, d, idx))

            candidates = self._qualify(bar_rows)
            scans.extend(row for row, _, _ in bar_rows)

            # -- the highest-scoring candidate wins; others wait for next bar
            candidates.sort(key=lambda c: -c[0])
            sides = {row.symbol: row.best_side for row, _, _ in bar_rows}
            for score, _symbol, d, idx in candidates:
                if len(portfolio.positions) >= self.config.risk.max_positions:
                    break
                side = sides.get(d.symbol, Side.BUY)
                if self._try_enter(portfolio, broker, d, idx, bar_ts, score, side):
                    entries_this_bar += 1
                    break

            if entries_this_bar == 0:
                self._maybe_arm(portfolio, bar_rows, bar_ts)

            self.conviction.note_bar(entered=entries_this_bar > 0)
            point = portfolio.record_equity(bar_ts, marks)
            self.risk.update_equity(point.equity, bar_ts)
            bars += 1

        self._close_remaining(portfolio, data, cursors, timeline[-1] if timeline else 0)

        return BacktestResult(
            portfolio=portfolio,
            scans=scans,
            bars_processed=bars,
            funnel=dict(self._funnel),
            config_summary={
                "symbols": len(data),
                "timeframes": self.config.timeframes.all,
                "threshold": self.config.conviction.base_threshold,
                "risk_per_trade": self.config.risk.risk_per_trade,
                "taker_fee": self.config.execution.assumed_taker_fee,
                "slippage_bps": self.config.execution.slippage_bps,
            },
        )

    # -- steps -------------------------------------------------------------

    def _build_timeline(
        self, data: dict[str, SymbolData], start_ts: int | None, end_ts: int | None
    ) -> list[int]:
        stamps: set[int] = set()
        for d in data.values():
            stamps.update(int(t) for t in d.frame["ts"].to_numpy())
        timeline = sorted(stamps)
        if start_ts:
            timeline = [t for t in timeline if t >= start_ts]
        if end_ts:
            timeline = [t for t in timeline if t < end_ts]
        return timeline

    def _correlations(self, data: dict[str, SymbolData]) -> dict[tuple[str, str], float]:
        """Return correlations between symbols' returns.

        In crypto, holding five coins is frequently holding one bet five times.
        Without this the position limit counts positions while actual exposure
        concentrates.
        """
        returns = {}
        for symbol, d in data.items():
            series = d.frame["close"].pct_change().tail(500)
            returns[symbol] = series.reset_index(drop=True)
        if len(returns) < 2:
            return {}

        frame = pd.DataFrame(returns).dropna()
        if len(frame) < 50:
            return {}
        matrix = frame.corr()

        out: dict[tuple[str, str], float] = {}
        symbols = list(matrix.columns)
        for i, a in enumerate(symbols):
            for b in symbols[i + 1 :]:
                value = matrix.loc[a, b]
                if pd.notna(value):
                    out[(a, b) if a < b else (b, a)] = float(value)
        return out

    def _qualify(
        self, bar_rows: list[tuple[ScanRow, SymbolData, int]]
    ) -> list[tuple[float, str, SymbolData, int]]:
        """Decide which scanned symbols may be entered on this bar.

        Two modes, because the measurement and the implementation disagreed.
        ``absolute`` compares each symbol to a fixed threshold, which is what the
        engine originally did. ``cross_sectional`` ranks symbols against each
        other and takes the top slice — which is what the tail analysis that
        justified the strategy actually measured.
        """
        if self.config.conviction.selection == "absolute":
            return [
                (row.score, row.symbol, d, idx)
                for row, d, idx in bar_rows
                if row.no_trade_reason is None
            ]

        # Rank only symbols that cleared the structural gates (regime, position
        # limits, correlation). Ranking a symbol the portfolio cannot take would
        # waste a slot in the top slice on something ineligible.
        open_rows = {
            row.symbol: (row, d, idx)
            for row, d, idx in bar_rows
            if row.no_trade_reason in (None, NoTradeReason.BELOW_THRESHOLD)
        }
        if not open_rows:
            return []

        ranked = self.ranker.rank({s: r[0].score for s, r in open_rows.items()})
        selected = []

        for candidate in ranked:
            row, d, idx = open_rows[candidate.symbol]
            if candidate.eligible:
                row.no_trade_reason = None
                selected.append((candidate.score, candidate.symbol, d, idx))
            elif row.no_trade_reason is None:
                # Passed the absolute bar but not the relative one.
                row.no_trade_reason = NoTradeReason.BELOW_THRESHOLD

        return selected

    def _scan_symbol(
        self,
        portfolio: Portfolio,
        d: SymbolData,
        idx: int,
        bar_ts: int,
        correlations: dict[tuple[str, str], float],
    ) -> ScanRow:
        regime = Regime(d.regime[idx]) if d.regime[idx] else Regime.UNKNOWN
        history = d.setup_frame.iloc[: max(0, d.setup_map[idx] + 1)]

        conflict = correlation_conflict(
            d.symbol, portfolio.open_symbols, correlations, self.config.risk.max_correlation
        )
        ctx = ScoringContext(
            symbol=d.symbol,
            bar_ts=bar_ts,
            regime=regime,
            trigger_signals=_row_dict(d.trigger_signals, idx),
            setup_signals=_row_dict(d.setup_signals, idx),
            permission_signals=_row_dict(d.permission_signals, idx),
            atr=float(d.atr[idx]) if not np.isnan(d.atr[idx]) else 0.0,
            price=float(d.frame["close"].iloc[idx]),
            already_open=portfolio.has_position(d.symbol),
            portfolio_full=len(portfolio.positions) >= self.config.risk.max_positions,
            correlation_conflict=conflict,
            allow_short=self.config.entries.allow_short,
        )

        # Mirrors the live engine exactly. Scoring one side here and both there
        # is the class of difference that makes a backtest describe a strategy
        # nobody is running.
        allow_short = self.config.entries.allow_short
        bump = self.risk.state.threshold_bump
        streak = self.risk.threshold_bump() - bump
        conviction = None
        for side in (Side.BUY, Side.SELL) if allow_short else (Side.BUY,):
            scored = self.conviction.score(ctx, side, bump, streak)
            if conviction is None:
                conviction = scored
                continue
            blocked_now = scored.no_trade_reason is not None
            blocked_best = conviction.no_trade_reason is not None
            if (blocked_best and not blocked_now) or (
                blocked_best == blocked_now and scored.total > conviction.total
            ):
                conviction = scored

        reason = conviction.no_trade_reason
        if reason is None and self.config.entries.require_setup and len(history) >= 220:
            candidates = [
                c
                for c in detect_all(
                    history,
                    regime,
                    self.config.exits.initial_stop_atr_mult,
                    setups_for(allow_short),
                )
                if c.side is conviction.side
            ]
            if not candidates:
                reason = NoTradeReason.NO_SETUP

        return ScanRow(
            symbol=d.symbol,
            bar_ts=bar_ts,
            regime=regime,
            best_side=conviction.side,
            score=conviction.total,
            threshold=conviction.threshold,
            no_trade_reason=reason,
        )

    def _try_enter(
        self,
        portfolio: Portfolio,
        broker: PaperBroker,
        d: SymbolData,
        idx: int,
        bar_ts: int,
        score: float,
        side: Side = Side.BUY,
    ) -> bool:
        regime = Regime(d.regime[idx]) if d.regime[idx] else Regime.UNKNOWN
        history = d.setup_frame.iloc[: max(0, d.setup_map[idx] + 1)]
        if len(history) < 220:
            return False

        candidates = [
            c
            for c in detect_all(
                history,
                regime,
                self.config.exits.initial_stop_atr_mult,
                setups_for(self.config.entries.allow_short),
            )
            if c.side is side
        ]
        if candidates:
            candidate = candidates[0]
        elif self.config.entries.require_setup:
            self._funnel["no_setup_at_entry"] += 1
            return False
        else:
            candidate = fallback_candidate(
                history, regime, self.config.exits.initial_stop_atr_mult, side
            )
            if candidate is None:
                self._funnel["no_setup_at_entry"] += 1
                return False
            self._funnel["ranked_entry"] += 1
        price = float(d.frame["close"].iloc[idx])
        # The setup was detected on the slower chart; the stop must be expressed
        # relative to the price we will actually pay.
        distance = abs(candidate.entry_price - candidate.stop_price)
        stop = price - distance if side is Side.BUY else price + distance
        if (side is Side.BUY and stop >= price) or (side is Side.SELL and stop <= price):
            self._funnel["stop_above_price"] += 1
            return False

        window = self.config.risk.vol_lookback_bars
        returns = (
            d.frame["close"].pct_change().iloc[max(0, idx - window) : idx + 1].dropna().to_numpy()
        )

        sizing = size_position(
            equity=self.risk.state.equity,
            risk_amount=self.risk.risk_amount(self.risk.state.equity),
            entry_price=price,
            stop_price=stop,
            spec=d.spec,
            config=self.config.risk,
            recent_returns=returns,
            bars_per_year=BARS_PER_YEAR.get(self.config.timeframes.trigger, 8760),
            available_cash=portfolio.cash,
            min_notional_buffer=self.config.execution.min_notional_buffer,
        )
        if not sizing.ok:
            reason = sizing.rejected.value if sizing.rejected else "unknown"
            self._funnel[f"sizing:{reason}"] += 1
            return False

        order = broker.build_order(
            namespace=self.config.engine.namespace,
            strategy=candidate.name,
            symbol=d.symbol,
            bar_ts=bar_ts,
            action="entry",
            side=side,
            quantity=sizing.quantity,
            order_type=OrderType.MARKET,
        )
        order.meta = {
            "stop": stop,
            "planned_entry": price,
            "setup": candidate.name,
            "conviction": score,
            "regime": regime.value,
            "risk_amount": sizing.risk_amount,
        }
        broker.submit(order)
        self._funnel["entered"] += 1
        return True

    def _maybe_arm(self, portfolio: Portfolio, bar_rows: list, bar_ts: int) -> None:
        """Mirror of ``Runner._maybe_arm``. One order, best candidate, one bar.

        Symmetry here is not tidiness. If the live engine rests an order the
        backtest never rests, the two take different trades and every measured
        number stops describing the thing that is running.
        """
        if not self.config.entries.armed_breakout or self._armed is not None:
            return
        if len(portfolio.positions) >= self.config.risk.max_positions:
            return

        waiting = sorted(
            (
                (row, d, idx)
                for row, d, idx in bar_rows
                if row.no_trade_reason is NoTradeReason.NO_SETUP and row.score >= row.threshold
            ),
            key=lambda item: -item[0].score,
        )
        for row, d, idx in waiting:
            if portfolio.has_position(d.symbol):
                continue
            history = d.setup_frame.iloc[: max(0, d.setup_map[idx] + 1)]
            if len(history) < 220:
                continue
            regime = Regime(d.regime[idx]) if d.regime[idx] else Regime.UNKNOWN
            candidate = None
            for setup in DEFAULT_SETUPS:
                arm = getattr(setup, "arm", None)
                if arm is None:
                    continue
                candidate = arm(
                    history,
                    regime,
                    self.config.exits.initial_stop_atr_mult,
                    self.config.entries.armed_max_distance_atr,
                )
                if candidate is not None and candidate.stop_distance > 0:
                    break
                candidate = None
            if candidate is None:
                continue

            window = self.config.risk.vol_lookback_bars
            returns = (
                d.frame["close"]
                .pct_change()
                .iloc[max(0, idx - window) : idx + 1]
                .dropna()
                .to_numpy()
            )
            sizing = size_position(
                equity=self.risk.state.equity,
                risk_amount=self.risk.risk_amount(self.risk.state.equity),
                entry_price=candidate.entry_price,
                stop_price=candidate.stop_price,
                spec=d.spec,
                config=self.config.risk,
                recent_returns=returns,
                bars_per_year=BARS_PER_YEAR.get(self.config.timeframes.trigger, 8760),
                available_cash=portfolio.cash,
                min_notional_buffer=self.config.execution.min_notional_buffer,
            )
            if not sizing.ok:
                continue

            self._armed = {
                "symbol": d.symbol,
                "trigger": candidate.entry_price,
                "stop": candidate.stop_price,
                "quantity": sizing.quantity,
                "setup": candidate.name,
                "conviction": row.score,
                "regime": regime.value,
                "risk_amount": sizing.risk_amount,
                "bar_ts": bar_ts,
                "entry_atr": candidate.atr,
            }
            self._funnel["armed"] += 1
            return

    def _resolve_armed(
        self,
        portfolio: Portfolio,
        broker: PaperBroker,
        data: dict,
        cursors: dict,
        bar_ts: int,
    ) -> None:
        """Fill an armed order if this bar traded through its level, else expire it.

        The fill price is ``max(trigger, open)``. A bar that gaps straight past
        the level does not fill at the level — the first price available is the
        open, and it is worse. Assuming the trigger price in that case is the
        same optimism that makes a backtest beat the live account it was
        supposed to predict.

        One bar and no more (invariant D41): the channel that produced the level
        has moved, so an order left resting would fill at a price this strategy
        has no opinion about.
        """
        armed = self._armed
        if armed is None:
            return
        self._armed = None  # one bar, whatever happens below

        if armed["bar_ts"] >= bar_ts:
            self._armed = armed  # armed on this very bar; it gets the next one
            return

        d = data.get(armed["symbol"])
        idx = cursors.get(armed["symbol"], 0)
        if d is None or idx >= len(d.frame) or int(d.frame["ts"].iloc[idx]) != bar_ts:
            return
        if portfolio.has_position(armed["symbol"]):
            return

        row = d.frame.iloc[idx]
        high, open_ = float(row["high"]), float(row["open"])
        trigger = float(armed["trigger"])
        if high < trigger:
            self._funnel["armed_expired"] += 1
            return

        order = broker.build_order(
            namespace=self.config.engine.namespace,
            strategy=str(armed["setup"]),
            symbol=armed["symbol"],
            bar_ts=bar_ts,
            action="armed-entry",
            side=Side.BUY,
            quantity=float(armed["quantity"]),
            order_type=OrderType.MARKET,
        )
        order.meta = {
            "stop": float(armed["stop"]),
            "planned_entry": trigger,
            "setup": str(armed["setup"]),
            "conviction": float(armed["conviction"]),
            "regime": str(armed["regime"]),
            "risk_amount": float(armed["risk_amount"]),
        }
        fill = broker.force_fill_at(order, max(trigger, open_), bar_ts)
        self._apply_fill(portfolio, broker, fill, d, idx, bar_ts)
        self._funnel["armed_filled"] += 1

    def _apply_fill(
        self, portfolio: Portfolio, broker: PaperBroker, fill, d: SymbolData, idx: int, bar_ts: int
    ) -> None:
        """Turn an entry fill into a position, carrying the plan across.

        The stop, setup and conviction were decided before the order was sent
        and travel on the order's ``meta``. Recomputing them here would risk
        opening a position against a different plan than the one that was
        scored and sized.
        """
        order = broker.orders.get(fill.order_client_id)
        if order is None or not order.meta:
            return
        if portfolio.has_position(d.symbol):
            return

        atr_now = float(d.exit_atr[idx]) if not np.isnan(d.exit_atr[idx]) else 0.0
        # The stop was computed against the signal-bar close; the fill happened
        # at the next open with slippage. Preserve the *distance*, so the risk
        # the position carries is the risk that was sized.
        planned_stop = float(order.meta["stop"])
        planned_entry = float(order.meta.get("planned_entry", fill.price))
        stop = fill.price - (planned_entry - planned_stop)

        position = portfolio.open_position(
            fill=fill,
            stop_price=stop,
            setup=str(order.meta.get("setup", "unknown")),
            risk_amount=float(order.meta.get("risk_amount", 0.0)),
            conviction=float(order.meta.get("conviction", 0.0)),
            regime=Regime(order.meta.get("regime", Regime.UNKNOWN.value)),
        )
        position.meta["entry_atr"] = atr_now

    def _manage_position(
        self, portfolio: Portfolio, broker: PaperBroker, d: SymbolData, idx: int, bar_ts: int
    ) -> None:
        position = portfolio.positions.get(d.symbol)
        if position is None:
            return

        row = d.frame.iloc[idx]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        portfolio.update_bar(d.symbol, high, low)

        atr_now = float(d.exit_atr[idx]) if not np.isnan(d.exit_atr[idx]) else 0.0
        entry_atr = float(position.meta.get("entry_atr", atr_now) or atr_now)
        regime = Regime(d.regime[idx]) if d.regime[idx] else Regime.UNKNOWN

        decision = self.exits.evaluate(
            position=position,
            high=high,
            low=low,
            close=close,
            atr=atr_now,
            entry_atr=entry_atr,
            regime=regime,
            conviction_now=self._conviction_for_open(d, idx, bar_ts, position.side, regime),
            collapse_threshold=self.config.conviction.collapse_threshold,
        )

        if decision.should_exit and decision.exit_price is not None:
            exit_price = decision.exit_price
            fee = exit_price * position.quantity * d.spec.taker_fee
            trade = portfolio.close_position(
                d.symbol, exit_price, bar_ts, decision.reason or ExitReason.TRAILING_STOP, fee
            )
            if trade:
                self.risk.record_trade(trade)
            return

        # Mirrors Runner._maybe_scale_out, and for the same reason: if one side
        # takes partial profits and the other does not, every measured number
        # stops describing the thing that is running.
        self._scale_out(portfolio, d, position, high, low)

        if decision.new_stop is not None:
            position.current_stop = decision.new_stop

    def _scale_out(self, portfolio: Portfolio, d: SymbolData, position, high, low) -> None:
        """Take the same slice off at the same level the live engine would.

        Reached only after the stop has been checked, so a bar that touched
        both the stop and the scale-out level is a loss (invariant D40).
        """
        plan = self.exits.scale_out(position, high, low)
        if plan is None:
            return

        original = float(position.meta.get("original_quantity", position.quantity))
        wanted = min(original * plan.fraction, position.quantity)
        remainder = position.quantity - wanted
        if wanted < d.spec.min_amount or (remainder > 0 and remainder < d.spec.min_amount):
            position.meta["scaled_out"] = True  # unsellable either way; do not retry
            return

        fee = plan.price * wanted * d.spec.taker_fee
        sold = portfolio.scale_out(d.symbol, plan.price, plan.fraction, fee)
        if sold:
            self._funnel["scaled_out"] += 1

    def _conviction_for_open(
        self, d: SymbolData, idx: int, bar_ts: int, side: Side, regime: Regime
    ) -> float | None:
        """Re-score an open position's thesis. Mirrors ``Runner._conviction_for_open``.

        Must stay symmetric with the live path: if one of them feeds the
        conviction-collapse exit and the other passes None, the backtest and the
        engine take different trades and Gate 2 measures a divergence that is
        entirely our own doing.

        Portfolio-state blockers are neutralised for the same reason as live —
        they answer "should I open this?", not "is this still true?" — and a
        blocked score reads as no opinion, never as zero.
        """
        try:
            ctx = ScoringContext(
                symbol=d.symbol,
                bar_ts=bar_ts,
                regime=regime,
                trigger_signals=_row_dict(d.trigger_signals, idx),
                setup_signals=_row_dict(d.setup_signals, idx),
                permission_signals=_row_dict(d.permission_signals, idx),
                atr=float(d.atr[idx]) if not np.isnan(d.atr[idx]) else 0.0,
                price=float(d.frame["close"].iloc[idx]),
                already_open=False,
                portfolio_full=False,
                correlation_conflict=False,
            )
            scored = self.conviction.score(ctx, side)
        except Exception:  # noqa: BLE001 — an exit check must never stop the replay
            return None

        # See ``Runner._conviction_for_open``: only a blocked verdict is "no
        # opinion". A low score is the signal this exit was built to read.
        if scored.no_trade_reason in UNSCORED_REASONS:
            return None
        return scored.total

    def _close_remaining(
        self, portfolio: Portfolio, data: dict[str, SymbolData], cursors: dict[str, int], ts: int
    ) -> None:
        """Mark open positions to the last close so results include them.

        Dropping them would flatter the result whenever the test window ends in
        a drawdown.
        """
        for symbol in list(portfolio.positions):
            d = data.get(symbol)
            if d is None:
                continue
            idx = min(cursors.get(symbol, 0), len(d.frame) - 1)
            price = float(d.frame["close"].iloc[idx])
            fee = price * portfolio.positions[symbol].quantity * d.spec.taker_fee
            trade = portfolio.close_position(symbol, price, ts, ExitReason.BACKTEST_END, fee)
            if trade:
                self.risk.record_trade(trade)


def _row_dict(frame: pd.DataFrame, idx: int) -> dict[str, float]:
    if frame.empty or idx >= len(frame):
        return {}
    row = frame.iloc[idx]
    return {str(k): float(v) if v is not None else np.nan for k, v in row.items()}
