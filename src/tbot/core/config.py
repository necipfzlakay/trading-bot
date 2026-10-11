"""Configuration: validated at load time, never at use time.

A misconfigured trading bot loses money silently. Everything here is checked
before the engine starts — bad risk numbers, an inverted timeframe hierarchy or
live mode without credentials all raise on load rather than surfacing later as a
strange trade.

Secrets come from the environment (``.env``), strategy and risk parameters come
from YAML. They are deliberately separate: YAML is committed and diffable,
``.env`` never leaves the machine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tbot.core.clock import tf_ms
from tbot.core.models import Mode, Venue

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# Secrets (environment)
# --------------------------------------------------------------------------


class Secrets(BaseSettings):
    """API credentials. Never logged, never sent to an LLM, never committed."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bybit_demo_api_key: str = ""
    bybit_demo_api_secret: str = ""
    bybit_live_api_key: str = ""
    bybit_live_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    web_token: str = "change-me-local-only"

    def credentials_for(self, venue: Venue) -> tuple[str, str]:
        if venue is Venue.BYBIT_DEMO:
            return self.bybit_demo_api_key, self.bybit_demo_api_secret
        return self.bybit_live_api_key, self.bybit_live_api_secret

    def has_credentials(self, venue: Venue) -> bool:
        key, secret = self.credentials_for(venue)
        return bool(key and secret)


