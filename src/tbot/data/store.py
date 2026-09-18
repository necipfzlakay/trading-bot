"""Parquet archive for candles and derivatives series.

One file per (symbol, timeframe) keeps reads cheap and lets DuckDB query the
whole archive with a glob. Writes are merge-then-rewrite rather than append:
re-fetching an overlapping range must converge to the same file, because a
backfill interrupted halfway is the normal case, not the exception.

Invariants enforced on every write, since a violation here quietly manufactures
edge that will not exist live:

* timestamps strictly increasing, no duplicates
* every timestamp aligned to its timeframe boundary
* only closed bars — a forming bar never reaches disk
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tbot.core.clock import floor_bar, is_bar_closed, now_ms, tf_ms, timeframe_offset
from tbot.core.logging import get_logger

log = get_logger("data.store")

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def safe_symbol(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTC_USDT``; ``BTC/USDT:USDT`` -> ``BTC_USDT_USDT``."""
    return symbol.replace("/", "_").replace(":", "_")


class ParquetStore:
    """File-backed store for OHLCV and derivative series."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        (self.root / "ohlcv").mkdir(parents=True, exist_ok=True)
        (self.root / "derivatives").mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------

    def ohlcv_path(self, symbol: str, timeframe: str) -> Path:
        return self.root / "ohlcv" / safe_symbol(symbol) / f"{timeframe}.parquet"

    def derivative_path(self, symbol: str, kind: str) -> Path:
        return self.root / "derivatives" / safe_symbol(symbol) / f"{kind}.parquet"

    # -- OHLCV -------------------------------------------------------------

    def write_ohlcv(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """Merge ``df`` into the stored series and rewrite the file.

        Returns the total row count after the merge. Overlapping timestamps
        resolve to the incoming values, so a re-fetch repairs bad rows instead
        of duplicating them.
        """
        df = self._normalise_ohlcv(df, timeframe)
        path = self.ohlcv_path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, df], ignore_index=True)
            combined = combined.drop_duplicates(subset="ts", keep="last")
        else:
            combined = df

        combined = combined.sort_values("ts").reset_index(drop=True)
        combined.to_parquet(path, index=False, compression="zstd")
        return len(combined)

    def read_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> pd.DataFrame:
        """Read a (half-open) range. Returns an empty frame when absent."""
        path = self.ohlcv_path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.read_parquet(path)
        if start_ts is not None:
            df = df[df["ts"] >= start_ts]
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        return df.reset_index(drop=True)

    def last_ts(self, symbol: str, timeframe: str) -> int | None:
        """Newest stored bar open time — the resume point for an incremental backfill."""
        path = self.ohlcv_path(symbol, timeframe)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["ts"])
        return None if df.empty else int(df["ts"].max())

    def first_ts(self, symbol: str, timeframe: str) -> int | None:
        path = self.ohlcv_path(symbol, timeframe)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["ts"])
        return None if df.empty else int(df["ts"].min())

    def _normalise_ohlcv(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        missing = set(OHLCV_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"OHLCV frame missing columns: {sorted(missing)}")

        out = df[OHLCV_COLUMNS].copy()
        out["ts"] = out["ts"].astype("int64")
        for col in ("open", "high", "low", "close", "volume"):
            out[col] = out[col].astype("float64")

        step = tf_ms(timeframe)
        offset = timeframe_offset(timeframe)
        misaligned = (out["ts"] - offset) % step != 0
        if misaligned.any():
            raise ValueError(
                f"{int(misaligned.sum())} timestamps not aligned to {timeframe} boundaries; "
                f"first offender ts={int(out.loc[misaligned, 'ts'].iloc[0])}"
            )

        # Drop any bar that has not finished forming. Letting one through would
        # give indicators a partial close — lookahead that looks like data.
        now = now_ms()
        still_forming = ~out["ts"].map(lambda ts: is_bar_closed(int(ts), timeframe, now))
        if still_forming.any():
            log.debug("dropping_forming_bars", count=int(still_forming.sum()), tf=timeframe)
            out = out[~still_forming]

        return (
            out.drop_duplicates(subset="ts", keep="last").sort_values("ts").reset_index(drop=True)
        )

    # -- derivatives -------------------------------------------------------

    def write_derivative(self, symbol: str, kind: str, df: pd.DataFrame) -> int:
        """Store a derivative series (``funding``, ``open_interest``, ``long_short``)."""
        if df.empty:
            return 0
        if "ts" not in df.columns:
            raise ValueError(f"derivative frame for {kind} needs a 'ts' column")

        df = df.copy()
        df["ts"] = df["ts"].astype("int64")
        path = self.derivative_path(symbol, kind)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)

        df = df.drop_duplicates(subset="ts", keep="last").sort_values("ts").reset_index(drop=True)
        df.to_parquet(path, index=False, compression="zstd")
        return len(df)

    def read_derivative(
        self, symbol: str, kind: str, start_ts: int | None = None, end_ts: int | None = None
    ) -> pd.DataFrame:
        path = self.derivative_path(symbol, kind)
        if not path.exists():
            return pd.DataFrame(columns=["ts"])
        df = pd.read_parquet(path)
        if start_ts is not None:
            df = df[df["ts"] >= start_ts]
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        return df.reset_index(drop=True)

    def last_derivative_ts(self, symbol: str, kind: str) -> int | None:
        path = self.derivative_path(symbol, kind)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["ts"])
        return None if df.empty else int(df["ts"].max())

    # -- alignment ---------------------------------------------------------

    def read_with_derivatives(
        self,
        symbol: str,
        timeframe: str,
        kinds: tuple[str, ...] = ("funding", "open_interest", "long_short"),
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> pd.DataFrame:
        """Candles joined to derivative series, aligned backwards in time.

        Derivative observations are stamped to the bar that was open when they
        were published and carried forward. Forward-filling only ever reuses a
        value the market had already seen, so the join cannot leak the future —
        which a naive nearest-timestamp merge absolutely would.
        """
        df = self.read_ohlcv(symbol, timeframe, start_ts, end_ts)
        if df.empty:
            return df

        step = tf_ms(timeframe)
        for kind in kinds:
            series = self.read_derivative(symbol, kind, start_ts=None, end_ts=end_ts)
            if series.empty:
                continue
            value_cols = [c for c in series.columns if c != "ts"]
            series = series.copy()
            series["bar_ts"] = (series["ts"] // step) * step
            # Several observations can fall inside one bar; the last one is the
            # only one fully known by the time the bar closes.
            collapsed = series.groupby("bar_ts")[value_cols].last().reset_index()
            df = df.merge(collapsed, left_on="ts", right_on="bar_ts", how="left").drop(
                columns=["bar_ts"]
            )
            df[value_cols] = df[value_cols].ffill()

        return df

    # -- inventory ---------------------------------------------------------

    def inventory(self) -> pd.DataFrame:
        """What is on disk: symbol, timeframe, bar count, coverage, file size."""
        rows = []
        for path in sorted((self.root / "ohlcv").glob("*/*.parquet")):
            df = pd.read_parquet(path, columns=["ts"])
            rows.append(
                {
                    "symbol": path.parent.name,
                    "timeframe": path.stem,
                    "bars": len(df),
                    "first_ts": int(df["ts"].min()) if not df.empty else None,
                    "last_ts": int(df["ts"].max()) if not df.empty else None,
                    "size_mb": round(path.stat().st_size / 1e6, 3),
                }
            )
        return pd.DataFrame(rows)

    def symbols(self) -> list[str]:
        return sorted(
            p.name.replace("_", "/", 1) for p in (self.root / "ohlcv").iterdir() if p.is_dir()
        )


def bar_align(ts: int, timeframe: str) -> int:
    """Convenience re-export so callers need not import clock alongside store."""
    return floor_bar(ts, timeframe)
