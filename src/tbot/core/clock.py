"""UTC time handling and bar-boundary arithmetic.

Every timestamp in the system is UTC milliseconds. Bar alignment lives here and
nowhere else, because an off-by-one bar boundary is the quietest way to
introduce lookahead: acting on a bar that has not actually closed yet reads the
future without ever looking like it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

# Canonical timeframes. Keys are what config and storage use; values are the
# duration in milliseconds. ccxt and Bybit both accept these string forms.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

# Bybit v5 uses bare minute counts plus D/W/M rather than ccxt's "1h" style.
BYBIT_INTERVAL: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
}


def tf_ms(timeframe: str) -> int:
    """Duration of one bar in milliseconds."""
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; known: {sorted(TIMEFRAME_MS)}"
        ) from None


def now_ms() -> int:
    """Current UTC time in milliseconds."""
    return int(time.time() * 1000)


def to_ms(dt: datetime | str | int) -> int:
    """Coerce a datetime, ISO string or epoch-ms int to UTC milliseconds."""
    if isinstance(dt, int):
        return dt
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def to_dt(ms: int) -> datetime:
    """UTC datetime from epoch milliseconds."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def iso(ms: int) -> str:
    """Human-readable UTC string, used in logs and reports."""
    return to_dt(ms).strftime("%Y-%m-%d %H:%M:%S")


# The Unix epoch fell on a Thursday, so epoch-aligned weekly buckets would start
# on Thursdays. Exchanges start the week on Monday, which is four days later.
# Every timeframe up to 1d divides the day evenly and needs no offset.
TIMEFRAME_EPOCH_OFFSET_MS: dict[str, int] = {"1w": 4 * 86_400_000}


def timeframe_offset(timeframe: str) -> int:
    """Milliseconds the timeframe's grid is shifted from the Unix epoch."""
    return TIMEFRAME_EPOCH_OFFSET_MS.get(timeframe, 0)


def floor_bar(ts: int, timeframe: str) -> int:
    """Open time of the bar containing ``ts``.

    Bars up to 1d align to the Unix epoch, matching how exchanges bucket
    candles. Weekly bars do not: the epoch was a Thursday and the trading week
    starts on Monday, so the grid is offset by four days. Ignoring that makes
    every weekly timestamp look misaligned — which is exactly what the storage
    layer's integrity check reported the first time weekly data was fetched.
    """
    step = tf_ms(timeframe)
    offset = timeframe_offset(timeframe)
    return ts - ((ts - offset) % step)


def next_bar(ts: int, timeframe: str) -> int:
    """Open time of the bar after the one containing ``ts``."""
    return floor_bar(ts, timeframe) + tf_ms(timeframe)


def bar_close_ts(bar_open_ts: int, timeframe: str) -> int:
    """The instant a bar stops accepting trades (exclusive end)."""
    return bar_open_ts + tf_ms(timeframe)


def is_bar_closed(bar_open_ts: int, timeframe: str, now: int | None = None) -> bool:
    """True once the bar is complete and therefore safe to compute on."""
    return (now if now is not None else now_ms()) >= bar_close_ts(bar_open_ts, timeframe)


def last_closed_bar(timeframe: str, now: int | None = None) -> int:
    """Open time of the most recent fully-closed bar.

    This is the newest bar an indicator is allowed to see.
    """
    return floor_bar(now if now is not None else now_ms(), timeframe) - tf_ms(timeframe)


def ms_until_next_close(timeframe: str, now: int | None = None) -> int:
    """Milliseconds to wait before the current bar closes. Never negative."""
    now = now if now is not None else now_ms()
    return max(0, next_bar(now, timeframe) - now)


def bars_between(start_ts: int, end_ts: int, timeframe: str) -> int:
    """How many bars fit in a span — used to size backfill requests."""
    return max(
        0, (floor_bar(end_ts, timeframe) - floor_bar(start_ts, timeframe)) // tf_ms(timeframe)
    )


def align_timeframes(timeframes: list[str]) -> list[str]:
    """Sort timeframes from fastest to slowest."""
    return sorted(timeframes, key=tf_ms)


def is_stale(
    last_bar_ts: int, timeframe: str, now: int | None = None, tolerance: float = 2.0
) -> bool:
    """True when data has fallen behind by more than ``tolerance`` bars.

    Stale data means the bot is reasoning about a market that has moved on, so
    the engine stops opening new positions when this trips.
    """
    now = now if now is not None else now_ms()
    return (now - last_bar_ts) > tf_ms(timeframe) * (1.0 + tolerance)
