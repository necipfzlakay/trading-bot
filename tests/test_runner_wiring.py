"""Wiring tests for the live engine.

These cover the gaps a unit test of each part in isolation would miss: the
pieces individually worked, but nothing connected them. Each test here
corresponds to something that was silently doing nothing.
"""

import json
import time

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


def test_key_expiry_accepts_every_shape_bybit_sends():
    """Bybit returns expiredAt as ISO-8601, epoch seconds or epoch millis.

    This was a real outage: the ISO form raised ValueError outside the API
    call's try block, so assert_key_is_safe crashed and demo/live could never
    start — under launchd that is a restart loop, not a single failure.
    """
    from tbot.execution.safety import _parse_expiry

    iso = _parse_expiry("2026-12-19T06:53:56Z")
    assert iso is not None
    assert _parse_expiry("1766127236") == 1766127236 * 1000  # seconds
    assert _parse_expiry("1766127236000") == 1766127236000  # milliseconds
    assert _parse_expiry(None) is None
    assert _parse_expiry("") is None
    assert _parse_expiry("0") is None


def test_unparseable_expiry_does_not_break_the_permission_check():
    """The expiry is informational; the withdrawal check is not."""
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            return {
                "result": {
                    "permissions": {"Spot": ["SpotTrade"], "Wallet": []},
                    "ips": ["1.2.3.4"],
                    "expiredAt": "whenever",
                }
            }

    report = assert_key_is_safe(Exchange())
    assert report.safe_for_trading
    assert report.expires_ts is None


def test_expired_key_is_refused():
    """An expired key means rejected orders and unmanaged open positions."""
    from tbot.execution.safety import assert_key_is_safe

    class Exchange:
        def privateGetV5UserQueryApi(self):
            return {
                "result": {
                    "permissions": {"Spot": ["SpotTrade"], "Wallet": []},
                    "ips": ["1.2.3.4"],
                    "expiredAt": "2020-01-01T00:00:00Z",
                }
            }

    with pytest.raises(RuntimeError, match="expired"):
        assert_key_is_safe(Exchange())


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


# --------------------------------------------------------------------------
# Live balance comes from the venue, never from config
# --------------------------------------------------------------------------


def test_portfolio_seeded_from_venue_replaces_configured_balance():
    """Invariant 19, enforced.

    This was a live failure: the portfolio kept starting_balance (1000) while
    the venue held 50000. Sizing then refused entries for lack of cash that
    existed, and the first bar's equity contradicted the startup reading hard
    enough for the implausible-jump guard to degrade the engine.
    """
    from tbot.engine.portfolio import Portfolio

    portfolio = Portfolio(starting_cash=1000.0)
    assert portfolio.cash == 1000.0

    portfolio.seed_from_venue(50000.0)

    assert portfolio.cash == 50000.0
    assert portfolio.starting_cash == 50000.0
    assert portfolio.peak_equity == 50000.0
    # No drawdown may be implied by adopting the real balance.
    point = portfolio.record_equity(0, {})
    assert point.equity == 50000.0
    assert point.drawdown == 0.0


def test_seeding_counts_positions_carried_in_from_reconciliation():
    from tbot.core.models import Position, Side
    from tbot.engine.portfolio import Portfolio

    portfolio = Portfolio(starting_cash=1000.0)
    portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.1,
        entry_price=50_000.0,
        entry_ts=0,
        initial_stop=45_000.0,
        current_stop=45_000.0,
        setup="test",
        risk_amount=500.0,
    )

    portfolio.seed_from_venue(2_000.0)

    # 2000 free cash plus a position worth 5000 at its entry price.
    assert portfolio.cash == 2_000.0
    assert portfolio.starting_cash == pytest.approx(7_000.0)


