"""Data contracts for the whole system.

Conventions that everything else depends on:

* **Time** is UTC milliseconds since epoch (``int``), matching ccxt and Bybit v5.
  Never store naive datetimes. Use :mod:`tbot.core.clock` for conversions.
* **Numbers** are ``float`` (IEEE-754 double). Crypto prices need ~8 significant
  digits and PnL accumulates over thousands of trades with relative error around
  1e-12, so Decimal's friction is not worth it. Exchange-side rounding is handled
  at the boundary by ccxt's ``amount_to_precision`` / ``price_to_precision``,
  which return strings — never round manually before sending an order.
* **Prices** are always quote currency per base unit (USDT per BTC).
* **Quantities** are always in base units (BTC), never in quote.
* **R** is the risk unit: 1R = the distance from entry to the initial stop.
  Every performance number the system reports is R-normalised so results stay
  comparable across symbols, position sizes and account equity levels.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class Mode(StrEnum):
    """How the engine sources data and routes orders.

    ``BACKTEST`` replays stored candles, ``PAPER`` uses the live feed with a
    simulated broker, ``LIVE`` sends real orders (to demo or real credentials —
    that distinction is :class:`Venue`, not mode).
    """

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Venue(StrEnum):
    BYBIT_DEMO = "bybit-demo"
    BYBIT_LIVE = "bybit-live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for long-increasing, -1 for long-decreasing."""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"  # created locally, not yet acknowledged
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class Regime(StrEnum):
    """Daily-timeframe market regime. Gates which setups may even be scored."""

    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    UNKNOWN = "unknown"


class IndicatorCategory(StrEnum):
    """Category diversity is the defence against counting the same signal twice.

    Redundancy analysis (``indicators/redundancy.py``) collapses correlated
    indicators into one voting block; category is the prior, correlation is the
    measurement.
    """

    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    FLOW = "flow"  # volume / order flow
    DERIVATIVES = "derivatives"  # funding, OI, long-short ratio — not price-derived


class ExitReason(StrEnum):
    TRAILING_STOP = "trailing_stop"
    INITIAL_STOP = "initial_stop"
    TAKE_PROFIT = "take_profit"
    REGIME_FLIP = "regime_flip"
    CONVICTION_COLLAPSE = "conviction_collapse"
    VOLATILITY_SHOCK = "volatility_shock"
    MANUAL_FLATTEN = "manual_flatten"
    LIQUIDATION = "liquidation"
    BACKTEST_END = "backtest_end"


