"""The one-minute maker scalper (H18): signal, stream, and the order lifecycle.

Each test pins one clause of the design against a fake venue whose answers are
the ones the demo account actually gave (research/scalp_venue_facts.md).
"""

from __future__ import annotations

import time

import pytest

from tbot.core.config import ScalpConfig, load_config
from tbot.core.models import MarketSpec, NoTradeReason, Order, OrderStatus, Side
from tbot.engine.claims import SymbolClaims
from tbot.execution.broker import Broker
from tbot.scalp.signal import arm_for, evaluate, exit_levels, required_bars
from tbot.scalp.stream import MINUTE_MS, KlineStream

SOL = "SOL/USDT:USDT"
SPEC = MarketSpec(
    symbol=SOL, base="SOL", quote="USDT", min_notional=5.0, min_amount=0.1,
    amount_step=0.1, price_step=0.01, maker_fee=0.0001, taker_fee=0.0006,
)


# --------------------------------------------------------------------------
# Signal
# --------------------------------------------------------------------------


def _quiet(n: int = 80, start: float = 100.0) -> list[float]:
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] * (1 + (0.0004 if i % 2 else -0.0004)))
    return out


def test_a_quiet_market_triggers_nothing():
    assert evaluate(SOL, _quiet(), 0, ScalpConfig()) is None


def test_a_sharp_move_triggers_and_the_arm_sets_the_side():
    closes = _quiet()
    closes[-5:] = [closes[-6] * (1 + 0.002 * i) for i in range(1, 6)]  # +1% in 5 bars
    config = ScalpConfig()
    for bar_ts in range(40):
        intent = evaluate(SOL, closes, bar_ts, config)
        assert intent is not None
        expected = Side.BUY if intent.arm == "momentum" else Side.SELL
        assert intent.side is expected
        assert intent.setup == f"scalp_{intent.arm}"


def test_warming_up_is_no_opinion():
    """Fewer bars than the window: None, not a neutral vote (invariant 10)."""
    config = ScalpConfig()
    assert evaluate(SOL, _quiet(required_bars(config) - 1), 0, config) is None


def test_arm_assignment_is_deterministic_across_processes_and_balanced():
    """``hash()`` is salted per process; the backtester and the live engine
    would disagree about which arm an event belongs to."""
    arms = ["momentum", "reversion"]
    assert arm_for(SOL, 123, arms) == arm_for(SOL, 123, list(arms))
    share = sum(arm_for(SOL, t, arms) == "momentum" for t in range(20_000)) / 20_000
    assert 0.48 < share < 0.52


def test_exit_levels_sit_on_the_right_side_of_the_entry():
    config = ScalpConfig()
    tp, sl = exit_levels(Side.BUY, 100.0, config)
    assert tp > 100.0 > sl
    tp, sl = exit_levels(Side.SELL, 100.0, config)
    assert tp < 100.0 < sl


# --------------------------------------------------------------------------
# Stream: only closed bars leave it
# --------------------------------------------------------------------------


def test_the_forming_bar_never_reaches_the_signal():
    """Invariant 1. A bar is handed over only once a later minute has begun."""
    stream = KlineStream([SOL])
    stream.seed(SOL, [[0, 1, 1, 1, 1, 1]])
    stream.ingest(SOL, [MINUTE_MS, 2, 2, 2, 2, 1])
    stream.ingest(SOL, [MINUTE_MS, 2, 2, 2, 2.5, 1])
    assert stream.closes(SOL) == [1.0] and stream.drain() == []

    stream.ingest(SOL, [2 * MINUTE_MS, 3, 3, 3, 3, 1])
    assert stream.closes(SOL) == [1.0, 2.5]  # closed with its final price
    assert stream.drain() == [SOL]


def test_a_missing_minute_is_reported_for_refill():
    stream = KlineStream([SOL])
    stream.seed(SOL, [[0, 1, 1, 1, 1, 1]])
    stream.ingest(SOL, [3 * MINUTE_MS, 3, 3, 3, 3, 1])
    stream.ingest(SOL, [4 * MINUTE_MS, 4, 4, 4, 4, 1])
    assert stream.take_gaps() == {SOL}


# --------------------------------------------------------------------------
# Order lifecycle against a fake venue
# --------------------------------------------------------------------------


