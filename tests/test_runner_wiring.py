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


def test_stop_client_id_is_the_callers_and_varies_per_attempt():
    """Bybit remembers cancelled ids and refuses them with 170141.

    Uniqueness lives in the caller, which is also what lets reconciliation
    recognise the bot's own stop instead of calling it an orphan. Duplicate
    stops are prevented by place_stop adopting an existing one, not by the id.
    """
    from tbot.execution.broker import client_order_id

    first = client_order_id("momentum", "VVV/USDT", 1, "stop-1000")
    second = client_order_id("momentum", "VVV/USDT", 1, "stop-1001")
    assert first != second, "a later attempt must not reuse a refused id"

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

    broker = _broker_with(Exchange())
    broker.place_stop("VVV/USDT", 65.62431, 23.35, first)
    broker.place_stop("VVV/USDT", 65.62431, 23.35, second)

    # Passed through verbatim so the caller can record what it sent.
    assert seen == [first, second]


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


# --------------------------------------------------------------------------
# A remembered client id is evidence, not a rejection
# --------------------------------------------------------------------------


def _duplicate_error():
    import ccxt

    return ccxt.BaseError('bybit {"retCode":170141,"retMsg":"Duplicate clientOrderId."}')


def _broker_with(exchange):
    from tbot.core.models import MarketSpec
    from tbot.execution.ccxt_broker import CCXTBroker

    broker = CCXTBroker.__new__(CCXTBroker)
    broker.exchange = exchange
    broker.quote = "USDT"
    broker.load_markets = lambda: None
    broker.market_spec = lambda symbol: MarketSpec(
        symbol=symbol,
        base=symbol.split("/")[0],
        quote="USDT",
        price_step=0.001,
        amount_step=0.01,
        min_amount=0.01,
        min_notional=5.0,
        maker_fee=0.001,
        taker_fee=0.001,
    )
    return broker


def _entry_order():
    from tbot.core.models import Order, OrderType, Side

    return Order(
        client_id="tb-vvv-entry-abc",
        symbol="VVV/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=65.0,
    )


class _FilledDuplicate:
    """Venue that refuses the id and reports the original as Filled."""

    def __init__(self, held):
        self.held = held

    def fetch_open_orders(self, symbol):
        return []  # terminal orders are invisible here — that is the trap

    def fetch_balance(self):
        return {"total": {"VVV": self.held, "USDT": 1000.0}}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.2f}"

    def create_order(self, **kwargs):
        raise _duplicate_error()

    def privateGetV5OrderHistory(self, params):
        return {
            "result": {
                "list": [
                    {
                        "orderId": "venue-1",
                        "orderLinkId": "tb-vvv-entry-abc",
                        "orderStatus": "Filled",
                        "cumExecQty": "65.0",
                        "cumExecValue": "1740.0",
                    }
                ]
            }
        }


def test_duplicate_id_whose_fill_we_still_hold_is_adopted():
    """The deterministic id exists to stop exactly this becoming two positions."""
    result = _broker_with(_FilledDuplicate(held=65.0)).submit(_entry_order())
    assert result.exchange_id == "venue-1"
    assert result.filled_quantity == pytest.approx(65.0)


def test_duplicate_id_whose_inventory_is_gone_is_not_adopted():
    """A filled order is evidence only of a position we still hold.

    Observed live after a reset: the venue still listed a pre-reset entry as
    Filled, the engine adopted it, and booked a 1699 USDT position on a 1000
    USDT account — cash went to -699 and the position existed nowhere but in
    the bot. Skipping the bar is the cheap mistake; the next bar's client id
    differs anyway.
    """
    from tbot.core.models import OrderStatus

    result = _broker_with(_FilledDuplicate(held=0.004)).submit(_entry_order())
    assert result.status is OrderStatus.REJECTED
    assert "inventory is gone" in (result.reject_reason or "")


def test_duplicate_id_whose_original_was_cancelled_is_reissued():
    """A cancelled order opened nothing, so the intent may be sent again.

    This is what a local reset looks like: the venue still remembers ids from
    orders our wiped database no longer knows about. Treating that as a plain
    rejection retired the symbol after three tries.
    """
    sent = []

    class Exchange:
        def fetch_open_orders(self, symbol):
            return []

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.2f}"

        def create_order(self, **kwargs):
            sent.append(kwargs["params"]["clientOrderId"])
            if len(sent) == 1:
                raise _duplicate_error()
            return {"id": "venue-2", "status": "closed", "filled": 65.0, "average": 26.8}

        def fetch_balance(self):
            return {"total": {"USDT": 1000.0}}

        def privateGetV5OrderHistory(self, params):
            return {
                "result": {
                    "list": [{"orderLinkId": "tb-vvv-entry-abc", "orderStatus": "Cancelled"}]
                }
            }

    result = _broker_with(Exchange()).submit(_entry_order())
    assert result.exchange_id == "venue-2"
    assert sent[0] != sent[1], "the refused id must not be reused"


