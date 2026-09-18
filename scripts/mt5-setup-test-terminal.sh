#!/usr/bin/env bash
# One-time: clone the installed MT5 into a separate portable "MetaTrader 5 Test" install
# used only for headless compiles and Strategy Tester runs. Re-run with --force to recreate.
set -euo pipefail
source "$(dirname "$0")/mt5-env.sh"

if [ -d "$MT5_TEST" ] && [ "${1:-}" != "--force" ]; then
  echo "already exists: $MT5_TEST (use --force to recreate)"
  exit 0
fi
pkill -f 'MetaTrader 5 Test' 2>/dev/null || true
rm -rf "$MT5_TEST"
mkdir -p "$MT5_TEST/config" "$MT5_TEST/MQL5" "$MT5_TEST/Bases"
cp "$MT5_MAIN"/*.exe "$MT5_TEST/"
cp "$MT5_MAIN"/config/accounts.dat "$MT5_MAIN"/config/servers.dat "$MT5_MAIN"/config/common.ini "$MT5_TEST/config/"
[ -d "$MT5_MAIN/config/certificates" ] && cp -R "$MT5_MAIN/config/certificates" "$MT5_TEST/config/"
# Cached price history so the tester does not need a long download.
for srv in "$MT5_MAIN"/Bases/*/; do
  n="$(basename "$srv")"
  [ -d "$srv/history" ] || continue
  mkdir -p "$MT5_TEST/Bases/$n"
  cp -R "$srv/history" "$MT5_TEST/Bases/$n/"
  [ -d "$srv/symbols" ] && cp -R "$srv/symbols" "$MT5_TEST/Bases/$n/"
done
cp -R "$MT5_MAIN/MQL5/Include" "$MT5_TEST/MQL5/Include"
touch "$MT5_TEST/portable.txt"
echo "created $MT5_TEST"