def test_logging_attaches_the_file_handler_after_an_import_time_call(tmp_path):
    """Import-time get_logger() must not permanently disable the JSONL log.

    Every module calls get_logger() at import, which configures logging with no
    log_dir. When that first call was treated as final, bot.jsonl was never
    written and the engine's only queryable log silently did not exist.
    """
    import logging as std_logging

    from tbot.core import logging as tlog

    tlog.get_logger("import-time")  # what a module-level call does
    tlog.configure(tmp_path)  # what the CLI does afterwards
    tlog.get_logger("after").info("event_written", value=1)
    std_logging.shutdown()

    path = tmp_path / "bot.jsonl"
    assert path.exists(), "configure(log_dir) after an import-time call must attach a file handler"
    assert "event_written" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Protective stops survive spot fee accounting
# --------------------------------------------------------------------------


def test_stop_quantity_is_clamped_to_what_is_actually_sellable():
    """Bybit spot charges the buy fee in the base asset.

    A filled buy for 65.69 leaves 65.62 in the account, so a stop for 65.69 is
    rejected with "Insufficient balance" and the position ends up with no venue
    stop at all. Observed live on demo; invariant 12 depends on this.
    """
    from tbot.execution.ccxt_broker import CCXTBroker

    placed = {}

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            return {"result": {"list": []}}

        def fetch_balance(self):
            return {"free": {"VVV": 65.62431}}

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.5f}"

        def price_to_precision(self, symbol, price):
            return f"{price:.4f}"

        def create_order(self, **kwargs):
            placed.update(kwargs)
            return {"id": "stop-1"}

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    order_id = broker.place_stop("VVV/USDT", 65.69, 23.35, "cid")

    assert order_id == "stop-1"
    assert placed["amount"] <= 65.62431, "must never ask to sell more than is held"


def test_stop_is_not_attempted_when_nothing_is_sellable():
    from tbot.execution.ccxt_broker import CCXTBroker

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            return {"result": {"list": []}}

        def fetch_balance(self):
            return {"free": {}}

        def create_order(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("must not send an order for zero quantity")

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    assert broker.place_stop("VVV/USDT", 65.69, 23.35, "cid") is None


def test_unknown_balance_does_not_block_protecting_the_position():
    """Unknown is not zero — let the venue decide rather than skip the stop."""
    import ccxt

    from tbot.execution.ccxt_broker import CCXTBroker

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            return {"result": {"list": []}}

        def fetch_balance(self):
            raise ccxt.NetworkError("down")

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.5f}"

        def price_to_precision(self, symbol, price):
            return f"{price:.4f}"

        def create_order(self, **kwargs):
            return {"id": "stop-2"}

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    assert broker.place_stop("VVV/USDT", 65.69, 23.35, "cid") == "stop-2"


def test_a_position_locked_by_its_own_stop_is_still_a_position():
    """A protective stop reserves the base asset, so free drops to dust.

    Reading free here made the engine conclude its own stop had closed the
    position: it dropped VVV from the portfolio, stopped managing a holding it
    still owned, and understated equity by the position's whole value.
    """
    from tbot.execution.ccxt_broker import CCXTBroker

    class Exchange:
        def fetch_balance(self):
            return {
                "free": {"USDT": 48300.93, "VVV": 0.00431},
                "used": {"VVV": 65.62},
                "total": {"USDT": 48300.93, "VVV": 65.62431},
            }

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.quote = "USDT"

    positions = broker.fetch_positions()
    assert positions["VVV/USDT"] == pytest.approx(65.62431)

    # Cash stays the spendable figure — locked funds cannot be sized against.
    assert broker.fetch_balance()["USDT"] == pytest.approx(48300.93)


