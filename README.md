# tbot

An autonomous crypto trading engine for Bybit spot: continuous scanning, high selectivity,
and a validation pipeline designed to **reject** strategies rather than accept them.

Design principle in one sentence: **being wrong should never look profitable.**
Right now the system rejects its own strategy (see [Current status](#current-status-gate-1-not-passed)).
That is not a failure; it is evidence that the validation works.

> Türkçe: [README.tr.md](README.tr.md)

> ⚠️ **Not financial advice.** This is a personal research project. Trading loses money.
> The code guarantees nothing. Use it at your own risk.

## Highlights

- **Python 3.11+**, `ccxt` for exchange access, WebSocket price feed between bars,
  Parquet + DuckDB data archive, SQLite state, FastAPI local dashboard, Telegram alerts.
- **300+ automated tests** (`pytest`) that guard the system's invariants, e.g. an indicator
  can never see the future, orders are idempotent, a stop always lives on the exchange.
- **Three broker implementations behind one interface**: backtest, paper and live (`ccxt`).
- **Statistical validation** before any real money: CPCV (combinatorial purged
  cross-validation), Deflated Sharpe Ratio, Probability of Backtest Overfitting, Monte Carlo.
- **Risk is continuous, not on/off**: position size shrinks and the entry threshold rises
  during drawdowns. New entries stop only at a hard floor (50% drawdown).
- **The LLM can only reduce risk.** It cannot create signals, set size or move stops.
  The response schema has no `side` / `size` / `entry` fields, so a "buy" answer cannot
  even be represented.

## Quick start

```bash
./ops/pre-install.sh          # setup + measurement + checks (run once)
./ops/bot.sh start paper      # run 24/7: bot + dashboard + health check
./ops/bot.sh status           # what is happening
./ops/bot.sh stop             # stop
```

`pre-install.sh` sets up the environment, downloads data, measures the indicators,
runs the backtest and Gate 1, then walks the engine's startup path up to the lock to
check that every step **actually** works. The first run takes about 20 minutes and
resumes if interrupted. It does not start the bot; that is a separate decision.

`bot.sh` generates launchd agents per mode (`paper` · `demo` · `live`), **checks that the
mode can start before installing it**, then waits for the engine to come up.
Real money requires Gate 1 and a typed confirmation (`TBOT_CONFIRM_LIVE=yes`).

API keys, Telegram and capital requirements: [SETUP.md](SETUP.md).
Step-by-step usage: [docs/USAGE.md](docs/USAGE.md).

## What it does differently

**Indicator selection is measured, not assumed.** Each of the 22 indicators is tested
with Information Coefficient and tail analysis; an indicator that fails cannot vote, no
matter how popular it is. Indicators with correlation above 0.8 fall into one "voting
block": in the measurement 7 indicators collapsed into a single block, so counting them
as separate confirmations would mean 7 copies of 1 reading.

**Validation is tied to a fixed bar.** Thresholds for CPCV, Deflated Sharpe and PBO are
set in advance and cannot be tuned to rescue a result. Every tried hypothesis is counted
(`research/hypotheses.md`), because the Deflated Sharpe Ratio prices the number of trials.

**Built against the real exchange.** Many design rules come from behaviour measured on
the Bybit demo account, not from documentation. For example, Bybit spot cannot hold a stop
and a take-profit order at the same time, so the stop lives on the exchange and the target
lives in the engine.

## Architecture

| Path | Contents |
|---|---|
| `core/` | Data models (R-multiples, MAE/MFE, no-trade reasons), pydantic config, clock, SQLite |
| `data/` | Parquet archive, resumable backfill, data quality gate, WebSocket feed |
| `indicators/` | 22 indicators (no external TA library), IC + tail analysis, correlation blocks |
| `strategy/` | Regime filter, entry setups, conviction engine (0–100), 4-layer exits |
| `risk/` | Continuous risk budget, stop-based sizing, leverage gate |
| `execution/` | Broker interface with backtest / paper / ccxt implementations, reconciliation, safety checks |
| `engine/` | Live loop, atomic state snapshots, watchdog, single-instance lock |
| `backtest/` | Replay engine, timeframe alignment, metrics, CPCV, DSR, Monte Carlo |
| `llm/` | Risk-only LLM roles, budget, redaction |
| `web/` | Local dashboard (localhost) |

## Current status: Gate 1 not passed

| check | value | |
|---|---|---|
| trades ≥ 50 | 113 | ✅ |
| profit factor ≥ 1.2 | 1.42 | ✅ |
| max drawdown ≤ 20% | 11.6% | ✅ |
| Deflated Sharpe significant | p = 1.000 | ❌ |
| majority of CPCV paths positive | 55.6% | ✅ |
| PBO ≤ 0.5 | 0.33 | ✅ |

Five of six checks pass; the Deflated Sharpe Ratio still rejects the strategy after
11 counted trials. For comparison, buy-and-hold BTC beat the strategy by a wide margin
over the same period. The way forward is fresh data (paper forward test), not more tuning
on the same data.

The infrastructure is complete and working. The strategy is not. Open hypotheses are in
`research/hypotheses.md`.

## Development

```bash
.venv/bin/python -m pytest -q             # tests
.venv/bin/ruff check src tests scripts    # lint
```

## License

[MIT](LICENSE). This software is provided "as is", without warranty of any kind.
It is not financial advice. You are responsible for any use with real money.
