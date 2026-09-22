"""Persistence: the local database is the only durable record of what happened."""

import pytest

from tbot.core.db import Database
from tbot.core.models import (
    Conviction,
    ConvictionComponent,
    EquityPoint,
    ExitReason,
    Fill,
    IndicatorCategory,
    IndicatorSignal,
    NoTradeReason,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Regime,
    ScanRow,
    Side,
    Trade,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.start_run(mode="paper", venue="bybit-demo", config_hash="abc123")
    yield database
    database.close()


def test_schema_and_run_bookkeeping(db):
    assert db.get_meta("schema_version") == "2"
    assert db.run_id == 1
    db.stop_run("clean")
    runs = db.query("SELECT * FROM runs")
    assert runs[0]["stop_reason"] == "clean"
    assert runs[0]["stopped_ts"] is not None


def test_order_upsert_is_idempotent_by_client_id(db):
    order = Order(
        client_id="donchian:BTC/USDT:1700000000000:entry",
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=0.001,
        created_ts=1,
        updated_ts=1,
    )
    db.upsert_order(order)
    assert db.order_exists(order.client_id)

    order.status = OrderStatus.FILLED
    order.filled_quantity = 0.001
    order.average_price = 60_000.0
    order.updated_ts = 2
    db.upsert_order(order)

    rows = db.query("SELECT * FROM orders")
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0]["status"] == "filled"
    assert rows[0]["average_price"] == 60_000.0


def test_order_exists_guards_against_duplicate_send_after_crash(db):
    cid = "donchian:ETH/USDT:1700000000000:entry"
    assert db.order_exists(cid) is False
    db.upsert_order(
        Order(
            client_id=cid,
            symbol="ETH/USDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=1.0,
            created_ts=1,
            updated_ts=1,
        )
    )
    assert db.order_exists(cid) is True


def test_duplicate_exchange_fill_is_ignored(db):
    fill = Fill(
        order_client_id="o1",
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.001,
        price=60_000.0,
        fee=0.06,
        ts=1,
        exchange_trade_id="t-1",
    )
    db.record_fill(fill)
    db.record_fill(fill)  # websocket replay of the same trade
    assert len(db.query("SELECT * FROM fills")) == 1


def test_position_roundtrip_and_delete(db):
    pos = Position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=0.001,
        entry_price=60_000.0,
        entry_ts=1,
        initial_stop=58_000.0,
        current_stop=58_000.0,
        setup="donchian",
        risk_amount=2.0,
    )
    db.upsert_position(pos)
    pos.current_stop = 59_000.0
    pos.bars_held = 5
    db.upsert_position(pos)

    loaded = db.load_positions()
    assert len(loaded) == 1
    assert loaded[0]["current_stop"] == 59_000.0
    assert loaded[0]["bars_held"] == 5

    db.delete_position("BTC/USDT")
    assert db.load_positions() == []


def test_trade_records_excursion_for_later_calibration(db):
    trade = Trade(
        symbol="BTC/USDT",
        side=Side.BUY,
        setup="donchian",
        entry_ts=1,
        exit_ts=2,
        entry_price=60_000.0,
        exit_price=63_000.0,
        quantity=0.001,
        pnl=3.0,
        fees=0.12,
        r_multiple=1.5,
        mfe_r=2.8,
        mae_r=-0.35,
        bars_held=12,
        exit_reason=ExitReason.TRAILING_STOP,
        regime_at_entry=Regime.TREND_UP,
        conviction_at_entry=74.0,
    )
    db.record_trade(trade)
    row = db.recent_trades()[0]
    assert row["mfe_r"] == 2.8
    assert row["mae_r"] == -0.35
    assert row["exit_reason"] == "trailing_stop"
    assert row["regime_at_entry"] == "trend_up"


def test_conviction_stores_full_component_breakdown(db):
    conv = Conviction(
        symbol="BTC/USDT",
        bar_ts=1_700_000_000_000,
        side=Side.BUY,
        total=74.0,
        threshold=68.0,
        regime=Regime.TREND_UP,
        blocks_voting=4,
        components=[
            ConvictionComponent(
                name="indicator_confluence",
                score=20.0,
                max_score=25.0,
                detail="4/5 blocks long",
                signals=[
                    IndicatorSignal(
                        name="ema_stack",
                        category=IndicatorCategory.TREND,
                        timeframe="4h",
                        value=0.85,
                        raw=1.0,
                    )
                ],
            )
        ],
    )
    db.record_conviction(conv)
    row = db.query("SELECT * FROM convictions")[0]
    assert row["passed"] == 1
    assert "ema_stack" in row["components"]  # signal detail survives for the dashboard


