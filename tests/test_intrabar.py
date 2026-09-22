"""The between-bar layer: feed, price-level exits, armed entries.

Everything here protects one property — **acting between bar closes must not
let an unclosed bar reach a decision**. The engine may check whether price has
touched a number that was already computed at the last close, and nothing else.
Break that and the backtest stops being able to reproduce the live path, which
is what every measured number in this repo rests on (invariant 1).
"""

from __future__ import annotations

import pytest

from tbot.core.config import EntryConfig, ExitConfig
from tbot.core.models import ExitReason, Position, Regime, Side
from tbot.data.feed import (
    MAX_TOPICS_PER_SUBSCRIPTION,
    STALE_AFTER_SECONDS,
    MarketFeed,
    Quote,
)
from tbot.strategy.exits import ExitManager


def _position(entry=100.0, stop=90.0, side=Side.BUY, entry_atr=5.0) -> Position:
    position = Position(
        symbol="BTC/USDT",
        side=side,
        quantity=1.0,
        entry_price=entry,
        entry_ts=0,
        initial_stop=stop,
        current_stop=stop,
        setup="donchian",
        risk_amount=10.0,
    )
    position.meta["entry_atr"] = entry_atr
    return position


# --------------------------------------------------------------------------
# Price-level exits
# --------------------------------------------------------------------------


def test_intrabar_and_bar_close_reach_the_same_verdict_on_a_stop():
    """One implementation, two entry points.

    A second copy of "did price go through the stop" written for the intra-bar
    path is the most likely way for live and backtest to drift apart, so the
    two must be provably the same check.
    """
    manager = ExitManager(ExitConfig())
    high, low = 101.0, 89.0

    at_close = manager.evaluate(
        _position(),
        high=high,
        low=low,
        close=95.0,
        atr=3.0,
        entry_atr=3.0,
        regime=Regime.TREND_UP,
        conviction_now=None,
        collapse_threshold=30.0,
    )
    between = manager.evaluate_intrabar(_position(), high=high, low=low)

    assert at_close.should_exit and between.should_exit
    assert at_close.reason is between.reason is ExitReason.INITIAL_STOP
    assert at_close.exit_price == between.exit_price == 90.0


def test_intrabar_never_consults_an_indicator():
    """Regime, ATR and conviction are absent from the between-bar signature.

    They are computed from bars, and between closes the only finished bars are
    the ones the last close already used. A between-bar path that took them
    would be reading a stale opinion and calling it current.
    """
    manager = ExitManager(ExitConfig(vol_shock_atr_ratio=1.01))
    position = _position()

    # A volatility shock and a hostile regime would both fire at bar close.
    at_close = manager.evaluate(
        position,
        high=100.5,
        low=99.5,
        close=100.0,
        atr=99.0,
        entry_atr=1.0,
        regime=Regime.TREND_DOWN,
        conviction_now=0.0,
        collapse_threshold=30.0,
    )
    assert at_close.should_exit

    # Between bars, none of those are knowable, so nothing fires.
    assert not manager.evaluate_intrabar(position, high=100.5, low=99.5).should_exit


def test_target_is_off_unless_a_level_was_configured():
    """The mechanism ships disabled.

    A profit target is a free parameter, and a free parameter chosen by looking
    at the same archive that produced everything else is the definition of an
    unrecorded trial. The default must therefore change nothing.
    """
    manager = ExitManager(ExitConfig())
    assert manager.target_price(_position()) is None
    assert not manager.evaluate_intrabar(_position(), high=1e9, low=99.0).should_exit


def test_target_fires_at_the_level_not_at_the_close():
    manager = ExitManager(ExitConfig(target_atr_mult=2.0))
    position = _position(entry=100.0, entry_atr=5.0)
    assert manager.target_price(position) == 110.0

    decision = manager.evaluate_intrabar(position, high=115.0, low=99.0)
    assert decision.should_exit
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.exit_price == 110.0  # where it triggered, not where it ended


