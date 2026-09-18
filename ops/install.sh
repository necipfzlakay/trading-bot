#!/usr/bin/env bash
# Install the launchd agents so the bot survives crashes and reboots.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$PROJ/runtime/logs"

# The plists carry absolute paths, generated for this checkout. If the project
# moves, regenerate them rather than editing by hand.
for label in com.tbot.bot com.tbot.web com.tbot.health; do
  if ! grep -q "$PROJ" "$PROJ/ops/$label.plist"; then
    echo "STOP: $label.plist points at a different directory than $PROJ" >&2
    echo "      Regenerate it before loading, or launchd will run the wrong checkout." >&2
    exit 1
  fi
  cp "$PROJ/ops/$label.plist" "$AGENTS/$label.plist"
  launchctl unload -w "$AGENTS/$label.plist" 2>/dev/null || true
  launchctl load -w "$AGENTS/$label.plist"
  echo "loaded $label"
done

echo
echo "Running. Check with:"
echo "  launchctl list | grep tbot"
echo "  tail -f $PROJ/runtime/logs/bot.jsonl"
echo "  open http://127.0.0.1:8787"
echo
echo "Stop with:"
echo "  launchctl unload -w $AGENTS/com.tbot.bot.plist"
