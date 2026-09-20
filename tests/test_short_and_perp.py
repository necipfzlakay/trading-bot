"""Two engines, one account — and the short side that spot could never express.

The tests here protect the boundaries that only start to matter once a second
engine shares the exchange account with the first. Each one corresponds to a
failure that is silent rather than loud: an order id collision that reads as a
successful submission, a failed balance read that reads as an empty account, a
short exit that sells again instead of buying back.
"""

from __future__ import annotations

import hashlib

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