def test_conviction_upsert_on_same_bar(db):
    for total in (60.0, 71.0):
        db.record_conviction(
            Conviction(symbol="BTC/USDT", bar_ts=1, side=Side.BUY, total=total, threshold=68.0)
        )
    rows = db.query("SELECT * FROM convictions")
    assert len(rows) == 1
    assert rows[0]["total"] == 71.0


def test_scan_rows_record_why_nothing_traded(db):
    rows = [
        ScanRow(
            symbol="BTC/USDT",
            bar_ts=1,
            regime=Regime.RANGE,
            best_side=Side.BUY,
            score=55.0,
            threshold=68.0,
            no_trade_reason=NoTradeReason.BELOW_THRESHOLD,
        ),
        ScanRow(
            symbol="ETH/USDT",
            bar_ts=1,
            regime=Regime.HIGH_VOL,
            best_side=None,
            score=0.0,
            threshold=68.0,
            no_trade_reason=NoTradeReason.REGIME_VETO,
        ),
    ]
    db.record_scans(rows)
    stored = db.query("SELECT * FROM scans ORDER BY symbol")
    assert [r["no_trade_reason"] for r in stored] == ["below_threshold", "regime_veto"]


def test_equity_and_events(db):
    db.record_equity(
        EquityPoint(ts=1, equity=100.0, cash=100.0, unrealized=0.0, open_positions=0, drawdown=0.0)
    )
    db.record_event(
        "risk_tier_change",
        "drawdown 11% -> multiplier 0.5",
        level="warning",
        payload={"multiplier": 0.5},
    )
    assert db.query("SELECT * FROM equity")[0]["equity"] == 100.0
    event = db.query("SELECT * FROM events")[0]
    assert event["kind"] == "risk_tier_change"
    assert "0.5" in event["payload"]


def test_llm_call_logging_and_daily_budget_counting(db):
    for _ in range(3):
        db.record_llm_call(
            role="event_guard",
            provider="gemini",
            model="gemini-flash-latest",
            prompt="...",
            response='{"veto": false}',
            parsed_ok=1,
            latency_ms=420,
            symbol="BTC/USDT",
            decision="allow",
        )
    assert db.llm_calls_today("event_guard", since_ts=0) == 3
    assert db.llm_calls_today("post_mortem", since_ts=0) == 0


def test_restart_storm_detection(db, tmp_path):
    for _ in range(4):
        db.start_run(mode="paper", venue="bybit-demo", config_hash="abc123")
    assert db.restarts_since(0) >= 5


def test_transaction_rolls_back_on_failure(db):
    with pytest.raises(RuntimeError), db.tx() as conn:
        conn.execute(
            "INSERT INTO events(run_id, ts, level, kind, message) VALUES(?,?,?,?,?)",
            (db.run_id, 1, "info", "x", "y"),
        )
        raise RuntimeError("boom")
    assert db.query("SELECT * FROM events") == []


def test_estimated_fees_survive_the_database(db):
    fill = Fill("o1", "BTC/USDT", Side.BUY, 1.0, 100.0, 0.0, 1, fee_estimated=0.1)
    db.record_fill(fill)
    assert db.query("SELECT fee, fee_estimated FROM fills") == [{"fee": 0.0, "fee_estimated": 0.1}]

    pos = Position(
        symbol="BTC/USDT", side=Side.BUY, quantity=1.0, entry_price=100.0, entry_ts=1,
        initial_stop=90.0, current_stop=90.0, setup="s", risk_amount=10.0,
        fees_estimated=0.1,
    )
    db.upsert_position(pos)
    assert db.load_positions()[0]["fees_estimated"] == 0.1

    trade = Trade(
        symbol="BTC/USDT", side=Side.BUY, setup="s", entry_ts=1, exit_ts=2,
        entry_price=100.0, exit_price=110.0, quantity=1.0, pnl=9.9, fees=0.1,
        r_multiple=1.0, mfe_r=1.0, mae_r=0.0, bars_held=1,
        exit_reason=ExitReason.TAKE_PROFIT, fees_estimated=0.11,
    )
    db.record_trade(trade)
    row = db.recent_trades()[0]
    assert row["fees_estimated"] == 0.11
    assert row["pnl_all_fees"] == 1  # written on the new basis, never restated
    assert db.realised_by_symbol()[0]["fees_estimated"] == pytest.approx(0.11)
    assert db.realised_by_setup()[0]["fees_estimated"] == pytest.approx(0.11)


