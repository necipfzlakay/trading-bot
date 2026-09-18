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
from pydantic import BaseModel, Field, field_validator, model_validator
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


class UniverseConfig(BaseModel):
    """Which symbols get scanned.

    ``symbols`` pins an explicit list; leaving it empty auto-selects the most
    liquid pairs by 24h volume, refreshed daily.
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


class TimeframeConfig(BaseModel):
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


class RiskConfig(BaseModel):
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
    max_leverage: float = Field(default=1.0, ge=1.0, le=2.0)

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
    vol_lookback_bars: int = Field(default=30, ge=5)
    vol_scale_min: float = Field(default=0.4, gt=0, le=1.0)
    vol_scale_max: float = Field(default=2.0, ge=1.0, le=5.0)

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


class ConvictionConfig(BaseModel):
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


class IndicatorConfig(BaseModel):
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


class ExecutionConfig(BaseModel):
    order_type: Literal["market", "limit"] = "market"
    # Backtest/paper cost assumptions. Live fees are fetched from the exchange
    # and override these; if the real cost is worse, the backtest was optimistic.
    assumed_taker_fee: float = Field(default=0.001, ge=0, le=0.01)
    assumed_maker_fee: float = Field(default=0.001, ge=0, le=0.01)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    # Extra headroom above the exchange minimum so rounding cannot reject an order.
    min_notional_buffer: float = Field(default=1.15, ge=1.0, le=3.0)
    limit_offset_bps: float = Field(default=2.0, ge=0, le=100)
    max_retries: int = Field(default=3, ge=0, le=10)


class ExitConfig(BaseModel):
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


class DataConfig(BaseModel):
    backfill_years: float = Field(default=3.0, gt=0, le=10)
    parquet_dir: Path = Path("runtime/data")
    fetch_derivatives: bool = True
    # Refuse to backtest on data with holes — silent gaps manufacture fake edge.
    max_gap_ratio: float = Field(default=0.005, ge=0, le=0.5)
    rest_rate_limit_ms: int = Field(default=120, ge=0)


class LLMRoleConfig(BaseModel):
    enabled: bool = False
    max_calls_per_day: int = Field(default=20, ge=0)
    model: str = ""


class LLMConfig(BaseModel):
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


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    enabled: bool = True


class TelegramConfig(BaseModel):
    enabled: bool = False
    notify_fills: bool = True
    notify_risk_changes: bool = True
    notify_errors: bool = True
    daily_summary_hour_utc: int = Field(default=0, ge=0, le=23)


class EngineConfig(BaseModel):
    loop_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    # Restart storm detection: beyond this, drop to safe mode instead of thrashing.
    max_restarts_per_hour: int = Field(default=10, ge=1)
    stale_data_tolerance_bars: float = Field(default=2.0, ge=0.5)
    reconcile_on_start: bool = True
    state_snapshot_seconds: int = Field(default=30, ge=1)


# --------------------------------------------------------------------------
# Root config
# --------------------------------------------------------------------------


class Config(BaseModel):
    """Root configuration, assembled from YAML and validated as a whole."""

    mode: Mode = Mode.PAPER
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
    data: DataConfig = Field(default_factory=DataConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)

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
        if self.conviction.collapse_threshold >= self.conviction.absolute_floor:
            raise ValueError(
                "conviction.collapse_threshold must sit below absolute_floor, "
                "otherwise positions exit the moment they are opened"
            )
        if self.execution.slippage_bps == 0 and self.mode is not Mode.LIVE:
            raise ValueError(
                "zero slippage in backtest/paper produces fiction; set execution.slippage_bps > 0"
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
            "the environment. Paper and demo modes need no such flag."
        )
    if cfg.llm.enabled and cfg.llm.primary_provider == "gemini" and not secrets.gemini_api_key:
        raise RuntimeError("llm.enabled with primary_provider=gemini but GEMINI_API_KEY is unset")
    if cfg.llm.enabled and cfg.llm.primary_provider == "groq" and not secrets.groq_api_key:
        raise RuntimeError("llm.enabled with primary_provider=groq but GROQ_API_KEY is unset")
    if cfg.telegram.enabled and not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        raise RuntimeError("telegram.enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are unset")
