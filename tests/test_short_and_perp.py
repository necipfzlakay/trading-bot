"""Two engines, one account — and the short side that spot could never express.

The tests here protect the boundaries that only start to matter once a second
engine shares the exchange account with the first. Each one corresponds to a
failure that is silent rather than loud: an order id collision that reads as a
successful submission, a failed balance read that reads as an empty account, a
short exit that sells again instead of buying back.
"""

from __future__ import annotations

import hashlib

import ccxt
import pytest

from tbot.core.config import ConvictionConfig, EntryConfig, ExitConfig, RiskConfig, load_config
from tbot.core.models import ExitReason, Fill, Position, Regime, Side
from tbot.engine.portfolio import Portfolio
from tbot.execution.broker import client_order_id
from tbot.strategy.exits import ExitManager
from tbot.strategy.regime import regime_allows
from tbot.strategy.setups import SHORT_SETUPS, setups_for

# --------------------------------------------------------------------------
# Order id namespacing
# --------------------------------------------------------------------------


def test_two_engines_never_compute_the_same_client_order_id():
    """The collision is exact, not probabilistic.

    With ``selection: cross_sectional`` every engine names its strategy
    ``ranked_entry``, so (strategy, symbol, bar, action) is identical across
    engines on the same bar. The second engine's submit would then find the
    first engine's order already at the venue and adopt it as its own.
    """
    args = ("ranked_entry", "SOL/USDT:USDT", 1_758_300_000_000, "entry")
    assert client_order_id(*args, "") != client_order_id(*args, "fast")


def test_the_empty_namespace_reproduces_the_pre_namespace_id_exactly():
    """The running engine has live positions and resting stops under its ids.

    If adding the namespace changed the default id, every order already at the
    venue would stop being recognisable as this engine's own — so the migration
    would have to happen with no position open. It does not need to.
    """
    raw = "ranked_entry:VVV/USDT:1758300000000:entry"
    expected = f"tb-vvv-entr-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
    assert client_order_id("ranked_entry", "VVV/USDT", 1_758_300_000_000, "entry") == expected


def test_a_namespaced_id_still_fits_the_venue_limit():
    """Bybit caps orderLinkId at 36 characters and rejects the whole order."""
    long_ids = [
        client_order_id("momentum_continuation", "MATIC/USDT:USDT", 1_758_300_000_000, "stop-1758300000", ns)
        for ns in ("", "fast")
    ]
    assert all(len(i) <= 36 for i in long_ids), long_ids


# --------------------------------------------------------------------------
# A failed venue read is not an empty account
# --------------------------------------------------------------------------


def _broker(exchange):
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import CCXTBroker

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = exchange
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}
    return broker


def test_an_unreadable_balance_reports_unknown_rather_than_empty():
    """Measured during a four-hour outage: the engine logged its live position
    as missing from the venue. It survived only because the stop lookup failed
    in the same breath and pushed it down the "report loudly" branch. An empty
    dict and an empty account have to stop being the same value.
    """
    import ccxt

    class Broken:
        def fetch_balance(self):
            raise ccxt.NetworkError("RequestTimeout: Connection timeout")

    assert _broker(Broken()).fetch_positions() is None


def test_an_account_that_genuinely_holds_nothing_still_reports_empty():
    class Empty:
        def fetch_balance(self):
            return {"total": {"USDT": 1000.0}}

    assert _broker(Empty()).fetch_positions() == {}


# --------------------------------------------------------------------------
# The permission layer
# --------------------------------------------------------------------------


def test_shorting_is_off_unless_asked_for():
    for regime in Regime:
        assert regime_allows(regime, "sell") is False


def test_enabling_shorts_mirrors_the_permission_layer_without_widening_it():
    assert regime_allows(Regime.TREND_DOWN, "sell", True) is True
    assert regime_allows(Regime.RANGE, "sell", True) is True
    # A volatility panic is not a direction, and neither is an unclassified bar.
    assert regime_allows(Regime.HIGH_VOL, "sell", True) is False
    assert regime_allows(Regime.UNKNOWN, "sell", True) is False
    # And the long side is untouched by the flag.
    assert regime_allows(Regime.TREND_UP, "buy", True) is True
    assert regime_allows(Regime.TREND_DOWN, "buy", True) is False


def test_short_setups_are_not_handed_to_a_spot_engine():
    """A spot engine reaching a short setup produces an order it cannot fill."""
    assert all(s not in setups_for(False) for s in SHORT_SETUPS)
    assert all(s in setups_for(True) for s in SHORT_SETUPS)


# --------------------------------------------------------------------------
# Short accounting
# --------------------------------------------------------------------------


