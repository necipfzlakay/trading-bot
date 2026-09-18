"""Multi-timeframe alignment.

The quietest source of lookahead in any multi-timeframe system, and the reason
it gets its own module with its own tests.

The naive approach maps a fast bar to the slow bar *containing* it. At 12:00 on
the 1h chart, that maps to the 4h bar starting at 12:00 — a bar that will not
close until 16:00 and whose high, low and close are therefore unknown. The
strategy ends up reading four hours into the future on every single bar, and the
backtest looks superb.

The correct rule is about close times, not containment: a slow bar is available
to a fast bar only once the slow bar has **closed at or before** the fast bar's
own close.

    1h bar opening 12:00 closes 13:00
    -> 4h bar opening 08:00 closed at 12:00  -> available
    -> 4h bar opening 12:00 closes at 16:00  -> not available
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tbot.core.clock import tf_ms


def align_slow_to_fast(
    fast_ts: pd.Series,
    slow_ts: pd.Series,
    fast_timeframe: str,
    slow_timeframe: str,
) -> np.ndarray:
    """Index into the slow series that each fast bar may legally see.

    Returns -1 where no slow bar has closed yet. Callers must treat -1 as "no
    information", never as index 0.
    """
    fast_close = fast_ts.to_numpy(dtype="int64") + tf_ms(fast_timeframe)
    slow_close = slow_ts.to_numpy(dtype="int64") + tf_ms(slow_timeframe)

    # searchsorted with side="right" gives the count of slow bars closing at or
    # before each fast close; minus one is the newest usable index.
    positions = np.searchsorted(slow_close, fast_close, side="right") - 1
    return positions


def project_series(values: pd.Series | np.ndarray, mapping: np.ndarray, fill=np.nan) -> np.ndarray:
    """Project a slow-timeframe series onto fast bars using ``mapping``."""
    source = values.to_numpy() if isinstance(values, pd.Series) else np.asarray(values)
    out = np.full(len(mapping), fill, dtype=object if source.dtype == object else "float64")
    valid = mapping >= 0
    out[valid] = source[mapping[valid]]
    return out


def project_frame(frame: pd.DataFrame, mapping: np.ndarray, suffix: str) -> pd.DataFrame:
    """Project every column of a slow-timeframe frame onto fast bars."""
    projected = {
        f"{column}{suffix}": project_series(frame[column], mapping) for column in frame.columns
    }
    return pd.DataFrame(projected)