_OLD_TRADES = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, symbol TEXT NOT NULL,
    side TEXT NOT NULL, setup TEXT NOT NULL, entry_ts INTEGER NOT NULL,
    exit_ts INTEGER NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
    quantity REAL NOT NULL, pnl REAL NOT NULL, fees REAL NOT NULL,
    r_multiple REAL NOT NULL, mfe_r REAL NOT NULL, mae_r REAL NOT NULL,
    bars_held INTEGER NOT NULL, exit_reason TEXT NOT NULL, regime_at_entry TEXT NOT NULL,
    conviction_at_entry REAL NOT NULL DEFAULT 0, leverage REAL NOT NULL DEFAULT 1.0,
    meta TEXT
);
"""


def _old_row(conn, side, entry, exit_, qty, pnl, fees):
    conn.execute(
        "INSERT INTO trades(symbol, side, setup, entry_ts, exit_ts, entry_price, exit_price,"
        " quantity, pnl, fees, r_multiple, mfe_r, mae_r, bars_held, exit_reason,"
        " regime_at_entry) VALUES('X/USDT', ?, 's', 1, 2, ?, ?, ?, ?, ?, 0, 0, 0, 1,"
        " 'take_profit', 'unknown')",
        (side, entry, exit_, qty, pnl, fees),
    )


def test_rows_written_on_the_old_basis_are_restated_once(tmp_path):
    """The old ``pnl`` left the entry fee out; every running engine has such rows.

    ``gross - fees`` recomputes the new figure from the row alone, and the gap
    must be an entry fee. A row whose gap is not one (a scale-out banked money
    the row does not carry) is left as written rather than restated wrongly.
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_TRADES)
    # long: gross +20, entry fee 0.2 and exit fee 0.22 -> old pnl 19.78
    _old_row(conn, "buy", 100.0, 110.0, 2.0, 20.0 - 0.22, 0.42)
    # short: gross +10, entry 0.06, exit 0.054 -> old pnl 9.946
    _old_row(conn, "sell", 100.0, 90.0, 1.0, 10.0 - 0.054, 0.114)
    # a scale-out banked +5 the row cannot show: gap 5.2 is not an entry fee
    _old_row(conn, "buy", 100.0, 110.0, 1.0, 10.0 + 5.0 - 0.2, 0.4)
    conn.commit()
    conn.close()

    database = Database(path)
    rows = database.query("SELECT pnl, pnl_all_fees, fees_estimated FROM trades ORDER BY id")
    assert rows[0]["pnl"] == pytest.approx(20.0 - 0.42) and rows[0]["pnl_all_fees"] == 1
    assert rows[1]["pnl"] == pytest.approx(10.0 - 0.114) and rows[1]["pnl_all_fees"] == 1
    assert rows[2]["pnl"] == pytest.approx(14.8) and rows[2]["pnl_all_fees"] == 0
    assert all(r["fees_estimated"] == 0.0 for r in rows)
    database.close()

    # The panel opens the same file: a second open must not subtract again.
    again = Database(path)
    assert again.query("SELECT pnl FROM trades ORDER BY id")[0]["pnl"] == pytest.approx(19.58)
    again.close()


def test_a_row_inserted_by_old_code_after_the_migration_is_restated_too(tmp_path):
    """Engine and panel restart separately: an engine still on the old code
    writes rows without mentioning the new column. The column's default marks
    them as old-basis so the next open brings them over."""
    path = tmp_path / "t.db"
    Database(path).close()
    import sqlite3

    conn = sqlite3.connect(path)
    _old_row(conn, "buy", 100.0, 110.0, 2.0, 20.0 - 0.22, 0.42)
    conn.commit()
    conn.close()

    database = Database(path)
    (row,) = database.query("SELECT pnl, pnl_all_fees FROM trades")
    assert row["pnl"] == pytest.approx(19.58) and row["pnl_all_fees"] == 1
    database.close()