class FakeVenue:
    """Answers the way the demo account answered the probe."""

    def __init__(self):
        self.placed: list[tuple[Order, float, float]] = []
        self.cancelled: list[str] = []
        self.submitted: list[Order] = []
        self.order_states: dict[str, dict | None] = {}
        self.positions: dict[str, float] = {}
        self.exit_fill: dict | None = None
        self.book = (99.99, 100.01)
        self.reject_next: str | None = None

    build_order = Broker.build_order

    def market_spec(self, _symbol):
        return SPEC

    def best_bid_ask(self, _symbol):
        return self.book

    def place_maker_entry(self, order, take_profit, stop_loss):
        if self.reject_next:
            order.status, order.reject_reason = OrderStatus.REJECTED, self.reject_next
            return order
        order.exchange_id = f"oid-{len(self.placed)}"
        order.status = OrderStatus.OPEN
        self.placed.append((order, take_profit, stop_loss))
        self.order_states[order.exchange_id] = {
            "status": "New", "filled": 0.0, "terminal": False, "filled_terminal": False,
        }
        return order

    def cancel_order(self, _symbol, order_id):
        self.cancelled.append(order_id)
        return True

    def order_state(self, _symbol, order_id):
        return self.order_states.get(order_id)

    def fetch_positions(self):
        return dict(self.positions)

    def recent_reduce_fill(self, _symbol, since, _side):
        # The real lookup drops a closing fill stamped before ``since`` as a
        # previous position's. A fake that ignored it hid audit #92.
        if self.exit_fill and int(self.exit_fill.get("updated_ms") or since) < since:
            return None
        return self.exit_fill

    def submit(self, order):
        self.submitted.append(order)
        order.status = OrderStatus.REJECTED
        order.reject_reason = "110017 current position is zero"
        # A refusal that still carries numbers — the shape that let the bar
        # engine book a phantom sale (audit #81). The status alone must decide.
        order.average_price, order.filled_quantity = 100.0, order.quantity
        return order


class FakeDatabase:
    def __init__(self):
        self.events: list[str] = []
        self.trades: list = []
        self.fills: list = []
        self.scans: list = []

    def order_exists(self, _cid):
        return False

    def upsert_order(self, _o):
        pass

    def record_fill(self, f):
        self.fills.append(f)

    def upsert_position(self, _p):
        pass

    def delete_position(self, _s):
        pass

    def record_trade(self, t):
        self.trades.append(t)

    def record_event(self, name, *_a, **_k):
        self.events.append(name)

    def record_scans(self, rows):
        self.scans.extend(rows)


@pytest.fixture
def engine(tmp_path):
    from tbot.scalp.engine import ScalpEngine

    config = load_config(
        "config/scalp.yaml",
        overrides={
            "runtime_dir": str(tmp_path / "scalp"),
            "engine": {"claims_path": str(tmp_path / "claims.sqlite")},
        },
    )
    venue, db = FakeVenue(), FakeDatabase()
    eng = ScalpEngine(config=config, secrets=None, db=db, broker=venue, symbols=[SOL])
    eng.portfolio.seed_from_venue(1_000.0)
    eng.stream._connected, eng.stream._last_message = True, time.time()
    return eng, venue, db


def _intent(side=Side.BUY, arm="momentum"):
    from tbot.scalp.signal import Intent

    return Intent(SOL, 1_790_000_000_000, arm, side, 0.01, 0.004)


def test_an_entry_is_claimed_then_sent_as_a_postonly_carrying_its_own_protection(engine):
    eng, venue, _db = engine
    assert eng._try_enter(_intent()) is None
    (order, tp, sl), = venue.placed
    assert order.price == 99.99  # joins the best bid: it can only ever rest
    assert tp > order.price > sl
    assert eng.claims.owner_of(SOL) == "sc"


def test_a_symbol_the_other_engine_holds_is_left_alone(engine):
    eng, venue, _db = engine
    SymbolClaims(eng.config.engine.claims_path, "fast").claim(SOL)
    assert eng._try_enter(_intent()) is NoTradeReason.CLAIMED_BY_OTHER_ENGINE
    assert venue.placed == []


def test_an_unfilled_entry_keeps_its_claim_until_the_cancel_is_confirmed(engine):
    """Invariant 41: a cancel the venue has not confirmed is not a cancel."""
    eng, venue, _db = engine
    eng._try_enter(_intent())
    eng.pending[SOL].placed -= eng.scalp.entry_ttl_seconds + 1

    eng._poll_venue()
    assert venue.cancelled == ["oid-0"]
    assert eng.claims.owner_of(SOL) == "sc"  # not released on the request alone

    venue.order_states["oid-0"] = {
        "status": "Cancelled", "filled": 0.0, "terminal": True, "filled_terminal": False,
        "reason": "EC_PostOnlyWillTakeLiquidity",
    }
    eng._poll_venue()
    assert SOL not in eng.pending
    assert eng.claims.owner_of(SOL) is None
    # The book says it ended, and why — not "open" forever.
    order = venue.placed[0][0]
    assert order.status is OrderStatus.CANCELED
    assert order.reject_reason == "EC_PostOnlyWillTakeLiquidity"