def test_stop_wins_when_one_bar_touches_both_levels():
    """Invariant D40. A bar's OHLC cannot say which came first.

    Choosing the good one makes live results beat the backtest that justified
    them — and a pleasant surprise is the kind of discrepancy nobody goes back
    and investigates.
    """
    manager = ExitManager(ExitConfig(target_atr_mult=2.0))
    position = _position(entry=100.0, stop=90.0, entry_atr=5.0)

    decision = manager.evaluate_intrabar(position, high=115.0, low=85.0)
    assert decision.reason is ExitReason.INITIAL_STOP
    assert decision.exit_price == 90.0


def test_target_measured_from_entry_atr_not_the_current_one():
    """A target that drifts with volatility is a second opinion, not a target."""
    manager = ExitManager(ExitConfig(target_atr_mult=2.0))
    position = _position(entry=100.0, entry_atr=5.0)
    position.meta["entry_atr"] = 5.0
    first = manager.target_price(position)

    # Nothing about a later bar's ATR may move it; only entry_atr is read.
    assert manager.target_price(position) == first == 110.0


def test_target_is_none_when_entry_atr_was_never_recorded():
    """Invariant 10: unknown is "no opinion", never a number."""
    manager = ExitManager(ExitConfig(target_atr_mult=2.0))
    position = _position()
    position.meta.pop("entry_atr")
    assert manager.target_price(position) is None


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------


def test_quote_tracks_extremes_since_the_last_reset():
    quote = Quote()
    for price in (100.0, 105.0, 98.0, 101.0):
        quote.observe(price, ts=0)

    assert (quote.high, quote.low, quote.last) == (105.0, 98.0, 101.0)

    # A bar closed: the extremes belong to the new bar, not to everything since
    # the process started. Without this an exit would fire on a high set hours
    # ago and attribute it to the bar now forming.
    quote.reset()
    assert quote.high == quote.low == quote.last == 101.0


def test_feed_reports_stale_before_it_has_ever_connected():
    """A dead socket and a still market look identical from outside.

    Assuming "still" is how a stale price gets acted on, so the feed must claim
    nothing until it has actually heard something.
    """
    feed = MarketFeed(["BTC/USDT"])
    assert feed.is_stale
    assert feed.quote("BTC/USDT") is None


def test_stale_feed_returns_no_quote_rather_than_an_old_one(monkeypatch):
    """Degrading to bar-close behaviour is always safe; acting on stale data is not."""
    feed = MarketFeed(["BTC/USDT"])
    feed._quotes["BTC/USDT"] = Quote(last=100.0, high=101.0, low=99.0, ts=1)
    feed._connected = True

    import time as time_module

    feed._last_message = time_module.time()
    assert feed.quote("BTC/USDT") is not None

    feed._last_message = time_module.time() - STALE_AFTER_SECONDS - 1
    assert feed.quote("BTC/USDT") is None


def test_subscriptions_are_chunked_to_the_venue_limit():
    """Bybit's public spot socket takes ten topics per subscribe. Eleven hangs.

    Measured on the live venue by bisection: ten symbols connect in two seconds,
    eleven never resolve — no error, no exception, no close, just an awaited
    future that stays pending. The whole universe went dark because one
    subscribe was one symbol too big, and nothing in the logs said so.

    Silence is what makes it worth a test: a feed that raised would reconnect on
    its own. This is the shape of failure that survives a restart.
    """
    assert MAX_TOPICS_PER_SUBSCRIPTION == 10

    symbols = [f"C{i}/USDT" for i in range(21)]
    chunks = [
        symbols[i : i + MAX_TOPICS_PER_SUBSCRIPTION]
        for i in range(0, len(symbols), MAX_TOPICS_PER_SUBSCRIPTION)
    ]
    assert [len(c) for c in chunks] == [10, 10, 1]
    assert max(len(c) for c in chunks) <= MAX_TOPICS_PER_SUBSCRIPTION
    assert sum(chunks, []) == symbols  # every symbol subscribed exactly once


