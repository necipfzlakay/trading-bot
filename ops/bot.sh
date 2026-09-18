#!/usr/bin/env bash
#
# 24/7 lifecycle. Generates the launchd agents for the mode you ask for, checks
# that this mode can actually start before installing anything, loads it, and
# then verifies the engine really came up rather than assuming it did.
#
#   ./ops/bot.sh start paper     simulated fills on live data, no keys needed
#   ./ops/bot.sh start demo      real order flow, virtual money
#   ./ops/bot.sh start live      REAL MONEY
#   ./ops/bot.sh stop
#   ./ops/bot.sh status
#   ./ops/bot.sh logs -f
#
# The preflight matters more than it looks. launchd runs these with
# KeepAlive=true, so a configuration that cannot start does not fail once — it
# fails every 30 seconds forever, burning API rate limit. Everything that can be
# checked is checked before the agent is installed.
#
set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ" || exit 1

PY="$PROJ/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
BOT=com.tbot.bot
WEB=com.tbot.web
HEALTH=com.tbot.health

if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; DIM=""; R=""; G=""; Y=""; N=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

usage() {
  cat <<'USAGE'
Usage: ./ops/bot.sh <command> [args]

  start [paper|demo|live]   preflight, install the launchd agents, verify start
  stop                      unload every agent (this is the "manual shutdown")
  restart                   stop, then start the mode that is currently installed
  status                    what is installed, what is running, what it is doing
  logs [-f] [N]             recent engine events (-f follows)
  mode                      which mode is installed right now

Modes:
  paper   live prices, simulated fills. No API key. Safe default.
  demo    real Bybit order flow against virtual money. Needs BYBIT_DEMO_* keys.
  live    REAL MONEY. Refuses to install unless Gate 1 passed and you type the
          confirmation phrase. Cannot be forced with a flag.
USAGE
}

# ------------------------------------------------------------------ inspection

installed_mode() {
  # The mode is whatever config the installed agent actually points at, read
  # back from the plist rather than remembered in a file that could drift.
  [[ -f "$AGENTS/$BOT.plist" ]] || return 1
  local cfg
  cfg=$(grep -oE 'config/[a-z0-9_]+\.yaml' "$AGENTS/$BOT.plist" | head -1) || return 1
  [[ -n "$cfg" ]] || return 1
  basename "$cfg" .yaml
}

agent_pid() {
  launchctl list "$1" 2>/dev/null | awk -F' = ' '/"PID"/ {gsub(/[^0-9]/,"",$2); print $2}'
}

agent_exit_status() {
  # launchctl reports the raw wait status, so exit code 1 appears as 256.
  launchctl list "$1" 2>/dev/null \
    | awk -F' = ' '/"LastExitStatus"/ {gsub(/[^0-9-]/,"",$2); print ($2 >= 256 ? int($2 / 256) : $2)}'
}

is_loaded() { launchctl list "$1" >/dev/null 2>&1; }

# Python loads its code once, at startup. launchd restarts the engine when it
# crashes, never when a file changes — so after an edit the running process
# keeps executing the old code, silently, for as long as it stays up. Nothing
# else in the system would ever mention it.
code_is_stale() {
  local pid newest started
  pid=$(pgrep -f "scripts/run.py" | head -1)
  [[ -n "$pid" ]] || return 1

  newest=$(find src scripts config -type f \( -name '*.py' -o -name '*.yaml' \) -print0 2>/dev/null \
    | xargs -0 stat -f '%m' 2>/dev/null | sort -rn | head -1)
  started=$(ps -o lstart= -p "$pid" 2>/dev/null)
  [[ -n "$newest" && -n "$started" ]] || return 1

  local started_epoch
  started_epoch=$(date -j -f "%a %b %d %T %Y" "$started" +%s 2>/dev/null) || return 1
  [[ "$newest" -gt "$started_epoch" ]]
}

# ------------------------------------------------------------------- preflight

