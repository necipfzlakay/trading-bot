"""The daily-close rebalancing engines: H19 carry, H20 trend, H21 xsmom."""

from __future__ import annotations

import math

import pytest

from tbot.core.config import load_config
from tbot.core.db import Database
from tbot.core.models import MarketSpec, NoTradeReason, OrderStatus, Side
from tbot.engine.portfolio import Portfolio
from tbot.execution.broker import Broker
from tbot.rebalance import backtest as bt
from tbot.rebalance.engine import RebalanceEngine, close_weekday, perp_of
from tbot.rebalance.signals import (
    DAY_MS,
    carry_wants,
    funding_sum,
    momentum,
    trend_wants,
    xsmom_wants,
)

# 20003 days after the epoch is a Monday 00:00 UTC: the weekly boundary.
MONDAY = 20_003 * DAY_MS
FEE = 0.001


def spec(symbol: str) -> MarketSpec:
    base = symbol.split("/")[0]
    return MarketSpec(symbol=symbol, base=base, quote="USDT", min_notional=5.0,
                      min_amount=1e-6, amount_step=1e-6, price_step=0.01,
                      maker_fee=FEE, taker_fee=FEE)


class FakeExchange:
    def __init__(self):
        self.candles: dict[str, list[list[float]]] = {}
        self.funding: dict[str, list[dict]] = {}

    def fetch_ohlcv(self, symbol, _tf, since=None, limit=None):
        return self.candles.get(symbol, [])

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        return self.funding.get(symbol, [])

    def amount_to_precision(self, _symbol, quantity):
        return str(math.floor(quantity * 1000) / 1000)


class FakeVenue:
    """A spot or perp venue that fills market orders at the set price."""

    build_order = Broker.build_order

    def __init__(self, *, perp: bool = False):
        self.perp = perp
        self.exchange = FakeExchange()
        self.prices: dict[str, float] = {}
        self.holdings: dict[str, float] = {}
        self.refuse: set[str] = set()
        self.submitted: list = []
        self.settlements: dict[str, list[tuple[int, float, str]]] = {}

    def load_markets(self):
        pass

    def market_spec(self, symbol):
        return spec(symbol)

    def fetch_ticker(self, symbol):
        return {"last": self.prices.get(symbol)}

    def fetch_balance(self):
        return {"USDT": 50_000.0}

    def fetch_positions(self):
        return {s: abs(q) for s, q in self.holdings.items() if abs(q) > 0}

    def _sellable(self, symbol, wanted):
        return min(wanted, self.holdings.get(symbol, 0.0))

    def funding_settlements(self, symbol, since):
        return [r for r in self.settlements.get(symbol, []) if r[0] > since]

    def submit(self, order):
        self.submitted.append(order)
        if order.symbol in self.refuse:
            order.status, order.reject_reason = OrderStatus.REJECTED, "refused"
            return order
        price = self.prices[order.symbol]
        order.status = OrderStatus.FILLED
        order.average_price, order.filled_quantity = price, order.quantity
        order.fee_paid = order.quantity * price * FEE
        signed = order.quantity * order.side.sign
        if not self.perp and order.side is Side.BUY:
            signed *= 1 - FEE  # spot buys pay in the base asset (invariant 23)
        self.holdings[order.symbol] = self.holdings.get(order.symbol, 0.0) + signed
        return order


def candles(closes: list[float], close_ms: int) -> list[list[float]]:
    """Daily candles whose last bar closes at ``close_ms``, plus the forming one."""
    first = close_ms - len(closes) * DAY_MS
    rows = [[first + i * DAY_MS, c, c, c, c, 1.0] for i, c in enumerate(closes)]
    rows.append([close_ms, closes[-1], closes[-1], closes[-1], 999_999.0, 1.0])  # forming
    return rows