def _fill(side, price, qty=1.0):
    return Fill(
        order_client_id="x", symbol="S/USDT", side=side, quantity=qty, price=price, fee=0.0, ts=0
    )


def _open(portfolio, side, price, stop):
    return portfolio.open_position(
        fill=_fill(side, price),
        stop_price=stop,
        setup="t",
        risk_amount=10.0,
        conviction=70.0,
        regime=Regime.RANGE,
    )


def test_a_short_counts_the_margin_it_posted_as_well_as_the_move():
    """Opening a short deducts margin from cash. Counting only the unrealised
    move then loses that margin: a short opened at 100 on a 300 account read as
    equity 200 before the price had moved at all.
    """
    p = Portfolio(starting_cash=300.0)
    _open(p, Side.SELL, 100.0, 110.0)
    assert p.equity({"S/USDT": 100.0}) == pytest.approx(300.0)
    assert p.equity({"S/USDT": 90.0}) == pytest.approx(310.0)
    assert p.equity({"S/USDT": 110.0}) == pytest.approx(290.0)


def test_closing_a_short_returns_the_margin_plus_the_result():
    p = Portfolio(starting_cash=300.0)
    _open(p, Side.SELL, 100.0, 110.0)
    trade = p.close_position("S/USDT", 90.0, 0, ExitReason.TAKE_PROFIT)
    assert p.cash == pytest.approx(310.0)
    assert trade.pnl == pytest.approx(10.0)
    assert trade.r_multiple == pytest.approx(1.0)


def test_the_long_side_is_unchanged_by_the_short_accounting():
    p = Portfolio(starting_cash=300.0)
    _open(p, Side.BUY, 100.0, 90.0)
    assert p.equity({"S/USDT": 110.0}) == pytest.approx(310.0)
    trade = p.close_position("S/USDT", 110.0, 0, ExitReason.TAKE_PROFIT)
    assert p.cash == pytest.approx(310.0)
    assert trade.r_multiple == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Exits, both directions
# --------------------------------------------------------------------------


def _position(side, entry, stop):
    return Position(
        symbol="S/USDT",
        side=side,
        quantity=1.0,
        entry_price=entry,
        entry_ts=0,
        initial_stop=stop,
        current_stop=stop,
        setup="t",
        risk_amount=10.0,
        meta={"entry_atr": 1.0},
    )


def test_a_short_stops_out_when_price_rises_through_the_stop():
    m = ExitManager(config=ExitConfig())
    d = m.evaluate_intrabar(_position(Side.SELL, 100.0, 110.0), high=110.5, low=99.0)
    assert d.should_exit and d.reason is ExitReason.INITIAL_STOP
    assert d.exit_price == pytest.approx(110.0)


def test_a_short_target_sits_below_the_entry():
    m = ExitManager(config=ExitConfig(target_atr_mult=2.0))
    assert m.target_price(_position(Side.SELL, 100.0, 110.0)) == pytest.approx(98.0)
    assert m.target_price(_position(Side.BUY, 100.0, 90.0)) == pytest.approx(102.0)


def test_the_time_stop_is_off_for_the_main_engine_and_fires_for_the_fast_one():
    """Invariant 51: every mechanism that cuts the tail costs more than it
    saves, because all the profit comes from the trades that run past 3R. The
    fast engine accepts that trade-off on purpose; the main engine must not
    inherit it as a default.
    """
    position = _position(Side.BUY, 100.0, 90.0)
    position.bars_held = 500
    kwargs = dict(
        high=101.0, low=99.0, close=100.0, atr=1.0, entry_atr=1.0,
        regime=Regime.RANGE, conviction_now=None, collapse_threshold=0.0,
    )
    assert ExitManager(config=ExitConfig()).evaluate(position, **kwargs).should_exit is False

    fired = ExitManager(config=ExitConfig(time_stop_bars=48)).evaluate(position, **kwargs)
    assert fired.should_exit and fired.reason is ExitReason.TIME_STOP


def test_the_stop_is_checked_before_the_time_limit():
    """A bar that reached the stop is a stop, not a timeout (invariant 40's
    spirit: report the pessimistic, specific reason)."""
    position = _position(Side.BUY, 100.0, 90.0)
    position.bars_held = 500
    d = ExitManager(config=ExitConfig(time_stop_bars=48)).evaluate(
        position, high=101.0, low=89.0, close=100.0, atr=1.0, entry_atr=1.0,
        regime=Regime.RANGE, conviction_now=None, collapse_threshold=0.0,
    )
    assert d.reason is ExitReason.INITIAL_STOP


