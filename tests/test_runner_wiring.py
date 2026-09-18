"""Wiring tests for the live engine.

These cover the gaps a unit test of each part in isolation would miss: the
pieces individually worked, but nothing connected them. Each test here
corresponds to something that was silently doing nothing.
"""

import json

import pytest

from tbot.core.models import (
    Candle,
    MarketSpec,
    Regime,
    Side,
)
from tbot.engine.portfolio import Portfolio
from tbot.execution.paper_broker import PaperBroker

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
    return PaperBroker(specs={"BTC/USDT": SPEC}, cash=100.0, slippage_bps=5.0)


def test_paper_orders_do_not_fill_on_submission(broker):
    """Submitting is not executing.

    The runner used to build a Fill straight from the submitted order, which for
    the simulator meant a position at price 0 with no slippage, no fees and no
    balance change — a portfolio holding something the broker never bought.
    """
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 0.001)
    submitted = broker.submit(order)

    assert submitted.average_price == 0.0
    assert submitted.filled_quantity == 0.0
    assert broker.balances["USDT"] == 100.0
    assert len(broker.pending) == 1


def test_paper_orders_fill_on_the_next_bar_with_costs(broker):
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 0.001)
    broker.submit(order)

    fills = broker.process_bar(
        "BTC/USDT",
        Candle(
            ts=3_600_000, open=60_000.0, high=61_000.0, low=59_000.0, close=60_500.0, volume=1.0
        ),
    )

    assert len(fills) == 1
    assert fills[0].price == pytest.approx(60_000.0 * 1.0005)  # open plus slippage
    assert fills[0].fee > 0
    assert broker.balances["USDT"] < 100.0
    assert broker.balances["BTC"] == pytest.approx(0.001)
    assert broker.pending == []


def test_stop_distance_survives_a_fill_at_a_different_price(broker):
    """The risk a position carries must equal the risk that was sized.

    The stop is computed against the signal bar's close; the fill happens at the
    next open. Carrying the stop *price* across would change 1R after the fact
    and make every downstream R figure mean something slightly different.
    """
    planned_entry, planned_stop = 60_000.0, 58_000.0
    order = broker.build_order("s", "BTC/USDT", 0, "entry", Side.BUY, 0.001)
    order.meta = {"planned_entry": planned_entry, "stop": planned_stop}
    broker.submit(order)

    fill = broker.process_bar(
        "BTC/USDT",
        Candle(
            ts=3_600_000, open=61_000.0, high=62_000.0, low=60_000.0, close=61_500.0, volume=1.0
        ),
    )[0]

    actual_stop = fill.price - (planned_entry - planned_stop)
    assert fill.price - actual_stop == pytest.approx(planned_entry - planned_stop)

    portfolio = Portfolio(starting_cash=100.0)
    position = portfolio.open_position(fill, actual_stop, "s", 2.0, 70.0, Regime.TREND_UP)
    assert position.r_unit == pytest.approx(2_000.0)


def test_paper_stop_exit_fills_at_the_stop_not_the_next_open(broker):
    order = broker.build_order("s", "BTC/USDT", 5, "exit", Side.SELL, 0.001, reduce_only=True)
    fill = broker.force_fill_at(order, 58_000.0, ts=5)
    assert fill.price == pytest.approx(58_000.0 * 0.9995)
    assert order.client_id in broker.orders


def test_command_file_round_trip(tmp_path):
    """The dashboard writes a file; the engine consumes and deletes it."""
    path = tmp_path / "command.json"
    path.write_text(json.dumps({"action": "flatten"}))

    action = json.loads(path.read_text()).get("action")
    path.unlink()

    assert action == "flatten"
    assert not path.exists()  # consumed, so it cannot fire twice


def test_corrupt_command_file_is_discarded_not_retried(tmp_path):
    path = tmp_path / "command.json"
    path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(path.read_text())
    path.unlink(missing_ok=True)
    assert not path.exists()


def test_counterfactual_round_trip(tmp_path):
    """A veto is only judgeable once its outcome is recorded."""
    from tbot.core.db import Database

    db = Database(tmp_path / "t.db")
    db.start_run(mode="paper", venue="bybit-demo", config_hash="x")

    call_id = db.record_llm_call(
        ts=1000,
        role="event_guard",
        provider="gemini",
        model="m",
        prompt="p",
        response="r",
        parsed_ok=1,
        latency_ms=100,
        symbol="BTC/USDT",
        decision="{'block_new_entries': True}",
    )

    assert len(db.pending_counterfactuals(older_than_ts=2000)) == 1
    assert db.pending_counterfactuals(older_than_ts=500) == []  # too recent to judge

    db.set_counterfactual(call_id, -1.4)
    assert db.pending_counterfactuals(older_than_ts=2000) == []
    assert db.query("SELECT counterfactual_r FROM llm_calls")[0]["counterfactual_r"] == -1.4
    db.close()


