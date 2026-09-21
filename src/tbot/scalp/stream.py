"""One-minute closed bars and last prices, streamed.

The bar engine tops up a parquet archive over REST on every close. At one
minute that is impossible — measured, the fast engine needs ~3.5 minutes to
refresh 79 symbols, and its public REST calls already draw ``10006`` rate-limit
refusals at the top of the hour. So the scalper keeps its bars in memory and
takes them from the websocket.

**Only closed bars leave this module** (invariant 1). The stream tracks each
symbol's forming candle and hands it over only once a *later* minute has begun.
Bybit sends a final update for a finished candle just before the next one opens;
that update replaces the forming values, so the bar that closes carries the
venue's final prices, not an intermediate tick.

**Gaps are reported, never papered over.** A reconnect can skip minutes. A
closed bar that does not follow the previous one exactly marks the symbol, and
the engine thread refills it over REST before the signal sees it again — a
missing minute in a 60-bar volatility window would quietly change the trigger.

**Threading (invariant 42).** Its own asyncio loop, its own thread, one lock,
no credentials. It never touches the broker, the database or the portfolio; the
engine thread reads what it needs and does all the acting.

**Topic chunks.** Linear accepted 20 kline topics in one subscribe (measured,
research/scalp_venue_facts.md), unlike spot's silent hang at 11 (invariant 43).
Chunks of ten are kept anyway: the measured ceiling is a fact about today, and
a chunk that hangs takes only its own ten symbols with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from tbot.core.logging import get_logger

log = get_logger("scalp.stream")

MINUTE_MS = 60_000
# A minute strategy cannot wait the bar feed's 90 seconds: past this with no
# message at all, no new entry is opened. Open positions are unaffected — their
# stop and target rest at the venue.
STALE_AFTER_SECONDS = 30.0
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
TOPICS_PER_SUBSCRIPTION = 10


@dataclass
class KlineStream:
    symbols: list[str]
    history: int = 240

    _closed: dict[str, deque] = field(default_factory=dict, init=False)
    _forming: dict[str, list[float]] = field(default_factory=dict, init=False)
    _fresh: set[str] = field(default_factory=set, init=False)
    _gaps: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _exchange: object | None = field(default=None, init=False)
    _running: bool = field(default=False, init=False)
    _connected: bool = field(default=False, init=False)
    _last_message: float = field(default=0.0, init=False)
    _errors: int = field(default=0, init=False)
    _last_error: str = field(default="", init=False)

    # -- engine thread -----------------------------------------------------

    def seed(self, symbol: str, candles: list[list[float]]) -> None:
        """Load closed history fetched over REST. Replaces whatever was held."""
        with self._lock:
            buffer: deque = deque(maxlen=self.history)
            for candle in sorted(candles, key=lambda c: c[0]):
                buffer.append([float(v) for v in candle[:6]])
            self._closed[symbol] = buffer
            self._gaps.discard(symbol)

    def drain(self) -> list[str]:
        """Symbols that closed a new bar since the last call."""
        with self._lock:
            fresh = sorted(self._fresh)
            self._fresh.clear()
            return fresh

    def take_gaps(self) -> set[str]:
        with self._lock:
            gaps = set(self._gaps)
            self._gaps.clear()
            return gaps

    def closes(self, symbol: str) -> list[float]:
        with self._lock:
            return [c[4] for c in self._closed.get(symbol, ())]

    def bars(self, symbol: str) -> list[list[float]]:
        with self._lock:
            return [list(c) for c in self._closed.get(symbol, ())]

    def last_bar_ts(self, symbol: str) -> int:
        with self._lock:
            buffer = self._closed.get(symbol)
            return int(buffer[-1][0]) if buffer else 0

    def last_price(self, symbol: str) -> float | None:
        """The latest trade price seen, forming bar included. None when stale."""
        if self.is_stale:
            return None
        with self._lock:
            forming = self._forming.get(symbol)
            if forming is not None:
                return forming[4]
            buffer = self._closed.get(symbol)
            return buffer[-1][4] if buffer else None

    @property
    def is_stale(self) -> bool:
        if not self._connected or self._last_message <= 0:
            return True
        return (time.time() - self._last_message) > STALE_AFTER_SECONDS

    def status(self) -> dict[str, object]:
        age = (time.time() - self._last_message) if self._last_message else None
        with self._lock:
            live = sum(1 for s in self.symbols if s in self._forming)
        return {
            "connected": self._connected,
            "stale": self.is_stale,
            "symbols": live,
            "age_seconds": round(age, 1) if age is not None else None,
            "errors": self._errors,
            "last_error": self._last_error[:160],
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            import ccxt.pro  # noqa: F401 — presence check only
        except ImportError:
            log.error("stream_unavailable", reason="ccxt.pro not importable")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="scalp-stream", daemon=True)
        self._thread.start()
        log.info("stream_started", symbols=len(self.symbols))
        return True

    def stop(self) -> None:
        """Close the socket from inside its own loop, then join (see ``MarketFeed.stop``)."""
        self._running = False
        loop, thread, exchange = self._loop, self._thread, self._exchange
        if loop is not None and loop.is_running() and exchange is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(exchange.close(), loop).result(timeout=5.0)
        if thread is not None:
            thread.join(timeout=5.0)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=2.0)
        self._thread = None
        self._exchange = None
        log.info("stream_stopped")

    # -- stream thread -----------------------------------------------------

    def ingest(self, symbol: str, candle: list[float]) -> None:
        """Fold one kline update in. Public so it can be exercised without a socket."""
        candle = [float(v) for v in candle[:6]]
        with self._lock:
            forming = self._forming.get(symbol)
            buffer = self._closed.setdefault(symbol, deque(maxlen=self.history))
            if forming is None or candle[0] == forming[0]:
                if buffer and candle[0] <= buffer[-1][0]:
                    self._finalise(buffer, candle)
                else:
                    self._forming[symbol] = candle
                return
            if candle[0] < forming[0]:
                # A late final update for a bar already handed over.
                self._finalise(buffer, candle)
                return
            # A later minute has begun: the forming bar is closed.
            if buffer and buffer[-1][0] + MINUTE_MS < forming[0]:
                self._gaps.add(symbol)  # minutes went missing in between
            if not buffer or forming[0] > buffer[-1][0]:
                buffer.append(forming)
                self._fresh.add(symbol)
            self._forming[symbol] = candle

    @staticmethod
    def _finalise(buffer: deque, candle: list[float]) -> None:
        for i in range(len(buffer) - 1, -1, -1):
            if buffer[i][0] == candle[0]:
                buffer[i] = candle
                return

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._stream())
        except Exception as exc:  # noqa: BLE001 — a stream crash must not take the engine
            log.error("stream_thread_died", error=str(exc)[:200])
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                loop.close()

    async def _stream(self) -> None:
        import ccxt.pro as ccxtpro

        exchange = ccxtpro.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})
        self._exchange = exchange
        chunks = [
            self.symbols[i : i + TOPICS_PER_SUBSCRIPTION]
            for i in range(0, len(self.symbols), TOPICS_PER_SUBSCRIPTION)
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
        attempt = 0
        topics = [[symbol, "1m"] for symbol in chunk]
        while self._running:
            try:
                update = await exchange.watch_ohlcv_for_symbols(topics)
                self._connected = True
                self._last_message = time.time()
                attempt = 0
                for symbol, by_tf in (update or {}).items():
                    for candle in sorted((by_tf or {}).get("1m") or [], key=lambda c: c[0]):
                        self.ingest(symbol, candle)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — reconnect rather than give up
                if not self._running:
                    return
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                log.warning(
                    "stream_reconnecting",
                    symbols=len(chunk),
                    error=self._last_error[:160],
                    retry_in=delay,
                    consequence="no new entries on these symbols; open positions "
                    "are protected at the venue",
                )
                # A reconnect may skip minutes; have them refilled before use.
                with self._lock:
                    self._gaps.update(chunk)
                await asyncio.sleep(delay)