# --------------------------------------------------------------------------
# Config sections
# --------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Base for every config section: an unknown key is an error, not a shrug.

    Pydantic ignores extra keys by default, which turns a setting nobody reads
    into one that looks read. This repo has now shipped two of them:
    ``universe.max_spread_bps`` sat in the file deciding nothing, and
    ``risk.vol_lookback_bars`` sat there for just as long — the volatility
    window was hard-coded to 30 in the engine while the config said 30 beside
    it, so nobody noticed until the number had to change and changing it did
    nothing.

    A typo now fails at startup, where it is cheap, instead of silently
    reverting a setting to a default nobody looked up.
    """

    model_config = ConfigDict(extra="forbid")


class UniverseConfig(StrictModel):
    """Which symbols get scanned.

    ``symbols`` pins an explicit list; leaving it empty makes the engine read
    the list ``scripts/backfill.py`` last wrote to ``runtime/universe.json``.

    That list is **not** refreshed by the engine, and saying it was "refreshed
    daily" here was wrong: ``select_universe`` has exactly one caller and it is
    the backfill script. A symbol whose turnover collapses stays in the traded
    universe until someone runs backfill again — measured on the live spot
    universe, three symbols have since fallen under the very threshold that
    admitted them. ``_check_universe_liquidity`` reports that drift every bar
    rather than hiding it; acting on it is a separate decision.
    """

    symbols: list[str] = Field(default_factory=list)
    auto_top_n: int = Field(default=30, ge=1, le=100)
    quote: str = "USDT"
    min_volume_24h_usd: float = Field(default=5_000_000, ge=0)
    max_spread_bps: float = Field(default=8.0, gt=0)
    # A symbol needs enough history to have been seen in more than one regime.
    # Newly listed pairs are excluded however liquid they look today: a strategy
    # validated only on a six-week listing has been validated on nothing.
    min_history_days: int = Field(default=540, ge=0)
    exclude: list[str] = Field(
        default_factory=lambda: [
            "USDC/USDT",
            "FDUSD/USDT",
            "DAI/USDT",
            "TUSD/USDT",
            "USD1/USDT",
            "USDE/USDT",
            "BUSD/USDT",
            "EURT/USDT",
        ]
    )


class TimeframeConfig(StrictModel):
    """The three-layer hierarchy. Higher layers veto lower ones, never the reverse."""

    permission: str = "1d"
    setup: str = "4h"
    trigger: str = "1h"

    @model_validator(mode="after")
    def _ordered(self) -> TimeframeConfig:
        p, s, t = tf_ms(self.permission), tf_ms(self.setup), tf_ms(self.trigger)
        if not p > s > t:
            raise ValueError(
                f"timeframes must be strictly slowest-to-fastest: "
                f"permission({self.permission}) > setup({self.setup}) > trigger({self.trigger})"
            )
        return self

    @property
    def all(self) -> list[str]:
        return [self.trigger, self.setup, self.permission]


class DrawdownTier(BaseModel):
    """One rung of the continuous de-risking ladder."""

    max_drawdown: float = Field(ge=0.0, le=1.0)  # upper bound of this tier
    risk_multiplier: float = Field(gt=0.0, le=1.0)
    threshold_bump: float = Field(ge=0.0, le=50.0)


class RiskConfig(StrictModel):
    """Capital preservation. The only layer allowed to say no unconditionally."""

    risk_per_trade: float = Field(default=0.0075, gt=0, le=0.05)
    max_positions: int = Field(default=3, ge=1, le=10)
    max_correlation: float = Field(default=0.85, gt=0, le=1.0)
    max_portfolio_exposure: float = Field(default=1.0, gt=0, le=2.0)

    # --- position size ceilings -------------------------------------------
    # Percentage-of-equity sizing scales without limit: at 0.75% risk, 1R is
    # 1.50 on a 200 account and 375 on a 50,000 one. That is arithmetically
    # consistent and operationally unwise — a strategy validated on small
    # positions has not been validated on ones large enough to move a thin
    # book, and confidence should not scale as fast as the balance does.
    #
    # These are hard ceilings applied after sizing. Whichever binds first wins;
    # none of them can ever increase a position, only cap it. Set to 0 to
    # disable an individual ceiling.

    # Largest notional a single position may hold, in quote currency.
    max_position_notional: float = Field(default=0.0, ge=0.0)
    # Largest share of equity a single position may hold.
    max_position_pct: float = Field(default=0.40, ge=0.0, le=1.0)
    # Largest 1R a single trade may risk, in quote currency. The most direct
    # ceiling: it caps the loss, not the exposure.
    max_risk_amount: float = Field(default=0.0, ge=0.0)

    # User-owned hard cap. The promotion gate may never exceed it, and code may
    # never raise it on its own.
    #
    # The 3.0 ceiling is the operator's explicit authorisation, recorded here so
    # it exists rather than because it is used. Raising it changes nothing on
    # its own: LeverageGate still requires 90 days, 100 trades, PF >= 1.3 and a
    # drawdown under 15% before granting anything above 1.0.
    #
    # And the default stays 1.0 on measurement, not caution. Leverage multiplies
    # expectancy without changing its sign, and the measured expectancy for the
    # short-horizon hypothesis is about -0.05% per trade. It also moves the one
    # number that bounds the damage: at 1x a position is 100% collateralised and
    # a short liquidates 99% away; at 3x that becomes roughly 33%, which a
    # crypto weekend gap can reach. Measured, see research/perp_venue_facts.md.
    max_leverage: float = Field(default=1.0, ge=1.0, le=3.0)

    # Continuous exposure ladder — no binary lockouts anywhere.
    drawdown_tiers: list[DrawdownTier] = Field(
        default_factory=lambda: [
            DrawdownTier(max_drawdown=0.05, risk_multiplier=1.00, threshold_bump=0.0),
            DrawdownTier(max_drawdown=0.10, risk_multiplier=0.75, threshold_bump=5.0),
            DrawdownTier(max_drawdown=0.15, risk_multiplier=0.50, threshold_bump=10.0),
            DrawdownTier(max_drawdown=1.00, risk_multiplier=0.25, threshold_bump=15.0),
        ]
    )
    # Hysteresis: recover only after climbing this far back above a tier edge,
    # so equity oscillating on a boundary does not flip size every bar.
    tier_recovery_buffer: float = Field(default=0.01, ge=0.0, le=0.1)

    # A floor, not a halt. Past this drawdown the bot stops *opening* positions
    # while continuing to manage and protect what is already open. Losing half
    # the account means something is wrong that smaller sizing will not fix, and
    # at that point the useful action is to stop adding to it — which is
    # different from abandoning open risk. Set to 1.0 to disable.
    freeze_drawdown: float = Field(default=0.50, gt=0.0, le=1.0)

    # Below this balance the bot stops opening positions. Not a preference —
    # a structural limit measured by scripts/capital_check.py.
    #
    # An account can only express stops wider than
    # (risk_amount x vol_scale) / min_notional. Below the limit the bot does not
    # trade a smaller version of the tested strategy; it trades a quieter subset
    # of the universe, because wide-stop and high-volatility setups are silently
    # skipped. The backtest that justified it does not describe that subset.
    #
    # Measured for this universe at 0.75% risk and a 5 USDT venue minimum:
    # 183 covers 95% of setups, 306 covers 99%. Re-measure after changing
    # risk_per_trade, the universe, or the stop multiple.
    # Hard ceiling on the capital this engine may size against, regardless of
    # what the venue balance says. **Zero means no cap** — size against the
    # real balance, which is right when one engine owns the account.
    #
    # It exists because two engines on one Unified account both read the same
    # free USDT and each adopt it as their own starting equity (measured: a
    # perp fill moves the very balance the spot engine sizes against). Without
    # a cap they would collectively risk twice what either believes it is
    # risking, and every percentage on each panel would be measured against a
    # number the other engine moves.
    max_capital: float = Field(default=0.0, ge=0)
    min_equity: float = Field(default=183.0, ge=0.0)

    # Consecutive losses raise the conviction bar; they never ban trading.
    loss_streak_threshold_bump: float = Field(default=2.0, ge=0.0, le=20.0)
    loss_streak_start: int = Field(default=3, ge=1)

    # Volatility targeting: normalise each position to the same portfolio risk.
    #
    # This must sit near the universe's *own* median volatility, because the
    # scaler works on the ratio target/realised — its job is to size a calm
    # symbol up and a wild one down relative to normal, not to impose an
    # absolute level. Measured across 21 Bybit spot pairs the median annualised
    # volatility is 79%; the previous 0.35 (an equity-portfolio figure) pinned
    # the scaler at its floor and quietly opened every position at 40% of the
    # intended size, which then fell below the exchange minimum. The mechanism
    # was not conservative — it was inoperative.
    vol_target_annual: float = Field(default=0.80, gt=0, le=3.0)
    vol_scale_min: float = Field(default=0.4, gt=0, le=1.0)
    vol_scale_max: float = Field(default=2.0, ge=1.0, le=5.0)
    # How many trigger bars the realised-volatility estimate looks back over.
    #
    # This was hard-coded at 30 in two places in the runner. Thirty hourly bars
    # is thirty hours, which is far too short to estimate an annualised number:
    # measured across the universe, the 30-bar estimate spans 4.0x from its
    # 10th to its 90th percentile against 3.2x at 240 bars, and the resulting
    # position scale jumps a median 0.87% per bar (p95 10.2%) against 0.09%
    # (p95 1.3%) at 240. Same positions, sized differently hour to hour for no
    # reason but estimator noise.
    vol_lookback_bars: int = Field(default=240, ge=10, le=2000)

    # Rolling performance window — the unit of evaluation is trades, not days.
    rolling_windows: list[int] = Field(default_factory=lambda: [20, 50, 100])

    @field_validator("drawdown_tiers")
    @classmethod
    def _tiers_monotonic(cls, tiers: list[DrawdownTier]) -> list[DrawdownTier]:
        if not tiers:
            raise ValueError("at least one drawdown tier required")
        for prev, curr in zip(tiers, tiers[1:], strict=False):
            if curr.max_drawdown <= prev.max_drawdown:
                raise ValueError("drawdown_tiers must have strictly increasing max_drawdown")
            if curr.risk_multiplier > prev.risk_multiplier:
                raise ValueError("risk_multiplier must not increase as drawdown deepens")
            if curr.threshold_bump < prev.threshold_bump:
                raise ValueError("threshold_bump must not decrease as drawdown deepens")
        if tiers[-1].max_drawdown < 1.0:
            raise ValueError("last drawdown tier must cover up to 1.0")
        if any(t.risk_multiplier <= 0 for t in tiers):
            raise ValueError(
                "risk_multiplier must stay above zero — the bot shrinks, it never halts"
            )
        return tiers


class ConvictionConfig(StrictModel):
    """Selectivity. High base threshold; sitting in cash is a valid outcome."""

    # Calibrated against the measured score distribution rather than picked:
    # across 3 years of the universe, scores reach p99 near 64 and cap around 81,
    # because several components (derivatives coverage, expected_r without
    # history) rarely max out. A nominal 68 selected 0.2% of bars — too few to
    # ever validate. 62 sits near the 98th percentile, which keeps the bot
    # highly selective while producing a measurable number of trades.
    base_threshold: float = Field(default=62.0, ge=0, le=100)
    # The floor the threshold can never fall below, however quiet the market is.
    absolute_floor: float = Field(default=56.0, ge=0, le=100)
    # If no trade has fired for this long, relax the bar — but only to the floor.
    scarcity_bars: int = Field(default=72, ge=1)
    scarcity_max_relief: float = Field(default=6.0, ge=0, le=20)

    component_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "regime_fit": 25.0,
            "indicator_confluence": 25.0,
            "mtf_alignment": 15.0,
            "flow_confirmation": 15.0,
            "derivatives": 10.0,
            "expected_r": 10.0,
        }
    )
    # Minimum independent voting blocks (post redundancy collapse) required.
    # Three correlated indicators agreeing is one opinion, not three.
    min_voting_blocks: int = Field(default=3, ge=1)
    # Exit when conviction decays this far below the entry threshold.
    collapse_threshold: float = Field(default=35.0, ge=0, le=100)

    # How a candidate qualifies. "absolute" compares each symbol to
    # base_threshold; "cross_sectional" ranks symbols against each other and
    # takes the top slice — which is what the tail analysis actually measured.
    # An absolute bar produces feast and famine; a relative one keeps the
    # opportunity count roughly constant and the sample measurable.
    selection: Literal["absolute", "cross_sectional"] = "cross_sectional"
    top_percentile: float = Field(default=0.9, gt=0.5, lt=1.0)
    min_universe_for_ranking: int = Field(default=8, ge=3)
    # A relative rank still needs a floor: best-of-a-bad-bunch is not a signal.
    rank_absolute_floor: float = Field(default=40.0, ge=0, le=100)

    @model_validator(mode="after")
    def _threshold_sane(self) -> ConvictionConfig:
        if self.absolute_floor > self.base_threshold:
            raise ValueError("absolute_floor cannot exceed base_threshold")
        total = sum(self.component_weights.values())
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"component_weights must sum to 100, got {total}")
        return self


class IndicatorConfig(StrictModel):
    """Which indicators may vote, and the evidence bar they must clear."""

    # Below this absolute Information Coefficient an indicator is noise and is
    # excluded from scoring, however popular it is.
    min_abs_ic: float = Field(default=0.02, ge=0.0, le=0.5)
    min_ic_ir: float = Field(default=0.3, ge=0.0)
    # Indicators correlating above this collapse into a single voting block.
    redundancy_threshold: float = Field(default=0.8, gt=0.0, le=1.0)
    ic_horizons: list[int] = Field(default_factory=lambda: [1, 4, 12, 24])
    weighting: Literal["equal", "ic_weighted"] = "equal"
    enabled: list[str] = Field(default_factory=list)  # empty = registry default


class ExecutionConfig(StrictModel):
    order_type: Literal["market", "limit"] = "market"
    # Backtest cost assumptions. Live fees are fetched from the exchange
    # and override these; if the real cost is worse, the backtest was optimistic.
    assumed_taker_fee: float = Field(default=0.001, ge=0, le=0.01)
    assumed_maker_fee: float = Field(default=0.001, ge=0, le=0.01)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    # Extra headroom above the exchange minimum so rounding cannot reject an order.
    min_notional_buffer: float = Field(default=1.15, ge=1.0, le=3.0)
    limit_offset_bps: float = Field(default=2.0, ge=0, le=100)
    max_retries: int = Field(default=3, ge=0, le=10)
    # Read live spread and 24h turnover and record them on every scan row.
    # Collection is free and always on in live mode; see the flag below for
    # whether it is allowed to change a decision.
    microstructure_enabled: bool = True
    # Let spread and liquidity reach a decision — both the soft penalties in
    # the conviction score and the hard floors below, which is what makes
    # ``universe.max_spread_bps`` a setting that is read rather than one that
    # merely looks read (invariant 50).
    #
    # Off by default, and the reason has changed. It used to be "the floors are
    # never crossed anyway": measured over 1302 live scan rows that is false —
    # 4.5% of rows sit above 10bps and 26.8% below the 10M turnover floor. So
    # turning this on now moves the trade population, and that makes it a
    # pre-registered hypothesis with a trial attached, not a tidy-up.
    microstructure_penalties: bool = False
    # How often the engine asks the venue whether a protective stop has filled,
    # while it is waiting between bars. Without this the engine learns its own
    # stop fired only at the next close — up to an hour on an hourly bar.
    venue_poll_seconds: int = Field(default=60, ge=10, le=900)
    # Which market this engine trades. ``spot`` buys the asset; ``linear``
    # trades USDT-margined perpetuals, which is the only way to take the short
    # side and cuts the round-trip fee from 0.20% to 0.07% (measured:
    # research/perp_venue_facts.md).
    market_type: Literal["spot", "linear"] = "spot"
    # Perpetuals only. Verified against the venue at startup rather than
    # trusted: ``set_margin_mode`` returns success on a Unified account and
    # leaves the position reporting cross, which is the ``fetchTradingFees``
    # failure all over again. What actually bounds the damage is leverage —
    # at 1.0 a position is 100% collateralised and a short liquidates 99% away
    # (measured) — so this is recorded and checked, not relied upon alone.
    margin_mode: Literal["isolated", "cross"] = "isolated"


class ExitConfig(StrictModel):
    """No fixed holding times. Levels come from excursion analysis of real trades."""

    initial_stop_atr_mult: float = Field(default=2.0, gt=0, le=10)
    trailing_atr_mult: float = Field(default=2.5, gt=0, le=10)
    atr_period: int = Field(default=14, ge=2)
    # Which timeframe's ATR the trailing stop is measured on. This single
    # setting decides the holding period more than anything else: a trail on
    # the trigger timeframe exits within hours, which throws away the
    # multi-week continuation the tail analysis actually measured.
    trail_timeframe: Literal["trigger", "setup", "permission"] = "permission"
    # Tighten the trail once measured edge decay says the setup is spent.
    edge_decay_enabled: bool = True
    edge_decay_tighten_factor: float = Field(default=0.6, gt=0, le=1.0)
    # A sudden ATR expansion re-prices risk rather than waiting for the stop.
    vol_shock_atr_ratio: float = Field(default=2.5, gt=1.0)
    breakeven_at_r: float = Field(default=1.0, ge=0)
    # Profit target, in ATR units above entry. **Zero disables it**, which is
    # the default on purpose: the mechanism is free, the *level* is a free
    # parameter and therefore a pre-registered trial. Turning this on without
    # first writing the hypothesis down is exactly what DSR penalises.
    #
    # Note this can never be a venue order on Bybit spot — a resting stop locks
    # the base asset, so a second reduce-only order is refused (170131,
    # measured). The target is evaluated by the engine, which means it only
    # works while the bot is running. That asymmetry is deliberate: a missed
    # target costs opportunity, a missing stop costs capital.
    target_atr_mult: float = Field(default=0.0, ge=0, le=20)
    # Close the position outright after this many trigger bars. **Zero
    # disables it**, which is right for the main engine: invariant 51 measured
    # that every mechanism cutting the tail costs more than it saves, and all
    # the profit comes from the ~20% of trades that run past 3R.
    #
    # The fast engine sets it anyway, deliberately. Its whole purpose is to
    # recycle capital, and a position that has neither hit its target nor its
    # stop after N bars is holding a slot without having been right about
    # anything. That is a different trade-off, not a better one, and it is
    # pre-registered as part of H17.
    time_stop_bars: int = Field(default=0, ge=0, le=2000)
    # Partial profit taking. At ``scale_out_at_r`` multiples of risk, sell
    # ``scale_out_pct`` of the ORIGINAL quantity and let the rest run.
    #
    # This is the measured gap, not a preference. Of 114 backtest trades, 33
    # (29%) ended at exactly 0.00R via the trailing stop after averaging +1.78R
    # of favourable excursion; the 1-2R bucket realised -0.01R on average and
    # the 2-3R bucket +0.00R. Everything the strategy earns comes from the 24
    # trades that reach 3R+, and nothing at all is taken from the rest.
    #
    # MEASURED, and the intuition is wrong. Scaling out does close the gap it
    # was aimed at - trades ending at exactly 0.00R drop from 31 to 3 at 1.0R -
    # but it does not leave the tail alone, because selling half at +1R halves
    # the position that later runs to 3R+, and that tail is the entire profit:
    #
    #     off:    21 tail trades +460.58, other 82 -385.47, net  +75.11
    #     1.0R:   21 tail trades +352.07, other 82 -305.80, net  +46.27
    #
    # It saves $80 on the mediocre trades and gives up $108 on the good ones.
    # Profit factor 1.19 -> 1.12, return +6.04% -> +2.60%. Max drawdown does
    # improve, 15.8% -> 12.8%.
    #
    # So this is a risk/return trade, not an improvement: less drawdown, less
    # money. Zero disables it, and it ships disabled because the operator has
    # not asked to trade return for smoothness.
    scale_out_at_r: float = Field(default=0.0, ge=0, le=20)
    scale_out_pct: float = Field(default=0.5, gt=0, lt=1.0)


class EntryConfig(StrictModel):
    """How an entry reaches the venue once conviction has chosen the symbol."""

    # Take the bottom of the cross-section short as well as the top long.
    # Requires ``execution.market_type: linear`` — spot cannot sell what it
    # does not hold, which is why the short side has never been measured here.
    #
    # Off by default. It doubles the trade population, so it is a trial rather
    # than a repair, and it is pre-registered as H17.
    allow_short: bool = False

    # Place the entry as a resting trigger order at the breakout level instead
    # of buying at the close. This is the only way to act between bar closes
    # without letting an unclosed bar reach an indicator (invariant 1).
    #
    # Off by default: it changes *which* trades happen, so it is a trial, not a
    # repair. The mechanism is built and tested; the decision to use it is not
    # made here.
    armed_breakout: bool = False
    # How far above the last close an armed level may sit. Beyond this the
    # level is stale rather than imminent and the order would rest for nothing.
    armed_max_distance_atr: float = Field(default=1.5, gt=0, le=10)
    # At most one armed order at a time. More than one and the *market* picks
    # which symbol fills — first to touch, not highest scored — silently
    # replacing the cross-sectional selection that was actually measured.
    armed_max_orders: int = Field(default=1, ge=1, le=3)
    # Require a setup to trigger before entering, on top of clearing the
    # cross-sectional rank. Open hypothesis #4 in research/hypotheses.md says
    # this may be an unmeasured extra condition: the tail study ranked symbols
    # against each other and took the top decile, it did not say "top decile
    # AND a trigger". The gate is the binding filter by a wide margin - 99.1%
    # of scored candidates die on it - so whether it earns its place decides
    # how often this bot trades at all.
    require_setup: bool = True
    # How long after a bar's close its decision may still become an entry.
    # Every number in the repo is the backtest's, and the backtest buys at the
    # next open — seconds after the close. The loop used to process whatever
    # bar had closed last on startup, however old: after the 2026-09-22 reset
    # the demo engine came up at 06:25 and bought AAVE at 06:26 off the close
    # of 06:00, 26 minutes after the price it was sized from (audit #97).
    # Measured on the bar the loop *picks up*, not when the entry is sent: the
    # 79-symbol fast engine needs ~3.5 minutes per bar on every bar, and that
    # is a constant of the engine, not a stale decision. Open positions are
    # managed whatever the age.
    max_bar_age_seconds: float = Field(default=300.0, ge=30, le=86_400)


class DataConfig(StrictModel):
    backfill_years: float = Field(default=3.0, gt=0, le=10)
    parquet_dir: Path = Path("runtime/data")
    fetch_derivatives: bool = True
    # Refuse to backtest on data with holes — silent gaps manufacture fake edge.
    max_gap_ratio: float = Field(default=0.005, ge=0, le=0.5)
    rest_rate_limit_ms: int = Field(default=120, ge=0)


class LLMRoleConfig(StrictModel):
    enabled: bool = False
    max_calls_per_day: int = Field(default=20, ge=0)
    model: str = ""


class LLMConfig(StrictModel):
    """Advisory only. The iron rule is enforced in code, not by prompt wording.

    An LLM may reduce risk (veto a new entry) and may produce text. It can never
    create a signal, size a position, move a stop, or raise a limit.
    """

    enabled: bool = False
    primary_provider: Literal["gemini", "groq", "none"] = "gemini"
    fallback_provider: Literal["gemini", "groq", "none"] = "groq"
    gemini_model: str = "gemini-flash-latest"
    groq_model: str = "llama-3.3-70b-versatile"
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_calls_per_day: int = Field(default=50, ge=0)
    cache_ttl_seconds: int = Field(default=900, ge=0)
    circuit_breaker_failures: int = Field(default=3, ge=1)

    roles: dict[str, LLMRoleConfig] = Field(
        default_factory=lambda: {
            "event_guard": LLMRoleConfig(enabled=False, max_calls_per_day=24),
            "post_mortem": LLMRoleConfig(enabled=False, max_calls_per_day=2),
            "anomaly_triage": LLMRoleConfig(enabled=False, max_calls_per_day=10),
            "report_writer": LLMRoleConfig(enabled=False, max_calls_per_day=4),
            "config_auditor": LLMRoleConfig(enabled=False, max_calls_per_day=5),
        }
    )
    # A veto role must prove itself against the counterfactual or be switched off.
    event_guard_min_trial_days: int = Field(default=90, ge=1)


class WebConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    enabled: bool = True


class TelegramConfig(StrictModel):
    enabled: bool = False
    notify_fills: bool = True
    notify_risk_changes: bool = True
    notify_errors: bool = True
    daily_summary_hour_utc: int = Field(default=0, ge=0, le=23)
    # How often the wait loop asks Telegram for commands. Measured from this
    # machine, one getUpdates round trip costs 3-8 seconds; at the old cadence
    # (every loop tick, 5s) the engine spent most of its waking life on it and
    # overshot every bar close by up to 38 minutes. Commands are conveniences,
    # so they get a slow lane.
    poll_seconds: float = Field(default=15.0, ge=1.0, le=300.0)
    # Separate from the send timeout: a slow poll must not be able to stall the
    # loop that is watching open positions.
    poll_timeout: float = Field(default=3.0, gt=0, le=30.0)


class SecurityConfig(StrictModel):
    """Acknowledgements, not switches.

    Nothing here disables a check. The only setting is whether a risk the
    operator has already weighed still reports as an open defect — because a
    warning that cannot be cleared is one the operator learns to skip past, and
    the next real warning goes with it.
    """

    # The bot warns when the API key has no IP whitelist. On a dynamic address
    # a whitelist locks the bot out mid-position every time the IP changes, so
    # declining it can be the correct call. The risk is bounded by the key
    # itself: withdrawal permission is refused outright and cannot be
    # acknowledged away, so a leak costs unwanted trades, not the balance.
    ip_whitelist_acknowledged: bool = False


class EngineConfig(StrictModel):
    # Identifies this engine to the venue when more than one shares an account.
    # Empty reproduces the pre-namespace client order ids exactly, so the
    # original engine needs no migration; anything else must be short, because
    # two characters of it ride in every client order id under a 36-character
    # ceiling.
    namespace: str = Field(default="", max_length=8, pattern=r"^[a-z0-9]*$")
    # Which loop runs this engine. ``bar`` is the Runner: closed bars on the
    # configured timeframes, the conviction engine, one decision per symbol per
    # bar. ``scalp`` is the one-minute maker loop in ``tbot.scalp`` — measured,
    # the Runner needs ~3.5 minutes to process one bar over 79 symbols, so it
    # cannot carry a one-minute strategy at all.
    # ``rebalance`` is the daily-close loop in ``tbot.rebalance``: H19 carry,
    # H20 trend and H21 cross-sectional momentum, each a few trades a month.
    kind: Literal["bar", "scalp", "rebalance"] = "bar"
    loop_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    # Restart storm detection: beyond this, drop to safe mode instead of thrashing.
    max_restarts_per_hour: int = Field(default=10, ge=1)
    stale_data_tolerance_bars: float = Field(default=2.0, ge=0.5)
    reconcile_on_start: bool = True
    state_snapshot_seconds: int = Field(default=30, ge=1)
    # Perpetual engines sharing one account must agree on who owns each symbol:
    # one-way mode holds a single position per symbol, and two engines opening
    # the same one are netted by the venue into a position neither ordered.
    # The first to open a symbol claims it until it is flat; the others skip it
    # meanwhile (``engine.claims``). Refused at startup if any perp engine on
    # the account leaves it off (``engine.siblings.assert_claims_consistent``).
    symbol_claims: bool = False
    claims_path: Path = Path("runtime-shared/claims.sqlite")


# --------------------------------------------------------------------------
# Root config
# --------------------------------------------------------------------------


class ScalpConfig(StrictModel):
    """The one-minute maker scalper (H18). Every number here is pre-registered.

    Measured before it was built (Bybit perp, 6 symbols x 7 days of 1m bars):
    the median one-minute move is 0.026-0.070% against a 0.07% maker/taker round
    trip, and every barrier configuration tested lost about the cost of a trade.
    The expected result is therefore negative, and these levels are the
    hypothesis under test — not settings to be tuned against the result.
    """

    # Trigger: the move over ``trigger_bars`` closed bars exceeds
    # ``trigger_sigma`` standard deviations of the last ``sigma_window`` one-bar
    # returns, scaled by sqrt(trigger_bars).
    trigger_bars: int = Field(default=5, ge=2, le=60)
    sigma_window: int = Field(default=60, ge=20, le=1440)
    trigger_sigma: float = Field(default=2.5, gt=0, le=10)
    # Each trigger goes to exactly one arm, by hash(symbol, bar): the same event
    # population split independently of its outcome. Momentum enters with the
    # move, reversion against it; on the same trigger they want opposite sides,
    # so they could never share a symbol anyway.
    arms: list[Literal["momentum", "reversion"]] = Field(
        default_factory=lambda: ["momentum", "reversion"], min_length=1
    )
    take_profit_pct: float = Field(default=0.003, gt=0, le=0.05)
    stop_loss_pct: float = Field(default=0.003, gt=0, le=0.05)
    time_stop_bars: int = Field(default=15, ge=1, le=240)
    # A PostOnly entry that has not filled in this long is cancelled, the cancel
    # confirmed (invariant 41), and the symbol's claim released.
    entry_ttl_seconds: float = Field(default=20.0, ge=2, le=300)
    # How often open positions and resting orders are read from the venue.
    poll_seconds: float = Field(default=3.0, ge=1, le=60)
    # Safety valves. Capacity, not a target: the trigger decides the real rate.
    max_orders_per_minute: int = Field(default=20, ge=1, le=120)
    max_entries_per_minute: int = Field(default=10, ge=1, le=60)
    reject_streak_limit: int = Field(default=5, ge=1, le=50)
    cooldown_seconds: int = Field(default=600, ge=0, le=86_400)
    # Pre-registered kill: at or below this equity no new entry is opened.
    kill_equity: float = Field(default=500.0, ge=0)


class RebalanceConfig(StrictModel):
    """The daily-close rebalancing strategies (H19-H21). Pre-registered.

    Fixed from the literature's defaults before the archive was asked anything
    (``research/hypotheses.md``); ``scripts/rebalance_research.py`` replays them
    once. They are the hypothesis under test, not settings to tune: changing one
    and re-running is a new trial, and the DSR prices it.

    Every position is sized once, at entry, to an equal share of the engine's
    equity, and held at that quantity until its rule says to leave.
    """

    strategy: Literal["trend", "xsmom", "carry"] = "trend"

    # H20 — time-series trend: long while the close is above its 140-day
    # (20-week) simple average, otherwise cash. Decided on Mondays.
    trend_symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    trend_sma_days: int = Field(default=140, ge=20, le=400)

    # H21 — cross-sectional momentum: rank by 28-day return, hold the top 4
    # with a positive return; a held name stays while it ranks within 8.
    xsmom_lookback_days: int = Field(default=28, ge=5, le=365)
    xsmom_top_k: int = Field(default=4, ge=1, le=20)
    xsmom_hold_rank: int = Field(default=8, ge=1, le=40)

    # H19 — funding carry: spot long + perp short on the same name. Enter when
    # the last 7 days of settled funding sum to >= 0.21% (~11%/yr), leave when
    # the sum falls to 0 or below. At most 4 pairs.
    carry_window_days: int = Field(default=7, ge=1, le=60)
    carry_enter_sum: float = Field(default=0.0021, gt=0, le=0.1)
    carry_exit_sum: float = Field(default=0.0, ge=-0.05, le=0.1)
    carry_max_pairs: int = Field(default=4, ge=1, le=20)
    # Funding paid or received is read back from the venue this often.
    carry_funding_poll_seconds: int = Field(default=900, ge=60, le=86_400)
    # A pair whose legs drift apart by more than this fraction of the spot
    # quantity is reported; the engine never resizes a leg on its own.
    carry_leg_tolerance: float = Field(default=0.02, gt=0, le=0.5)

    # Weekly strategies decide on this weekday's daily close (0 = Monday,
    # the venue's own week, invariant 17). Carry decides every day.
    decision_weekday: int = Field(default=0, ge=0, le=6)
    # Daily bars fetched over REST for each decision; must cover the longest
    # lookback with room for a gap.
    history_days: int = Field(default=200, ge=30, le=1000)
    # Pre-registered freeze: at or below this equity no new position is opened;
    # open ones are still managed by their rule. 0 disables it.
    freeze_equity: float = Field(default=0.0, ge=0)


class Config(BaseModel):
    """Root configuration, assembled from YAML and validated as a whole.

    ``extra="forbid"`` here closes the last gap in invariant 50. Every section
    already rejected unknown keys, but the root did not: a top-level typo landed
    in a model that silently ignored it, which looks exactly like a setting that
    is being read. Two dead settings reached production that way.
    """

    model_config = ConfigDict(extra="forbid")

    # The default cannot send an order. A config that forgets to say what it
    # is gets the harmless reading, not the expensive one.
    mode: Mode = Mode.BACKTEST
    venue: Venue = Venue.BYBIT_DEMO
    starting_balance: float = Field(default=100.0, gt=0)
    quote_currency: str = "USDT"

    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    timeframes: TimeframeConfig = Field(default_factory=TimeframeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    conviction: ConvictionConfig = Field(default_factory=ConvictionConfig)
    indicators: IndicatorConfig = Field(default_factory=IndicatorConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    entries: EntryConfig = Field(default_factory=EntryConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    scalp: ScalpConfig = Field(default_factory=ScalpConfig)
    rebalance: RebalanceConfig = Field(default_factory=RebalanceConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    runtime_dir: Path = Path("runtime")
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _cross_section_checks(self) -> Config:
        # Leverage above 1.0 needs a passed promotion gate; the file is checked
        # at engine start. Config alone can raise the ceiling, never the usage.
        if self.risk.max_leverage > 1.0 and self.mode is Mode.LIVE:
            # Allowed, but only meaningful once the gate passes — recorded here
            # so the engine can log the distinction clearly.
            pass
        if self.engine.kind == "scalp":
            # The scalper only makes sense on the venue it was measured on, and
            # it shares that venue with another perp engine.
            if self.execution.market_type != "linear":
                raise ValueError("engine.kind: scalp trades linear perpetuals only")
            if not self.engine.symbol_claims:
                raise ValueError("engine.kind: scalp requires engine.symbol_claims: true")
            if self.risk.max_capital <= 0:
                raise ValueError("engine.kind: scalp requires risk.max_capital (its allocation)")
            if self.scalp.kill_equity >= self.risk.max_capital:
                raise ValueError("scalp.kill_equity must sit below risk.max_capital")
        if self.engine.kind == "rebalance":
            # Several engines now share the spot wallet as well as the perp
            # one; a coin two engines hold is one neither can account for
            # (invariant 81). The allocation is what a forward test is judged on.
            if not self.engine.symbol_claims:
                raise ValueError("engine.kind: rebalance requires engine.symbol_claims: true")
            if self.risk.max_capital <= 0:
                raise ValueError("engine.kind: rebalance requires risk.max_capital")
            if self.risk.max_leverage != 1.0:
                raise ValueError("engine.kind: rebalance runs unlevered (risk.max_leverage: 1.0)")
            if self.rebalance.freeze_equity >= self.risk.max_capital:
                raise ValueError("rebalance.freeze_equity must sit below risk.max_capital")
            if self.rebalance.xsmom_hold_rank < self.rebalance.xsmom_top_k:
                raise ValueError("rebalance.xsmom_hold_rank cannot be tighter than xsmom_top_k")
        if self.engine.symbol_claims and self.entries.armed_breakout:
            # An armed entry rests at the venue and fills on its own, without
            # passing through the path that claims a symbol first — so it could
            # open into a symbol another engine holds, and one-way mode would
            # net the two. Refused rather than half-supported.
            raise ValueError(
                "entries.armed_breakout cannot be combined with engine.symbol_claims: "
                "armed entries fill without claiming their symbol"
            )
        if self.conviction.collapse_threshold >= self.conviction.absolute_floor:
            raise ValueError(
                "conviction.collapse_threshold must sit below absolute_floor, "
                "otherwise positions exit the moment they are opened"
            )
        if self.execution.slippage_bps == 0 and self.mode is not Mode.LIVE:
            raise ValueError(
                "zero slippage in a backtest produces fiction; set execution.slippage_bps > 0"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.runtime_dir / "tbot.db"

    @property
    def log_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.runtime_dir / "reports"

    @property
    def promotion_path(self) -> Path:
        return self.runtime_dir / "promotion.json"

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    def is_live_money(self) -> bool:
        """True only when real funds are at stake."""
        return self.mode is Mode.LIVE and self.venue is Venue.BYBIT_LIVE


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    path: str | Path = "config/base.yaml",
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Load YAML (optionally layered on ``base.yaml``) and validate.

    A profile file may declare ``extends: base.yaml`` to inherit; profile keys
    win over the base, so ``live.yaml`` only states what differs.
    """
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    parent = raw.pop("extends", None)
    if parent:
        parent_path = path.parent / parent
        base_raw = yaml.safe_load(parent_path.read_text(encoding="utf-8")) or {}
        base_raw.pop("extends", None)
        raw = _deep_merge(base_raw, raw)

    if overrides:
        raw = _deep_merge(raw, overrides)

    cfg = Config.model_validate(raw)

    if not cfg.runtime_dir.is_absolute():
        cfg.runtime_dir = PROJECT_ROOT / cfg.runtime_dir
    if not cfg.data.parquet_dir.is_absolute():
        cfg.data.parquet_dir = PROJECT_ROOT / cfg.data.parquet_dir
    if not cfg.engine.claims_path.is_absolute():
        cfg.engine.claims_path = PROJECT_ROOT / cfg.engine.claims_path
    return cfg


