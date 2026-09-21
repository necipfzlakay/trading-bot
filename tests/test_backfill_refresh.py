"""The per-bar data refresh: ask only for what has closed, and survive "too many".

Measured on the fast engine at 14:00: ~237 unpaced kline requests for 79
symbols, 40 of them refused ``10006``, and 12 symbols dropped out of that bar's
scan as stale. Most of the requests could not have returned anything the store
would keep.
"""

from __future__ import annotations

import ccxt
import pandas as pd
import pytest

from tbot.core.clock import tf_ms
from tbot.data.backfill import Backfiller, BybitFetcher

HOUR = 3_600_000


class Store:
    def __init__(self, last_ts):
        self._last = last_ts

    def last_ts(self, _symbol, _tf):
        return self._last

    def first_ts(self, _symbol, _tf):
        return 0

    def read_ohlcv(self, _symbol, _tf):
        return pd.DataFrame({"ts": [0]})

    def write_ohlcv(self, _symbol, _tf, df):
        return len(df)


class Fetcher:
    def __init__(self):
        self.calls = 0

    def fetch_ohlcv_range(self, _symbol, _tf, _since, _until):
        self.calls += 1
        return pd.DataFrame()


@pytest.mark.parametrize(
    ("timeframe", "last_stored_open", "now", "expected_calls"),
    [
        # 14:00:05, 4h series whose last stored bar opened 08:00 (closed 12:00):
        # the 12:00 bar is still forming until 16:00, so there is nothing to ask.
        ("4h", 8 * HOUR, 14 * HOUR + 5_000, 0),
        # Same series at 16:00:05: the 12:00 bar has closed and must be fetched.
        ("4h", 8 * HOUR, 16 * HOUR + 5_000, 1),
        # 1h series at 14:00:05 with the 12:00 bar stored: 13:00 has just closed.
        ("1h", 12 * HOUR, 14 * HOUR + 5_000, 1),
    ],
)
def test_a_series_is_requested_only_when_a_new_bar_has_closed(
    timeframe, last_stored_open, now, expected_calls
):
    fetcher = Fetcher()
    Backfiller(fetcher, Store(last_stored_open)).backfill_symbol(
        "SOL/USDT:USDT", timeframe, years=0.1, until_ts=now
    )
    assert fetcher.calls == expected_calls


def test_a_symbol_with_no_history_is_always_requested():
    fetcher = Fetcher()
    Backfiller(fetcher, Store(None)).backfill_symbol(
        "SOL/USDT:USDT", "1d", years=0.1, until_ts=tf_ms("1d") * 10
    )
    assert fetcher.calls == 1


def test_a_rate_limited_request_is_retried_rather_than_dropping_the_symbol(monkeypatch):
    monkeypatch.setattr("tbot.data.backfill.time.sleep", lambda _s: None)

    class Client:
        def __init__(self):
            self.calls = 0

        def fetch_ohlcv(self, *_a, **_k):
            self.calls += 1
            if self.calls == 1:
                raise ccxt.RateLimitExceeded('bybit {"retCode":10006,"retMsg":"Too many visits."}')
            return [[0, 1, 1, 1, 1, 1]]

    fetcher = BybitFetcher.__new__(BybitFetcher)
    fetcher.rate_limit_ms = 0
    client = Client()
    assert fetcher._fetch_page(client, "SOL/USDT:USDT", "1h", 0) == [[0, 1, 1, 1, 1, 1]]
    assert client.calls == 2


def test_a_venue_that_keeps_refusing_still_surfaces_the_error(monkeypatch):
    monkeypatch.setattr("tbot.data.backfill.time.sleep", lambda _s: None)

    class Client:
        def fetch_ohlcv(self, *_a, **_k):
            raise ccxt.RateLimitExceeded("10006")

    fetcher = BybitFetcher.__new__(BybitFetcher)
    fetcher.rate_limit_ms = 0
    with pytest.raises(ccxt.RateLimitExceeded):
        fetcher._fetch_page(Client(), "SOL/USDT:USDT", "1h", 0)
