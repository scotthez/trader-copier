#!/usr/bin/env bash
# Shared environment for the macOS MT5 (Wine) dev harness. Source this; do not run it.
# Any installed MetaTrader 5 app bundle carries the Wine build; override with MT5_APP=/Applications/<name>.app.
if [ -z "${MT5_APP:-}" ]; then
  for MT5_APP in "/Applications/MetaTrader 5 FTMO.app" "/Applications/MetaTrader 5.app" /Applications/MetaTrader\ 5*.app; do
    [ -x "$MT5_APP/Contents/SharedSupport/wine/bin/wine" ] && break
  done
fi
MT5_WINE="$MT5_APP/Contents/SharedSupport/wine/bin/wine"
export DYLD_FALLBACK_LIBRARY_PATH="$MT5_APP/Contents/SharedSupport/wine/lib/external"
MT5_PREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
MT5_MAIN="$MT5_PREFIX/drive_c/Program Files/MetaTrader 5"
MT5_TEST="$MT5_PREFIX/drive_c/Program Files/MetaTrader 5 Test"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WINEPREFIX="$MT5_PREFIX" WINEDEBUG=-all

# MT5 writes UTF-16LE logs; print one as UTF-8 without CRs.
utf16() { iconv -f UTF-16LE -t UTF-8 "$1" 2>/dev/null | tr -d '\r'; }