# --------------------------------------------------------------------------
# The perpetual broker
# --------------------------------------------------------------------------


def test_the_perp_broker_does_not_inherit_the_spot_fee_clamp():
    """``_sellable`` exists because Bybit spot charges the buy fee in the base
    asset. A perpetual has no base balance, so inheriting the clamp would read
    zero and refuse to place any protective stop at all.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    broker = PerpBroker.__new__(PerpBroker)
    broker.exchange = None
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    assert broker._sellable("SOL/USDT:USDT", 1.25) == 1.25


def test_the_perp_broker_refuses_to_start_above_the_configured_leverage():
    """Nothing downstream reads the venue's leverage. An engine that started
    while the venue was left at 10x would size every position correctly and
    carry ten times the liquidation risk it believes it has.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    broker = PerpBroker.__new__(PerpBroker)
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker.margin_mode = "isolated"
    broker.leverage = 1.0
    broker._markets_loaded = True
    broker._specs = {}

    class Exchange:
        def set_leverage(self, *a, **k):
            return None

        def set_margin_mode(self, *a, **k):
            return None

        def privateGetV5PositionList(self, params):
            return {"result": {"list": [{"symbol": "SOLUSDT", "leverage": "10"}]}}

        def privateGetV5AccountInfo(self, params):
            return {"result": {"marginMode": "ISOLATED_MARGIN"}}

    broker.exchange = Exchange()
    with pytest.raises(RuntimeError, match="10x"):
        broker.assert_venue_ready(["SOL/USDT:USDT"])


