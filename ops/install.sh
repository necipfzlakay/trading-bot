#!/usr/bin/env bash
# Kept as the name the docs have always used. The lifecycle lives in bot.sh,
# which generates the launchd agents for whichever mode you ask for — having two
# sources of truth for the same plists is how they drift apart.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bot.sh" start "${1:-demo}"
