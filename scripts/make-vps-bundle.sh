#!/usr/bin/env bash
# Builds dist/tg-mirror-vps.zip: the Windows VPS bundle for the account mirror (deploy/windows/SETUP.md):
# the Python package, SignalBridge (destination), CopierSource from ../mt5-copytrader (source), scripts.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
copier="${COPIER_REPO:-$repo/../mt5-copytrader}"
out="$repo/dist"; stage="$out/tg-mirror"
rm -rf "$stage" "$out/tg-mirror-vps.zip"; mkdir -p "$stage/tg-signal-trader" "$stage/mql5-destination" "$stage/mql5-source/Experts/CopyTrader" "$stage/mql5-source/Include"
git -C "$repo" archive HEAD tg_signal_trader pyproject.toml README.md | tar -x -C "$stage/tg-signal-trader"
cp -R "$repo/mql5/Include" "$repo/mql5/Experts" "$stage/mql5-destination/"
rm -rf "$stage/mql5-destination/Experts/SignalBridgeTests"
cp -R "$copier/src/MQL5/Include/CopyTrader" "$stage/mql5-source/Include/"
rm -f "$stage/mql5-source/Include/CopyTrader/TestRunner.mqh"
cp "$copier/src/MQL5/Experts/CopyTrader/CopierSource.mq5" "$stage/mql5-source/Experts/CopyTrader/"
cp "$repo"/deploy/windows/* "$stage/"
for f in "$stage"/*.bat; do perl -pi -e 's/\r?\n/\r\n/' "$f"; done     # Windows line endings
(cd "$out" && zip -qr tg-mirror-vps.zip tg-mirror)
echo "$out/tg-mirror-vps.zip"