class NoTradeReason(StrEnum):
    """Why a scanned symbol did not become a position on this bar.

    Logged for every symbol on every bar. Without this, "why did the bot do
    nothing yesterday?" is unanswerable except by guessing.
    """

    BELOW_THRESHOLD = "below_threshold"
    NO_SETUP = "no_setup"  # scored well enough, but nothing triggered an entry
    REGIME_VETO = "regime_veto"
    HTF_VETO = "htf_veto"  # higher timeframe disagrees
    CORRELATION_FULL = "correlation_full"
    MAX_POSITIONS = "max_positions"
    ALREADY_IN_POSITION = "already_in_position"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LIQUIDITY_TOO_LOW = "liquidity_too_low"
    MIN_NOTIONAL = "min_notional"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    LLM_EVENT_VETO = "llm_event_veto"
    STALE_DATA = "stale_data"
    RISK_BUDGET_EXHAUSTED = "risk_budget_exhausted"
    COOLDOWN_SAME_BAR = "cooldown_same_bar"
    ENTERED = "entered"  # not a rejection — the row that did become a trade


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Candle:
    """One OHLCV bar. ``ts`` is the bar's OPEN time in UTC ms.

    A candle enters the system only once it is closed. The engine never feeds a
    forming bar to indicators — that is what makes lookahead structurally
    impossible rather than merely discouraged.
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(slots=True, frozen=True)
class DerivativeSnapshot:
    """Derivatives-market state at a point in time.

    Structurally independent of price-derived indicators, which is exactly why
    it earns its own indicator category.
    """

    ts: int
    funding_rate: float | None = None
    open_interest: float | None = None
    long_short_ratio: float | None = None
    taker_buy_volume: float | None = None
    taker_sell_volume: float | None = None

    @property
    def cvd_delta(self) -> float | None:
        """Cumulative volume delta contribution for this period."""
        if self.taker_buy_volume is None or self.taker_sell_volume is None:
            return None
        return self.taker_buy_volume - self.taker_sell_volume


@dataclass(slots=True, frozen=True)
class BookSnapshot:
    ts: int
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return 0.0 if mid <= 0 else (self.ask - self.bid) / mid * 10_000.0

    @property
    def imbalance(self) -> float:
        """-1 (all ask) .. +1 (all bid)."""
        total = self.bid_volume + self.ask_volume
        return 0.0 if total <= 0 else (self.bid_volume - self.ask_volume) / total


@dataclass(slots=True, frozen=True)
class MarketSpec:
    """Exchange constraints for one symbol. Every order must satisfy these."""

    symbol: str
    base: str
    quote: str
    min_notional: float
    min_amount: float
    amount_step: float
    price_step: float
    maker_fee: float
    taker_fee: float
    active: bool = True

    def replace(self, **changes) -> MarketSpec:
        """Copy with overrides. ``slots=True`` means there is no ``__dict__``
        to splat, so this is the supported way to derive a modified spec."""
        return dataclasses.replace(self, **changes)


# --------------------------------------------------------------------------
# Signals and conviction
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IndicatorSignal:
    """One indicator's normalised opinion.

    ``value`` is always in [-1, +1] regardless of the indicator's native scale,
    so signals from RSI, ADX and funding rate can be compared and correlated.
    """

    name: str
    category: IndicatorCategory
    timeframe: str
    value: float
    raw: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not -1.0000001 <= self.value <= 1.0000001:
            raise ValueError(f"{self.name}: normalised value {self.value} outside [-1, 1]")


@dataclass(slots=True)
class ConvictionComponent:
    """One scoring component: what it contributed and why."""

    name: str
    score: float  # 0..max_score (negative for penalties)
    max_score: float
    detail: str = ""
    signals: list[IndicatorSignal] = field(default_factory=list)


@dataclass(slots=True)
class Conviction:
    """Full scoring breakdown for one symbol on one bar.

    Kept whole (not just the total) so the dashboard can answer "which indicator
    gave how many points" for any trade, months later.
    """

    symbol: str
    bar_ts: int
    side: Side
    total: float
    threshold: float
    components: list[ConvictionComponent] = field(default_factory=list)
    regime: Regime = Regime.UNKNOWN
    blocks_voting: int = 0  # independent voting blocks after redundancy collapse
    no_trade_reason: NoTradeReason | None = None

    @property
    def passed(self) -> bool:
        return self.total >= self.threshold and self.no_trade_reason in (
            None,
            NoTradeReason.ENTERED,
        )

    @property
    def margin(self) -> float:
        """How far above (or below) the threshold. Useful for ranking."""
        return self.total - self.threshold


@dataclass(slots=True)
class EntryPlan:
    """A conviction that passed, turned into an executable intention.

    Stop and size are decided here, before any order exists, so that the risk
    layer can veto with full knowledge of what would be committed.
    """

    symbol: str
    side: Side
    bar_ts: int
    setup: str
    entry_price: float
    stop_price: float
    quantity: float
    risk_amount: float  # quote currency at risk if stop hits = 1R
    conviction: Conviction
    leverage: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Order:
    """An order as the bot knows it. ``client_id`` is the idempotency key.

    ``client_id`` is derived from (strategy, symbol, bar_ts, action) so a crash
    between "sent" and "acknowledged" cannot produce a duplicate on restart:
    the exchange refuses the second one as a duplicate client order id.

    The venue remembers ids past an order's life, so the same refusal also
    arrives for intents that never opened anything — after a local reset, say.
    ``CCXTBroker._resolve_duplicate`` tells the two apart by looking up what
    became of the original: filled or live is adopted, cancelled is re-sent.
    """

    client_id: str
    symbol: str
    side: Side
    type: OrderType
    quantity: float
    price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    exchange_id: str | None = None
    filled_quantity: float = 0.0
    average_price: float = 0.0
    fee_paid: float = 0.0
    created_ts: int = 0
    updated_ts: int = 0
    reduce_only: bool = False
    reject_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


@dataclass(slots=True, frozen=True)
class Fill:
    order_client_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    ts: int
    is_maker: bool = False
    exchange_trade_id: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass(slots=True)
class Position:
    """An open position, tracked by the bot (not by the exchange).

    Bybit demo keeps order history for only 7 days, so the local database is the
    only durable record. On restart the engine reconciles this against the
    exchange and the exchange wins on any disagreement.
    """

    symbol: str
    side: Side
    quantity: float
    entry_price: float
    entry_ts: int
    initial_stop: float
    current_stop: float
    setup: str
    risk_amount: float  # 1R in quote currency
    leverage: float = 1.0
    fees_paid: float = 0.0
    # Excursion tracking — the raw material for empirical stop/target calibration
    max_favorable_price: float = 0.0
    max_adverse_price: float = 0.0
    bars_held: int = 0
    conviction_at_entry: float = 0.0
    entry_conviction_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_favorable_price == 0.0:
            self.max_favorable_price = self.entry_price
        if self.max_adverse_price == 0.0:
            self.max_adverse_price = self.entry_price

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def r_unit(self) -> float:
        """Price distance that equals 1R."""
        return abs(self.entry_price - self.initial_stop)

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.quantity * self.side.sign

    def unrealized_r(self, price: float) -> float:
        r = self.r_unit
        if r <= 0:
            return 0.0
        return (price - self.entry_price) * self.side.sign / r

    def update_excursion(self, high: float, low: float) -> None:
        """Track MFE/MAE in price terms; conversion to R happens on close."""
        if self.side is Side.BUY:
            self.max_favorable_price = max(self.max_favorable_price, high)
            self.max_adverse_price = min(self.max_adverse_price, low)
        else:
            self.max_favorable_price = min(self.max_favorable_price, low)
            self.max_adverse_price = max(self.max_adverse_price, high)

    @property
    def mfe_r(self) -> float:
        r = self.r_unit
        if r <= 0:
            return 0.0
        return (self.max_favorable_price - self.entry_price) * self.side.sign / r

    @property
    def mae_r(self) -> float:
        """Always <= 0. The most negative excursion, in R."""
        r = self.r_unit
        if r <= 0:
            return 0.0
        return (self.max_adverse_price - self.entry_price) * self.side.sign / r


@dataclass(slots=True)
class Trade:
    """A closed round trip. The unit of performance measurement.

    Carries MFE/MAE so that ``backtest/excursion.py`` can answer, from real
    history rather than intuition: where should the stop have been, and how much
    of the favourable move did we actually capture.
    """

    symbol: str
    side: Side
    setup: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    fees: float
    r_multiple: float
    mfe_r: float
    mae_r: float
    bars_held: int
    exit_reason: ExitReason
    regime_at_entry: Regime = Regime.UNKNOWN
    conviction_at_entry: float = 0.0
    leverage: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return self.exit_ts - self.entry_ts

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def capture_ratio(self) -> float:
        """Fraction of the favourable move actually captured. Low = exiting early."""
        return self.r_multiple / self.mfe_r if self.mfe_r > 0 else 0.0


# --------------------------------------------------------------------------
# Account and risk state
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EquityPoint:
    ts: int
    equity: float
    cash: float
    unrealized: float
    open_positions: int
    drawdown: float  # fraction below peak, 0.0..1.0


@dataclass(slots=True)
class RiskState:
    """Continuous exposure control — no binary lockouts.

    The bot never stops trading outright on losses; it shrinks and becomes more
    selective, then re-expands as equity recovers. ``risk_multiplier`` scales
    position size, ``threshold_bump`` raises the conviction bar.
    """

    equity: float = 0.0
    peak_equity: float = 0.0
    risk_multiplier: float = 1.0
    threshold_bump: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    rolling_expectancy_r: float = 0.0
    rolling_profit_factor: float = 0.0
    rolling_window_trades: int = 0
    degraded: bool = False
    degraded_reason: str | None = None
    updated_ts: int = 0

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self.peak_equity)


@dataclass(slots=True)
class ScanRow:
    """One row of the live scan table: what the bot sees for one symbol, now."""

    symbol: str
    bar_ts: int
    regime: Regime
    best_side: Side | None
    score: float
    threshold: float
    no_trade_reason: NoTradeReason | None
    spread_bps: float = 0.0
    volume_24h: float = 0.0

    @property
    def distance_to_threshold(self) -> float:
        return self.score - self.threshold