def test_a_fill_and_its_venue_exit_are_booked_on_the_venues_terms(engine):
    eng, venue, db = engine
    eng._try_enter(_intent())
    venue.order_states["oid-0"] = {
        "status": "Filled", "filled": 1.2, "average_price": 99.99, "fee": 0.012,
        "terminal": True, "filled_terminal": True,
    }
    venue.positions = {SOL: 1.2}
    eng._poll_venue()
    position = eng.portfolio.positions[SOL]
    assert position.entry_price == 99.99 and position.fees_paid == pytest.approx(0.012)

    venue.positions = {}
    venue.exit_fill = {"average_price": 100.29, "fee": 0.012, "kind": "take_profit",
                       "filled": 1.2, "filled_terminal": True}
    eng._poll_venue()
    (trade,) = db.trades
    assert trade.exit_price == pytest.approx(100.29)
    assert trade.exit_reason.value == "take_profit"
    assert eng.claims.owner_of(SOL) is None  # flat at the venue: free again


def test_a_target_that_fills_before_the_poll_sees_the_entry_is_still_found(engine):
    """Audit #92: the attached target lives from the fill, not from our poll.

    Entry filled at T, target at T+1 s, the engine's 3 s poll noticed both at
    once. ``opened_ms`` was the moment of noticing, so the target's fill was
    older than the position and filtered out as a previous one's; five polls
    later the exit was booked as a stop at whatever the last price was.
    """
    from tbot.core.clock import now_ms

    eng, venue, db = engine
    eng._try_enter(_intent())
    filled_at = now_ms() - 3_000
    venue.order_states["oid-0"] = {
        "status": "Filled", "filled": 1.2, "average_price": 99.99, "fee": 0.012,
        "terminal": True, "filled_terminal": True, "updated_ms": filled_at,
    }
    venue.positions = {}  # the target already took it: flat by the first poll
    venue.exit_fill = {"average_price": 100.29, "fee": 0.012, "kind": "take_profit",
                       "filled": 1.2, "filled_terminal": True, "updated_ms": filled_at + 1_000}
    eng._poll_venue()

    (trade,) = db.trades
    assert trade.exit_reason.value == "take_profit"
    assert trade.exit_price == pytest.approx(100.29)


def test_without_a_venue_time_the_position_opens_at_submission_not_at_noticing(engine):
    """The fallback must still precede the attached legs — and not be the
    signal bar, which can precede the previous position's close."""
    from tbot.core.clock import now_ms

    eng, venue, _db = engine
    before = now_ms()
    eng._try_enter(_intent())
    submitted = eng.pending[SOL].placed_ms
    assert before <= submitted <= now_ms()
    venue.order_states["oid-0"] = {
        "status": "Filled", "filled": 1.2, "average_price": 99.99, "fee": 0.012,
        "terminal": True, "filled_terminal": True,
    }
    venue.positions = {SOL: 1.2}
    eng._poll_venue()
    assert eng.portfolio.positions[SOL].meta["opened_ms"] == submitted


def test_a_refused_time_stop_is_never_booked_as_an_exit(engine):
    """The venue's own stop or target got there first; the poll books that."""
    eng, venue, db = engine
    eng._try_enter(_intent())
    venue.order_states["oid-0"] = {
        "status": "Filled", "filled": 1.2, "average_price": 99.99, "fee": 0.012,
        "terminal": True, "filled_terminal": True,
    }
    venue.positions = {SOL: 1.2}
    eng._poll_venue()

    eng._time_stop(SOL, 1_790_000_900_000)
    assert db.trades == [] and SOL in eng.portfolio.positions


def _filled_and_held(eng, venue):
    eng._try_enter(_intent())
    venue.order_states["oid-0"] = {
        "status": "Filled", "filled": 1.2, "average_price": 99.99, "fee": 0.012,
        "terminal": True, "filled_terminal": True,
    }
    venue.positions = {SOL: 1.2}
    eng._poll_venue()
    assert SOL in eng.portfolio.positions


