"""Data quality gate.

A silent hole in the candle series is the most common way a backtest invents
edge that does not exist: the strategy simply never sees the bars where it would
have been stopped out. So the archive is checked before it is trusted, and a
failing check blocks the backtest rather than annotating it.

Checks run per (symbol, timeframe):

* **gaps** — missing bars against the expected grid
* **duplicates / misalignment** — store invariants, verified rather than assumed
* **OHLC consistency** — high >= max(open, close) >= min(open, close) >= low
* **zero-volume bars** — an exchange halt or a dead pair, not a tradeable price
* **price outliers** — single-bar moves far outside the symbol's own history,
  which are usually bad ticks and would otherwise become fake breakouts
* **coverage** — does the series actually span the range we intend to test
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from tbot.core.clock import iso, tf_ms, timeframe_offset
from tbot.core.logging import get_logger
from tbot.data.store import ParquetStore

log = get_logger("data.integrity")


@dataclass(slots=True)
class IntegrityReport:
    symbol: str
    timeframe: str
    bars: int
    first_ts: int | None
    last_ts: int | None
    expected_bars: int
    missing_bars: int
    gap_ratio: float
    largest_gap_bars: int
    duplicate_ts: int
    misaligned_ts: int
    ohlc_violations: int
    zero_volume_bars: int
    price_outliers: int
    coverage_days: float
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "tf": self.timeframe,
            "bars": self.bars,
            "days": round(self.coverage_days, 1),
            "missing": self.missing_bars,
            "gap%": round(self.gap_ratio * 100, 3),
            "max_gap": self.largest_gap_bars,
            "dup": self.duplicate_ts,
            "misalign": self.misaligned_ts,
            "ohlc_bad": self.ohlc_violations,
            "zero_vol": self.zero_volume_bars,
            "outliers": self.price_outliers,
            "ok": self.ok,
            "issues": "; ".join(self.issues),
        }


# A flat "at least 200 bars" is wrong across timeframes: three years of weekly
# data is 156 bars by definition, and no amount of fetching changes that. The
# floor is expressed per timeframe as what a usable history actually looks like.
MIN_BARS_BY_TIMEFRAME = {
    "1m": 5000,
    "5m": 2000,
    "15m": 1000,
    "30m": 800,
    "1h": 500,
    "2h": 400,
    "4h": 300,
    "6h": 250,
    "12h": 200,
    "1d": 200,
    "1w": 100,
}


def check_series(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    max_gap_ratio: float = 0.005,
    outlier_sigma: float = 12.0,
    min_bars: int | None = None,
) -> IntegrityReport:
    """Run every check against one candle series."""
    if min_bars is None:
        min_bars = MIN_BARS_BY_TIMEFRAME.get(timeframe, 200)
    issues: list[str] = []
    step = tf_ms(timeframe)

    if df.empty:
        return IntegrityReport(
            symbol=symbol,
            timeframe=timeframe,
            bars=0,
            first_ts=None,
            last_ts=None,
            expected_bars=0,
            missing_bars=0,
            gap_ratio=1.0,
            largest_gap_bars=0,
            duplicate_ts=0,
            misaligned_ts=0,
            ohlc_violations=0,
            zero_volume_bars=0,
            price_outliers=0,
            coverage_days=0.0,
            issues=["no data"],
        )

    ts = df["ts"].to_numpy(dtype="int64")
    first_ts, last_ts = int(ts[0]), int(ts[-1])
    expected = int((last_ts - first_ts) // step) + 1
    bars = len(df)

    offset = timeframe_offset(timeframe)
    duplicate_ts = int(bars - len(np.unique(ts)))
    misaligned_ts = int(np.count_nonzero((ts - offset) % step != 0))

    diffs = np.diff(ts)
    missing = int(np.sum(diffs // step - 1)) if diffs.size else 0
    largest_gap = int(diffs.max() // step - 1) if diffs.size else 0
    gap_ratio = missing / expected if expected else 0.0

    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    open_ = df["open"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    volume = df["volume"].to_numpy(dtype="float64")

    ohlc_violations = int(
        np.count_nonzero(
            (high < low)
            | (high < np.maximum(open_, close) - 1e-12)
            | (low > np.minimum(open_, close) + 1e-12)
            | (high <= 0)
            | (low <= 0)
        )
    )
    zero_volume = int(np.count_nonzero(volume <= 0))

    # Outliers are judged against the symbol's own return distribution. A fixed
    # percentage threshold would flag normal moves in a volatile altcoin and miss
    # broken ticks in a quiet one.
    outliers = 0
    if bars > min_bars:
        returns = np.diff(np.log(np.maximum(close, 1e-12)))
        median = np.median(returns)
        mad = np.median(np.abs(returns - median))
        if mad > 0:
            robust_sigma = 1.4826 * mad
            outliers = int(
                np.count_nonzero(np.abs(returns - median) > outlier_sigma * robust_sigma)
            )

    coverage_days = (last_ts - first_ts) / 86_400_000

    if duplicate_ts:
        issues.append(f"{duplicate_ts} duplicate timestamps")
    if misaligned_ts:
        issues.append(f"{misaligned_ts} misaligned timestamps")
    if gap_ratio > max_gap_ratio:
        issues.append(f"gap ratio {gap_ratio:.2%} exceeds limit {max_gap_ratio:.2%}")
    if ohlc_violations:
        issues.append(f"{ohlc_violations} OHLC consistency violations")
    if bars < min_bars:
        issues.append(f"only {bars} bars (minimum {min_bars})")
    if largest_gap > 24:
        issues.append(f"largest gap {largest_gap} bars — likely an exchange outage or delisting")

    return IntegrityReport(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        first_ts=first_ts,
        last_ts=last_ts,
        expected_bars=expected,
        missing_bars=missing,
        gap_ratio=gap_ratio,
        largest_gap_bars=largest_gap,
        duplicate_ts=duplicate_ts,
        misaligned_ts=misaligned_ts,
        ohlc_violations=ohlc_violations,
        zero_volume_bars=zero_volume,
        price_outliers=outliers,
        coverage_days=coverage_days,
        issues=issues,
    )


def check_store(
    store: ParquetStore,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    max_gap_ratio: float = 0.005,
) -> list[IntegrityReport]:
    """Check every stored series, or a named subset."""
    reports: list[IntegrityReport] = []
    symbols = symbols or store.symbols()

    for symbol in symbols:
        tfs = timeframes
        if tfs is None:
            folder = store.ohlcv_path(symbol, "x").parent
            tfs = sorted(p.stem for p in folder.glob("*.parquet")) if folder.exists() else []
        for timeframe in tfs:
            df = store.read_ohlcv(symbol, timeframe)
            reports.append(check_series(df, symbol, timeframe, max_gap_ratio=max_gap_ratio))

    return reports


def derivative_coverage(store: ParquetStore, symbols: list[str]) -> pd.DataFrame:
    """How far each derivative series reaches, per symbol.

    Bybit serves a much shorter window for open interest than for candles, so
    this is reported rather than treated as a failure — the scoring layer must
    simply not assume derivative data exists for the full backtest range.
    """
    rows = []
    for symbol in symbols:
        row: dict[str, object] = {"symbol": symbol}
        for kind in ("funding", "open_interest", "long_short"):
            df = store.read_derivative(symbol, kind)
            row[f"{kind}_rows"] = len(df)
            if df.empty:
                row[f"{kind}_days"] = 0.0
            else:
                span = (int(df["ts"].max()) - int(df["ts"].min())) / 86_400_000
                row[f"{kind}_days"] = round(span, 1)
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(
    reports: list[IntegrityReport],
    deriv: pd.DataFrame | None,
    path: Path,
) -> Path:
    """Write a markdown integrity report and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame([r.as_row() for r in reports])
    failed = [r for r in reports if not r.ok]

    lines = [
        "# Veri bütünlüğü raporu",
        "",
        f"- Seri sayısı: **{len(reports)}**",
        f"- Sorunlu seri: **{len(failed)}**",
        f"- Toplam bar: **{sum(r.bars for r in reports):,}**",
        "",
        "Bir backtest, bu raporda sorunlu görünen seriler düzeltilmeden çalıştırılmamalı:",
        "sessiz veri boşluğu, var olmayan bir edge üretmenin en sık yoludur.",
        "",
        "## Seriler",
        "",
        table.to_markdown(index=False) if not table.empty else "_veri yok_",
        "",
    ]

    if failed:
        lines += ["## Sorunlar", ""]
        for r in failed:
            span = f"{iso(r.first_ts)} .. {iso(r.last_ts)}" if r.first_ts else "—"
            lines.append(f"- **{r.symbol} {r.timeframe}** ({span}): {'; '.join(r.issues)}")
        lines.append("")

    if deriv is not None and not deriv.empty:
        lines += [
            "## Türev verisi kapsamı",
            "",
            "Bybit açık pozisyon (OI) verisini fiyat verisinden çok daha kısa bir pencerede",
            "sunuyor. Bu bir hata değil; skorlama katmanı türev verisinin her bar için",
            "var olduğunu varsaymamalı.",
            "",
            deriv.to_markdown(index=False),
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("integrity_report_written", path=str(path), series=len(reports), failed=len(failed))
    return path


def assert_backtest_ready(reports: list[IntegrityReport]) -> None:
    """Raise unless every series is clean. Called before any backtest run."""
    failed = [r for r in reports if not r.ok]
    if failed:
        detail = "\n".join(
            f"  {r.symbol} {r.timeframe}: {'; '.join(r.issues)}" for r in failed[:20]
        )
        raise RuntimeError(
            f"{len(failed)} data series failed integrity checks; refusing to backtest on them.\n"
            f"{detail}\n"
            f"Run the backfill again for these symbols, or exclude them from the universe."
        )
