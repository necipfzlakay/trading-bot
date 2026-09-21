"""Historical data acquisition from Bybit.

Bybit caps every history endpoint (1000 candles, 200 funding or open-interest
rows per request), so everything here is paginated and resumable: a backfill
interrupted at any point continues from what is already on disk rather than
starting over.

Derivatives live on the linear perpetual market even when we trade spot. That is
deliberate — funding, open interest and the long/short ratio are not derived
from spot price, which is exactly what makes them non-redundant with the
price-based indicators.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from tbot.core.clock import floor_bar, now_ms, tf_ms
from tbot.core.logging import get_logger
from tbot.data.store import OHLCV_COLUMNS, ParquetStore

log = get_logger("data.backfill")

OHLCV_PAGE = 1000  # Bybit v5 /market/kline hard cap
EPOCH_PROBE_MS = 1_420_070_400_000  # 2015-01-01, safely before any listing
DERIV_PAGE = 200  # funding-rate-history and open-interest caps


@dataclass(slots=True)
class BackfillResult:
    symbol: str
    timeframe: str
    bars_fetched: int
    total_bars: int
    first_ts: int | None
    last_ts: int | None
    error: str | None = None


# Pauses before retrying a request Bybit refused with 10006.
RATE_LIMIT_BACKOFF_S = (1.5, 4.0)


class BybitFetcher:
    """Public-data client. No API key needed for anything in this module."""

    def __init__(self, rate_limit_ms: int = 120):
        self.rate_limit_ms = rate_limit_ms
        self.spot = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self.linear = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})
        self._markets_loaded = False

    def load_markets(self) -> None:
        if not self._markets_loaded:
            self.spot.load_markets()
            self.linear.load_markets()
            self._markets_loaded = True

    def _pace(self) -> None:
        if self.rate_limit_ms:
            time.sleep(self.rate_limit_ms / 1000)

    def _fetch_page(self, client, symbol: str, timeframe: str, since: int) -> list:
        """One request, paced, and retried when the venue says "too many".

        The pause used to sit only *between pages of one series*, so a refresh
        of many short series — every symbol on every bar — went out unpaced at
        ccxt's own 20ms spacing, and Bybit answered ``10006 Too many visits``.
        A refused request is retried after a pause instead of costing the
        symbol its place in this bar's scan.
        """
        for attempt, backoff in enumerate((*RATE_LIMIT_BACKOFF_S, None)):
            self._pace()
            try:
                return client.fetch_ohlcv(symbol, timeframe, since=since, limit=OHLCV_PAGE)
            except ccxt.RateLimitExceeded:
                if backoff is None:
                    raise
                log.info("rate_limited_retry", symbol=symbol, tf=timeframe, attempt=attempt + 1)
                time.sleep(backoff)
        return []

    # -- universe ----------------------------------------------------------

    def _client(self, category: str) -> ccxt.bybit:
        return self.linear if category == "linear" else self.spot

    def listing_ts(self, symbol: str, category: str = "spot") -> int | None:
        """Timestamp of the earliest daily candle Bybit has for a symbol.

        Costs one request, so it is only called when refreshing the universe.
        Uses an explicit pre-crypto date rather than ``since=0``: ccxt drops a
        falsy ``since``, and the exchange then answers with the *newest* candle,
        which would make every symbol look freshly listed.
        """
        try:
            first = self._client(category).fetch_ohlcv(
                symbol, "1d", since=EPOCH_PROBE_MS, limit=1
            )
        except ccxt.BaseError:
            return None
        return int(first[0][0]) if first else None

    def select_universe(
        self,
        top_n: int = 30,
        quote: str = "USDT",
        min_volume_24h: float = 5_000_000,
        exclude: list[str] | None = None,
        min_history_days: int = 0,
        category: str = "spot",
    ) -> list[str]:
        """Most liquid pairs by 24h turnover, filtered by volume and history.

        Two filters, both deliberate. Stablecoin pairs carry volume but no
        directional opportunity. Newly listed pairs carry both volume and
        opportunity but cannot be validated: a strategy tested on a six-week
        listing has been tested against one regime, which is no test at all.

        ``category="linear"`` selects USDT-margined perpetuals instead of spot.
        The venue names them ``BTCUSDT`` in the ticker feed either way; ccxt
        calls the perpetual ``BTC/USDT:USDT``, and that settled form is what
        every downstream caller must see, because it is what distinguishes the
        contract from the spot pair of the same name.
        """
        self.load_markets()
        exclude_set = set(exclude or [])
        client = self._client(category)
        response = client.publicGetV5MarketTickers({"category": category})
        rows = response["result"]["list"]

        candidates: list[tuple[str, float]] = []
        for row in rows:
            raw = row["symbol"]
            if not raw.endswith(quote):
                continue
            base = raw[: -len(quote)]
            symbol = f"{base}/{quote}:{quote}" if category == "linear" else f"{base}/{quote}"
            if symbol in exclude_set or f"{base}/{quote}" in exclude_set:
                continue
            market = client.markets.get(symbol)
            if not market or not market.get("active"):
                continue
            turnover = float(row.get("turnover24h") or 0.0)
            if turnover < min_volume_24h:
                continue
            candidates.append((symbol, turnover))

        candidates.sort(key=lambda item: -item[1])

        selected: list[str] = []
        too_young: list[str] = []
        if min_history_days > 0:
            cutoff = now_ms() - min_history_days * 86_400_000
            for symbol, _ in candidates:
                if len(selected) >= top_n:
                    break
                listed = self.listing_ts(symbol, category)
                if listed is None or listed > cutoff:
                    too_young.append(symbol)
                    continue
                selected.append(symbol)
                self._pace()
        else:
            selected = [symbol for symbol, _ in candidates[:top_n]]

        log.info(
            "universe_selected",
            category=category,
            count=len(selected),
            considered=len(rows),
            passed_volume=len(candidates),
            rejected_young=len(too_young),
            min_volume=min_volume_24h,
            symbols=selected[:10],
        )
        if too_young:
            log.info("universe_excluded_new_listings", symbols=too_young[:15])
        return selected

    def market_spec(self, symbol: str) -> dict:
        """Exchange constraints for one spot symbol."""
        self.load_markets()
        m = (self.linear if ":" in symbol else self.spot).markets[symbol]
        return {
            "symbol": symbol,
            "base": m["base"],
            "quote": m["quote"],
            "min_notional": float(m["limits"]["cost"]["min"] or 0.0),
            "min_amount": float(m["limits"]["amount"]["min"] or 0.0),
            "amount_step": float(m["precision"]["amount"] or 0.0),
            "price_step": float(m["precision"]["price"] or 0.0),
            "maker_fee": float(m.get("maker") or 0.001),
            "taker_fee": float(m.get("taker") or 0.001),
            "active": bool(m.get("active")),
        }

    def linear_symbol(self, symbol: str) -> str | None:
        """Matching USDT perpetual for a symbol, if one is listed.

        A symbol that is *already* a settled contract is returned unchanged.
        Appending the settle currency a second time produced
        ``SOL/USDT:USDT:USDT``, which matches no market — so a perpetual
        universe silently backfilled zero funding, zero open interest and zero
        long/short ratio, and the derivatives category (10% of the conviction
        score) voted on nothing at all. It degrades safely, which is exactly
        what makes it easy to miss.
        """
        self.load_markets()
        candidate = symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"
        market = self.linear.markets.get(candidate)
        if market and market.get("active") and market.get("swap"):
            return candidate
        return None

    # -- candles -----------------------------------------------------------

    def fetch_ohlcv_range(
        self, symbol: str, timeframe: str, since_ts: int, until_ts: int | None = None
    ) -> pd.DataFrame:
        """Paginate candles forward from ``since_ts`` to ``until_ts``.

        Stops when the exchange returns nothing new, which is also how the true
        listing date is discovered — Bybit simply has no rows before it.
        """
        self.load_markets()
        until_ts = until_ts or now_ms()
        step = tf_ms(timeframe)
        cursor = floor_bar(since_ts, timeframe)
        rows: list[list[float]] = []

        while cursor < until_ts:
            # ``BTC/USDT:USDT`` is a contract and ``BTC/USDT`` is a pair. They
            # have different prices, different history and different minimums,
            # so the settled form has to be fetched from the linear market.
            client = self.linear if ":" in symbol else self.spot
            batch = self._fetch_page(client, symbol, timeframe, cursor)
            if not batch:
                break
            rows.extend(batch)
            last = int(batch[-1][0])
            if last < cursor:  # exchange went backwards; bail rather than loop
                break
            cursor = last + step
            if len(batch) < OHLCV_PAGE:
                break

        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
        df = df[df["ts"] < until_ts]
        return df.drop_duplicates(subset="ts", keep="last").sort_values("ts").reset_index(drop=True)

    # -- derivatives -------------------------------------------------------

    def fetch_funding_history(
        self, linear_symbol: str, since_ts: int, until_ts: int | None = None
    ) -> pd.DataFrame:
        """Funding rate history (published every 8 hours on most perps)."""
        self.load_markets()
        until_ts = until_ts or now_ms()
        cursor = since_ts
        rows: list[dict] = []

        while cursor < until_ts:
            batch = self.linear.fetch_funding_rate_history(
                linear_symbol, since=cursor, limit=DERIV_PAGE
            )
            if not batch:
                break
            for entry in batch:
                rows.append(
                    {"ts": int(entry["timestamp"]), "funding_rate": float(entry["fundingRate"])}
                )
            last = int(batch[-1]["timestamp"])
            if last <= cursor:
                break
            cursor = last + 1
            if len(batch) < DERIV_PAGE:
                break
            self._pace()

        return pd.DataFrame(rows, columns=["ts", "funding_rate"])

    def fetch_open_interest_history(
        self, linear_symbol: str, timeframe: str = "1h", limit_pages: int = 30
    ) -> pd.DataFrame:
        """Open interest history, walked backwards from now.

        Bybit serves a limited window here (far shorter than candle history), so
        this walks back page by page and stops when the exchange runs out. The
        integrity report flags the resulting shorter coverage rather than
        pretending the series is as long as the price series.
        """
        self.load_markets()
        rows: list[dict] = []
        cursor: int | None = None

        for _ in range(limit_pages):
            params = {"until": cursor} if cursor else {}
            batch = self.linear.fetch_open_interest_history(
                linear_symbol, timeframe, limit=DERIV_PAGE, params=params
            )
            if not batch:
                break
            for entry in batch:
                amount = entry.get("openInterestAmount")
                if amount is None:
                    continue
                rows.append({"ts": int(entry["timestamp"]), "open_interest": float(amount)})
            oldest = min(int(e["timestamp"]) for e in batch)
            if cursor is not None and oldest >= cursor:
                break
            cursor = oldest - 1
            if len(batch) < DERIV_PAGE:
                break
            self._pace()

        df = pd.DataFrame(rows, columns=["ts", "open_interest"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    def fetch_long_short_ratio(
        self, linear_symbol: str, period: str = "1h", limit_pages: int = 30
    ) -> pd.DataFrame:
        """Account long/short ratio — crowding, straight from the exchange.

        Uses the raw v5 endpoint because ccxt has no unified method for it.
        """
        self.load_markets()
        raw_symbol = linear_symbol.split(":")[0].replace("/", "")
        rows: list[dict] = []
        cursor: int | None = None

        for _ in range(limit_pages):
            params = {
                "category": "linear",
                "symbol": raw_symbol,
                "period": period,
                "limit": DERIV_PAGE,
            }
            if cursor:
                params["endTime"] = cursor
            try:
                response = self.linear.publicGetV5MarketAccountRatio(params)
            except ccxt.BaseError as exc:
                log.warning("long_short_fetch_failed", symbol=linear_symbol, error=str(exc)[:120])
                break

            batch = response.get("result", {}).get("list", [])
            if not batch:
                break
            for entry in batch:
                buy = float(entry["buyRatio"])
                sell = float(entry["sellRatio"])
                rows.append(
                    {
                        "ts": int(entry["timestamp"]),
                        "long_short_ratio": buy / sell if sell > 0 else 0.0,
                        "long_account_pct": buy,
                    }
                )
            oldest = min(int(e["timestamp"]) for e in batch)
            if cursor is not None and oldest >= cursor:
                break
            cursor = oldest - 1
            if len(batch) < DERIV_PAGE:
                break
            self._pace()

        df = pd.DataFrame(rows, columns=["ts", "long_short_ratio", "long_account_pct"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


class Backfiller:
    """Drives a fetcher into a store, resuming from whatever is already there."""

    def __init__(self, fetcher: BybitFetcher, store: ParquetStore):
        self.fetcher = fetcher
        self.store = store

    def backfill_symbol(
        self, symbol: str, timeframe: str, years: float = 3.0, until_ts: int | None = None
    ) -> BackfillResult:
        until_ts = until_ts or now_ms()
        target_start = until_ts - int(years * 365.25 * 86_400_000)

        stored_last = self.store.last_ts(symbol, timeframe)
        since = stored_last + tf_ms(timeframe) if stored_last is not None else target_start

        # Nothing has closed since the last stored bar: the only candle the
        # venue could return is the forming one, which the store refuses
        # (invariant 1). Asking anyway was most of the traffic — refreshing 79
        # symbols every hour re-requested each 4h and 1d series that had not
        # closed a bar, ~160 of ~237 calls, and the burst drew 10006 refusals
        # that knocked 12 symbols out of one bar's scan (measured, fast engine).
        if stored_last is not None and since + tf_ms(timeframe) > until_ts:
            return BackfillResult(
                symbol=symbol,
                timeframe=timeframe,
                bars_fetched=0,
                total_bars=len(self.store.read_ohlcv(symbol, timeframe)),
                first_ts=self.store.first_ts(symbol, timeframe),
                last_ts=stored_last,
            )

        try:
            df = self.fetcher.fetch_ohlcv_range(symbol, timeframe, since, until_ts)
        except ccxt.BaseError as exc:
            log.warning("ohlcv_fetch_failed", symbol=symbol, tf=timeframe, error=str(exc)[:160])
            return BackfillResult(symbol, timeframe, 0, 0, None, None, error=str(exc)[:200])

        fetched = len(df)
        total = (
            self.store.write_ohlcv(symbol, timeframe, df)
            if fetched
            else (len(self.store.read_ohlcv(symbol, timeframe)))
        )

        result = BackfillResult(
            symbol=symbol,
            timeframe=timeframe,
            bars_fetched=fetched,
            total_bars=total,
            first_ts=self.store.first_ts(symbol, timeframe),
            last_ts=self.store.last_ts(symbol, timeframe),
        )
        log.info(
            "backfilled",
            symbol=symbol,
            tf=timeframe,
            fetched=fetched,
            total=total,
        )
        return result

    def backfill_derivatives(self, symbol: str, years: float = 3.0) -> dict[str, int]:
        """Fetch funding, open interest and long/short for a symbol's perp.

        Symbols without a listed perpetual return zeros; that is a normal outcome
        for smaller spot pairs, not an error.
        """
        linear = self.fetcher.linear_symbol(symbol)
        if linear is None:
            log.debug("no_perp_for_symbol", symbol=symbol)
            return {"funding": 0, "open_interest": 0, "long_short": 0}

        counts: dict[str, int] = {}
        until = now_ms()
        since = until - int(years * 365.25 * 86_400_000)

        last_funding = self.store.last_derivative_ts(symbol, "funding")
        funding = self.fetcher.fetch_funding_history(
            linear, since if last_funding is None else last_funding + 1, until
        )
        counts["funding"] = self.store.write_derivative(symbol, "funding", funding)

        oi = self.fetcher.fetch_open_interest_history(linear, "1h")
        counts["open_interest"] = self.store.write_derivative(symbol, "open_interest", oi)

        ls = self.fetcher.fetch_long_short_ratio(linear, "1h")
        counts["long_short"] = self.store.write_derivative(symbol, "long_short", ls)

        log.info("derivatives_backfilled", symbol=symbol, perp=linear, **counts)
        return counts
