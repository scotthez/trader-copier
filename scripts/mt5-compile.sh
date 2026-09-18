#!/usr/bin/env bash
# Usage: scripts/mt5-compile.sh Experts/CopyTraderTests/CopyTraderUnitTests.mq5
# Syncs src/MQL5 into the test terminal and compiles one file with MetaEditor (Wine).
# Prints error/warning/Result lines. Exit 1 on compile errors.
set -euo pipefail
source "$(dirname "$0")/mt5-env.sh"
rel="${1:?path relative to MQL5/, e.g. Experts/CopyTraderTests/CopyTraderUnitTests.mq5}"
"$REPO_ROOT/scripts/mt5-sync.sh" >/dev/null
log="$MT5_TEST/MQL5/${rel%.mq5}.log"
rm -f "$log"
win_rel="MQL5\\$(echo "$rel" | tr '/' '\\')"
( cd "$MT5_TEST" && "$MT5_WINE" MetaEditor64.exe "/compile:$win_rel" /log >/dev/null 2>&1 ) || true
for _ in $(seq 1 90); do
  if [ -f "$log" ] && utf16 "$log" | grep -q '^Result:'; then break; fi
  sleep 1
done
if [ ! -f "$log" ]; then echo "COMPILE: no log produced for $rel" >&2; exit 1; fi
utf16 "$log" | grep -E ' : (error|warning) |^Result:' || true
utf16 "$log" | grep -E '^Result: 0 errors' >/dev/null
