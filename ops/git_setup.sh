#!/usr/bin/env bash
# Initialise the repo and push it to a private GitHub repo.
#
# Run this after accepting the Xcode licence and logging into gh:
#   sudo xcodebuild -license accept
#   gh auth login
#   ./ops/git_setup.sh
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_NAME="${1:-trading-bot}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  git init -b main
fi

git config user.name  "$(git config --global user.name  || echo necipfazilakay)"
git config user.email "$(git config --global user.email || echo akaynecip@gmail.com)"

# runtime/ holds the database, logs, parquet archive and state; .env holds keys.
# Both are excluded by .gitignore — verify before the first push.
# One path per call: `check-ignore -q` refuses several at once and exits 128,
# which would read as "not ignored" and block every push.
for path in .env runtime; do
  if ! git check-ignore -q "$path"; then
    echo "STOP: $path is not ignored. Fix .gitignore before pushing." >&2
    exit 1
  fi
done
echo "ok: .env and runtime/ are ignored"

git add -A
git commit -m "feat: autonomous crypto trading bot with measured indicator selection

Bybit spot bot that scans a liquid universe continuously and acts only on
high-conviction setups. Built so that being wrong is visible rather than
profitable-looking.

Structural guarantees, each with a test that fails if it breaks:
- signals computed only on closed bars, enforced at the storage layer
- multi-timeframe reads mapped on close times, not containment
- orders idempotent by deterministic client id, so a crash cannot duplicate
- stops fill where they trigger rather than at the next open

Indicator selection is measured, not assumed: Information Coefficient and
tail analysis decide which indicators may vote, and a correlation pass
collapses duplicates into voting blocks.

Validation gates entry to live trading: CPCV, Deflated Sharpe and PBO, with
thresholds fixed in advance.

Risk is continuous rather than binary — drawdown shrinks position size and
raises the conviction bar, and never locks the bot out.

LLM roles are advisory and can only reduce risk; the boundary is enforced in
code, not requested in a prompt." || echo "nothing to commit"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh repo view "$REPO_NAME" >/dev/null 2>&1; then
    echo "repo $REPO_NAME already exists; pushing"
    git remote get-url origin >/dev/null 2>&1 || \
      git remote add origin "$(gh repo view "$REPO_NAME" --json sshUrl -q .sshUrl)"
    git push -u origin main
  else
    gh repo create "$REPO_NAME" --private --source=. --remote=origin --push
  fi
  echo
  echo "pushed: $(gh repo view "$REPO_NAME" --json url -q .url)"
else
  echo
  echo "gh is not authenticated. Run 'gh auth login', then re-run this script."
  echo "The commit is already made locally, so nothing is lost."
fi
