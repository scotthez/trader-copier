//+------------------------------------------------------------------+
//| SignalBridge - generic file-driven executor for tg-signal-trader. |
//| Attach to ONE chart per terminal. Python writes                   |
//| MQL5\Files\<InpBridgeDir>\commands.jsonl; this EA executes each   |
//| command once, appends results.jsonl and rewrites state.json.      |
//+------------------------------------------------------------------+
#property version     "1.00"
#property description "File bridge: executes commands.jsonl, writes results.jsonl and state.json."
#include <SignalBridge/BridgeCore.mqh>
#include <SignalBridge/BridgeState.mqh>

input long   InpExpectedLogin   = 0;             // Refuse to run unless attached to this account login (0 = any)
input ulong  InpMagic           = 903001;        // Magic number for all orders
input int    InpPollMs          = 500;           // Poll interval (ms)
input int    InpDeviationPoints = 20;            // Max slippage (points)
input int    InpMaxRetries      = 3;             // Attempts per command
input string InpSymbols         = "XAUUSD";      // Comma-separated symbols kept in Market Watch and state.json
input string InpBridgeDir       = "signalbridge";// Directory under MQL5\Files

CBridgeCore g_core;
string      g_symbols[];
int         g_state_failures = 0;

int OnInit()
{
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   if(InpExpectedLogin != 0 && login != InpExpectedLogin)
   {
      Print("[SignalBridge] REFUSING to start: attached to account ", login, " but InpExpectedLogin=", InpExpectedLogin);
      return INIT_PARAMETERS_INCORRECT;
   }
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("[SignalBridge] REFUSING to start: account is not in hedging mode (one position per leg is required)");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpPollMs < 100 || InpMaxRetries < 1) { Print("[SignalBridge] REFUSING to start: InpPollMs >= 100 and InpMaxRetries >= 1 required"); return INIT_PARAMETERS_INCORRECT; }
   int n = StringSplit(InpSymbols, ',', g_symbols);
   for(int i = 0; i < n; i++)
   {
      StringTrimLeft(g_symbols[i]); StringTrimRight(g_symbols[i]);
      if(!SymbolSelect(g_symbols[i], true)) Print("[SignalBridge] WARNING: symbol '", g_symbols[i], "' not found on this account");
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("[SignalBridge] WARNING: automated trading is disabled; commands will fail until Algo Trading is enabled");
   g_core.Init(InpBridgeDir, InpMagic, InpDeviationPoints, InpMaxRetries);
   if(!WriteStateJson(InpBridgeDir, g_symbols, InpMagic)) { Print("[SignalBridge] REFUSING to start: cannot write ", InpBridgeDir, "\\state.json"); return INIT_FAILED; }
   EventSetMillisecondTimer(InpPollMs);
   Print("[SignalBridge] started on account ", login, " @ ", AccountInfoString(ACCOUNT_SERVER), " | bridge dir MQL5\\Files\\", InpBridgeDir,
         " | ", n, " symbol(s) | magic ", InpMagic, " | poll ", InpPollMs, "ms", (InpExpectedLogin != 0 ? " | login guard on" : ""));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); Print("[SignalBridge] stopped (reason ", reason, ") after ", g_core.ExecutedCount(), " command(s)"); }
void OnTick() {}

void OnTimer()
{
   g_core.Poll();
   if(!WriteStateJson(InpBridgeDir, g_symbols, InpMagic)) { if(++g_state_failures % 120 == 1) Print("[SignalBridge] WARNING: state.json write failing (", g_state_failures, ")"); }
   else g_state_failures = 0;
}