def test_a_lagging_history_is_waited_out_not_guessed(engine):
    """Measured at the 00:00 UTC funding settlement: a TAO stop filled at
    318.79 was still missing from the order history 15 s later — five 3 s
    polls — and was booked at the last price, 319.37: -1.1R written as -0.49R.
    The venue is already flat; waiting costs nothing, a guess is permanent."""
    eng, venue, db = engine
    _filled_and_held(eng, venue)

    venue.positions = {}  # the stop filled; the history has not caught up
    for _ in range(8):  # longer than the old five-poll limit
        eng._poll_venue()
    assert db.trades == [] and "exit_terms_unknown" not in db.events

    venue.exit_fill = {"average_price": 99.39, "fee": 0.07, "kind": "stop",
                       "filled": 1.2, "filled_terminal": True}
    eng._poll_venue()
    (trade,) = db.trades
    assert trade.exit_price == pytest.approx(99.39)
    assert trade.exit_reason.value == "initial_stop"


def test_a_history_that_never_answers_is_booked_after_a_minute_and_flagged(engine):
    eng, venue, db = engine
    _filled_and_held(eng, venue)

    venue.positions = {}
    eng._poll_venue()
    assert db.trades == []
    eng._flat_since[SOL] -= 61.0  # a minute of real time, not a count of polls
    eng._poll_venue()

    (trade,) = db.trades
    assert trade.exit_reason.value == "initial_stop"
    assert SOL not in eng.portfolio.positions


def test_a_restart_carries_the_book_at_the_prices_its_equity_used(engine, tmp_path):
    """The scalper's half of the seeding fix: the snapshot saves the marks, the
    next start takes the position out at them — not at its entry."""
    from tbot.engine.portfolio import Portfolio

    eng, venue, _db = engine
    _filled_and_held(eng, venue)
    eng.stream.ingest(SOL, [1_790_000_000_000, 99.0, 99.0, 98.0, 98.5, 1.0])
    eng._snapshot()
    cash_before = eng.portfolio.cash
    assert eng.state.equity_marks == {SOL: 98.5}

    held = eng.portfolio.positions[SOL]
    eng.portfolio = Portfolio(starting_cash=eng.config.risk.max_capital)
    eng.portfolio.positions[SOL] = held  # what reconciliation restores
    venue.fetch_balance = lambda: {"USDT": 5_000.0}
    eng._seed_capital()

    assert eng.portfolio.cash == pytest.approx(cash_before)
    assert eng.portfolio.equity({SOL: 98.5}) == pytest.approx(eng.state.equity)


def test_the_entry_rate_is_capped(engine):
    eng, _venue, _db = engine
    now = time.monotonic()
    eng._entries.extend([now] * eng.scalp.max_entries_per_minute)
    assert eng._try_enter(_intent()) is NoTradeReason.THROTTLED


def test_a_run_of_refusals_cools_the_engine_down(engine):
    eng, venue, db = engine
    venue.reject_next = "10001 params error"
    for _ in range(eng.scalp.reject_streak_limit):
        assert eng._try_enter(_intent()) is NoTradeReason.INSUFFICIENT_BALANCE
    assert "rejection_cooldown" in db.events
    venue.reject_next = None
    assert eng._try_enter(_intent()) is NoTradeReason.THROTTLED
    assert eng.claims.owner_of(SOL) is None  # every refused entry gave its claim back


def test_a_wide_book_is_not_traded(engine):
    eng, venue, _db = engine
    venue.book = (99.0, 101.0)  # 200bps
    assert eng._try_enter(_intent()) is NoTradeReason.SPREAD_TOO_WIDE


def test_below_the_kill_equity_nothing_new_is_opened(engine):
    """The pre-registered H18 kill criterion — with nothing open.

    Realised losses and every position closed is exactly when the floor is
    reached. The check used to sit after the poll's early return for "no open
    positions", so in that state it never ran.
    """
    eng, venue, db = engine
    eng.portfolio.cash = eng.scalp.kill_equity - 1
    assert not eng.portfolio.positions and not eng.pending

    eng._poll_venue()

    assert "kill_equity_reached" in db.events
    assert eng.state.status == "killed"
    assert eng._try_enter(_intent()) is NoTradeReason.RISK_BUDGET_EXHAUSTED
    assert venue.placed == []


# --------------------------------------------------------------------------
# Backtest: the same signal, and a fill model that never flatters
# --------------------------------------------------------------------------