def test_leverage_above_one_requires_isolated_margin():
    """At 1x a position is fully collateralised and a short liquidates 99%
    away, so cross margin does not bind. Above 1x it does, and a liquidation in
    cross can reach balances this engine does not own.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    broker = PerpBroker.__new__(PerpBroker)
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker.margin_mode = "isolated"
    broker.leverage = 3.0
    broker._markets_loaded = True
    broker._specs = {}

    class Exchange:
        def set_leverage(self, *a, **k):
            return None

        def set_margin_mode(self, *a, **k):
            return None

        def privateGetV5PositionList(self, params):
            return {"result": {"list": []}}

        def privateGetV5AccountInfo(self, params):
            return {"result": {"marginMode": "REGULAR_MARGIN"}}

    broker.exchange = Exchange()
    with pytest.raises(RuntimeError, match="isolated margin"):
        broker.assert_venue_ready(["SOL/USDT:USDT"])


# --------------------------------------------------------------------------
# Configuration contracts
# --------------------------------------------------------------------------


def test_the_fast_engine_is_isolated_from_the_main_one():
    main, fast = load_config("config/demo.yaml"), load_config("config/fast.yaml")
    assert fast.engine.namespace and not main.engine.namespace
    assert fast.runtime_dir != main.runtime_dir
    assert fast.db_path != main.db_path
    assert fast.state_path != main.state_path
    assert fast.web.port != main.web.port
    assert fast.data.parquet_dir != main.data.parquet_dir
    # Telegram's getUpdates offset is per token, so two engines sharing one
    # would steal each other's commands.
    assert fast.telegram.enabled is False


def test_the_fast_engine_sizes_against_its_own_allocation():
    fast = load_config("config/fast.yaml")
    assert fast.risk.max_capital == 300.0
    assert fast.risk.max_leverage == 1.0
    assert fast.execution.market_type == "linear"
    assert fast.entries.allow_short is True


def test_the_main_engine_keeps_every_default_it_had():
    """This work must not change the running engine as a side effect."""
    main = load_config("config/demo.yaml")
    assert main.risk.max_capital == 0.0
    assert main.entries.allow_short is False
    assert main.exits.time_stop_bars == 0
    assert main.execution.market_type == "spot"
    assert main.engine.namespace == ""


def test_an_unknown_top_level_config_key_is_an_error():
    """Invariant 50 covered every section but not the root, so a top-level typo
    landed in a model that ignored it — which looks exactly like a setting that
    is being read."""
    from pydantic import ValidationError

    from tbot.core.config import Config

    with pytest.raises(ValidationError):
        Config(reisk={"max_positions": 3})


def test_short_config_defaults_do_not_change_the_long_engine():
    assert EntryConfig().allow_short is False
    assert ExitConfig().time_stop_bars == 0
    assert RiskConfig().max_capital == 0.0
    assert ConvictionConfig().selection == "cross_sectional"


# --------------------------------------------------------------------------
# Selection: the live path and the backtest must ask the same question
# --------------------------------------------------------------------------


def _row(symbol, score, reason=None):
    from tbot.core.models import ScanRow

    return ScanRow(
        symbol=symbol,
        bar_ts=0,
        regime=Regime.TREND_UP,
        best_side=Side.BUY,
        score=score,
        threshold=62.0,
        no_trade_reason=reason,
    )


def _runner_with(selection, **conviction_kwargs):
    from tbot.engine.runner import Runner
    from tbot.strategy.ranking import CrossSectionalRanker

    cfg = ConvictionConfig(selection=selection, **conviction_kwargs)
    runner = Runner.__new__(Runner)
    runner.config = type("C", (), {"conviction": cfg})()
    runner.ranker = CrossSectionalRanker(
        top_percentile=cfg.top_percentile,
        min_universe=cfg.min_universe_for_ranking,
        min_absolute_score=cfg.rank_absolute_floor,
    )
    return runner


def test_cross_sectional_selection_readmits_the_best_of_a_weak_bar():
    """This is the divergence. The backtest ranks symbols against each other and
    re-admits the top slice even when it sits below the absolute threshold; the
    live engine only ever compared against the threshold. Every number in the
    repo comes from the backtest's answer, so the live path had to learn it.
    """
    from tbot.core.models import NoTradeReason

    rows = [_row(f"S{i}/USDT", 50.0 - i, NoTradeReason.BELOW_THRESHOLD) for i in range(12)]
    selected = _runner_with("cross_sectional")._select(rows)

    # Every score here is below the 62 threshold, so the absolute rule admits
    # nothing. The top slice of twelve is two symbols, and they are the two
    # highest — which is what the tail study actually measured.
    assert [r.symbol for r in selected] == ["S0/USDT", "S1/USDT"]
    assert all(r.no_trade_reason is None for r in selected)
    assert all(r.no_trade_reason is NoTradeReason.BELOW_THRESHOLD for r in rows[2:])


def test_absolute_selection_never_readmits_anything():
    from tbot.core.models import NoTradeReason

    rows = [_row(f"S{i}/USDT", 50.0 - i, NoTradeReason.BELOW_THRESHOLD) for i in range(12)]
    assert _runner_with("absolute")._select(rows) == []


def test_the_rank_floor_still_binds_on_a_uniformly_bad_bar():
    """Being the best of a uniformly bad cross-section is not a reason to buy."""
    from tbot.core.models import NoTradeReason

    rows = [_row(f"S{i}/USDT", 20.0 - i, NoTradeReason.BELOW_THRESHOLD) for i in range(12)]
    assert _runner_with("cross_sectional")._select(rows) == []
    assert all(r.no_trade_reason is NoTradeReason.BELOW_THRESHOLD for r in rows)


# --------------------------------------------------------------------------
# The short side must survive the trip from config to the permission layer
# --------------------------------------------------------------------------


def _downtrend_frames(bars: int = 400):
    """One symbol in a steady decline — the regime only the short side may take."""
    import numpy as np
    import pandas as pd

    close = pd.Series(400 * np.exp(np.linspace(0, -0.9, bars)))
    frame = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": pd.Series(np.full(bars, 1000.0)),
        }
    )
    frame.index = pd.date_range("2024-01-01", periods=bars, freq="1h", tz="UTC")
    return {"SOL/USDT:USDT": {"trigger": frame, "setup": frame, "permission": frame}}


def test_the_live_scan_carries_allow_short_into_the_scoring_context():
    """The flag reached the loop but never the context it was read from.

    ``_scan`` asked for both sides, and the scorer then asked
    ``regime_allows(ctx.regime, side, ctx.allow_short)`` — where the context's
    flag was still its default. Every short was refused as ``regime_veto``
    before it was scored, on a config that says ``allow_short: true``. The
    backtest passed the flag, so the two paths measured different strategies,
    and the short side is the whole of what the fast engine pre-registered.
    """
    from tbot.core.config import Config
    from tbot.core.models import NoTradeReason
    from tbot.engine.runner import Runner
    from tbot.risk.budget import RiskBudget
    from tbot.strategy.conviction import ConvictionEngine

    config = Config()
    config.entries.allow_short = True

    runner = Runner.__new__(Runner)
    runner.config = config
    runner.conviction = ConvictionEngine(config=config.conviction)
    runner.db = type("D", (), {"record_conviction": lambda self, _c: None})()
    runner.portfolio = Portfolio(starting_cash=1000.0)
    runner.risk = RiskBudget(config.risk)
    runner._microstructure = {}
    runner._llm_veto = False

    rows = runner._scan(_downtrend_frames(), bar_ts=1_758_300_000_000)

    # A downtrend is the short side's own regime. With the flag lost, both
    # sides came back vetoed and the row kept the long it had scored first.
    assert rows[0].regime is Regime.TREND_DOWN
    assert rows[0].best_side is Side.SELL
    assert rows[0].no_trade_reason is not NoTradeReason.REGIME_VETO


# --------------------------------------------------------------------------
# The fill is the record. Its side is not decoration.
# --------------------------------------------------------------------------


def _spec(symbol: str = "SOL/USDT:USDT"):
    from tbot.core.models import MarketSpec

    return MarketSpec(
        symbol=symbol,
        base=symbol.split("/")[0],
        quote="USDT",
        min_notional=5.0,
        min_amount=0.001,
        amount_step=0.001,
        price_step=0.01,
        maker_fee=0.0001,
        taker_fee=0.0006,
    )


class _Venue:
    """A live venue that is neither the simulator nor a CCXT broker.

    Enough to take ``_enter`` down its live branch without reaching the
    exchange-side stop, which ``_place_protective_stop`` skips for anything
    that is not a :class:`CCXTBroker`.
    """

    def __init__(self, fill_price: float):
        self.fill_price = fill_price
        self.submitted: list = []

    def market_spec(self, symbol: str):
        return _spec(symbol)

    def build_order(self, **kwargs):
        from tbot.execution.broker import Broker

        return Broker.build_order(self, **kwargs)

    def submit(self, order):
        from tbot.core.models import OrderStatus

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_price = self.fill_price
        order.fee_paid = self.fill_price * order.quantity * 0.0006
        self.submitted.append(order)
        return order


def _live_runner(config, cash: float = 5_000.0):
    from tbot.engine.runner import Runner
    from tbot.execution.safety import SlippageTracker
    from tbot.risk.budget import RiskBudget

    runner = Runner.__new__(Runner)
    runner.config = config
    runner.portfolio = Portfolio(starting_cash=cash)
    runner.risk = RiskBudget(config.risk)
    runner.risk.state.equity = cash
    runner.slippage = SlippageTracker(assumed_bps=config.execution.slippage_bps)
    runner.notifier = None
    runner._rejections = {}
    runner.db = type(
        "D",
        (),
        {
            "order_exists": lambda self, _cid: False,
            "upsert_order": lambda self, _o: None,
            "record_fill": lambda self, _f: None,
            "upsert_position": lambda self, _p: None,
        },
    )()
    runner.watchdog = type("W", (), {"call": lambda self, _n, fn: fn()})()
    return runner


def test_a_live_short_entry_is_recorded_as_a_short():
    """The ``Fill`` carried ``side=Side.BUY`` as a literal.

    ``Portfolio.open_position`` reads ``position.side`` straight off the fill,
    so a short opened at the venue became a long in every downstream reading:
    unrealised PnL with its sign inverted, a stop sitting above an entry the
    engine believed was a buy, and an exit condition that could never be met.
    The order itself went out correctly — only the record of it was wrong,
    which is the kind of divergence nothing reconciles.
    """
    from tbot.core.config import Config
    from tbot.core.models import ScanRow

    config = Config()
    config.entries.allow_short = True
    config.entries.require_setup = False

    runner = _live_runner(config)
    runner.broker = _Venue(fill_price=160.0)

    frames = _downtrend_frames()
    data = frames["SOL/USDT:USDT"]
    row = ScanRow(
        symbol="SOL/USDT:USDT",
        bar_ts=1_758_300_000_000,
        regime=Regime.TREND_DOWN,
        best_side=Side.SELL,
        score=80.0,
        threshold=60.0,
        no_trade_reason=None,
    )

    assert runner._enter(data, row, bar_ts=1_758_300_000_000) is True

    position = runner.portfolio.positions["SOL/USDT:USDT"]
    assert position.side is Side.SELL
    # A short's stop sits above the entry. If the side were wrong this would
    # still hold numerically while meaning the opposite.
    assert position.initial_stop > position.entry_price


def test_scaling_out_of_a_short_is_recorded_as_a_buy_to_cover():
    """The order was built with ``position.side.opposite`` and the ``Fill``
    with a literal ``Side.SELL``. Reducing a short is a buy, so the fills table
    gained a sale that never happened in that direction — and the fills table
    is what every audit of a disputed trade is reconstructed from.
    """
    from tbot.core.config import Config
    from tbot.core.models import OrderStatus

    config = Config()
    config.exits.scale_out_at_r = 1.0

    runner = _live_runner(config)
    runner.broker = _Venue(fill_price=140.0)
    runner.exits = ExitManager(config=config.exits)
    recorded: list[Fill] = []
    runner.db = type(
        "D",
        (),
        {
            "upsert_order": lambda self, _o: None,
            "record_fill": lambda self, f: recorded.append(f),
            "upsert_position": lambda self, _p: None,
        },
    )()

    position = Position(
        symbol="SOL/USDT:USDT",
        side=Side.SELL,
        quantity=2.0,
        entry_price=160.0,
        entry_ts=0,
        initial_stop=168.0,
        current_stop=168.0,
        setup="momentum_decline",
        risk_amount=16.0,
        meta={"original_quantity": 2.0},
    )
    runner.portfolio.positions[position.symbol] = position

    # Price has run 1R in the short's favour, which is what arms the scale-out.
    runner._maybe_scale_out(position, high=152.0, low=151.0, bar_ts=1)

    assert recorded, "the scale-out did not execute"
    assert recorded[-1].side is Side.BUY
    assert runner.broker.submitted[-1].side is Side.BUY
    assert runner.broker.submitted[-1].status is OrderStatus.FILLED


# --------------------------------------------------------------------------
# A stop the venue holds is only worth the level it currently holds
# --------------------------------------------------------------------------


def test_the_perp_trailing_stop_actually_reaches_the_venue():
    """``PerpBroker`` overrode ``place_stop`` but not ``amend_stop``.

    Its ``place_stop`` returns ``position:<symbol>`` rather than an order id,
    because the venue tracks a perpetual's stop as a property of the position.
    The inherited ``amend_stop`` called ``edit_order`` against that string, the
    venue refused, the exception was caught and failure returned — so the local
    book showed a trailed stop while the venue kept the original one for the
    life of the position. The gap opens exactly when this process is down,
    which is the only reason a venue-side stop exists (invariant 12).

    The return is the id now protecting the position, so that a caller whose
    stop had to be *replaced* rather than amended keeps a working handle to it.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    sent: list[dict] = []

    class Exchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def privatePostV5PositionTradingStop(self, params):
            sent.append(params)
            return {"retCode": 0}

    broker = PerpBroker.__new__(PerpBroker)
    broker.exchange = Exchange()
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}

    order_id = broker.place_stop("SOL/USDT:USDT", 2.0, 150.0, "cid")
    assert order_id == "position:SOLUSDT"

    # The trail ratchets the stop up. This is the call that used to be a no-op.
    assert broker.amend_stop("SOL/USDT:USDT", order_id, 155.0) == "position:SOLUSDT"
    assert sent[-1]["stopLoss"] == "155.0"
    assert sent[-1]["symbol"] == "SOLUSDT"


