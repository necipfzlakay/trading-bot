#!/usr/bin/env bash
#
# One-shot setup. Runs everything SETUP.md and docs/USAGE.md ask you to type by
# hand, in order, and checks that each step actually worked rather than that it
# merely exited.
#
# Safe to re-run: completed steps are recorded and skipped. Nothing here starts
# the bot, installs launchd agents, pushes to git, or touches real money — those
# are separate decisions and stay separate commands.
#
set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ" || exit 1

usage() {
  cat <<'USAGE'
Usage: ./ops/pre-install.sh [options]

Sets the project up end to end: environment, secrets file, tests, market data,
indicator measurement, backtest, Gate 1, and a smoke test of the engine's own
startup path. Re-running skips whatever already succeeded.

  --years N        history to backfill (default 3)
  --fast           1 year instead of 3 — enough to smoke-test the pipeline,
                   NOT enough to judge a strategy
  --trials N       trial count for Gate 1 (default: read from research/hypotheses.md)
  --skip-data      use the existing archive, fetch nothing
  --skip-research  measure nothing (no IC, backtest or Gate 1)
  --check          verify only; change nothing, fetch nothing
  --force          re-run steps already marked done
  --yes, -y        never prompt
USAGE
}

YEARS=3
TRIALS=""
SKIP_DATA=0
SKIP_RESEARCH=0
CHECK_ONLY=0
FORCE=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --years)         YEARS="$2"; shift 2 ;;
    --fast)          YEARS=1; shift ;;
    --trials)        TRIALS="$2"; shift 2 ;;
    --skip-data)     SKIP_DATA=1; shift ;;
    --skip-research) SKIP_RESEARCH=1; shift ;;
    --check)         CHECK_ONLY=1; SKIP_DATA=1; SKIP_RESEARCH=1; shift ;;
    --force)         FORCE=1; shift ;;
    --yes|-y)        ASSUME_YES=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- presentation

if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; DIM=""; R=""; G=""; Y=""; N=""
fi

TOTAL_STEPS=12
STEP_NO=0
FAILED=()
WARNED=()
NOTES=()

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; WARNED+=("$*"); }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; FAILED+=("$*"); }
note() { NOTES+=("$*"); }

banner() {
  STEP_NO=$((STEP_NO + 1))
  printf '\n%s[%d/%d] %s%s\n' "$B" "$STEP_NO" "$TOTAL_STEPS" "$1" "$N"
}

LOGDIR="$PROJ/runtime/logs"
mkdir -p "$LOGDIR" "$PROJ/runtime"

# Run a command, keep the full output in a log, show the tail, and return the
# command's own exit code. Piping to `tail` directly would return tail's status
# and quietly turn every failure into a success.
run_logged() {
  local label="$1" mode="$2" lines="$3"; shift 3
  local logf="$LOGDIR/preinstall-$label.log"
  local start rc
  start=$(date +%s)
  if [[ "$mode" == stream ]]; then
    # Long downloads print progress; showing it live is the point.
    "$@" 2>&1 | tee "$logf" | sed 's/^/    /'
    rc=${PIPESTATUS[0]}
  else
    "$@" >"$logf" 2>&1
    rc=$?
    [[ "$lines" -gt 0 ]] && tail -n "$lines" "$logf" | sed 's/^/    /'
  fi
  printf '  %s%ds · runtime/logs/preinstall-%s.log%s\n' "$DIM" "$(( $(date +%s) - start ))" "$label" "$N"
  return $rc
}

# ------------------------------------------------------------------ step state

STATE_FILE="$PROJ/runtime/.preinstall.state"
[[ -f "$STATE_FILE" ]] || : > "$STATE_FILE"

done_already() { [[ $FORCE -eq 0 ]] && grep -qx "$1" "$STATE_FILE" 2>/dev/null; }
mark_done()    { grep -qx "$1" "$STATE_FILE" 2>/dev/null || echo "$1" >> "$STATE_FILE"; }

PY="$PROJ/.venv/bin/python"

say ""
say "${B}TRADING-BOT — pre-install${N}"
say "${DIM}$PROJ${N}"
[[ $CHECK_ONLY -eq 1 ]] && say "${DIM}check mode: nothing will be changed${N}"

# ============================================================ 1. preflight

banner "Preflight"

case "$(uname -s)" in
  Darwin) ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')" ;;
  Linux)  warn "Linux — everything works except the launchd agents in ops/" ;;
  *)      bad "unsupported platform: $(uname -s)" ;;
