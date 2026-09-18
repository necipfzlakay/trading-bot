"""Execution: pessimistic fills, idempotent orders, and honest reconciliation."""

import pytest

from tbot.core.models import (
    Candle,
    ExitReason,
    MarketSpec,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Regime,
    Side,
)
from tbot.engine.portfolio import Portfolio
from tbot.execution.broker import client_order_id
from tbot.execution.paper_broker import PaperBroker
from tbot.execution.reconcile import apply, reconcile

SPEC = MarketSpec(
    symbol="BTC/USDT",
    base="BTC",
    quote="USDT",
    min_notional=5.0,
    min_amount=1e-6,
    amount_step=1e-6,
    price_step=0.1,
    maker_fee=0.001,
    taker_fee=0.001,
)


@pytest.fixture
def broker() -> PaperBroker:
    return PaperBroker(specs={"BTC/USDT": SPEC}, cash=1000.0, slippage_bps=10.0)


def _candle(ts=0, o=100.0, h=105.0, low=95.0, c=102.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=low, close=c, volume=10.0)


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_client_order_id_is_deterministic_and_within_the_venue_limit():
    a = client_order_id("donchian", "BTC/USDT", 1_700_000_000_000, "entry")
    b = client_order_id("donchian", "BTC/USDT", 1_700_000_000_000, "entry")
    c = client_order_id("donchian", "BTC/USDT", 1_700_000_003_600, "entry")
    assert a == b
    assert a != c
    assert len(a) <= 36  # Bybit's cap


def test_resubmitting_the_same_order_does_not_create_a_second(broker):
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    first = broker.submit(order)
    second = broker.submit(broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0))
    assert first.client_id == second.client_id
    assert len(broker.pending) == 1


# --------------------------------------------------------------------------
# Fill realism
# --------------------------------------------------------------------------


def test_market_orders_fill_at_the_next_bar_open_not_the_signal_close(broker):
    """The close is not a price anyone could trade at once it is known."""
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    broker.submit(order)
    fills = broker.process_bar("BTC/USDT", _candle(ts=1, o=100.0, c=140.0))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(100.0 * 1.001)  # the open, plus slippage


def test_slippage_always_moves_against_the_order(broker):
    buy = broker.build_order("s", "BTC/USDT", 0, "buy", Side.BUY, 1.0)
    broker.submit(buy)
    buy_fill = broker.process_bar("BTC/USDT", _candle(ts=1))[0]

    sell = broker.build_order("s", "BTC/USDT", 2, "sell", Side.SELL, 1.0)
    broker.submit(sell)
    sell_fill = broker.process_bar("BTC/USDT", _candle(ts=3))[0]

    assert buy_fill.price > 100.0
    assert sell_fill.price < 100.0


def test_limit_order_does_not_fill_at_a_price_the_market_never_reached(broker):
    order = broker.build_order(
        "s", "BTC/USDT", 0, "entry", Side.BUY, 1.0, OrderType.LIMIT, price=50.0
    )
    broker.submit(order)
    assert broker.process_bar("BTC/USDT", _candle(ts=1, low=95.0)) == []
    assert broker.process_bar("BTC/USDT", _candle(ts=2, low=45.0))  # now it traded there


def test_fees_are_charged_on_every_fill(broker):
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    broker.submit(order)
    fill = broker.process_bar("BTC/USDT", _candle(ts=1))[0]
    assert fill.fee == pytest.approx(fill.price * 1.0 * SPEC.taker_fee)


def test_balances_move_on_both_sides_of_a_fill(broker):
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    broker.submit(order)
    broker.process_bar("BTC/USDT", _candle(ts=1))
    assert broker.balances["BTC"] == pytest.approx(1.0)
    assert broker.balances["USDT"] < 1000.0


def test_stop_exits_fill_at_the_stop_not_the_next_open(broker):
    """A stop is a resting instruction; deferring it would understate losses."""
    order = broker.build_order("s", "BTC/USDT", 5, "exit", Side.SELL, 1.0, reduce_only=True)
    fill = broker.force_fill_at(order, 90.0, ts=5)
    assert fill.price == pytest.approx(90.0 * 0.999)  # stop price, plus slippage against us


def test_cancel_removes_a_pending_order(broker):
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    broker.submit(order)
    assert broker.cancel(order.client_id)
    assert broker.process_bar("BTC/USDT", _candle(ts=1)) == []
    assert not broker.cancel(order.client_id)  # already terminal


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