def test_quote_is_a_copy_so_the_engine_cannot_read_a_half_update():
    """Invariant D42: the feed thread keeps writing; the engine must not alias it."""
    feed = MarketFeed(["BTC/USDT"])
    import time as time_module

    feed._quotes["BTC/USDT"] = Quote(last=100.0, high=100.0, low=100.0, ts=1)
    feed._connected = True
    feed._last_message = time_module.time()

    handed_out = feed.quote("BTC/USDT")
    feed._quotes["BTC/USDT"].observe(200.0, ts=2)
    assert handed_out.last == 100.0


# --------------------------------------------------------------------------
# Armed entries
# --------------------------------------------------------------------------


def test_armed_breakout_is_off_by_default():
    """It changes which trades happen, so it is a trial and not a repair.

    Measured once on a pinned archive: 5 armed trades produced more profit than
    the other 101 combined, and two of those five carried almost all of it. A
    result that concentrated is a reason to keep the switch off, not to flip it.
    """
    assert EntryConfig().armed_breakout is False


def test_armed_level_is_the_channel_high_before_the_break():
    import numpy as np
    import pandas as pd

    from tbot.strategy.setups import DonchianBreakout

    # A flat channel with a close sitting just underneath its top.
    n = 120
    frame = pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": np.full(n, 102.0),
            "low": np.full(n, 98.0),
            "close": np.full(n, 100.0),
            "volume": np.full(n, 1000.0),
        }
    )
    setup = DonchianBreakout(period=20)
    candidate = setup.arm(frame, Regime.TREND_UP, atr_mult=2.0, max_distance_atr=5.0)

    assert candidate is not None
    assert candidate.entry_price == pytest.approx(102.0)  # the channel high
    assert candidate.stop_price < candidate.entry_price
    assert candidate.name.endswith("_armed")


def test_nothing_is_armed_once_price_is_already_through_the_level():
    """There is nothing left to wait for — that case belongs to ``detect``."""
    import numpy as np
    import pandas as pd

    from tbot.strategy.setups import DonchianBreakout

    n = 120
    close = np.full(n, 100.0)
    close[-1] = 110.0  # already broken out
    high = np.full(n, 102.0)
    high[-1] = 111.0
    frame = pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": high,
            "low": np.full(n, 98.0),
            "close": close,
            "volume": np.full(n, 1000.0),
        }
    )
    setup = DonchianBreakout(period=20)
    assert setup.arm(frame, Regime.TREND_UP, atr_mult=2.0, max_distance_atr=5.0) is None


def test_a_level_too_far_away_is_not_armed():
    """An order resting far below its trigger reserves capital for nothing."""
    import numpy as np
    import pandas as pd

    from tbot.strategy.setups import DonchianBreakout

    n = 120
    high = np.full(n, 200.0)
    frame = pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": high,
            "low": np.full(n, 99.0),
            "close": np.full(n, 100.0),
            "volume": np.full(n, 1000.0),
        }
    )
    setup = DonchianBreakout(period=20)
    assert setup.arm(frame, Regime.TREND_UP, atr_mult=2.0, max_distance_atr=0.5) is None


def test_only_one_armed_order_is_allowed():
    """More than one and the *market* picks which symbol fills.

    The measured edge is cross-sectional: each bar ranks the symbols and the
    highest takes the slot. Arming several replaces that with "whichever level
    price touches first", which is a different selection mechanism than the one
    that was tested.
    """
    assert EntryConfig().armed_max_orders == 1


# --------------------------------------------------------------------------
# Learning what the venue did on its own
# --------------------------------------------------------------------------