preflight() {
  local mode="$1"
  head_ "Preflight — $mode"

  if [[ ! -x "$PY" ]]; then
    bad ".venv missing — run ./ops/pre-install.sh first"
    return 1
  fi
  ok "interpreter $("$PY" -V 2>&1 | cut -d' ' -f2)"

  if [[ ! -f "config/$mode.yaml" ]]; then
    bad "config/$mode.yaml does not exist"
    return 1
  fi

  # An engine already holding the lock means starting another would be refused
  # at best and duplicate every position at worst. Catch it here, not in a
  # restart loop.
  if [[ -f runtime/engine.lock ]]; then
    local holder
    holder=$(cat runtime/engine.lock 2>/dev/null)
    if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
      bad "an engine is already running (pid $holder) — ./ops/bot.sh stop first"
      return 1
    fi
  fi
  ok "no other engine holds the lock"

  # Everything the engine does before it trades, in the same order it does it.
  TBOT_CONFIRM_LIVE="${TBOT_CONFIRM_LIVE:-}" "$PY" - "$mode" <<'PYEOF'
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from _bootstrap import indicator_selection, setup, universe
from tbot.core.config import assert_runnable, load_secrets
from tbot.core.models import Mode
from tbot.engine.runner import build_broker, venue_label
from tbot.execution.ccxt_broker import CCXTBroker
from tbot.execution.safety import assert_key_is_safe

GOOD, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"

mode = sys.argv[1]
fail = 0

cfg = setup(f"config/{mode}.yaml")
secrets = load_secrets()

try:
    assert_runnable(cfg, secrets)
    print(f"  {GOOD} {cfg.mode.value} on {cfg.venue.value} [{venue_label(cfg.venue)}]")
except Exception as exc:
    print(f"  {BAD} {exc}")
    raise SystemExit(1)

symbols = universe(cfg)
if symbols:
    print(f"  {GOOD} universe: {len(symbols)} symbols")
else:
    print(f"  {BAD} universe is empty — run scripts/backfill.py --refresh-universe")
    fail = 1

blocks, _sel, _decay = indicator_selection(cfg)
if blocks:
    print(f"  {GOOD} indicator selection: {len(set(blocks.values()))} voting blocks")
else:
    print(f"  {WARN} no indicator selection — every indicator votes separately")

# Building the broker is the real connectivity test: for demo and live it
# authenticates, loads markets and reads fees. A withdrawal-capable key is
# rejected here rather than 30 seconds into a launchd restart loop.
try:
    broker = build_broker(cfg, secrets, symbols[:3] or ["BTC/USDT"])
    print(f"  {GOOD} broker ready")
    if isinstance(broker, CCXTBroker):
        acknowledged = cfg.security.ip_whitelist_acknowledged
        report = assert_key_is_safe(broker.exchange, ip_whitelist_acknowledged=acknowledged)
        print(f"  {GOOD} API key permissions: {report.summary()}")
        if not report.ip_restricted:
            if acknowledged:
                print(f"  {GOOD} no IP whitelist (acknowledged) — key cannot withdraw")
            else:
                print(f"  {WARN} key is not IP-restricted — add a whitelist on Bybit")
except Exception as exc:
    print(f"  {BAD} {exc}")
    fail = 1

if cfg.mode is Mode.LIVE:
    try:
        # fetch_balance returns {currency: free_amount}, already flattened.
        free = broker.fetch_balance().get(cfg.quote_currency, 0.0)
        if free and free < cfg.risk.min_equity:
            print(f"  {WARN} balance {free:.2f} {cfg.quote_currency} is below "
                  f"min_equity {cfg.risk.min_equity:.0f} — no new entries will open")
        else:
            print(f"  {GOOD} balance {free:.2f} {cfg.quote_currency}")
    except Exception as exc:
        print(f"  {WARN} could not read balance: {exc}")

raise SystemExit(fail)
PYEOF
  return $?
}