def test_portfolio_round_trip_produces_a_correct_r_multiple(broker):
    portfolio = Portfolio(starting_cash=1000.0)
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 1.0)
    broker.submit(order)
    fill = broker.process_bar("BTC/USDT", _candle(ts=1, o=100.0))[0]

    # entry ~100.1, stop 90 -> 1R is about 10.1
    portfolio.open_position(fill, 90.0, "donchian", 10.0, 72.0, Regime.TREND_UP)
    trade = portfolio.close_position("BTC/USDT", 120.0, 2, ExitReason.TRAILING_STOP, fee=0.12)

    assert trade is not None
    assert trade.r_multiple == pytest.approx((120.0 - fill.price) / (fill.price - 90.0), rel=1e-6)
    assert trade.pnl > 0
    assert portfolio.positions == {}


def test_excursion_is_carried_into_the_closed_trade():
    portfolio = Portfolio(starting_cash=1000.0)
    from tbot.core.models import Fill

    fill = Fill("o1", "BTC/USDT", Side.BUY, 1.0, 100.0, 0.1, 0)
    portfolio.open_position(fill, 90.0, "s", 10.0, 70.0, Regime.TREND_UP)
    portfolio.update_bar("BTC/USDT", high=125.0, low=97.0)
    portfolio.update_bar("BTC/USDT", high=110.0, low=93.0)

    trade = portfolio.close_position("BTC/USDT", 108.0, 2, ExitReason.TRAILING_STOP)
    assert trade.mfe_r == pytest.approx(2.5)  # best was 125 = +2.5R
    assert trade.mae_r == pytest.approx(-0.7)  # worst was 93 = -0.7R
    assert trade.bars_held == 2


def test_portfolio_summary_reports_profit_factor_and_expectancy():
    portfolio = Portfolio(starting_cash=1000.0)
    from tbot.core.models import Fill

    for i, exit_price in enumerate((120.0, 95.0, 130.0)):
        fill = Fill(f"o{i}", "BTC/USDT", Side.BUY, 1.0, 100.0, 0.0, i)
        portfolio.open_position(fill, 90.0, "s", 10.0, 70.0, Regime.TREND_UP)
        portfolio.close_position("BTC/USDT", exit_price, i + 1, ExitReason.TRAILING_STOP)

    summary = portfolio.summary()
    assert summary["trades"] == 3
    assert summary["wins"] == 2
    assert summary["profit_factor"] > 1


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def _position(symbol="BTC/USDT", qty=1.0) -> Position:
    return Position(
        symbol=symbol,
        side=Side.BUY,
        quantity=qty,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="s",
        risk_amount=10.0,
    )


def test_reconcile_reports_a_clean_match():
    report = reconcile({"BTC/USDT": _position()}, {"BTC/USDT": 1.0}, [], set())
    assert report.clean
    assert report.matched == ["BTC/USDT"]


def test_position_closed_while_offline_is_detected():
    report = reconcile({"BTC/USDT": _position()}, {}, [], set())
    assert not report.clean
    assert report.missing_remotely == ["BTC/USDT"]


def test_unexpected_venue_position_is_flagged_not_adopted():
    """The bot has no stop or thesis for a position it did not open."""
    report = reconcile({}, {"ETH/USDT": 5.0}, [], set())
    assert report.missing_locally == ["ETH/USDT"]

    updated = apply(report, {}, {"ETH/USDT": 5.0})
    assert "ETH/USDT" not in updated  # surfaced for a human, not managed blindly


def test_quantity_mismatch_resolves_in_favour_of_the_venue():
    local = {"BTC/USDT": _position(qty=1.0)}
    report = reconcile(local, {"BTC/USDT": 0.6}, [], set())
    assert report.quantity_mismatch

    updated = apply(report, local, {"BTC/USDT": 0.6})
    assert updated["BTC/USDT"].quantity == pytest.approx(0.6)


def test_dust_balances_are_not_treated_as_positions():
    report = reconcile({}, {"BTC/USDT": 1e-12}, [], set())
    assert report.clean


def test_orphan_open_orders_are_reported():
    order = Order(
        client_id="unknown-id",
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        status=OrderStatus.OPEN,
    )
    report = reconcile({}, {}, [order], known_client_ids=set())
    assert report.orphan_orders == ["unknown-id"]