def engine(tmp_path, name: str, symbols: list[str] | None = None, monkeypatch=None):
    config = load_config(
        f"config/{name}.yaml",
        overrides={
            "runtime_dir": str(tmp_path / name),
            "engine": {"claims_path": str(tmp_path / "claims.sqlite")},
        },
    )
    spot = FakeVenue()
    perp = FakeVenue(perp=True) if name == "carry" else None
    eng = RebalanceEngine(
        config=config, secrets=None, db=Database(tmp_path / f"{name}.db"),
        broker=spot, symbols=symbols or list(config.universe.symbols), perp=perp,
    )
    eng.portfolio = Portfolio(starting_cash=1_000.0)
    return eng, spot, perp


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------


def test_trend_has_no_opinion_without_history():
    """Invariant 10: too little history is "no opinion", never "flat"."""
    assert trend_wants([100.0] * 10, 20) is None
    assert trend_wants([100.0] * 19 + [120.0], 20) is True
    assert trend_wants([100.0] * 19 + [80.0], 20) is False


def test_momentum_needs_one_more_close_than_its_lookback():
    assert momentum([100.0] * 28, 28) is None
    assert momentum([100.0] + [0.0] * 27 + [110.0], 28) == pytest.approx(0.10)


def test_xsmom_buys_only_positive_names_and_keeps_its_buffer():
    up = {f"S{i}": [100.0] * 28 + [100.0 + 10 - i] for i in range(10)}  # S0 best
    wanted = xsmom_wants(up, set(), lookback_days=28, top_k=4, hold_rank=8)
    assert wanted == ["S0", "S1", "S2", "S3"]
    # S6 is held at rank 7: inside the buffer, so it stays and takes a slot.
    wanted = xsmom_wants(up, {"S6"}, lookback_days=28, top_k=4, hold_rank=8)
    assert wanted == ["S6", "S0", "S1", "S2"]
    # A flat name (0% return) is never bought, and a held one is sold.
    up["FLAT"] = [100.0] * 29
    assert "FLAT" not in xsmom_wants(up, {"FLAT"}, lookback_days=28, top_k=4, hold_rank=20)
    down = {s: [100.0] * 28 + [90.0] for s in up}
    assert xsmom_wants(down, set(), lookback_days=28, top_k=4, hold_rank=8) == []


def test_carry_enters_above_the_bar_and_leaves_only_below_the_floor():
    sums = {"A": 0.003, "B": 0.001, "C": 0.0025, "D": None}
    assert carry_wants(sums, set(), enter_sum=0.0021, exit_sum=0.0, max_pairs=4) == ["A", "C"]
    # B is held at 0.1%: below the entry bar, above the exit floor — kept.
    assert "B" in carry_wants(sums, {"B"}, enter_sum=0.0021, exit_sum=0.0, max_pairs=4)
    sums["B"] = -0.0001
    assert "B" not in carry_wants(sums, {"B"}, enter_sum=0.0021, exit_sum=0.0, max_pairs=4)
    assert carry_wants(sums, set(), enter_sum=0.0021, exit_sum=0.0, max_pairs=1) == ["A"]


def test_funding_sum_reads_only_the_window_and_only_what_settled():
    now = 10 * DAY_MS
    rows = [(now - 8 * DAY_MS, 1.0), (now - 3 * DAY_MS, 0.001), (now, 0.002), (now + 1, 5.0)]
    assert funding_sum(rows, now, 7) == pytest.approx(0.003)
    assert funding_sum([], now, 7) is None


# --------------------------------------------------------------------------
# when to decide
# --------------------------------------------------------------------------


def test_weekly_engines_decide_at_the_monday_close_only(tmp_path):
    eng, _spot, _ = engine(tmp_path, "trend")
    assert close_weekday(MONDAY) == 0
    assert eng._due_close(MONDAY + 30_000) is None, "the venue gets its grace period"
    assert eng._due_close(MONDAY + 120_000) == MONDAY
    assert eng._due_close(MONDAY + DAY_MS + 120_000) is None, "Tuesday is not a decision"
    eng._last_decision = MONDAY
    assert eng._due_close(MONDAY + 120_000) is None, "a close is decided once"


