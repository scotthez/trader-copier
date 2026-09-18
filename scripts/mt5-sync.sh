#!/usr/bin/env bash
# Copies src/MQL5/** into the test terminal (default) or into the real terminal (--main).
set -euo pipefail
source "$(dirname "$0")/mt5-env.sh"
target="$MT5_TEST"
[ "${1:-}" = "--main" ] && target="$MT5_MAIN"
for sub in Include/SignalBridge Experts/SignalBridge Experts/SignalBridgeTests; do
  [ -d "$REPO_ROOT/mql5/$sub" ] || continue
  mkdir -p "$target/MQL5/$sub"
  rsync -a --delete --exclude '*.ex5' --exclude '*.log' "$REPO_ROOT/mql5/$sub/" "$target/MQL5/$sub/"
done
echo "synced src/MQL5 -> $target/MQL5"