# Live money gets a second gate on top of the preflight: the measured verdict,
# and a phrase the user has to type. Deliberately not bypassable by a flag —
# a flag is something a script can pass by accident.
confirm_live() {
  head_ "Real money"

  local verdict="unknown"
  if [[ -f runtime/gate1.json ]]; then
    verdict=$("$PY" -c 'import json;print("PASS" if json.load(open("runtime/gate1.json"))["passed"] else "FAIL")' 2>/dev/null || echo unknown)
  fi

  if [[ "$verdict" != "PASS" ]]; then
    bad "Gate 1 verdict is $verdict — the strategy has not been shown to beat chance"
    say ""
    say "  runtime/reports/cpcv.md lists which checks failed."
    say "  Live money is refused while Gate 1 has not passed."
    say ""
    say "  ${DIM}To measure again after changing the strategy:${N}"
    say "  ${DIM}  .venv/bin/python scripts/cpcv_report.py --trials N${N}"
    return 1
  fi
  ok "Gate 1 passed"

  if [[ ! -t 0 ]]; then
    bad "live mode needs an interactive terminal for confirmation"
    return 1
  fi

  cat <<WARNING

  ${Y}This installs an agent that trades REAL MONEY, restarts itself after every
  crash, and starts again at every login. TBOT_CONFIRM_LIVE=yes will be written
  into the plist, which makes that barrier permanent for as long as it is
  installed.${N}

  Before typing: Gate 2 (paper vs backtest divergence under 10%) and Gate 3
  (four weeks on demo without manual intervention) are separate checks this
  script cannot verify for you.

  Stop it at any time with: ./ops/bot.sh stop

WARNING

  local phrase="GERCEK PARA"
  read -r -p "  Type exactly '$phrase' to continue: " typed
  if [[ "$typed" != "$phrase" ]]; then
    say ""
    bad "not confirmed — nothing was installed"
    return 1
  fi
  ok "confirmed"
  return 0
}

# ------------------------------------------------------------- plist generation

# Generated, never hand-edited: the paths have to match this checkout, and a
# stale plist pointing at an old directory is a silent way to run the wrong code.
write_plists() {
  local mode="$1"
  mkdir -p "$AGENTS" "$PROJ/runtime/logs"

  local live_env=""
  if [[ "$mode" == "live" ]]; then
    live_env="    <key>TBOT_CONFIRM_LIVE</key>
    <string>yes</string>"
  fi

  cat > "$AGENTS/$BOT.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!--
  GENERATED by ops/bot.sh — do not edit. Re-run ./ops/bot.sh start <mode>.
  mode: $mode
-->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$BOT</string>

  <key>ProgramArguments</key>
  <array>
    <!-- caffeinate -i: crypto trades around the clock, and a sleeping Mac is a
         bot that is not running. -->
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$PY</string>
    <string>$PROJ/scripts/run.py</string>
    <string>--config</string>
    <string>config/$mode.yaml</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$PROJ</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>

  <key>StandardOutPath</key>
  <string>$PROJ/runtime/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PROJ/runtime/logs/launchd.err.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
$live_env
  </dict>

  <key>ProcessType</key>
  <string>Background</string>
  <key>LowPriorityIO</key>
  <false/>
</dict>
</plist>
PLIST

  cat > "$AGENTS/$WEB.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!-- GENERATED by ops/bot.sh. Dashboard on localhost only, never exposed. -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$WEB</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$PROJ/scripts/dashboard.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJ</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$PROJ/runtime/logs/web.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PROJ/runtime/logs/web.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST

  cat > "$AGENTS/$HEALTH.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!--
  GENERATED by ops/bot.sh. Dead-man switch, every 15 minutes.
  A crashed engine is loud — launchd restarts it. A wedged engine is silent:
  the process lives, nothing restarts it, and positions stop being managed
  while everything looks fine. This is what notices that.
-->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$HEALTH</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$PROJ/scripts/healthcheck.py</string>
    <string>--backup</string>
    <string>--quiet</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJ</string>
  <key>StartInterval</key>
  <integer>900</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$PROJ/runtime/logs/health.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PROJ/runtime/logs/health.err.log</string>
</dict>
</plist>
PLIST
}

# --------------------------------------------------------------- start / verify