def test_the_venue_is_set_to_the_earned_leverage_not_the_operator_ceiling():
    """``risk.max_leverage`` is a forward authorisation, not a permission.

    Every sizing call site asks ``LeverageGate.allowed_leverage``, which stays
    at 1.0 until 90 days, 100 trades, PF >= 1.3 and drawdown < 15% are on
    record. ``build_broker`` handed the venue the raw ceiling instead, so an
    operator who had written ``max_leverage: 3.0`` in advance ran an account
    set to 3x while the engine sized as if at 1x — a position roughly 33%
    collateralised where the risk model assumes 100%, which is the liquidation
    distance invariant 57 exists to hold open.
    """
    from tbot.core.config import Config, Mode
    from tbot.engine import runner as runner_module

    config = Config()
    config.mode = Mode.LIVE
    config.execution.market_type = "linear"
    config.risk.max_leverage = 3.0

    seen: dict = {}

    class FakePerp:
        def __init__(self, *_args, **kwargs):
            seen.update(kwargs)

        def load_markets(self):
            return None

        def refresh_fees(self, _symbols):
            return None

        def assert_venue_ready(self, _symbols):
            return None

    original = runner_module.PerpBroker
    runner_module.PerpBroker = FakePerp
    try:
        secrets = type("S", (), {"credentials_for": lambda self, _v: ("k", "s")})()
        runner_module.build_broker(config, secrets, ["SOL/USDT:USDT"])
    finally:
        runner_module.PerpBroker = original

    # No promotion record exists, so nothing has been earned.
    assert seen["leverage"] == 1.0