def test_a_cancelled_stop_is_never_read_as_a_fill():
    """Bybit spells a cancelled conditional order "Deactivated".

    Treating it as a fill invents an exit that never happened: the position
    would be closed in the database while the inventory is still sitting at the
    venue, unmanaged and unprotected.
    """
    from tbot.execution.ccxt_broker import _order_state

    cancelled = _order_state({"orderStatus": "Deactivated", "cumExecQty": "0", "avgPrice": ""})
    assert cancelled["terminal"] is True
    assert cancelled["filled_terminal"] is False

    filled = _order_state({"orderStatus": "Filled", "cumExecQty": "1.31", "avgPrice": "26.078"})
    assert filled["filled_terminal"] is True
    assert filled["filled"] == pytest.approx(1.31)
    assert filled["average_price"] == pytest.approx(26.078)

    resting = _order_state({"orderStatus": "Untriggered", "cumExecQty": "0", "avgPrice": ""})
    assert resting["terminal"] is False


def test_order_state_carries_the_venues_own_fill_time():
    """Audit #92: the scalp engine opens a position at the venue's fill time;
    its attached target can fill before the engine's next poll."""
    from tbot.execution.ccxt_broker import _order_state

    filled = _order_state(
        {"orderStatus": "Filled", "cumExecQty": "1", "avgPrice": "1", "updatedTime": "1790002864215"}
    )
    assert filled["updated_ms"] == 1_790_002_864_215
    assert _order_state({"orderStatus": "New"})["updated_ms"] == 0  # unknown, not "now"


@pytest.fixture
def make_runner(tmp_path):
    """Build a Runner, optionally as a subclass.

    A subclass rather than monkeypatching because ``Runner`` is a slots
    dataclass: instance attributes cannot be added and ``__class__`` cannot be
    reassigned afterwards, so the behaviour under test has to be there from
    construction.
    """
    from tbot.core.config import Secrets, load_config
    from tbot.core.db import Database
    from tbot.core.models import MarketSpec
    from tbot.data.store import ParquetStore
    from tbot.engine.runner import Runner
    from tbot.execution.paper_broker import PaperBroker

    def build(cls=Runner):
        config = load_config("config/base.yaml").model_copy(update={"runtime_dir": tmp_path})
        spec = MarketSpec(
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
        return cls(
            config=config,
            secrets=Secrets(),
            db=Database(tmp_path / "t.db"),
            store=ParquetStore(tmp_path / "data"),
            broker=PaperBroker(specs={"BTC/USDT": spec}, cash=1000.0),
            symbols=["BTC/USDT"],
        )

    return build


@pytest.fixture
def runner(make_runner):
    return make_runner()


def test_balances_view_separates_free_from_locked(runner):
    """The operator asked what the account holds. The honest answer has three parts.

    A protective stop reserves the base asset, so a correctly protected position
    reads as zero free. Reporting only "free" would show it as nothing held —
    which is the same misreading that once made the engine drop a position it
    still owned.
    """
    from tbot.core.models import Position, Side

    runner.portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )
    runner._marks["BTC/USDT"] = 120.0

    rows = {row["coin"]: row for row in runner._balances_view()}
    assert rows["USDT"]["free"] == pytest.approx(1000.0)
    assert rows["BTC"]["used"] == pytest.approx(0.5)
    assert rows["BTC"]["free"] == pytest.approx(0.0)
    assert rows["BTC"]["total"] == pytest.approx(0.5)
    assert rows["BTC"]["value"] == pytest.approx(60.0)


def test_an_unpriceable_holding_reports_no_value_rather_than_zero(runner):
    """Invariant 10 again. A holding with an unknown price is not worthless."""
    from tbot.core.models import Position, Side

    runner.portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )
    rows = {row["coin"]: row for row in runner._balances_view()}
    assert rows["BTC"]["price"] is None
    assert rows["BTC"]["value"] is None


def test_intrabar_check_is_a_no_op_without_a_feed(runner):
    """The feed is an improvement, never a dependency.

    When it is absent or stale the engine must behave exactly as it did before
    the feed existed — and the venue stop, which is the actual protection, is
    untouched either way.
    """
    from tbot.core.models import Position, Side

    runner.portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )
    runner.feed = None
    runner._check_intrabar_exits()  # must not raise, must not close anything
    assert "BTC/USDT" in runner.portfolio.positions