def test_carry_decides_every_day(tmp_path):
    eng, _spot, _ = engine(tmp_path, "carry", ["BTC/USDT"])
    assert eng._due_close(MONDAY + DAY_MS + 120_000) == MONDAY + DAY_MS


# --------------------------------------------------------------------------
# trend: one name, the whole life of a position
# --------------------------------------------------------------------------


def test_trend_buys_above_its_average_and_sells_below_it(tmp_path):
    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    rising = [100.0 + i for i in range(150)]
    falling = [300.0 - i for i in range(150)]
    spot.exchange.candles = {"BTC/USDT": candles(rising, MONDAY),
                             "ETH/USDT": candles(falling, MONDAY)}
    spot.prices = {"BTC/USDT": 250.0, "ETH/USDT": 151.0}

    assert eng._decide(MONDAY, stale=False)
    assert set(eng.portfolio.positions) == {"BTC/USDT"}
    position = eng.portfolio.positions["BTC/USDT"]
    # Half the allocation: two names, two equal slots.
    assert position.quantity * 250.0 == pytest.approx(500.0, rel=0.01)
    assert eng.claims.owner_of("BTC/USDT") == "tr"
    scans = {r["symbol"]: r["no_trade_reason"] for r in eng.db.query("SELECT * FROM scans")}
    assert scans == {"BTC/USDT": "entered", "ETH/USDT": "below_threshold"}

    week = MONDAY + 7 * DAY_MS
    spot.exchange.candles["BTC/USDT"] = candles(rising[7:] + [100.0] * 7, week)
    spot.exchange.candles["ETH/USDT"] = candles(falling[7:] + [140.0] * 7, week)
    spot.prices["BTC/USDT"] = 100.0
    assert eng._decide(week, stale=False)
    assert eng.portfolio.positions == {}
    (trade,) = eng.db.query("SELECT exit_reason, pnl, fees FROM trades")
    assert trade["exit_reason"] == "signal_exit"
    assert eng.claims.owner_of("BTC/USDT") is None, "flat means released"
    # Invariant 76: the book and the trades agree.
    assert eng._equity() == pytest.approx(1_000.0 + trade["pnl"])


def test_a_coin_another_engine_holds_is_left_alone(tmp_path):
    """Invariant 81: one engine per coin on the shared spot wallet."""
    from tbot.engine.claims import SymbolClaims

    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    SymbolClaims(eng.config.engine.claims_path, "main").claim("BTC/USDT")
    spot.exchange.candles = {
        "BTC/USDT": candles([100.0 + i for i in range(150)], MONDAY),
        "ETH/USDT": candles([100.0 + i for i in range(150)], MONDAY),
    }
    spot.prices = {"BTC/USDT": 250.0, "ETH/USDT": 250.0}
    eng._decide(MONDAY, stale=False)
    assert set(eng.portfolio.positions) == {"ETH/USDT"}
    assert all(o.symbol != "BTC/USDT" for o in spot.submitted)
    reasons = {r["symbol"]: r["no_trade_reason"] for r in eng.db.query("SELECT * FROM scans")}
    assert reasons["BTC/USDT"] == "claimed_by_other_engine"


def test_a_stale_decision_exits_but_never_enters(tmp_path):
    """Invariant 77 at this engine's timescale: the backtest fills at the next
    open, and an entry hours later is a different trade. An exit is still the
    rule's answer."""
    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    spot.exchange.candles = {
        "BTC/USDT": candles([100.0 + i for i in range(150)], MONDAY),
        "ETH/USDT": candles([100.0 + i for i in range(150)], MONDAY),
    }
    spot.prices = {"BTC/USDT": 250.0, "ETH/USDT": 250.0}
    eng._decide(MONDAY, stale=True)
    assert eng.portfolio.positions == {}
    reasons = {r["symbol"]: r["no_trade_reason"] for r in eng.db.query("SELECT * FROM scans")}
    assert set(reasons.values()) == {NoTradeReason.STALE_BAR.value}


