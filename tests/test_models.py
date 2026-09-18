"""Data-contract invariants — especially R arithmetic and excursion tracking."""

import pytest

from tbot.core.models import (
    BookSnapshot,
    Candle,
    Conviction,
    ConvictionComponent,
    DerivativeSnapshot,
    ExitReason,
    IndicatorCategory,
    IndicatorSignal,
    NoTradeReason,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Regime,
    Side,
    Trade,
)


def test_side_helpers():
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1


def test_order_status_terminality():
    assert OrderStatus.FILLED.is_terminal
    assert OrderStatus.REJECTED.is_terminal
    assert not OrderStatus.OPEN.is_terminal
    assert not OrderStatus.PARTIALLY_FILLED.is_terminal


def test_indicator_signal_rejects_unnormalised_value():
    IndicatorSignal(name="rsi", category=IndicatorCategory.MOMENTUM, timeframe="4h", value=1.0)
    with pytest.raises(ValueError, match="outside"):
        IndicatorSignal(name="rsi", category=IndicatorCategory.MOMENTUM, timeframe="4h", value=1.5)


def test_candle_geometry():
    c = Candle(ts=0, open=100.0, high=110.0, low=95.0, close=105.0, volume=10.0)
    assert c.range == 15.0
    assert c.body == 5.0
    assert c.is_bullish


def test_book_snapshot_metrics():
    b = BookSnapshot(ts=0, bid=99.95, ask=100.05, bid_volume=30.0, ask_volume=10.0)
    assert b.mid == pytest.approx(100.0)
    assert b.spread_bps == pytest.approx(10.0)
    assert b.imbalance == pytest.approx(0.5)


def test_derivative_cvd_delta_is_none_without_taker_data():
    assert DerivativeSnapshot(ts=0, funding_rate=0.0001).cvd_delta is None
    assert DerivativeSnapshot(ts=0, taker_buy_volume=7.0, taker_sell_volume=4.0).cvd_delta == 3.0


def _long_position() -> Position:
    # entry 100, stop 90 -> 1R = 10 price units
    return Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=1.0,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="donchian",
        risk_amount=10.0,
    )


def test_position_r_unit_and_unrealized():
    pos = _long_position()
    assert pos.r_unit == 10.0
    assert pos.unrealized_pnl(120.0) == 20.0
    assert pos.unrealized_r(120.0) == 2.0
    assert pos.unrealized_r(90.0) == -1.0


def test_short_position_r_signs_invert():
    pos = Position(
        symbol="ETH/USDT",
        side=Side.SELL,
        quantity=2.0,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=110.0,
        current_stop=110.0,
        setup="fade",
        risk_amount=20.0,
    )
    assert pos.r_unit == 10.0
    assert pos.unrealized_pnl(90.0) == 20.0  # price fell, short profits
    assert pos.unrealized_r(90.0) == 1.0


def test_excursion_tracking_long():
    pos = _long_position()
    pos.update_excursion(high=115.0, low=97.0)
    pos.update_excursion(high=108.0, low=93.0)
    assert pos.max_favorable_price == 115.0  # best ever seen, not most recent
    assert pos.max_adverse_price == 93.0
    assert pos.mfe_r == pytest.approx(1.5)
    assert pos.mae_r == pytest.approx(-0.7)


def test_excursion_tracking_short_uses_opposite_extremes():
    pos = Position(
        symbol="ETH/USDT",
        side=Side.SELL,
        quantity=1.0,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=110.0,
        current_stop=110.0,
        setup="fade",
        risk_amount=10.0,
    )
    pos.update_excursion(high=104.0, low=88.0)
    assert pos.max_favorable_price == 88.0  # for a short, favourable is lower
    assert pos.max_adverse_price == 104.0
    assert pos.mfe_r == pytest.approx(1.2)
    assert pos.mae_r == pytest.approx(-0.4)


def test_mae_is_never_positive():
    pos = _long_position()
    pos.update_excursion(high=130.0, low=101.0)  # never traded below entry
    assert pos.mae_r <= 0.0


def test_trade_capture_ratio_flags_early_exits():
    t = Trade(
        symbol="BTC/USDT",
        side=Side.BUY,
        setup="donchian",
        entry_ts=0,
        exit_ts=1,
        entry_price=100.0,
        exit_price=112.0,
        quantity=1.0,
        pnl=12.0,
        fees=0.2,
        r_multiple=1.2,
        mfe_r=2.5,
        mae_r=-0.4,
        bars_held=9,
        exit_reason=ExitReason.TRAILING_STOP,
    )
    assert t.is_win
    assert t.capture_ratio == pytest.approx(0.48)  # captured under half the move


def test_conviction_pass_requires_threshold_and_no_veto():
    conv = Conviction(
        symbol="BTC/USDT",
        bar_ts=0,
        side=Side.BUY,
        total=72.0,
        threshold=68.0,
        regime=Regime.TREND_UP,
        blocks_voting=4,
    )
    assert conv.passed
    assert conv.margin == pytest.approx(4.0)

    conv.no_trade_reason = NoTradeReason.CORRELATION_FULL
    assert not conv.passed  # score was enough, portfolio state said no

    below = Conviction(
        symbol="BTC/USDT",
        bar_ts=0,
        side=Side.BUY,
        total=60.0,
        threshold=68.0,
    )
    assert not below.passed
    assert below.margin == pytest.approx(-8.0)


def test_conviction_components_retain_signals():
    comp = ConvictionComponent(
        name="indicator_confluence",
        score=18.0,
        max_score=25.0,
        signals=[
            IndicatorSignal(
                name="ema_stack", category=IndicatorCategory.TREND, timeframe="4h", value=0.8
            )
        ],
    )
    conv = Conviction(
        symbol="BTC/USDT",
        bar_ts=0,
        side=Side.BUY,
        total=70.0,
        threshold=68.0,
        components=[comp],
    )
    assert conv.components[0].signals[0].name == "ema_stack"


def test_order_remaining_never_negative():
    o = Order(
        client_id="x",
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        filled_quantity=1.0,
    )
    assert o.remaining == 0.0
    o.filled_quantity = 1.2  # exchange over-reporting must not go negative
    assert o.remaining == 0.0
