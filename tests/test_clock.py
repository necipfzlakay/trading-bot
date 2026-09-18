"""Bar-boundary arithmetic. An off-by-one here is silent lookahead."""

import pytest

from tbot.core import clock


def test_timeframe_durations():
    assert clock.tf_ms("1m") == 60_000
    assert clock.tf_ms("1h") == 3_600_000
    assert clock.tf_ms("4h") == 14_400_000
    assert clock.tf_ms("1d") == 86_400_000
    with pytest.raises(ValueError, match="unknown timeframe"):
        clock.tf_ms("7h")


def test_floor_bar_aligns_to_epoch():
    ts = clock.to_ms("2026-09-17T13:47:31")
    assert clock.floor_bar(ts, "1h") == clock.to_ms("2026-09-17T13:00:00")
    assert clock.floor_bar(ts, "4h") == clock.to_ms("2026-09-17T12:00:00")
    assert clock.floor_bar(ts, "1d") == clock.to_ms("2026-09-17T00:00:00")


def test_floor_bar_is_idempotent():
    ts = clock.to_ms("2026-09-17T13:47:31")
    once = clock.floor_bar(ts, "4h")
    assert clock.floor_bar(once, "4h") == once


def test_bar_closed_boundary_is_exclusive_on_open_inclusive_on_close():
    # A bar opening at 12:00 on 1h is closed exactly at 13:00, not a ms earlier.
    open_ts = clock.to_ms("2026-09-17T12:00:00")
    assert clock.is_bar_closed(open_ts, "1h", now=open_ts + 3_599_999) is False
    assert clock.is_bar_closed(open_ts, "1h", now=open_ts + 3_600_000) is True


def test_last_closed_bar_never_returns_forming_bar():
    now = clock.to_ms("2026-09-17T13:00:00")  # exactly on a boundary
    # The 13:00 bar has just opened and is still forming, so 12:00 is the newest
    # bar an indicator may see.
    assert clock.last_closed_bar("1h", now=now) == clock.to_ms("2026-09-17T12:00:00")
    assert clock.last_closed_bar("1h", now=now + 1) == clock.to_ms("2026-09-17T12:00:00")


def test_ms_until_next_close_never_negative():
    for offset in (0, 1, 3_599_999, 3_600_000):
        assert clock.ms_until_next_close("1h", now=offset) >= 0


def test_bars_between():
    start = clock.to_ms("2026-09-01T00:00:00")
    end = clock.to_ms("2026-09-17T00:00:00")
    assert clock.bars_between(start, end, "1d") == 16
    assert clock.bars_between(start, end, "4h") == 96
    assert clock.bars_between(end, start, "1d") == 0  # reversed span -> zero


def test_staleness_detection():
    bar = clock.to_ms("2026-09-17T12:00:00")
    fresh = bar + clock.tf_ms("1h")
    assert clock.is_stale(bar, "1h", now=fresh) is False
    assert clock.is_stale(bar, "1h", now=bar + 4 * clock.tf_ms("1h")) is True


def test_roundtrip_ms_datetime():
    ts = clock.to_ms("2026-09-17T13:47:31")
    assert clock.to_ms(clock.to_dt(ts)) == ts
    assert clock.iso(ts) == "2026-09-17 13:47:31"


def test_bybit_interval_mapping_covers_configured_timeframes():
    for tf in ("1h", "4h", "1d"):
        assert tf in clock.BYBIT_INTERVAL
    assert clock.BYBIT_INTERVAL["1h"] == "60"
    assert clock.BYBIT_INTERVAL["1d"] == "D"


def test_weekly_bars_align_to_monday_not_the_epoch():
    """The Unix epoch was a Thursday; the trading week starts on Monday.

    Treating weekly bars as epoch-aligned makes every real weekly timestamp look
    misaligned — which is how this was found: the storage layer rejected an
    entire weekly backfill as corrupt.
    """
    monday = clock.to_ms("2023-09-18T00:00:00")
    assert clock.to_dt(monday).strftime("%A") == "Monday"
    assert clock.floor_bar(monday, "1w") == monday

    midweek = clock.to_ms("2023-09-20T13:00:00")
    assert clock.floor_bar(midweek, "1w") == monday

    assert clock.timeframe_offset("1w") == 4 * 86_400_000
    assert clock.timeframe_offset("1d") == 0
    assert clock.timeframe_offset("1h") == 0


def test_sub_daily_timeframes_stay_epoch_aligned():
    ts = clock.to_ms("2026-09-17T13:47:31")
    for timeframe in ("1h", "4h", "1d"):
        assert clock.floor_bar(ts, timeframe) % clock.tf_ms(timeframe) == 0


def test_weekly_bar_close_is_the_next_monday():
    monday = clock.to_ms("2023-09-18T00:00:00")
    assert clock.bar_close_ts(monday, "1w") == clock.to_ms("2023-09-25T00:00:00")