def load_secrets() -> Secrets:
    return Secrets()


def assert_runnable(cfg: Config, secrets: Secrets) -> None:
    """Refuse to start a run that cannot possibly work. Called before the engine.

    Live-money runs get the strictest check, because that is the one where a
    missing key turns into a half-open position instead of an error message.
    """
    if cfg.mode is Mode.LIVE and not secrets.has_credentials(cfg.venue):
        raise RuntimeError(
            f"mode=live venue={cfg.venue.value} requires API credentials in .env (see .env.example)"
        )
    if cfg.is_live_money() and os.getenv("TBOT_CONFIRM_LIVE") != "yes":
        raise RuntimeError(
            "Refusing to start with REAL money. This run would place live orders on "
            "bybit-live. If that is genuinely intended, set TBOT_CONFIRM_LIVE=yes in "
            "the environment. The demo venue needs no such flag."
        )
    if cfg.llm.enabled and cfg.llm.primary_provider == "gemini" and not secrets.gemini_api_key:
        raise RuntimeError("llm.enabled with primary_provider=gemini but GEMINI_API_KEY is unset")
    if cfg.llm.enabled and cfg.llm.primary_provider == "groq" and not secrets.groq_api_key:
        raise RuntimeError("llm.enabled with primary_provider=groq but GROQ_API_KEY is unset")
    if cfg.telegram.enabled and not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        raise RuntimeError("telegram.enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are unset")