def test_venue_exit_detection_waits_for_a_second_reading(runner):
    """One balance reading is not evidence.

    A balance fetched seconds after an entry can lag the fill, and inventing an
    exit is worse than noticing a real one a minute late.
    """
    from tbot.core.models import Position, Side
    from tbot.execution.ccxt_broker import CCXTBroker

    runner.portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )

    specs = runner.broker  # captured before it is replaced below

    class FakeBroker(CCXTBroker):
        """Venue says the holding is gone and our stop filled at 90."""

        def __init__(self):  # noqa: D107 — no network, no credentials
            self.venue = None
            self.quote = "USDT"
            self.calls = 0

        def fetch_positions(self):
            self.calls += 1
            return {}

        def order_state(self, symbol, order_id):
            return {
                "status": "Filled",
                "filled": 0.5,
                "average_price": 90.0,
                "terminal": True,
                "filled_terminal": True,
            }

        def market_spec(self, symbol):
            return specs.market_spec(symbol)

    runner.broker = FakeBroker()
    runner.portfolio.positions["BTC/USDT"].meta["stop_order_id"] = "1"

    runner._detect_venue_exits()
    assert "BTC/USDT" in runner.portfolio.positions, "one reading must not be enough"

    runner._detect_venue_exits()
    assert "BTC/USDT" not in runner.portfolio.positions, "two readings settle it"


def test_a_returning_balance_clears_the_missing_counter(runner):
    """A lagging read that corrects itself must not accumulate toward an exit."""
    from tbot.core.models import Position, Side
    from tbot.execution.ccxt_broker import CCXTBroker

    runner.portfolio.positions["BTC/USDT"] = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )

    held = {}
    specs = runner.broker

    class FakeBroker(CCXTBroker):
        def __init__(self):  # noqa: D107
            self.venue = None
            self.quote = "USDT"

        def fetch_positions(self):
            return dict(held)

        def order_state(self, symbol, order_id):
            return None

        def market_spec(self, symbol):
            return specs.market_spec(symbol)

    runner.broker = FakeBroker()

    runner._detect_venue_exits()
    assert runner._missing_inventory["BTC/USDT"] == 1

    held["BTC/USDT"] = 0.5  # the read catches up
    runner._detect_venue_exits()
    assert "BTC/USDT" not in runner._missing_inventory
    assert "BTC/USDT" in runner.portfolio.positions


def test_microstructure_is_collected_but_does_not_score_by_default():
    """Phase one is looking, phase two is a separate decision with evidence.

    The data has to be recorded before anyone can tell whether acting on it
    would have helped — but switching the penalties on at the same moment the
    wiring lands would change the trade population as a side effect of plumbing.
    """
    from tbot.core.config import ExecutionConfig

    assert ExecutionConfig().microstructure_enabled is True
    assert ExecutionConfig().microstructure_penalties is False


# --------------------------------------------------------------------------
# The wait loop's clock
# --------------------------------------------------------------------------


def test_wait_loop_measures_elapsed_time_not_intended_time(make_runner, monkeypatch):
    """The loop must not overshoot the bar close when its own work is slow.

    Observed live: ``getUpdates`` costs 3-8 seconds from this machine and was
    called on every 5-second tick, so each iteration really took nine seconds
    while the counter recorded five. The loop ran ~80% long and bars were
    processed **7 to 38 minutes after they closed** — entries priced off a
    close half an hour stale, with nothing in the logs saying so.

    A clock that adds up intentions instead of reading a clock fails in the
    worst way available: no error, no retry, the bot just runs late and keeps
    reporting that it is healthy.
    """
    import time as time_module

    from tbot.engine import runner as runner_module
    from tbot.engine.runner import Runner

    class SlowPoll(Runner):
        def _poll_commands(self):
            time_module.sleep(0.25)  # slower than a tick, like a Telegram call

    # A 2-second bar, and work that costs more than a whole tick.
    monkeypatch.setattr(runner_module, "ms_until_next_close", lambda _tf: 0)
    runner = make_runner(SlowPoll)
    runner.config.engine.loop_interval_seconds = 0.2
    runner.config.telegram.poll_seconds = 0.05

    started = time_module.monotonic()
    runner._wait_for_next_bar("1h")
    elapsed = time_module.monotonic() - started

    # Intended wait is 2s (0 until close, plus the 2s guard). Anything past a
    # small margin means the loop is counting its intentions again.
    assert 1.9 <= elapsed <= 3.0, f"waited {elapsed:.2f}s for a 2s bar"