verify_started() {
  local mode="$1"
  head_ "Verifying"

  # The engine writes state.json as it comes up. Waiting for that is the only
  # check that distinguishes "launchd spawned something" from "the bot is
  # running the mode I asked for".
  local waited=0
  while [[ $waited -lt 90 ]]; do
    if "$PY" - "$mode" <<'PYEOF' 2>/dev/null
import json, pathlib, sys, time

mode = sys.argv[1]
want = {"paper": ("paper", "bybit-demo"),
        "demo": ("live", "bybit-demo"),
        "live": ("live", "bybit-live")}[mode]

p = pathlib.Path("runtime/state.json")
if not p.exists():
    raise SystemExit(1)
s = json.loads(p.read_text(encoding="utf-8"))
fresh = (time.time() * 1000 - int(s.get("updated_ts", 0))) < 120_000
if fresh and s.get("status") == "running" and (s.get("mode"), s.get("venue")) == want:
    raise SystemExit(0)
raise SystemExit(1)
PYEOF
    then
      ok "engine is up and reporting mode=$mode"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
    [[ -t 1 ]] && printf '\r  %swaiting for the engine to report… %ds%s' "$DIM" "$waited" "$N"
  done
  [[ -t 1 ]] && printf '\r%*s\r' 50 ''

  # It did not come up. Leaving a KeepAlive agent to retry forever is worse
  # than being stopped, so unload it and show why.
  bad "the engine did not report running within 90s"
  local status
  status=$(agent_exit_status "$BOT")
  [[ -n "$status" && "$status" != "0" ]] && say "  last exit status: $status"

  if [[ -s runtime/logs/launchd.err.log ]]; then
    say ""
    say "  ${DIM}runtime/logs/launchd.err.log (tail):${N}"
    tail -15 runtime/logs/launchd.err.log | sed 's/^/    /'
  fi

  say ""
  say "  Unloading so it does not restart-loop."
  for label in "$BOT" "$WEB" "$HEALTH"; do
    launchctl unload -w "$AGENTS/$label.plist" 2>/dev/null
  done
  say "  Fix the cause, then: ./ops/bot.sh start $mode"
  return 1
}

cmd_start() {
  local mode="${1:-paper}"
  case "$mode" in
    paper|demo|live) ;;
    *) bad "unknown mode: $mode (paper, demo or live)"; exit 2 ;;
  esac

  if [[ "$mode" == "live" ]]; then
    confirm_live || exit 1
    # The plist carries TBOT_CONFIRM_LIVE; the preflight runs outside it, so it
    # needs the same variable or assert_runnable would refuse for the wrong reason.
    export TBOT_CONFIRM_LIVE=yes
  fi

  preflight "$mode" || { say ""; bad "preflight failed — nothing was installed"; exit 1; }

  head_ "Installing launchd agents"
  # Unload first: reloading a changed plist without this keeps the old command.
  for label in "$BOT" "$WEB" "$HEALTH"; do
    launchctl unload -w "$AGENTS/$label.plist" 2>/dev/null
  done
  write_plists "$mode"
  for label in "$BOT" "$WEB" "$HEALTH"; do
    if launchctl load -w "$AGENTS/$label.plist" 2>/dev/null; then
      ok "$label loaded"
    else
      bad "$label failed to load"
    fi
  done

  verify_started "$mode" || exit 1

  head_ "Running"
  say "  mode      $mode"
  say "  panel     http://127.0.0.1:8787"
  say "  logs      ./ops/bot.sh logs -f"
  say "  status    ./ops/bot.sh status"
  say "  stop      ./ops/bot.sh stop"
  say ""
  say "  ${DIM}Restarts after any crash and at every login. The health agent checks"
  say "  every 15 minutes and only speaks up when something is wrong.${N}"
}

cmd_stop() {
  head_ "Stopping"
  local any=0
  for label in "$BOT" "$WEB" "$HEALTH"; do
    if [[ -f "$AGENTS/$label.plist" ]]; then
      launchctl unload -w "$AGENTS/$label.plist" 2>/dev/null
      ok "$label unloaded"
      any=1
    fi
  done
  [[ $any -eq 0 ]] && warn "no agents were installed"

  # A manually started engine is not launchd's to stop; say so rather than
  # reporting success while something is still trading.
  if pgrep -f "scripts/run.py" >/dev/null 2>&1; then
    warn "a manually started engine is still running: $(pgrep -f 'scripts/run.py' | tr '\n' ' ')"
  fi

  say ""
  say "  ${DIM}Open positions keep their exchange-side stops — those live on Bybit,"
  say "  not in this process. To close them instead, use /flatten before stopping.${N}"
}