def test_a_series_missing_its_last_bar_is_not_decided_on(tmp_path):
    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    # The venue has not published Sunday's bar yet: the series ends a day early.
    late = candles([100.0 + i for i in range(150)], MONDAY - DAY_MS)[:-1]
    spot.exchange.candles = {"BTC/USDT": late, "ETH/USDT": late}
    assert eng._decide(MONDAY, stale=False) is False, "retried, not decided on old data"


# --------------------------------------------------------------------------
# carry: two legs, one position
# --------------------------------------------------------------------------


def _carry_ready(eng, spot, perp, rate=0.001):
    spot.prices["BTC/USDT"] = 100.0
    perp.prices[perp_of("BTC/USDT")] = 100.0
    settled = [{"timestamp": MONDAY - k * 8 * 3600_000, "fundingRate": rate} for k in range(21)]
    perp.exchange.funding = {perp_of("BTC/USDT"): settled}


def test_carry_hedges_what_the_spot_leg_actually_holds(tmp_path):
    eng, spot, perp = engine(tmp_path, "carry", ["BTC/USDT"])
    _carry_ready(eng, spot, perp)
    assert eng._decide(MONDAY, stale=False)

    long, short = eng.portfolio.positions["BTC/USDT"], eng.portfolio.positions["BTC/USDT:USDT"]
    assert short.side is Side.SELL and long.side is Side.BUY
    # The spot fee came out of the base asset; the short matches what is held.
    assert short.quantity <= spot.holdings["BTC/USDT"] <= long.quantity
    assert spot.holdings["BTC/USDT"] - short.quantity < 0.002
    # One leg is an eighth of the allocation: four pairs, two legs each.
    assert long.quantity * 100.0 == pytest.approx(125.0, rel=0.02)
    assert eng.claims.owner_of("BTC/USDT") == eng.claims.owner_of("BTC/USDT:USDT") == "ca"


def test_a_refused_hedge_unwinds_the_spot_leg(tmp_path):
    """An unhedged carry pair is a directional bet nobody registered."""
    eng, spot, perp = engine(tmp_path, "carry", ["BTC/USDT"])
    _carry_ready(eng, spot, perp)
    perp.refuse.add("BTC/USDT:USDT")
    eng._decide(MONDAY, stale=False)
    assert eng.portfolio.positions == {}
    assert spot.holdings["BTC/USDT"] == pytest.approx(0.0, abs=1e-9)
    assert eng.claims.owner_of("BTC/USDT") is None
    assert eng.claims.owner_of("BTC/USDT:USDT") is None
    events = [e["kind"] for e in eng.db.query("SELECT kind FROM events")]
    assert "carry_hedge_failed" in events


def test_funding_reaches_the_cash_and_the_trade(tmp_path):
    """Invariant 76 with income: equity minus the start is the sum of the
    trades, funding included — it is banked on the short and folded into its
    trade when the pair closes."""
    eng, spot, perp = engine(tmp_path, "carry", ["BTC/USDT"])
    _carry_ready(eng, spot, perp)
    eng._decide(MONDAY, stale=False)
    opened = int(eng.portfolio.positions["BTC/USDT:USDT"].meta["opened_ms"])
    perp.settlements["BTC/USDT:USDT"] = [(opened + 1, 0.12, "a"), (opened + 2, 0.13, "b")]
    eng._book_funding()
    eng._book_funding()  # the cursor moved: nothing is booked twice
    assert eng.portfolio.positions["BTC/USDT:USDT"].meta["income"] == pytest.approx(0.25)

    day = MONDAY + DAY_MS
    perp.exchange.funding[perp_of("BTC/USDT")] = [
        {"timestamp": day - k * 8 * 3600_000, "fundingRate": -0.001} for k in range(21)
    ]
    eng._decide(day, stale=False)
    assert eng.portfolio.positions == {}
    trades = eng.db.query("SELECT symbol, pnl FROM trades")
    assert len(trades) == 2
    short_pnl = next(t["pnl"] for t in trades if t["symbol"] == "BTC/USDT:USDT")
    assert short_pnl > 0.25 - 0.3, "the funding is in the short's pnl, net of its fees"
    assert eng._equity() == pytest.approx(1_000.0 + sum(t["pnl"] for t in trades))


