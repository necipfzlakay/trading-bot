"""Between-bar price feed.

The engine wakes on bar closes. Between them it was blind: a symbol could move
twenty percent and the bot would not look until the hour was up. This closes
that window — but only for the one question it is allowed to ask.

**What this feed may be used for.** Whether price has reached a level that was
already decided at the last bar close. Nothing else. The forming bar never
reaches an indicator, because an indicator computed on an unclosed bar cannot be
reproduced in a backtest, and invariant 1 is the reason any backtest number here
means anything at all.

So the feed maintains exactly three things per symbol: the last trade price, and
the high and low seen **since the last bar close**. Those two extremes are the
same quantity the backtester reads off a finished bar's ``high`` and ``low``,
arriving earlier. Same question, same code (``ExitManager``), earlier answer.

**What it is not.** It is not a dependency. A dropped socket makes the feed
stale, the engine skips the between-bar check and falls back entirely to
bar-close behaviour, and the protective stop sitting at the venue is untouched
throughout. The feed is an improvement on reaction time, never the thing that
keeps a position safe. Any design where a websocket outage costs money is the
wrong design for a market that trades continuously on a laptop.

**Threading (invariant D42).** This runs its own asyncio loop on its own thread
and writes to one dict behind one lock. It never touches the broker, the
database, the portfolio or the parquet store — the engine thread reads the dict
and does all the acting. Public market data needs no credentials, so no key
material reaches this thread either.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field

from tbot.core.logging import get_logger

log = get_logger("data.feed")

# Past this with no message at all, treat the feed as untrustworthy rather than
# quiet. A dead socket and a still market look identical from the outside, and
# assuming "still" is how a stale price gets acted on.
STALE_AFTER_SECONDS = 90.0

# Bybit sends ticker updates continuously; reconnect backoff keeps a venue
# outage from turning into a reconnect storm.
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

# **Measured, not documented.** Bybit's public spot websocket accepts at most
# ten topics in one subscribe message, and ccxt sends the whole list in one. Ask
# for eleven and the subscription never completes: no error, no exception, no
# close — the awaited future simply never resolves. Bisected on the live venue:
# ten symbols connect in two seconds, eleven time out indefinitely.
#
# Silence is the dangerous part. A feed that raised would reconnect and recover;
# a feed that hangs looks exactly like a quiet market, which is why this is a
# constant with a comment rather than a number in a loop.
MAX_TOPICS_PER_SUBSCRIPTION = 10


@dataclass(slots=True)
class Quote:
    """One symbol's running state since the last bar close."""

    last: float = 0.0
    high: float = 0.0
    low: float = 0.0
    ts: int = 0
    updates: int = 0

    def observe(self, price: float, ts: int) -> None:
        if price <= 0:
            return
        self.last = price
        self.high = price if self.high <= 0 else max(self.high, price)
        self.low = price if self.low <= 0 else min(self.low, price)
        self.ts = ts
        self.updates += 1

    def reset(self) -> None:
        """Start a new bar's extremes from the current price.

        Called by the engine at each bar close. The high and low must measure
        *this* bar so far, not everything since the process started — otherwise
        a stale extreme from hours ago would trigger an exit today.
        """
        self.high = self.last
        self.low = self.last
        self.updates = 0