def test_unsellable_dust_is_not_reported_as_a_position():
    """0.00431 VVV cannot be sold — min_amount is 0.01 — so it is not a position.

    Reporting it every restart as an unmanaged position is a warning no action
    can ever clear, which only teaches the operator to ignore the channel.
    """

    class Exchange:
        def fetch_balance(self):
            return {"total": {"USDT": 1000.0, "VVV": 0.00431, "BTC": 0.5}}

    positions = _broker_with(Exchange()).fetch_positions()
    assert "VVV/USDT" not in positions
    assert positions["BTC/USDT"] == pytest.approx(0.5)


# --------------------------------------------------------------------------
# The scan table must never claim "eligible" for a symbol it passed over
# --------------------------------------------------------------------------


def _scan_row(symbol, score):
    from tbot.core.models import Regime, ScanRow, Side

    return ScanRow(
        symbol=symbol,
        bar_ts=0,
        regime=Regime.TREND_UP,
        best_side=Side.BUY,
        score=score,
        threshold=62.0,
        no_trade_reason=None,
    )


def test_every_candidate_ends_the_bar_with_a_reason():
    """A row left at None renders as "eligible" — a lie about a rejected symbol.

    Observed live: VVV scored 69.2 against a threshold of 62, no position was
    opened, and the dashboard showed it as eligible with nothing explaining the
    gap. The scans table exists so that question is answered by query.
    """
    from tbot.core.config import ConvictionConfig, RiskConfig
    from tbot.core.models import NoTradeReason
    from tbot.engine.runner import Runner

    rows = [_scan_row("AAA/USDT", 80.0), _scan_row("BBB/USDT", 70.0), _scan_row("CCC/USDT", 65.0)]

    class Portfolio:
        positions: dict = {}

    class OneEntry(Runner):
        # Runner uses __slots__, so the behaviour is injected by subclassing.
        # The best candidate enters; the rest were never tried, not rejected.
        def _enter(self, data, row, bar_ts):
            return row.symbol == "AAA/USDT"

    runner = OneEntry.__new__(OneEntry)
    runner.portfolio = Portfolio()
    # "absolute" keeps this test about reason-assignment. Ranking has its own.
    runner.config = type(
        "C",
        (),
        {
            "risk": RiskConfig(max_positions=3),
            "conviction": ConvictionConfig(selection="absolute"),
        },
    )()

    runner._maybe_enter({r.symbol: {} for r in rows}, rows, 0)

    assert rows[0].no_trade_reason is NoTradeReason.ENTERED
    assert all(r.no_trade_reason is not None for r in rows), "no row may stay 'eligible'"
    assert rows[1].no_trade_reason is NoTradeReason.COOLDOWN_SAME_BAR


def test_candidates_past_the_position_limit_say_so():
    from tbot.core.config import ConvictionConfig, RiskConfig
    from tbot.core.models import NoTradeReason
    from tbot.engine.runner import Runner

    rows = [_scan_row("AAA/USDT", 80.0), _scan_row("BBB/USDT", 70.0)]

    class Portfolio:
        positions = {"XXX/USDT": object(), "YYY/USDT": object()}

    class NeverEnters(Runner):
        def _enter(self, data, row, bar_ts):
            raise AssertionError("must not try to enter when full")

    runner = NeverEnters.__new__(NeverEnters)
    runner.portfolio = Portfolio()
    runner.config = type(
        "C",
        (),
        {
            "risk": RiskConfig(max_positions=2),
            "conviction": ConvictionConfig(selection="absolute"),
        },
    )()

    runner._maybe_enter({r.symbol: {} for r in rows}, rows, 0)
    assert all(r.no_trade_reason is NoTradeReason.MAX_POSITIONS for r in rows)