def _frame(closes, highs=None, lows=None):
    import pandas as pd

    n = len(closes)
    return pd.DataFrame(
        {
            "ts": [i * MINUTE_MS for i in range(n)],
            "open": closes,
            "high": highs or [c * 1.0001 for c in closes],
            "low": lows or [c * 0.9999 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
        }
    )


def _spike_series():
    closes = _quiet(120)
    base = closes[89]
    closes[90:95] = [base * (1 + 0.002 * i) for i in range(1, 6)]  # trigger at 94
    tail = closes[94]
    closes[95:] = [tail] * (120 - 95)
    return closes


def test_every_backtest_trade_is_one_the_live_signal_would_take(tmp_path):
    """Invariant 59: the vectorised pass only proposes; ``evaluate`` decides."""
    from tbot.scalp.backtest import candidates

    config = load_config("config/scalp.yaml").scalp
    closes = _spike_series()
    import numpy as np

    proposed = set(candidates(np.array(closes), config).tolist())
    live = {
        i for i in range(len(closes))
        if evaluate(SOL, closes[: i + 1], i * MINUTE_MS, config) is not None
    }
    assert live <= proposed  # a superset: nothing the live function takes is missed


def test_an_entry_the_next_bar_only_touches_does_not_fill():
    """A maker order at the touch may be behind the queue: trade-through only."""
    from tbot.scalp.backtest import run

    config = load_config("config/scalp.yaml")
    closes = _spike_series()
    highs = [c * 1.0001 for c in closes]
    lows = [c * 0.9999 for c in closes]
    # From the trigger on, every bar trades exactly at its close and never
    # through it — so no entry placed at a close can fill, including the
    # re-triggers that follow while the 5-bar window still contains the spike.
    for i in range(94, len(closes)):
        highs[i] = lows[i] = closes[i]
    result = run(config, {SOL: _frame(closes, highs, lows)})
    assert result.trades == []
    assert sum(result.unfilled.values()) >= 1


def test_a_bar_that_reaches_both_levels_is_a_stop():
    """Invariant 40, in the scalper's own simulator."""
    from tbot.scalp.backtest import _exit_after_fill

    config = load_config("config/scalp.yaml").scalp
    closes = [100.0] * 20
    highs = [100.05] * 20
    lows = [99.95] * 20
    highs[3], lows[3] = 101.0, 99.0  # bar 3 spans both the target and the stop
    index, price, reason = _exit_after_fill(Side.BUY, 100.0, 1, highs, lows, closes, config)
    assert (index, reason) == (3, "stop")
    assert price == pytest.approx(100.0 * (1 - config.stop_loss_pct))


def test_the_target_is_never_taken_in_the_fill_bar():
    """Which came first inside the fill bar is unknowable; the kind reading is refused."""
    from tbot.scalp.backtest import _exit_after_fill

    config = load_config("config/scalp.yaml").scalp
    closes = [100.0] * 20
    highs = [100.05] * 20
    lows = [99.95] * 20
    highs[1] = 101.0  # the fill bar alone reaches the target
    _index, _price, reason = _exit_after_fill(Side.BUY, 100.0, 1, highs, lows, closes, config)
    assert reason != "take_profit"


def test_a_contract_the_account_may_not_trade_is_dropped_not_counted_against_the_engine(engine):
    """Measured live: XAU answered ``110123 You must agree to the Trading Terms``
    on every entry, and each refusal fed the engine-wide streak — one
    untradeable contract was walking the whole engine into cool-down."""
    eng, venue, db = engine
    venue.reject_next = 'bybit {"retCode":110123,"retMsg":"You must agree to the Trading Terms"}'

    assert eng._try_enter(_intent()) is NoTradeReason.INSUFFICIENT_BALANCE
    assert SOL in eng._dropped
    assert eng._reject_streak == 0
    assert "symbol_dropped" in db.events

    # The next trigger on it is never evaluated, so no order is ever sent.
    venue.reject_next = None
    eng.stream.seed(SOL, [[i * MINUTE_MS, 1, 1, 1, 1, 1] for i in range(80)])
    eng.stream.ingest(SOL, [80 * MINUTE_MS, 1, 1, 1, 1.05, 1])
    eng.stream.ingest(SOL, [81 * MINUTE_MS, 1, 1, 1, 1.05, 1])
    for symbol in eng.stream.drain():
        eng._on_closed_bar(symbol)
    assert venue.placed == []


@pytest.mark.parametrize(
    "reason",
    [
        'bybit {"retCode":110123,"retMsg":"You must agree to the Trading Terms before trading this contract."}',
        'bybit {"retCode":110125,"retMsg":"You must agree to the Crude Oil Trading Terms before trading this contract."}',
        'bybit {"retCode":110126,"retMsg":"You must sign the required agreement before trading this contract."}',
    ],
)
def test_every_agreement_refusal_family_drops_the_symbol(engine, reason):
    """Measured live: XAU, CL and SOXL each answered with a different code.
    Recognising only the first let CL and SOXL count against the whole engine."""
    eng, venue, _db = engine
    venue.reject_next = reason
    eng._try_enter(_intent())
    assert SOL in eng._dropped
    assert eng._reject_streak == 0