def test_an_existing_stop_at_the_right_level_is_adopted_not_replaced():
    """Cancel-then-place lost real protection when the placement failed.

    Observed live: a working stop was cancelled, the replacement was refused
    with "Duplicate clientOrderId" (the id is deterministic), and the position
    was left with no stop at all.
    """
    from tbot.execution.ccxt_broker import CCXTBroker

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            return {"result": {"list": [{"orderId": "live-stop", "qty": "65.62", "triggerPrice": "23.352"}]}}

        def cancel_order(self, id, symbol):  # pragma: no cover - must not be reached
            raise AssertionError("must not cancel a stop that already protects the position")

        def create_order(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("must not place a second stop")

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    assert broker.place_stop("VVV/USDT", 65.62431, 23.352, "cid") == "live-stop"


def test_retrying_a_stop_does_not_reuse_a_refused_client_id():
    """Bybit remembers cancelled ids and refuses them with 170141."""
    from tbot.execution.ccxt_broker import CCXTBroker

    seen = []

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            return {"result": {"list": []}}

        def fetch_balance(self):
            return {"free": {"VVV": 65.62431}}

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.5f}"

        def price_to_precision(self, symbol, price):
            return f"{price:.4f}"

        def create_order(self, **kwargs):
            seen.append(kwargs["params"]["clientOrderId"])
            return {"id": f"stop-{len(seen)}"}

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    broker.place_stop("VVV/USDT", 65.62431, 23.35, "cid")
    time.sleep(0.002)
    broker.place_stop("VVV/USDT", 65.62431, 23.35, "cid")

    assert seen[0] != seen[1], "a retry must not reuse an id the venue already refused"


def test_replacing_a_stop_at_a_different_level_releases_the_old_one():
    """Otherwise the clamp sees only dust and the replacement protects nothing."""
    from tbot.execution.ccxt_broker import CCXTBroker

    cancelled, placed = [], {}
    state = {"free": 0.00431}  # everything is locked by the resting stop

    class Exchange:
        def privateGetV5OrderRealtime(self, params):
            # Resting at a level the new stop does not match, so it must go.
            resting = [{"orderId": "old-stop", "qty": "65.62", "triggerPrice": "19.00"}]
            return {"result": {"list": [] if cancelled else resting}}

        def cancel_order(self, id, symbol):
            cancelled.append(id)
            state["free"] = 65.62431  # the venue releases the reservation
            return {}

        def fetch_balance(self):
            return {"free": {"VVV": state["free"]}}

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.5f}"

        def price_to_precision(self, symbol, price):
            return f"{price:.4f}"

        def create_order(self, **kwargs):
            placed.update(kwargs)
            return {"id": "new-stop"}

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = Exchange()
    broker.load_markets = lambda: None

    assert broker.place_stop("VVV/USDT", 65.62431, 23.35, "cid") == "new-stop"
    assert cancelled == ["old-stop"]
    assert placed["amount"] == pytest.approx(65.62431), "must protect the whole holding"


def test_balance_that_changed_while_the_engine_was_down_is_reported(tmp_path):
    """The bar-to-bar guard cannot see across a restart.

    Observed: the account went from 50063 to 1763 between runs and the engine
    adopted the new figure without a word, because startup treats the venue
    balance as the baseline. Across a restart no market move explains a jump
    like that — it is a withdrawal, a manual trade, or the wrong account.
    """
    from tbot.engine.runner import Runner
    from tbot.engine.state import EngineState, StateWriter

    writer = StateWriter(tmp_path / "state.json")
    writer.write(EngineState(mode="live", venue="bybit-demo", status="running", equity=50063.15))

    recorded = []

    class DB:
        def record_event(self, kind, message, level="info"):
            recorded.append((kind, level, message))

    runner = Runner.__new__(Runner)
    runner.writer = writer
    runner.db = DB()
    runner.notifier = None

    runner._check_balance_against_last_run(1763.15)
    assert recorded and recorded[0][0] == "balance_changed_while_down"
    assert recorded[0][1] == "error"

    # A normal session's worth of movement must stay quiet, or the alert is noise.
    recorded.clear()
    runner._check_balance_against_last_run(50063.15 * 0.95)
    assert not recorded