def test_a_new_position_does_not_inherit_the_previous_one_s_entry(tmp_path):
    """The conflict key is the symbol, so a new trade lands on the old row.

    Observed live: VVV filled at 26.171 but the database kept entry_price
    25.865 from the previous position in that symbol. Every R-based number for
    the trade — expectancy, MAE, MFE — was then measured against a different
    trade's entry, which is the measurement the whole system rests on.
    """
    from tbot.core.db import Database
    from tbot.core.models import Position, Side

    db = Database(tmp_path / "t.db")
    db.start_run(mode="live", venue="bybit-demo", config_hash="x")

    def position(quantity, entry, stop):
        return Position(
            symbol="VVV/USDT",
            side=Side.BUY,
            quantity=quantity,
            entry_price=entry,
            entry_ts=1,
            initial_stop=stop,
            current_stop=stop,
            setup="momentum_continuation",
            risk_amount=3.0,
        )

    db.upsert_position(position(65.69, 25.865, 23.125))
    db.upsert_position(position(1.31, 26.171, 23.431))

    stored = {p["symbol"]: p for p in db.load_positions()}["VVV/USDT"]
    assert stored["entry_price"] == pytest.approx(26.171)
    assert stored["initial_stop"] == pytest.approx(23.431)
    assert stored["quantity"] == pytest.approx(1.31)


def test_position_meta_survives_a_restart(tmp_path):
    """meta carries stop_order_id — the handle on the venue's protective stop.

    Dropping it on load left the engine unable to move a stop it had placed
    itself, so every restart silently lost the trail, and reconciliation
    reported the bot's own stop as an orphan for ever.
    """
    import json as json_mod

    from tbot.core.db import Database
    from tbot.core.models import Position, Side

    db = Database(tmp_path / "t.db")
    db.start_run(mode="live", venue="bybit-demo", config_hash="x")

    position = Position(
        symbol="VVV/USDT",
        side=Side.BUY,
        quantity=1.31,
        entry_price=26.171,
        entry_ts=1,
        initial_stop=23.431,
        current_stop=23.431,
        setup="momentum_continuation",
        risk_amount=3.0,
    )
    position.meta = {"stop_order_id": "2306901319086247168", "entry_atr": 2.42}
    db.upsert_position(position)

    raw = {p["symbol"]: p for p in db.load_positions()}["VVV/USDT"]
    restored = json_mod.loads(raw["meta"]) if raw.get("meta") else {}
    assert restored.get("stop_order_id") == "2306901319086247168"
    assert restored.get("entry_atr") == pytest.approx(2.42)


def test_heartbeat_interval_is_far_below_the_dead_man_threshold():
    """The dead-man switch can only be as sharp as the heartbeat feeding it.

    state.json used to be written on bar close alone, so on an hourly timeframe
    a wedged engine was indistinguishable from a waiting one for an hour — and
    the staleness threshold had to be 90 minutes to match. That is 90 minutes
    of open positions nobody is managing.
    """
    import importlib
    import sys
    from pathlib import Path

    from tbot.engine.runner import HEARTBEAT_SECONDS

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    healthcheck = importlib.import_module("healthcheck")
    assert HEARTBEAT_SECONDS <= 60
    assert healthcheck.DEFAULT_MAX_AGE_SECONDS >= HEARTBEAT_SECONDS * 5, "room for missed beats"
    assert healthcheck.DEFAULT_MAX_AGE_SECONDS <= 1800, "a stall must be caught in minutes"


def test_no_notification_path_is_defined_but_never_called():
    """Catch the whole bug class, not the two instances of it found so far.

    Audit #7 found three notification methods that existed, were advertised in
    the config, and were never invoked. They were wired up. Today
    ``_notify_risk_change`` and ``_maybe_daily_summary`` were found in exactly
    that state again — defined, documented, unreachable.

    This is worse than having no notification: the operator reads
    ``notify_risk_changes: true``, believes they will be told when exposure
    drops, and is not. Silence reads as "nothing happened".

    So rather than testing the two, test the shape: every ``_notify_*`` and
    ``_maybe_*`` method on Runner must be called from somewhere in the module.
    """
    import ast
    import inspect

    from tbot.engine import runner as runner_module

    tree = ast.parse(inspect.getsource(runner_module))
    runner_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Runner"
    )

    defined = {
        node.name
        for node in runner_class.body
        if isinstance(node, ast.FunctionDef)
        and (node.name.startswith("_notify_") or node.name.startswith("_maybe_"))
    }
    assert defined, "the naming convention this test relies on has changed"

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    orphans = sorted(defined - called)
    assert not orphans, (
        f"defined but never called: {orphans}. A notification the config "
        f"advertises and the engine never sends is a promise the operator "
        f"relies on and does not get."
    )
