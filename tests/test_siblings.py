"""Capital between engines that share one Unified wallet.

Three engines, one USDT balance. Each owns a slice, and the slices have to stay
separate across restarts — otherwise one engine's losses become another's
starting capital, and a kill criterion measured on equity can never be reached.
"""

from __future__ import annotations

import json

import pytest

from tbot.core.config import load_config
from tbot.engine.siblings import (
    STATE_FRESH_MS,
    own_starting_cash,
    sibling_reservation,
)

NOW = 1_790_000_000_000


def _sibling(tmp_path, namespace: str, cap: float, state: dict | None):
    cfg = load_config(
        "config/fast.yaml",
        overrides={
            "engine": {"namespace": namespace},
            "risk": {"max_capital": cap},
            "runtime_dir": str(tmp_path / namespace),
        },
    )
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        cfg.state_path.write_text(json.dumps(state))
    return cfg


# --------------------------------------------------------------------------
# Capped engines continue from their own equity
# --------------------------------------------------------------------------


def test_a_capped_engine_that_lost_money_does_not_refill_at_restart():
    """The kill criterion depends on this.

    The seed used to be ``min(cap, free)``: an engine at $500 of a $1000
    allocation restarted at $1000, refilled from the shared wallet, and an
    equity floor of $500 was unreachable by construction.
    """
    cash = own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=1_000.0,
        last_equity=500.0, reserved_for_siblings=0.0,
    )
    assert cash == 500.0


def test_open_positions_count_inside_the_equity_not_on_top_of_it():
    """Measured: the fast engine seeded 313.94 on a 300 cap — cash capped, the
    open SOL position's 112.09 added on top of it."""
    cash = own_starting_cash(
        venue_free=1_500.0, positions_value=112.09, cap=300.0,
        last_equity=300.0, reserved_for_siblings=0.0,
    )
    assert cash + 112.09 == pytest.approx(300.0)


def test_a_capped_engine_keeps_its_own_profit_across_restarts():
    """With every engine capped, clipping at the cap would take each engine's
    profit away at every restart — and the main engine's whole edge is in the
    long winners that span restarts (invariant 51)."""
    cash = own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=1_000.0,
        last_equity=1_100.0, reserved_for_siblings=0.0,
    )
    assert cash == 1_100.0


def test_a_capped_engine_never_seeds_more_than_the_wallet_has_free():
    cash = own_starting_cash(
        venue_free=120.0, positions_value=0.0, cap=300.0,
        last_equity=300.0, reserved_for_siblings=0.0,
    )
    assert cash == 120.0


def test_a_capped_engine_on_its_first_run_starts_at_the_cap():
    assert own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=300.0,
        last_equity=None, reserved_for_siblings=0.0,
    ) == 300.0


# --------------------------------------------------------------------------
# The residual engine owns what the others do not
# --------------------------------------------------------------------------


def test_the_residual_engine_does_not_adopt_its_siblings_money():
    """After the $1300 top-up: fast owns 300, scalp 1000, main what is left."""
    assert own_starting_cash(
        venue_free=1_616.12, positions_value=0.0, cap=0.0,
        last_equity=1_021.0, reserved_for_siblings=1_300.0,
    ) == pytest.approx(316.12)


def test_a_running_sibling_reserves_its_reported_cash(tmp_path):
    fresh = {"cash": 187.9, "equity": 300.0, "updated_ts": NOW - 60_000}
    siblings = [_sibling(tmp_path, "fast", 300.0, fresh)]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert total == pytest.approx(187.9)
    assert breakdown == {"fast": 187.9}


def test_a_sibling_that_is_down_or_never_ran_reserves_its_whole_allocation(tmp_path):
    """A stopped engine still owns its money."""
    stale = {"cash": 12.0, "updated_ts": NOW - STATE_FRESH_MS - 1}
    siblings = [
        _sibling(tmp_path, "fast", 300.0, stale),
        _sibling(tmp_path, "sc", 1_000.0, None),
    ]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert breakdown == {"fast": 300.0, "sc": 1_000.0}
    assert total == 1_300.0


def test_an_uncapped_sibling_reserves_nothing_and_says_so(tmp_path):
    """Two engines each owning 'the rest' cannot both be right; it is logged,
    not silently split."""
    siblings = [_sibling(tmp_path, "other", 0.0, None)]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert total == 0.0 and breakdown == {}