def test_a_duplicate_id_is_not_resolved_against_a_venue_that_cannot_be_read():
    """``_balances`` returns ``None`` on a failed read, and this call site
    assumed a dict.

    The path is not exotic: a network hiccup is what makes the venue keep an
    order the engine did not record, which is what produces the duplicate id in
    the first place — so the read most likely to fail here is the one this
    depends on. It raised ``AttributeError`` out of ``submit``. Reading the
    failure as "holds nothing" would discard a real position and reading it as
    "holds it" would invent one, so the bar is skipped instead: the next one
    carries a different ``bar_ts`` and a fresh id.
    """
    from tbot.core.models import Order, OrderStatus, OrderType, Venue
    from tbot.execution.ccxt_broker import CCXTBroker

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}
    broker._find_in_history = lambda _s, _c: {
        "orderStatus": "Filled",
        "cumExecQty": "1.31",
    }
    broker._balances = lambda _kind: None

    order = Order(
        client_id="cid",
        symbol="VVV/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=1.31,
        price=None,
        created_ts=0,
        updated_ts=0,
    )
    resolved = broker._resolve_duplicate(order, 1.31, None, {})

    assert resolved.status is OrderStatus.REJECTED
    assert "balance read failed" in resolved.reject_reason


# --------------------------------------------------------------------------
# Audit #72: the spot stop amend was refused on every trail, silently
# --------------------------------------------------------------------------


