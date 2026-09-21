#!/usr/bin/env bash
#
# Start over: clean venue, clean local history.
#
#   ./ops/reset.sh --usdt 1000
#
# Stops the bot, cancels every order, sells every holding back to USDT, sets the
# demo balance, and wipes the local trading history. The market-data archive and
# the indicator measurements are kept by default — they are expensive to rebuild
# and nothing about them is dirty.
#
# Never touches real money: the venue side refuses any venue but bybit-demo, and
# there is no flag that changes that.
#
set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ" || exit 1
PY="$PROJ/.venv/bin/python"

usage() {
  cat <<'USAGE'
Usage: ./ops/reset.sh [options]

  --usdt N       demo balance to end up with (default 1000)
  --local-only   wipe local history, leave the venue alone
  --venue-only   reset the venue, keep local history
  --all          also delete the market-data archive and the measurements
                 (adds ~20 minutes of refetching to the next setup)
  --yes          skip the confirmation prompt

Kept unless --all is given:
  runtime/data/                    3 years of OHLCV and derivatives (~23MB)
  runtime/indicator_selection.json which indicators earned a vote
  runtime/universe.json            the traded symbol list
  runtime/reports/                 measurement reports
USAGE
}

USDT=1000
DO_LOCAL=1
DO_VENUE=1
WIPE_ALL=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --usdt)       USDT="$2"; shift 2 ;;
    --local-only) DO_VENUE=0; shift ;;
    --venue-only) DO_LOCAL=0; shift ;;
    --all)        WIPE_ALL=1; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; DIM=""; R=""; G=""; Y=""; N=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

# Every engine's runtime directory. Three engines share this account now, each
# with its own database and state; resetting one and not the others would
# leave two books describing positions the venue no longer holds.
RUNTIMES=$("$PY" - <<'RUNTIMES_PY' 2>/dev/null
import pathlib, sys
sys.path.insert(0, "src")
from tbot.core.config import load_config
seen = []
for path in sorted(pathlib.Path("config").glob("*.yaml")):
    if path.stem == "base":
        continue
    try:
        cfg = load_config(str(path))
    except Exception:
        continue
    if cfg.mode.value != "live":
        continue
    rel = str(cfg.runtime_dir.relative_to(pathlib.Path.cwd()))
    if rel not in seen:
        seen.append(rel)
print(" ".join(seen))
RUNTIMES_PY
)
[[ -n "$RUNTIMES" ]] || RUNTIMES="runtime"

# A live agent would restart mid-wipe and reconcile against a half-reset account.
head_ "Stopping every engine"
for engine in $("$PROJ/ops/bot.sh" engines 2>/dev/null); do
  "$PROJ/ops/bot.sh" stop "$engine" >/dev/null 2>&1
done
if pgrep -f "scripts/run.py" >/dev/null 2>&1; then
  warn "an engine is still running; killing it"
  pkill -f "scripts/run.py"
  sleep 2
fi
ok "stopped"

head_ "This will permanently delete"
if [[ $DO_VENUE -eq 1 ]]; then
  say "  ${R}venue${N}  every perpetual position, closed at market, and every perp order"
  say "  ${R}venue${N}  every resting spot order and protective stop on bybit-demo"
  say "  ${R}venue${N}  every holding, sold to USDT at market"
  say "  ${R}venue${N}  the USDT balance, set to ${USDT}"
fi
if [[ $DO_LOCAL -eq 1 ]]; then
  say "  ${R}local${N}  in each of: ${RUNTIMES}"
  say "  ${R}local${N}    tbot.db — every trade, scan, order, fill and equity point"
  say "  ${R}local${N}    state.json, gate1.json, restarts.json, promotion.json, logs"
  say "  ${R}local${N}  runtime-shared/claims.sqlite — which engine owns which perpetual"
  if [[ $WIPE_ALL -eq 1 ]]; then
    say "  ${R}local${N}  runtime/data/ — the whole market-data archive"
    say "  ${R}local${N}  runtime/indicator_selection.json, universe.json, reports/"
  else
    say "  ${DIM}kept${N}   data/, indicator_selection.json, universe.json, reports/ (each engine)"
  fi