esac

SYS_PY="$(command -v python3 || true)"
if [[ -z "$SYS_PY" ]]; then
  bad "python3 not found — install it before continuing"
elif ! "$SYS_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  bad "python3 is $("$SYS_PY" -V 2>&1 | cut -d' ' -f2); 3.11 or newer required"
else
  ok "python3 $("$SYS_PY" -V 2>&1 | cut -d' ' -f2)"
fi

LAYOUT_OK=1
for f in pyproject.toml config/base.yaml src/tbot/__init__.py scripts/run.py; do
  if [[ ! -e "$f" ]]; then
    bad "missing $f — is this the project root?"
    LAYOUT_OK=0
  fi
done
[[ $LAYOUT_OK -eq 1 ]] && ok "project layout intact"

# Three years across 21 symbols and 3 timeframes plus derivatives lands near
# 1 GB. Ask for headroom rather than discovering the shortfall 12 minutes in.
if [[ "$(uname -s)" == Darwin ]]; then
  AVAIL_GB=$(df -g . 2>/dev/null | awk 'NR==2 {print $4}')
else
  AVAIL_GB=$(df -BG . 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}')
fi
if [[ -n "${AVAIL_GB:-}" && "$AVAIL_GB" =~ ^[0-9]+$ && "$AVAIL_GB" -lt 3 ]]; then
  warn "${AVAIL_GB}GB free — the archive wants ~1GB and grows"
else
  ok "disk ${AVAIL_GB:-?}GB free"
fi

if curl -sf --max-time 10 https://api.bybit.com/v5/market/time >/dev/null 2>&1; then
  ok "api.bybit.com reachable"
elif [[ $SKIP_DATA -eq 1 ]]; then
  warn "api.bybit.com unreachable (data steps are skipped anyway)"
else
  bad "api.bybit.com unreachable — backfill and live runs both need it"
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  say ""
  say "${R}Preflight failed. Fix the above, then re-run.${N}"
  exit 1
fi

# ============================================================ 2. virtualenv

banner "Virtual environment"

if [[ ! -x "$PY" ]]; then
  if [[ $CHECK_ONLY -eq 1 ]]; then
    bad ".venv missing"
  else
    say "  creating .venv"
    "$SYS_PY" -m venv "$PROJ/.venv" || bad "venv creation failed"
  fi
fi

if [[ -x "$PY" ]]; then
  ok "interpreter $("$PY" -V 2>&1 | cut -d' ' -f2) at .venv/bin/python"

  HAVE_DEPS=0
  if "$PY" -c 'import tbot, ccxt, pandas, pyarrow, pydantic, fastapi, uvicorn, structlog, duckdb, scipy, httpx, tabulate' 2>/dev/null \
     && [[ -x "$PROJ/.venv/bin/pytest" && -x "$PROJ/.venv/bin/ruff" ]]; then
    HAVE_DEPS=1
  fi

  if [[ $HAVE_DEPS -eq 1 ]]; then
    ok "dependencies present"
    mark_done deps
  elif [[ $CHECK_ONLY -eq 1 ]]; then
    bad "dependencies incomplete — run without --check to install them"
  else
    say "  installing dependencies (a few minutes on first run)"
    "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
    if run_logged deps quiet 3 "$PY" -m pip install -e '.[dev]'; then
      ok "dependencies installed"
      mark_done deps
    else
      bad "pip install failed — see runtime/logs/preinstall-deps.log"
    fi
  fi
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  say ""
  say "${R}Cannot continue without a working environment.${N}"
  exit 1
fi

# ============================================================ 3. secrets file

banner "Secrets file (.env)"

if [[ ! -f .env ]]; then
  if [[ $CHECK_ONLY -eq 1 ]]; then
    warn ".env missing — demo trading needs BYBIT_DEMO_* keys"
  else
    cp .env.example .env
    # Localhost only, but a documented default is still a default.
    TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    "$PY" - "$TOKEN" <<'PYEOF'
import pathlib, sys
p = pathlib.Path(".env")
p.write_text(
    p.read_text(encoding="utf-8").replace(
        "WEB_TOKEN=change-me-local-only", f"WEB_TOKEN={sys.argv[1]}"
    ),
    encoding="utf-8",
)
PYEOF
    ok ".env created from .env.example, WEB_TOKEN generated"
  fi