@dataclass
class MarketFeed:
    """Streams tickers for a symbol list and exposes them to the engine thread.

    Usage from the engine::

        feed = MarketFeed(symbols)
        feed.start()
        ...
        quote = feed.quote("BTC/USDT")     # None when unknown or stale
        feed.reset_extremes()              # at every bar close
        feed.stop()
    """

    symbols: list[str]
    _quotes: dict[str, Quote] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _exchange: object | None = field(default=None, init=False)
    _running: bool = field(default=False, init=False)
    _last_message: float = field(default=0.0, init=False)
    _connected: bool = field(default=False, init=False)
    _errors: int = field(default=0, init=False)
    _last_error: str = field(default="", init=False)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Begin streaming. False when ccxt.pro is unavailable.

        A missing websocket library is a downgrade, not a failure: the caller
        carries on with bar-close behaviour.
        """
        if self._thread is not None:
            return True
        try:
            import ccxt.pro  # noqa: F401 — presence check only
        except ImportError:
            log.warning(
                "feed_unavailable",
                reason="ccxt.pro not importable",
                consequence="between-bar checks disabled; bar-close behaviour unchanged",
            )
            return False

        self._running = True
        self._thread = threading.Thread(target=self._run, name="market-feed", daemon=True)
        self._thread.start()
        log.info("feed_started", symbols=len(self.symbols))
        return True

    def stop(self) -> None:
        """Close the socket and join the thread.

        Closing the exchange from *inside* its own loop is what makes this
        clean: it wakes the pending ``watch_tickers`` so the stream coroutine
        returns and its tasks are awaited. Stopping the loop from outside
        instead leaves aiohttp connectors and ping tasks dangling, which on a
        restarting engine accumulates sockets until the process runs out.
        """
        self._running = False
        loop, thread = self._loop, self._thread
        exchange = self._exchange
        if loop is not None and loop.is_running() and exchange is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(exchange.close(), loop).result(timeout=5.0)
        if thread is not None:
            thread.join(timeout=5.0)
        if loop is not None and loop.is_running():
            # Only if the graceful path did not get there.
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=2.0)
        self._thread = None
        self._exchange = None
        log.info("feed_stopped")

    # -- engine-thread API -------------------------------------------------

    def quote(self, symbol: str) -> Quote | None:
        """This symbol's running quote, or None when unusable.

        Returns None rather than a stale price on purpose. A caller that gets a
        number will act on it; a caller that gets None skips the check and waits
        for the bar close, which is the behaviour the bot had before this feed
        existed and is always safe.
        """
        if self.is_stale:
            return None
        with self._lock:
            quote = self._quotes.get(symbol)
            if quote is None or quote.last <= 0:
                return None
            # A copy: the engine must not read a field mid-update, and must not
            # hold a reference the feed thread keeps writing to.
            return Quote(quote.last, quote.high, quote.low, quote.ts, quote.updates)

    def reset_extremes(self) -> None:
        """Roll the running high/low forward at a bar close."""
        with self._lock:
            for quote in self._quotes.values():
                quote.reset()

    @property
    def is_stale(self) -> bool:
        if not self._connected or self._last_message <= 0:
            return True
        return (time.time() - self._last_message) > STALE_AFTER_SECONDS

    def status(self) -> dict[str, object]:
        """What the dashboard and ``state.json`` report about the feed."""
        with self._lock:
            live = sum(1 for q in self._quotes.values() if q.last > 0)
        age = (time.time() - self._last_message) if self._last_message else None
        return {
            "connected": self._connected,
            "stale": self.is_stale,
            "symbols": live,
            "age_seconds": round(age, 1) if age is not None else None,
            "errors": self._errors,
            "last_error": self._last_error[:160],
        }

    # -- feed thread -------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._stream())
        except Exception as exc:  # noqa: BLE001 — a feed crash must not take the engine with it
            log.error("feed_thread_died", error=str(exc)[:200])
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                loop.close()

    async def _stream(self) -> None:
        import ccxt.pro as ccxtpro

        # No credentials: this is public market data. Keeping keys out of this
        # thread means a feed bug can never reach the account.
        exchange = ccxtpro.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self._exchange = exchange

        # One subscription per chunk, all multiplexed over the same socket. See
        # MAX_TOPICS_PER_SUBSCRIPTION: a single oversized subscribe hangs
        # forever and takes every symbol with it, including the ones that would
        # have worked on their own.
        chunks = [
            self.symbols[i : i + MAX_TOPICS_PER_SUBSCRIPTION]
            for i in range(0, len(self.symbols), MAX_TOPICS_PER_SUBSCRIPTION)
        ]
        try:
            await asyncio.gather(
                *(self._watch_chunk(exchange, chunk) for chunk in chunks),
                return_exceptions=True,
            )
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                await exchange.close()

    async def _watch_chunk(self, exchange, chunk: list[str]) -> None:
        """Keep one subscription alive, reconnecting on its own schedule.

        Each chunk fails independently: half the universe going quiet must not
        silence the other half, and a symbol delisted underneath us must not
        take the feed down with it.
        """
        attempt = 0
        while self._running:
            try:
                tickers = await exchange.watch_tickers(chunk)
                self._connected = True
                attempt = 0
                self._ingest(tickers)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — reconnect rather than give up
                if not self._running:
                    return  # stop() closed the socket underneath us; not an error
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                log.warning(
                    "feed_reconnecting",
                    symbols=len(chunk),
                    error=self._last_error[:160],
                    retry_in=delay,
                    consequence="between-bar checks paused for these symbols; "
                    "venue stop still in place",
                )
                await asyncio.sleep(delay)

    def _ingest(self, tickers: dict) -> None:
        now = int(time.time() * 1000)
        with self._lock:
            for symbol, ticker in (tickers or {}).items():
                price = ticker.get("last") or ticker.get("close")
                if price is None:
                    continue
                quote = self._quotes.get(symbol)
                if quote is None:
                    quote = Quote()
                    self._quotes[symbol] = quote
                quote.observe(float(price), int(ticker.get("timestamp") or now))
        self._last_message = time.time()