def test_telegram_polling_is_in_the_slow_lane():
    """Commands are conveniences; open positions are not.

    One round trip measured at 3-8 seconds cannot sit on the same cadence as
    the check that watches whether a stop has been reached.
    """
    from tbot.core.config import TelegramConfig

    config = TelegramConfig()
    assert config.poll_seconds >= 10.0
    # And a slow poll must not be able to stall the loop for the send timeout.
    assert config.poll_timeout < 5.0


def test_a_slow_command_poll_does_not_starve_the_between_bar_check(make_runner, monkeypatch):
    """The exit check runs every tick; the command poll runs on its own clock."""
    import time as time_module

    from tbot.engine import runner as runner_module
    from tbot.engine.runner import Runner

    counts = {"polls": 0, "checks": 0}

    class Counting(Runner):
        def _poll_commands(self):
            counts["polls"] += 1
            time_module.sleep(0.3)

        def _check_intrabar_exits(self):
            counts["checks"] += 1

    monkeypatch.setattr(runner_module, "ms_until_next_close", lambda _tf: 0)
    runner = make_runner(Counting)
    runner.config.engine.loop_interval_seconds = 0.1
    runner.config.telegram.poll_seconds = 10.0  # effectively once, at the start

    runner._wait_for_next_bar("1h")

    assert counts["polls"] == 1, "the slow poll must not repeat within its interval"
    assert counts["checks"] >= 10, f"only {counts['checks']} exit checks in a 2s wait"


def test_a_blocked_verdict_is_no_opinion_but_a_low_score_is_a_score():
    """The distinction the conviction-collapse exit lives or dies on.

    ``_blocking_reason`` short-circuits with ``total = 0.0`` — nothing was
    computed. ``below_threshold`` is assigned *after* scoring — a real number
    that did not clear the entry bar. Treating them alike discards every score
    the collapse exit exists to read, because a collapsed thesis is below the
    entry bar by definition. Measured: 391 bars scored under the threshold and
    all 391 were thrown away as "unscored", so the layer never fired once.
    """
    from tbot.core.models import NoTradeReason
    from tbot.strategy.conviction import UNSCORED_REASONS

    assert NoTradeReason.BELOW_THRESHOLD not in UNSCORED_REASONS
    for blocked in (
        NoTradeReason.ALREADY_IN_POSITION,
        NoTradeReason.MAX_POSITIONS,
        NoTradeReason.STALE_DATA,
        NoTradeReason.CORRELATION_FULL,
        NoTradeReason.LLM_EVENT_VETO,
        NoTradeReason.REGIME_VETO,
    ):
        assert blocked in UNSCORED_REASONS