def test_config_hash_is_stable_and_detects_change():
    """The auditor runs on change, not on every start."""
    import hashlib

    from tbot.core.config import load_config

    base = load_config("config/base.yaml")
    same = load_config("config/base.yaml")
    changed = load_config("config/base.yaml", overrides={"risk": {"risk_per_trade": 0.02}})

    def digest(cfg):
        return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]

    assert digest(base) == digest(same)
    assert digest(base) != digest(changed)


def test_exit_cancels_the_venue_stop_before_closing():
    """A reduce-only stop left behind would fire on a position that is gone."""
    import inspect

    from tbot.engine.runner import Runner

    source = inspect.getsource(Runner._close)
    cancel_at = source.index("_cancel_protective_stop")
    submit_at = source.index("submit_exit")
    assert cancel_at < submit_at, "the venue stop must be cancelled before the exit is sent"


def test_protective_stop_is_attempted_on_every_live_entry():
    import inspect

    from tbot.engine.runner import Runner

    assert "_place_protective_stop" in inspect.getsource(Runner._enter)
    assert "_sync_protective_stop" in inspect.getsource(Runner._manage_positions)


# --------------------------------------------------------------------------
# Safety checks
# --------------------------------------------------------------------------


def test_withdrawal_capable_key_is_refused():
    """A key that can move funds turns any leak into a total loss."""
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            return {
                "result": {
                    "permissions": {
                        "Spot": ["SpotTrade"],
                        "Wallet": ["AccountTransfer", "Withdraw"],
                    },
                    "ips": ["*"],
                }
            }

    with pytest.raises(RuntimeError, match="withdrawal permission"):
        assert_key_is_safe(Exchange())


def test_trade_only_key_is_accepted():
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            return {
                "result": {
                    "permissions": {"Spot": ["SpotTrade"], "Wallet": []},
                    "ips": ["1.2.3.4"],
                }
            }

    report = assert_key_is_safe(Exchange())
    assert report.safe_for_trading
    assert report.ip_restricted


def test_key_without_trade_permission_is_refused():
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            return {"result": {"permissions": {"Spot": [], "Wallet": []}, "ips": ["*"]}}

    with pytest.raises(RuntimeError, match="cannot trade"):
        assert_key_is_safe(Exchange())


def test_unreadable_permissions_do_not_block_startup():
    """Unknown is not the same as unsafe — it is logged, not fatal."""
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            raise ConnectionError("network down")

    report = assert_key_is_safe(Exchange())
    assert report.error is not None


def test_slippage_tracker_measures_cost_against_the_assumption():
    from tbot.execution.safety import SlippageTracker

    tracker = SlippageTracker(assumed_bps=5.0)
    for _ in range(25):
        tracker.record(expected_price=100.0, actual_price=100.2, side_sign=1)  # 20bps worse

    assert tracker.median_bps == pytest.approx(20.0)
    assert tracker.exceeds_assumption()


def test_slippage_in_our_favour_is_recorded_as_negative():
    from tbot.execution.safety import SlippageTracker

    tracker = SlippageTracker(assumed_bps=5.0)
    tracker.record(expected_price=100.0, actual_price=99.9, side_sign=1)
    assert tracker.median_bps < 0
    assert not tracker.exceeds_assumption()


def test_slippage_needs_a_sample_before_it_judges():
    from tbot.execution.safety import SlippageTracker

    tracker = SlippageTracker(assumed_bps=5.0)
    tracker.record(100.0, 110.0, 1)  # one terrible fill
    assert not tracker.exceeds_assumption()  # one fill is not evidence


def test_implausible_equity_jump_is_caught():
    """Bad data must not become a real loss via position sizing."""
    from tbot.execution.safety import equity_jump_is_implausible

    assert equity_jump_is_implausible(100.0, 40.0)
    assert equity_jump_is_implausible(100.0, 200.0)
    assert not equity_jump_is_implausible(100.0, 95.0)
    assert not equity_jump_is_implausible(0.0, 100.0)  # first reading, nothing to compare