def _spot_broker(exchange):
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import CCXTBroker

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = exchange
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}
    return broker


def test_the_spot_stop_amend_uses_the_shape_bybit_actually_accepts():
    """ccxt's ``edit_order`` builds a derivatives amend and adds ``triggerBy``.

    Bybit **spot** answers that with ``10001 Request parameter error`` — on
    every trail, measured. The bare v5 amend (category, symbol, orderId,
    triggerPrice) is accepted, also measured, on the first attempt.

    The test pins the request shape rather than the outcome: an implementation
    that goes back through ``edit_order`` would pass an outcome assertion
    against a fake and fail against the venue, which is precisely how this got
    to production in the first place.
    """
    sent: list[dict] = []

    class Exchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def privatePostV5OrderAmend(self, params):
            sent.append(params)
            return {"retCode": 0, "result": {"orderId": params["orderId"]}}

        def edit_order(self, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("edit_order mangles the spot amend; use v5 directly")

    broker = _spot_broker(Exchange())

    assert broker.amend_stop("VVV/USDT", "2306901319086247168", 26.455) == (
        "2306901319086247168"
    )
    assert sent == [
        {
            "category": "spot",
            "symbol": "VVVUSDT",
            "orderId": "2306901319086247168",
            "triggerPrice": "26.455",
        }
    ]


def test_a_refused_amend_falls_back_to_cancel_and_replace():
    """The docstring promised this fallback; the code never had it.

    A refused amend logged one warning and returned failure, and the caller
    ignored the result — so the venue went on holding the entry stop while
    every local view showed the trailed one.
    """
    calls: list[str] = []

    class Exchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def privatePostV5OrderAmend(self, _params):
            calls.append("amend")
            raise ccxt.BaseError("bybit 10001 Request parameter error")

        def cancel_order(self, **_kwargs):
            calls.append("cancel")
            return {}

    broker = _spot_broker(Exchange())
    broker._resting_stops = lambda _s: [{"orderId": "old", "qty": "1.31"}]
    placed: list[tuple] = []

    def place_stop(symbol, quantity, stop_price, client_id):
        placed.append((symbol, quantity, stop_price, client_id))
        return "new-order-id"

    broker.place_stop = place_stop

    assert broker.amend_stop("VVV/USDT", "old", 26.455) == "new-order-id"
    assert calls == ["amend", "cancel"]
    # The replacement covers what the cancelled stop covered, not what the
    # local book thinks the position is: a spot buy's fee comes out of the base
    # asset, so the two differ (invariant 23).
    assert placed[0][1] == 1.31


def test_a_stop_that_could_not_be_moved_is_reported_not_assumed():
    """Both calls fail. The engine must not carry on believing it is protected."""

    class Exchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def privatePostV5OrderAmend(self, _params):
            raise ccxt.BaseError("bybit 10001 Request parameter error")

        def cancel_order(self, **_kwargs):
            raise ccxt.BaseError("bybit 110001 order does not exist")

    broker = _spot_broker(Exchange())
    broker._resting_stops = lambda _s: []

    assert broker.amend_stop("VVV/USDT", "old", 26.455) is None


def test_microstructure_is_fetched_for_the_category_the_broker_trades():
    """Hardcoded ``"spot"`` made this silently useless on the perp engine.

    Two failures in one method: the wrong ticker list, and ``SOLUSDT:USDT`` as
    the key to index it by. Measured on a ten-hour run — 1213 of 1213 scan rows
    carried ``spread_bps = 0``, which reads as "perfectly tight" rather than
    "never measured", and ``universe.max_spread_bps`` therefore decided nothing
    at all.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    asked: list[dict] = []

    class Exchange:
        def publicGetV5MarketTickers(self, params):
            asked.append(params)
            return {
                "result": {
                    "list": [
                        {
                            "symbol": "SOLUSDT",
                            "bid1Price": "199.95",
                            "ask1Price": "200.05",
                            "turnover24h": "1000000",
                        }
                    ]
                }
            }

    broker = PerpBroker.__new__(PerpBroker)
    broker.exchange = Exchange()
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}

    out = broker.fetch_microstructure(["SOL/USDT:USDT"])

    assert asked == [{"category": "linear"}]
    assert out["SOL/USDT:USDT"]["spread_bps"] == pytest.approx(5.0, rel=1e-3)
    assert out["SOL/USDT:USDT"]["turnover_24h"] == 1_000_000.0


def test_an_amend_the_venue_had_nothing_to_change_is_success():
    """Bybit files "nothing to change" under the same 10001 as a real error.

    Read as a failure it sends the cancel-and-replace path down a route that
    leaves the position with no stop at all for a moment, every bar, to arrive
    back at the level it already had. Observed live on the first restart after
    the amend was fixed: all three open positions were needlessly replaced.
    """
    cancelled: list[str] = []

    class Exchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def privatePostV5OrderAmend(self, _params):
            raise ccxt.BaseError(
                'bybit {"retCode":10001,"retMsg":"The order remains unchanged as '
                'the parameters entered match the existing ones."}'
            )

        def cancel_order(self, **kwargs):  # pragma: no cover - must not be called
            cancelled.append(kwargs.get("id", ""))
            raise AssertionError("a no-op amend must not churn the resting stop")

    broker = _spot_broker(Exchange())

    assert broker.amend_stop("VVV/USDT", "resting", 26.455) == "resting"
    assert cancelled == []


def test_the_positions_own_stop_and_target_are_not_reported_as_orphans():
    """On a perpetual the stop and take-profit belong to the position.

    The venue reports them as orders with an empty ``orderLinkId``, because no
    client id was ever sent for them. Reconciliation matches on client id, so
    both came back as orphans on every start — two warnings per open position,
    for protection working exactly as designed, that no action could clear.
    Observed the first time the fast engine opened a position.
    """
    from tbot.core.models import Venue
    from tbot.execution.ccxt_broker import PerpBroker

    class Exchange:
        def fetch_open_orders(self, _symbol=None):
            return [
                {
                    "symbol": "SOL/USDT:USDT", "side": "sell", "type": "market",
                    "amount": 1.0, "price": None, "status": "open", "id": "a",
                    "timestamp": 0,
                    "info": {"orderLinkId": "", "stopOrderType": "StopLoss"},
                },
                {
                    "symbol": "SOL/USDT:USDT", "side": "sell", "type": "market",
                    "amount": 1.0, "price": None, "status": "open", "id": "b",
                    "timestamp": 0,
                    "info": {"orderLinkId": "", "stopOrderType": "TakeProfit"},
                },
                {
                    "symbol": "SOL/USDT:USDT", "side": "buy", "type": "limit",
                    "amount": 1.0, "price": 100.0, "status": "open", "id": "c",
                    "timestamp": 0,
                    "info": {"orderLinkId": "tbfa-sol-entr-1234", "stopOrderType": ""},
                },
            ]

    broker = PerpBroker.__new__(PerpBroker)
    broker.exchange = Exchange()
    broker.quote = "USDT"
    broker.venue = Venue.BYBIT_DEMO
    broker._markets_loaded = True
    broker._specs = {}

    kept = broker.fetch_open_orders()

    # The armed entry is a real order and must still be reconciled.
    assert [o.client_id for o in kept] == ["tbfa-sol-entr-1234"]


def test_a_market_order_is_looked_up_by_id_not_scanned_out_of_the_open_list():
    """An unfiltered realtime query answers with the *open* orders.

    A market order that just filled is by definition not among them, so the
    scan found nothing and the settle fell through to the requested price —
    H17's first trade was recorded at 112.09 against the 112.58 it paid.
    """
    asked: list[dict] = []

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            asked.append(params)
            if "orderId" not in params:
                return {"result": {"list": []}}
            return {"result": {"list": [{
                "orderId": params["orderId"], "orderStatus": "Filled",
                "cumExecQty": "1", "avgPrice": "112.58", "cumExecValue": "112.58",
                "cumExecFee": "0.061919", "side": "Buy",
            }]}}

    broker = _spot_broker(Exchange())
    state = broker.order_state("SOL/USDT:USDT", "dc8c794c")

    assert asked[0]["orderId"] == "dc8c794c"  # asked for by id first
    assert state["average_price"] == 112.58
    assert state["filled"] == 1.0