cmd_status() {
  local mode
  mode=$(installed_mode) || mode=""

  head_ "Agents"
  if [[ -z "$mode" ]]; then
    warn "nothing installed — ./ops/bot.sh start paper"
  else
    say "  mode      ${B}$mode${N}"
    for label in "$BOT" "$WEB" "$HEALTH"; do
      if is_loaded "$label"; then
        local pid; pid=$(agent_pid "$label")
        if [[ -n "$pid" ]]; then
          ok "$label running (pid $pid)"
        else
          local st; st=$(agent_exit_status "$label")
          if [[ "$label" == "$HEALTH" ]]; then
            ok "$label scheduled (runs every 15 min)"
          else
            warn "$label loaded but not running (last exit ${st:-?})"
          fi
        fi
      else
        warn "$label not loaded"
      fi
    done
  fi

  head_ "Engine"
  "$PY" - <<'PYEOF'
import json, pathlib, time

p = pathlib.Path("runtime/state.json")
if not p.exists():
    print("  no state.json — the engine has never run")
    raise SystemExit(0)

s = json.loads(p.read_text(encoding="utf-8"))
age = (time.time() * 1000 - int(s.get("updated_ts", 0))) / 60_000
stale = age > 90  # generous: the engine writes on bar close

G, Y = "\033[32m✓\033[0m", "\033[33m!\033[0m"
mark = Y if stale else G
print(f"  {mark} status     {s.get('status')} · updated {age:.0f} min ago")
print(f"    mode       {s.get('mode')} on {s.get('venue')}")
print(f"    uptime     {s.get('uptime_human')} · {s.get('bars_processed')} bars · {s.get('restarts')} restarts")
print(f"    equity     {s.get('equity', 0):.2f}  drawdown {s.get('drawdown', 0):.1%}  "
      f"risk x{s.get('risk_multiplier', 1):.2f}")

positions = s.get("open_positions") or []
if positions:
    print(f"    positions  {len(positions)} open")
    for pos in positions[:5]:
        sym = pos.get("symbol", "?")
        print(f"      {sym:<12} {pos.get('side','')} qty={pos.get('quantity', 0):g} "
              f"R={pos.get('r_multiple', 0):+.2f}")
else:
    print("    positions  none")

if s.get("degraded_reason"):
    print(f"  {Y} degraded   {s['degraded_reason']}")
if s.get("last_error"):
    print(f"  {Y} last error {s['last_error']}")
if stale:
    print(f"  {Y} state is stale — if an agent shows as running, it may be wedged")
PYEOF

  head_ "Health"
  if "$PY" scripts/healthcheck.py >/dev/null 2>&1; then
    ok "healthy"
  else
    warn "healthcheck reports a problem — .venv/bin/python scripts/healthcheck.py"
  fi

  if code_is_stale; then
    warn "source files are newer than the running engine — it is executing old code"
    say "    ${DIM}./ops/bot.sh restart${N}"
  fi
}

cmd_logs() {
  local follow=0 lines=40
  for arg in "$@"; do
    case "$arg" in
      -f|--follow) follow=1 ;;
      [0-9]*) lines="$arg" ;;
    esac
  done

  local log="runtime/logs/bot.jsonl"
  if [[ ! -f "$log" ]]; then
    warn "no engine log yet ($log)"
    # launchd's own stderr catches failures that happen before logging starts.
    [[ -s runtime/logs/launchd.err.log ]] && tail -20 runtime/logs/launchd.err.log
    return 0
  fi

  if [[ $follow -eq 1 ]]; then
    tail -n "$lines" -f "$log"
  else
    tail -n "$lines" "$log"
  fi
}

# ------------------------------------------------------------------ dispatch

CMD="${1:-}"
[[ $# -gt 0 ]] && shift

case "$CMD" in
  start)   cmd_start "$@" ;;
  stop)    cmd_stop ;;
  restart)
    mode=$(installed_mode) || { bad "nothing installed — ./ops/bot.sh start paper"; exit 1; }
    cmd_stop
    cmd_start "$mode"
    ;;
  status)  cmd_status ;;
  logs)    cmd_logs "$@" ;;
  mode)    installed_mode || { echo "not installed"; exit 1; } ;;
  ""|-h|--help|help) usage ;;
  *) bad "unknown command: $CMD"; say ""; usage; exit 2 ;;
esac