# --------------------------------------------------------------------------
# A restart takes the positions out of the equity at the prices inside it
# --------------------------------------------------------------------------

SPOT = "BTC/USDT"


def _capped_runner(tmp_path, cap: float = 1_000.0):
    from tbot.core.config import Secrets
    from tbot.core.db import Database
    from tbot.core.models import MarketSpec
    from tbot.data.store import ParquetStore
    from tbot.engine.runner import Runner
    from tbot.execution.paper_broker import PaperBroker

    base = load_config("config/base.yaml")
    config = base.model_copy(
        update={
            "runtime_dir": tmp_path,
            "risk": base.risk.model_copy(update={"max_capital": cap}),
        }
    )
    spec = MarketSpec(
        symbol=SPOT, base="BTC", quote="USDT", min_notional=5.0, min_amount=1e-6,
        amount_step=1e-6, price_step=0.1, maker_fee=0.001, taker_fee=0.001,
    )
    return Runner(
        config=config,
        secrets=Secrets(),
        db=Database(tmp_path / "t.db"),
        store=ParquetStore(tmp_path / "data"),
        broker=PaperBroker(specs={SPOT: spec}, cash=5_000.0),
        symbols=[SPOT],
    )


def _long(symbol: str = SPOT, entry: float = 100.0, quantity: float = 1.0):
    from tbot.core.models import Position, Side

    return Position(
        symbol=symbol, side=Side.BUY, quantity=quantity, entry_price=entry, entry_ts=0,
        initial_stop=entry * 0.9, current_stop=entry * 0.9, setup="test",
        risk_amount=entry * quantity * 0.1,
    )


def _last_run(config, **snapshot) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(json.dumps(snapshot))


@pytest.mark.parametrize("mark", [50.0, 150.0])
def test_a_restart_counts_an_open_positions_move_once(tmp_path, mark):
    """Measured on the demo engine: three spot longs 4.50 under water at
    shutdown, seeded 314.82 instead of 319.31.

    The last equity valued the long at its mark; seeding took it back out at
    its entry. The loss landed in cash and was then counted again when the
    position was marked. A winner doubled the same way, upwards.
    """
    runner = _capped_runner(tmp_path)
    runner.portfolio.positions[SPOT] = _long(entry=100.0)
    # Last run: 900 cash + one BTC long bought at 100, marked at ``mark``.
    _last_run(runner.config, equity=900.0 + mark, cash=900.0,
              equity_marks={SPOT: mark}, peak_equity=1_000.0, starting_equity=1_000.0)

    equity = runner._seed_capital(5_000.0)

    assert runner.portfolio.cash == pytest.approx(900.0)
    assert equity == pytest.approx(900.0 + mark)
    assert runner.portfolio.equity({SPOT: mark}) == pytest.approx(900.0 + mark)


def test_a_bar_and_the_restart_after_it_agree_on_the_book(tmp_path):
    """Both halves through the engine's own code: the bar saves the prices
    its equity used, the next run takes the position out at those prices."""
    first = _capped_runner(tmp_path)
    first.portfolio.cash = 900.0
    first.portfolio.positions[SPOT] = _long(entry=100.0)
    point = first._record_bar_equity(0, {SPOT: 80.0, "ETH/USDT": 3_000.0})
    first.state.equity, first.state.cash = point.equity, point.cash  # as ``_snapshot``
    first.writer.write(first.state)

    second = _capped_runner(tmp_path)
    second.portfolio.positions[SPOT] = _long(entry=100.0)
    equity = second._seed_capital(5_000.0)

    assert second.portfolio.cash == pytest.approx(900.0)
    assert equity == pytest.approx(980.0)


def test_a_capped_engine_remembers_its_drawdown_across_a_restart(tmp_path):
    """Invariant 4 across a restart. The peak was reset to the equity of the
    moment, so an engine 12% down on 0.5x came back on 1.0x — and the capital
    freeze forgot the same way."""
    runner = _capped_runner(tmp_path)
    _last_run(runner.config, equity=880.0, cash=880.0, peak_equity=1_000.0,
              starting_equity=1_000.0)

    runner._seed_capital(5_000.0)

    assert runner.portfolio.peak_equity == pytest.approx(1_000.0)
    assert runner.risk.state.drawdown == pytest.approx(0.12)
    assert runner.risk.state.risk_multiplier == pytest.approx(0.5)
    assert runner.state.drawdown == pytest.approx(0.12)
    # Every percentage is still measured from the allocation (invariant 33).
    assert runner.portfolio.starting_cash == pytest.approx(1_000.0)