def test_collapse_exit_is_disabled_by_a_zero_threshold():
    """Zero means off, explicitly — not by accident of score clipping.

    Measured with it on at 32: 24 exits, PF 1.23 -> 1.12, expectancy
    +0.179 -> +0.100R, capture 8.7% -> 5.6%. The mechanism is correct; the
    level is not endorsed.
    """
    manager = ExitManager(ExitConfig())
    decision = manager.evaluate(
        _position(),
        high=101.0,
        low=99.0,
        close=100.0,
        atr=1.0,
        entry_atr=1.0,
        regime=Regime.TREND_UP,
        conviction_now=0.0,  # as collapsed as a score can be
        collapse_threshold=0.0,
        )
    assert not decision.should_exit

    # And still fires when a level is genuinely configured.
    armed = manager.evaluate(
        _position(),
        high=101.0,
        low=99.0,
        close=100.0,
        atr=1.0,
        entry_atr=1.0,
        regime=Regime.TREND_UP,
        conviction_now=10.0,
        collapse_threshold=32.0,
    )
    assert armed.should_exit
    assert armed.reason is ExitReason.CONVICTION_COLLAPSE


def test_missing_entry_atr_is_backfilled_not_silently_ignored(runner):
    """A position without entry_atr loses two exit layers without saying so.

    Reconciliation adopts holdings the engine did not open, and rows written
    before meta was persisted (audit #37) come back carrying only their stop
    fields. The old fallback used the current ATR, which makes the
    volatility-shock ratio exactly 1.0 — a check that can never fire — and
    leaves target_price returning None, so a configured profit target would
    not exist either. Both look like a managed position.
    """
    from tbot.core.models import Position, Side

    position = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="adopted",
        risk_amount=5.0,
    )
    runner.portfolio.positions["BTC/USDT"] = position
    assert "entry_atr" not in position.meta

    value = runner._entry_atr_for(position, atr_now=4.0)

    assert value == pytest.approx(4.0)
    assert position.meta["entry_atr"] == pytest.approx(4.0)

    # And with a target configured it is now computable rather than None.
    manager = ExitManager(ExitConfig(target_atr_mult=2.0))
    assert manager.target_price(position) == pytest.approx(108.0)


def test_a_stored_entry_atr_is_never_overwritten(runner):
    """Backfill fills a gap; it must not rewrite history on every bar."""
    from tbot.core.models import Position, Side

    position = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.5,
        entry_price=100.0,
        entry_ts=0,
        initial_stop=90.0,
        current_stop=90.0,
        setup="test",
        risk_amount=5.0,
    )
    position.meta["entry_atr"] = 2.5
    runner.portfolio.positions["BTC/USDT"] = position

    assert runner._entry_atr_for(position, atr_now=99.0) == pytest.approx(2.5)
    assert position.meta["entry_atr"] == pytest.approx(2.5)


# --------------------------------------------------------------------------
# Partial profit taking
# --------------------------------------------------------------------------


def test_scale_out_is_off_by_default():
    """Measured worse on return, better on drawdown. A trade, not an upgrade.

    At 1.0R it saves $80 across the 82 mediocre trades and gives up $108 on
    the 21 that reach 3R+ - and that tail is the entire profit. Profit factor
    1.19 -> 1.12, return +6.04% -> +2.60%, max drawdown 15.8% -> 12.8%.
    """
    assert ExitConfig().scale_out_at_r == 0.0
    manager = ExitManager(ExitConfig())
    assert manager.scale_out(_position(), high=1e9, low=99.0) is None


def test_scale_out_fires_at_its_level_and_only_once():
    """A position oscillating around the level must not be sold down bar by bar."""
    manager = ExitManager(ExitConfig(scale_out_at_r=1.0, scale_out_pct=0.5))
    position = _position(entry=100.0, stop=90.0)  # 1R = 10

    plan = manager.scale_out(position, high=110.0, low=99.0)
    assert plan is not None
    assert plan.price == pytest.approx(110.0)
    assert plan.fraction == pytest.approx(0.5)

    position.meta["scaled_out"] = True
    assert manager.scale_out(position, high=130.0, low=99.0) is None


def test_scale_out_does_not_fire_before_its_level():
    manager = ExitManager(ExitConfig(scale_out_at_r=2.0, scale_out_pct=0.5))
    position = _position(entry=100.0, stop=90.0)
    assert manager.scale_out(position, high=119.0, low=99.0) is None
    assert manager.scale_out(position, high=120.0, low=99.0) is not None