else
  ok ".env exists (left untouched)"
  if grep -q '^WEB_TOKEN=change-me-local-only' .env; then
    warn "WEB_TOKEN is still the default — fine on localhost, change it if you expose the panel"
  fi
fi

# Which optional credentials are present. The values are never printed.
if [[ -f .env ]]; then
  filled() { grep -qE "^$1=.+" .env; }

  if filled BYBIT_DEMO_API_KEY; then ok "Bybit demo key set"
  else note "Bybit demo keys not set — needed only for demo mode (SETUP.md §3)"; fi

  if filled TELEGRAM_BOT_TOKEN; then ok "Telegram token set"
  else note "Telegram not configured — no alerts or remote commands (SETUP.md §4)"; fi

  if filled GEMINI_API_KEY || filled GROQ_API_KEY; then ok "LLM key set"
  else note "No LLM key — the five LLM roles stay off; trading is unaffected (SETUP.md §5)"; fi

  if filled BYBIT_LIVE_API_KEY; then
    warn "a LIVE Bybit key is present in .env — real money is one config away"
  fi
fi

if git -C "$PROJ" rev-parse --git-dir >/dev/null 2>&1; then
  # One path per call: `check-ignore -q` rejects several at once.
  UNIGNORED=()
  for path in .env runtime; do
    git -C "$PROJ" check-ignore -q "$path" 2>/dev/null || UNIGNORED+=("$path")
  done
  if [[ ${#UNIGNORED[@]} -eq 0 ]]; then
    ok ".env and runtime/ are git-ignored"
  else
    bad "NOT git-ignored: ${UNIGNORED[*]} — fix .gitignore before any push"
  fi
fi

# ============================================================ 4. test suite

banner "Test suite"

if done_already tests; then
  ok "already passed (--force to re-run)"
elif run_logged pytest quiet 4 "$PY" -m pytest -q; then
  ok "tests pass"
  mark_done tests
else
  bad "tests failed — do not run the bot until this is green"
fi

if "$PROJ/.venv/bin/ruff" check src tests scripts --quiet >/dev/null 2>&1; then
  ok "lint clean"
else
  warn "ruff reported issues (cosmetic; does not block)"
fi

# ============================================================ 5. config validation

banner "Configuration"

for cfg in config/*.yaml; do
  if out="$("$PY" - "$cfg" <<'PYEOF' 2>&1
import sys
sys.path.insert(0, "src")
from tbot.core.config import load_config
c = load_config(sys.argv[1])
print(
    f"{c.mode.value}/{c.venue.value}  risk={c.risk.risk_per_trade:.4f} "
    f"min_equity={c.risk.min_equity:.0f} threshold={c.conviction.base_threshold:.0f} "
    f"{c.conviction.selection}"
)
PYEOF
  )"; then
    ok "$(basename "$cfg") — $out"
  else
    bad "$(basename "$cfg") invalid: $(echo "$out" | tail -2 | tr '\n' ' ')"
  fi
done

# ============================================================ 6. market data

banner "Market data"

if [[ $SKIP_DATA -eq 1 ]]; then
  if [[ -d runtime/data/ohlcv ]]; then
    ok "skipped; archive holds $(find runtime/data/ohlcv -name '*.parquet' 2>/dev/null | wc -l | tr -d ' ') files"
  else
    warn "skipped and no archive exists — backtests and the engine will find nothing"
  fi
elif done_already "data-${YEARS}y"; then
  say "  archive already built for ${YEARS}y; fetching only the missing recent bars"
  if run_logged backfill-update quiet 4 "$PY" scripts/backfill.py; then
    ok "archive up to date"
  else
    warn "update reported failures — re-run; it resumes where it stopped"
  fi
else
  say "  fetching ${YEARS}y of 1h/4h/1d bars plus funding, open interest, long/short"
  say "  ${DIM}resumable — if interrupted, re-run this script and it continues${N}"
  if run_logged backfill stream 0 "$PY" scripts/backfill.py --years "$YEARS" --with-derivatives; then
    mark_done "data-${YEARS}y"
    ok "archive built"
  else
    bad "backfill reported failures — re-run this script, it resumes"
  fi
fi

# ============================================================ 7. data integrity

banner "Data integrity"

if [[ -f runtime/reports/integrity.md ]]; then
  SERIES=$(grep -oE 'Seri sayısı: \*\*[0-9]+' runtime/reports/integrity.md | grep -oE '[0-9]+$' || echo "?")
  BROKEN=$(grep -oE 'Sorunlu seri: \*\*[0-9]+' runtime/reports/integrity.md | grep -oE '[0-9]+$' || echo "?")
  if [[ "$BROKEN" == "0" ]]; then
    ok "$SERIES/$SERIES series clean"
  else
    bad "$BROKEN of $SERIES series have gaps or misalignment — runtime/reports/integrity.md"
    note "A silent data gap is the most common way to manufacture an edge that is not there."
  fi
else
  warn "no integrity report yet — run the data step"
fi

# ============================================================ 8. measurements

banner "Indicator measurement"

if [[ $SKIP_RESEARCH -eq 1 ]]; then
  if [[ -f runtime/indicator_selection.json ]]; then
    ok "skipped; runtime/indicator_selection.json exists"
  else
    warn "skipped and never measured — every indicator would vote separately"
  fi
elif done_already research; then
  ok "already measured (--force to re-measure)"
else
  say "  measuring which indicators earn a vote (IC + tail analysis)"
  if run_logged ic quiet 4 "$PY" scripts/ic_report.py --timeframe 1d; then
    ok "runtime/indicator_selection.json written"
  else
    bad "ic_report failed — see runtime/logs/preinstall-ic.log"
  fi
  say "  measuring derivative signal polarity"
  if run_logged polarity quiet 4 "$PY" scripts/polarity.py; then
    ok "polarity measured"
    mark_done research
  else
    warn "polarity failed — the registry keeps its current directions"
  fi
fi

# ============================================================ 9. backtest

banner "Backtest"

if [[ $SKIP_RESEARCH -eq 1 ]]; then
  say "  ${DIM}skipped${N}"
elif run_logged backtest quiet 10 "$PY" scripts/backtest.py; then
  ok "runtime/reports/backtest.md written"
else
  bad "backtest failed — see runtime/logs/preinstall-backtest.log"
fi

# ============================================================ 10. Gate 1

banner "Gate 1 — is the result distinguishable from luck"

GATE1="unknown"
read_gate1() {
  [[ -f runtime/gate1.json ]] || return 1
  GATE1=$("$PY" -c 'import json;print("PASS" if json.load(open("runtime/gate1.json"))["passed"] else "FAIL")' 2>/dev/null || echo unknown)
}

if [[ $SKIP_RESEARCH -eq 1 ]]; then
  if read_gate1; then
    ok "skipped; last recorded verdict: $GATE1"
  else
    say "  ${DIM}skipped${N}"
  fi
else
  if [[ -z "$TRIALS" ]]; then
    TRIALS=$(sed -nE 's/.*Kümülatif deneme:[^0-9]*([0-9]+).*/\1/p' research/hypotheses.md 2>/dev/null | head -1)
    if [[ -z "$TRIALS" ]]; then
      # Defaulting to 1 would understate the bar, which is the one direction
      # this must never fail in. Say so instead of assuming.
      TRIALS=1
      warn "could not read the trial counter from research/hypotheses.md — using 1, which flatters the result"
    fi
  fi
  say "  counting $TRIALS previous attempts — Deflated Sharpe prices every one of them"
  # A non-zero exit here means the strategy failed the gate. That is a
  # measurement, not a broken install, so it is recorded and not counted as an
  # error. Only a crash would be one, and a crash leaves no gate1.json.
  run_logged cpcv quiet 10 "$PY" scripts/cpcv_report.py --trials "$TRIALS"
  if read_gate1; then
    if [[ "$GATE1" == "PASS" ]]; then
      ok "Gate 1 PASSED"
    else
      warn "Gate 1 not passed — demo forward testing is fine, real money is not"
    fi
  else
    bad "cpcv_report did not produce a verdict — see runtime/logs/preinstall-cpcv.log"
  fi
fi

# ============================================================ 11. capital

banner "Capital requirement"

if run_logged capital quiet 16 "$PY" scripts/capital_check.py; then
  ok "measured"
else
  warn "capital_check failed — the configured min_equity is unverified"
fi

# ============================================================ 12. smoke test

banner "Smoke test"

"$PY" - <<'PYEOF'
"""Walk the engine's own startup path, stopping short of acquiring the lock.

Everything here is what scripts/run.py does before it trades: validate config
and secrets, resolve the universe, load the measured indicator selection, reach
the venue for market specs, and build the dashboard app.
"""
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from _bootstrap import indicator_selection, setup, universe
from tbot.core.config import assert_runnable, load_secrets
from tbot.engine.runner import build_broker, venue_label
from tbot.web.app import create_app

GOOD, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"
fail = 0

cfg = setup("config/demo.yaml")
secrets = load_secrets()

try:
    assert_runnable(cfg, secrets)
    print(f"  {GOOD} config runnable — {cfg.mode.value} on {cfg.venue.value} [{venue_label(cfg.venue)}]")
except Exception as exc:
    print(f"  {BAD} config refused: {exc}")
    fail = 1

symbols = universe(cfg)
if symbols:
    print(f"  {GOOD} universe resolved — {len(symbols)} symbols, first {symbols[0]}")
else:
    print(f"  {WARN} universe empty — run scripts/backfill.py --refresh-universe")

blocks, _selected, _decay = indicator_selection(cfg)
if blocks:
    print(f"  {GOOD} indicator selection loaded — {len(set(blocks.values()))} voting blocks")
else:
    print(f"  {WARN} no indicator selection — every indicator would vote separately")

# Three symbols prove the venue answers; twenty-one only costs time.
try:
    build_broker(cfg, secrets, symbols[:3] or ["BTC/USDT"])
    print(f"  {GOOD} broker built, venue answered for market specs")
except Exception as exc:
    print(f"  {BAD} broker failed: {exc}")
    fail = 1

try:
    create_app(cfg)
    print(f"  {GOOD} dashboard app builds — http://{cfg.web.host}:{cfg.web.port}")
except Exception as exc:
    print(f"  {BAD} dashboard failed: {exc}")
    fail = 1

raise SystemExit(fail)
PYEOF
if [[ $? -eq 0 ]]; then
  mark_done smoke
else
  bad "smoke test failed — the engine would not start"
fi

if lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "port 8787 is in use — the dashboard will not bind"
fi
if pgrep -f "scripts/run.py" >/dev/null 2>&1; then
  warn "an engine is already running (pgrep matched scripts/run.py)"
fi

# ============================================================ summary

say ""
say "${B}──────────────────────────────────────────────────────────${N}"

if [[ ${#FAILED[@]} -gt 0 ]]; then
  say "${R}${B}SETUP INCOMPLETE — ${#FAILED[@]} problem(s)${N}"
  for f in "${FAILED[@]}"; do say "  ${R}✗${N} $f"; done
else
  say "${G}${B}SETUP COMPLETE${N}"
fi

if [[ ${#WARNED[@]} -gt 0 ]]; then
  say ""
  say "${B}Warnings${N}"
  for w in "${WARNED[@]}"; do say "  ${Y}!${N} $w"; done
fi

if git -C "$PROJ" rev-parse --git-dir >/dev/null 2>&1 \
   && ! git -C "$PROJ" remote get-url origin >/dev/null 2>&1; then
  note "No git remote — 'gh auth login' then './ops/git_setup.sh' pushes privately"
fi

if [[ ${#NOTES[@]} -gt 0 ]]; then
  say ""
  say "${B}Left for you${N} ${DIM}(none of these block demo trading)${N}"
  for n in "${NOTES[@]}"; do say "  · $n"; done
fi

say ""
say "${B}Measured${N}"
[[ -f runtime/reports/backtest.md ]] && say "  backtest   runtime/reports/backtest.md"
case "$GATE1" in
  PASS) say "  Gate 1     ${G}PASSED${N} — the gate permits real money" ;;
  FAIL) say "  Gate 1     ${Y}NOT PASSED${N} — demo only; runtime/reports/cpcv.md names the failing checks" ;;
  *)    say "  Gate 1     not measured in this run" ;;
esac

say ""
if [[ ${#FAILED[@]} -eq 0 ]]; then
  say "${B}Start demo trading${N}"
  say "  .venv/bin/python scripts/run.py --config config/demo.yaml"
  say ""
  say "${B}And in a second terminal${N}"
  say "  .venv/bin/python scripts/dashboard.py        ${DIM}→ http://127.0.0.1:8787${N}"
  say ""
  say "${DIM}24/7 operation is a separate, deliberate step: ./ops/install.sh${N}"
  say "${DIM}Full guide: docs/USAGE.md${N}"
  exit 0
else
  say "Fix the failures above, then re-run. Completed steps are skipped."
  exit 1
fi
