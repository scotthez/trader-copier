#!/usr/bin/env bash
# Usage: scripts/mt5-test.sh CopyTraderUnitTests
# Compiles Experts/CopyTraderTests/<Name>.mq5, runs it headlessly in the test terminal's
# Strategy Tester, prints TEST PASS/FAIL lines. Exit 0 only on "TESTS COMPLETE: N passed, 0 failed".
# The terminal is tracked by PID (the wine launcher execs in place, so $! is the terminal
# for the whole run). Do not match it by name: its command line is
# "terminal64.exe /portable /config:config\tester.ini" and does not contain the install path;
# the only process that does is the short-lived metaeditor64.exe child the terminal spawns
# at startup to compile the bundled Examples, which exits long before the test finishes.
set -euo pipefail
source "$(dirname "$0")/mt5-env.sh"
name="${1:?test expert name, e.g. CopyTraderUnitTests}"

"$REPO_ROOT/scripts/mt5-compile.sh" "Experts/SignalBridgeTests/$name.mq5"

pkill -f 'MetaTrader 5 Test' 2>/dev/null || true
pkill -f 'terminal64.exe /portable /config:config' 2>/dev/null || true
sleep 1
rm -rf "$MT5_TEST/Tester/logs" "$MT5_TEST/logs"
cat > "$MT5_TEST/config/tester.ini" <<INI
[Tester]
Expert=SignalBridgeTests\\$name
Symbol=EURUSD
Period=H1
Model=0
FromDate=2026.09.01
ToDate=2026.09.02
Deposit=10000
Leverage=100
Report=$name
ShutdownTerminal=1
INI

cd "$MT5_TEST"
nohup "$MT5_WINE" terminal64.exe /portable '/config:config\tester.ini' >/dev/null 2>&1 &
tester_pid=$!
cd "$REPO_ROOT"
for _ in $(seq 1 180); do
  kill -0 "$tester_pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$tester_pid" 2>/dev/null; then
  echo "TEST: terminal still running after 180s, killing" >&2
  kill "$tester_pid" || true
fi

log="$(ls -t "$MT5_TEST"/Tester/logs/*.log 2>/dev/null | head -1 || true)"
if [ -z "$log" ]; then
  echo "TEST: no tester log found; terminal log follows:" >&2
  for f in "$MT5_TEST"/logs/*.log; do utf16 "$f" | tail -20 >&2; done
  exit 1
fi
utf16 "$log" | grep -E 'TEST (PASS|FAIL)|TESTS COMPLETE|OnInit|cannot|invalid|error' | sed -E 's/^[A-Z]+\t[0-9]+\t//' || true
utf16 "$log" | grep -E 'TESTS COMPLETE: [0-9]+ passed, 0 failed' >/dev/null