# --------------------------------------------------------------------------
# restarts and the venue
# --------------------------------------------------------------------------


def test_a_restart_keeps_the_previous_book(tmp_path, monkeypatch):
    """Invariant 79 for this engine: the previous state is read before
    reconciliation can write a snapshot over it."""
    from tbot.rebalance import engine as module

    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    eng.state.equity, eng.state.peak_equity, eng.state.starting_equity = 812.0, 1_040.0, 1_000.0
    eng.state.last_bar_ts, eng.state.bars_processed = MONDAY, 3
    eng.writer.write(eng.state)

    monkeypatch.setattr(module, "assert_key_is_safe", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_loop", lambda: None)
    monkeypatch.setattr(eng, "_reconcile", lambda *_: eng._snapshot() or 0.0)
    eng.portfolio = Portfolio(starting_cash=1_000.0)
    eng.start()
    assert eng.portfolio.cash == pytest.approx(812.0)
    assert eng.portfolio.peak_equity == pytest.approx(1_040.0)
    assert eng._last_decision == MONDAY, "the same close is not decided twice"


def test_a_position_gone_from_the_venue_is_booked_after_two_readings(tmp_path):
    eng, spot, _ = engine(tmp_path, "trend", ["BTC/USDT", "ETH/USDT"])
    spot.exchange.candles = {
        "BTC/USDT": candles([100.0 + i for i in range(150)], MONDAY),
        "ETH/USDT": candles([300.0 - i for i in range(150)], MONDAY),
    }
    spot.prices = {"BTC/USDT": 250.0, "ETH/USDT": 151.0}
    eng._decide(MONDAY, stale=False)
    spot.holdings["BTC/USDT"] = 0.0
    eng._check_venue()
    assert "BTC/USDT" in eng.portfolio.positions, "one reading is not evidence"
    eng._check_venue()
    assert eng.portfolio.positions == {}
    (trade,) = eng.db.query("SELECT exit_reason FROM trades")
    assert trade["exit_reason"] == "manual_flatten"


def test_spot_engines_must_all_claim(tmp_path):
    """Invariant 81: one spot engine without claims makes everyone's blind."""
    from tbot.engine.siblings import assert_claims_consistent

    trend = load_config("config/trend.yaml")
    demo = load_config("config/demo.yaml")
    assert_claims_consistent(trend, [demo])  # both claim: fine
    demo.engine.symbol_claims = False
    with pytest.raises(RuntimeError, match="spot"):
        assert_claims_consistent(trend, [demo])
    carry = load_config("config/carry.yaml")
    fast = load_config("config/fast.yaml")
    fast.engine.symbol_claims = False
    with pytest.raises(RuntimeError, match="linear"):
        assert_claims_consistent(carry, [fast])


# --------------------------------------------------------------------------
# the replay
# --------------------------------------------------------------------------


def test_the_replay_fills_at_the_next_open_and_pays_both_sides():
    days = [MONDAY + i * DAY_MS for i in range(4)]
    bars = bt.Bars(days=days, open={"A": [10.0, 20.0, 20.0, 20.0]},
                   close={"A": [10.0, 20.0, 20.0, 20.0]})
    result = bt.replay_holdings(
        bars, lambda i, _held: ["A"] if i == 0 else [], slots=1, cost=0.01,
    )
    (trade,) = result.trades
    # Bought at day 1's open (20), sold at day 2's open (20): only the costs.
    assert trade["return"] == pytest.approx(0.99 / 1.01 - 1.0)
    assert result.equity[-1][1] == pytest.approx(1_000.0 * 0.99 / 1.01)