fi
say ""
say "  ${DIM}The database is backed up first, so this is recoverable from"
say "  runtime/backups/ until you delete that too.${N}"

if [[ $ASSUME_YES -eq 0 ]]; then
  if [[ ! -t 0 ]]; then
    say ""
    say "${R}Refusing: this needs an interactive terminal (or --yes).${N}"
    exit 1
  fi
  say ""
  read -r -p "  Type RESET to continue: " typed
  [[ "$typed" == "RESET" ]] || { say ""; say "${R}Not confirmed. Nothing changed.${N}"; exit 1; }
fi

# ------------------------------------------------------------------- venue

if [[ $DO_VENUE -eq 1 ]]; then
  head_ "Venue"
  if "$PY" scripts/venue_reset.py --usdt "$USDT"; then
    ok "venue reset"
  else
    say ""
    say "${R}Venue reset failed. Local state left alone so the two stay in step.${N}"
    exit 1
  fi
fi

# ------------------------------------------------------------------- local

if [[ $DO_LOCAL -eq 1 ]]; then
  head_ "Local state"

  for RT in $RUNTIMES; do
    if [[ -f "$RT/tbot.db" ]]; then
      mkdir -p "$RT/backups"
      BACKUP="$RT/backups/pre-reset-$(date +%Y%m%d-%H%M%S).db"
      # sqlite3 .backup rather than cp: a WAL database copied file-by-file can
      # land mid-transaction and come back corrupt.
      if sqlite3 "$RT/tbot.db" ".backup '$BACKUP'" 2>/dev/null; then
        ok "$RT database backed up to $BACKUP"
      else
        cp "$RT/tbot.db" "$BACKUP" 2>/dev/null && warn "$RT backed up with cp (sqlite3 unavailable)"
      fi
    fi

    rm -f "$RT/tbot.db" "$RT/tbot.db-shm" "$RT/tbot.db-wal"
    rm -f "$RT/state.json" "$RT/gate1.json" "$RT/restarts.json" \
          "$RT/promotion.json" "$RT/engine.lock" "$RT/command.json" \
          "$RT/last_backtest.json"
    rm -f "$RT"/logs/*.jsonl "$RT"/logs/*.jsonl.* \
          "$RT"/logs/launchd.*.log "$RT"/logs/web.*.log "$RT"/logs/health.*.log
    ok "$RT: trading history and logs cleared"

    if [[ $WIPE_ALL -eq 1 ]]; then
      rm -rf "$RT/data" "$RT/reports"
      rm -f "$RT/indicator_selection.json" "$RT/universe.json" \
            "$RT/polarity.json" "$RT/plateau.json" "$RT/exit_calibration.json"
      rm -f "$RT/.preinstall.state"
      ok "$RT: market data and measurements deleted"
    fi
  done

  # Claims describe positions; with the venue flat there are none to describe.
  rm -f runtime-shared/claims.sqlite runtime-shared/claims.sqlite-shm \
        runtime-shared/claims.sqlite-wal
  ok "symbol claims cleared"
  [[ $WIPE_ALL -eq 1 ]] || ok "market data and measurements kept"
fi

# ------------------------------------------------------------------- summary

head_ "Done"
if [[ $WIPE_ALL -eq 1 ]]; then
  say "  Start from zero:"
  say "    ./ops/pre-install.sh"
else
  say "  The archive and measurements are intact, so you can start straight away:"
  say "    ./ops/bot.sh start demo; ./ops/bot.sh start fast; ./ops/bot.sh start scalp"
fi
say ""
say "  ${DIM}Verify first with: ./ops/bot.sh status${N}"