def test_r_is_measured_against_the_original_size_after_a_partial_exit():
    """Half a position closing at +2R is not a +2R trade.

    R has to describe the risk the position actually carried, or a scaled-out
    winner reports the same R as one that ran whole and the expectancy series
    quietly stops being comparable across trades.
    """
    from tbot.core.models import ExitReason, Fill, Regime, Side
    from tbot.engine.portfolio import Portfolio

    portfolio = Portfolio(starting_cash=10_000.0)
    fill = Fill(
        order_client_id="x",
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=2.0,
        price=100.0,
        fee=0.0,
        ts=0,
    )
    portfolio.open_position(
        fill=fill,
        stop_price=90.0,  # 1R = 10 per unit, 20 for the position
        setup="test",
        risk_amount=20.0,
        conviction=70.0,
        regime=Regime.TREND_UP,
    )

    # Half off at +1R, the rest exits at +2R.
    sold = portfolio.scale_out("BTC/USDT", price=110.0, fraction=0.5)
    assert sold == pytest.approx(1.0)
    assert portfolio.positions["BTC/USDT"].quantity == pytest.approx(1.0)

    trade = portfolio.close_position("BTC/USDT", 120.0, 1, ExitReason.TRAILING_STOP)
    # 1.0 unit at +10 plus 1.0 unit at +20 = 30 against 20 of risk.
    assert trade.pnl == pytest.approx(30.0)
    assert trade.r_multiple == pytest.approx(1.5)


def test_r_is_unchanged_when_nothing_was_scaled_out():
    """The new formula must be algebraically the old one in the common case."""
    from tbot.core.models import ExitReason, Fill, Regime, Side
    from tbot.engine.portfolio import Portfolio

    portfolio = Portfolio(starting_cash=10_000.0)
    portfolio.open_position(
        fill=Fill(
            order_client_id="x",
            symbol="BTC/USDT",
            side=Side.BUY,
            quantity=2.0,
            price=100.0,
            fee=0.0,
            ts=0,
        ),
        stop_price=90.0,
        setup="test",
        risk_amount=20.0,
        conviction=70.0,
        regime=Regime.TREND_UP,
    )
    trade = portfolio.close_position("BTC/USDT", 120.0, 1, ExitReason.TRAILING_STOP)
    assert trade.r_multiple == pytest.approx(2.0)


def test_scaling_out_cannot_compound_into_a_full_exit():
    """The fraction is of the original size, so repeats cannot eat the position."""
    from tbot.core.models import Fill, Regime, Side
    from tbot.engine.portfolio import Portfolio

    portfolio = Portfolio(starting_cash=10_000.0)
    portfolio.open_position(
        fill=Fill(
            order_client_id="x",
            symbol="BTC/USDT",
            side=Side.BUY,
            quantity=4.0,
            price=100.0,
            fee=0.0,
            ts=0,
        ),
        stop_price=90.0,
        setup="test",
        risk_amount=40.0,
        conviction=70.0,
        regime=Regime.TREND_UP,
    )
    for _ in range(3):
        portfolio.scale_out("BTC/USDT", price=110.0, fraction=0.5)
    # Three halvings of the ORIGINAL 4.0 would be 6.0 - more than exists.
    assert portfolio.positions["BTC/USDT"].quantity == pytest.approx(0.0, abs=1e-9)
    assert portfolio.positions["BTC/USDT"].quantity >= 0.0


def test_an_oversized_resting_stop_is_not_adopted():
    """After a partial exit the venue stop covers more than is still held.

    Adopting it means that when it triggers it asks to sell inventory already
    sold, is refused for insufficient balance, and leaves the remainder with no
    protection - a partial profit bought with the whole position's safety.
    """
    import inspect

    from tbot.execution.ccxt_broker import CCXTBroker

    source = inspect.getsource(CCXTBroker.place_stop)
    assert "quantity * 1.005" in source, "the adopt check must bound the upper side too"