def test_a_short_restarts_on_the_same_book(tmp_path):
    """A 1x short posted its margin out of cash and owes the move."""
    from tbot.core.models import Side
    from tbot.engine.portfolio import Portfolio
    from tbot.engine.siblings import carried_book

    short = _long(symbol="SOL/USDT:USDT", entry=100.0)
    short.side = Side.SELL
    before = Portfolio(starting_cash=1_000.0, cash=900.0)
    before.positions[short.symbol] = short
    snapshot = {"equity": before.equity({short.symbol: 110.0}),
                "equity_marks": {short.symbol: 110.0}}

    after = Portfolio(starting_cash=1_000.0)
    after.positions[short.symbol] = short
    last = carried_book(snapshot)
    cash = own_starting_cash(
        venue_free=5_000.0, positions_value=after.equity(last.marks) - after.cash,
        cap=1_000.0, last_equity=last.equity, reserved_for_siblings=0.0,
    )
    assert cash == pytest.approx(900.0)


def test_a_snapshot_is_read_defensively():
    from tbot.engine.siblings import carried_book

    assert carried_book(None).equity is None
    last = carried_book({"equity": "990.5", "equity_marks": {"A": 0, "B": "x", "C": 2.5},
                         "peak_equity": None})
    assert last.equity == pytest.approx(990.5)
    assert last.marks == {"C": 2.5}  # zero and garbage are "no price", not a price
    assert last.peak is None


class _VenueClosedIt:
    """A venue on which the position's own stop filled while we were down."""

    def __new__(cls, specs, price: float, fee: float):
        from tbot.execution.ccxt_broker import CCXTBroker

        class Broker(CCXTBroker):
            def __init__(self):  # noqa: D107 — no network, no credentials
                self.venue, self.quote = None, "USDT"

            def fetch_positions(self):
                return {}

            def fetch_open_orders(self):
                return []

            def order_state(self, symbol, order_id):
                return {"status": "Filled", "filled": 1.0, "average_price": price, "fee": fee,
                        "terminal": True, "filled_terminal": True}

            def market_spec(self, symbol):
                return specs.market_spec(symbol)

        return Broker()


def test_a_stop_that_filled_while_down_is_booked_not_dropped(tmp_path):
    """Startup reconciliation used to drop a position the venue had closed: no
    trade row, and the book settled at the last mark rather than the fill.
    What fills while nobody watches is mostly stops, so every statistic read
    better than the account. Now it is the venue's fill, in the trades and in
    the book alike (invariant 76)."""
    runner = _capped_runner(tmp_path)
    position = _long(entry=100.0)
    position.meta["stop_order_id"] = "1"
    runner.db.upsert_position(position)
    runner.broker = _VenueClosedIt(runner.broker, price=85.0, fee=0.085)
    _last_run(runner.config, equity=995.0, cash=900.0, equity_marks={SPOT: 95.0},
              peak_equity=1_000.0, starting_equity=1_000.0)

    from tbot.engine.siblings import carried_book

    carried = carried_book(runner.writer.read())
    realised = runner._reconcile(carried.marks)
    equity = runner._seed_capital(5_000.0, carried, realised_since=realised)

    (trade,) = runner.db.query("SELECT exit_price, pnl, exit_reason FROM trades")
    assert trade["exit_price"] == pytest.approx(85.0)
    assert trade["exit_reason"] == "initial_stop"
    assert equity == pytest.approx(900.0 + 85.0 - 0.085)
    assert equity == pytest.approx(1_000.0 + trade["pnl"])
    assert SPOT not in runner.portfolio.positions


def test_a_trailed_short_stop_is_labelled_trailing(tmp_path):
    """A short's stop trails *down*. ``current <= initial`` called every one
    of them the initial stop, and the exit-reason split misread the shorts."""
    from tbot.core.models import Position, Side

    runner = _capped_runner(tmp_path)
    runner.portfolio.positions[SPOT] = Position(
        symbol=SPOT, side=Side.SELL, quantity=1.0, entry_price=100.0, entry_ts=0,
        initial_stop=110.0, current_stop=104.0, setup="test", risk_amount=10.0,
    )
    runner._close_from_venue_fill(SPOT, 104.0, fee=0.1)
    (trade,) = runner.db.query("SELECT exit_reason FROM trades")
    assert trade["exit_reason"] == "trailing_stop"
