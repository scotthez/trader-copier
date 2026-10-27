//+------------------------------------------------------------------+
//| CopierSource - attach to ONE chart on the SOURCE account.         |
//| Writes every position open/close/partial-close/SL-TP change as a |
//| JSON line to Common\Files\<base>_events_YYYYMMDD.jsonl.           |
//+------------------------------------------------------------------+
#property version   "1.00"
#property description "Copy-trader SOURCE: journals this account's position events to the shared events file."
#include <CopyTrader/SourceHandler.mqh>

input string InpEventsFileBase = "copytrader";   // Events file base name (must match CopierDestination)
input long   InpExpectedLogin  = 0;              // Refuse to run unless attached to this account login (0 = any)
input int    InpReconcileMs    = 2000;           // Reconcile sweep interval: live positions vs tracker (ms, 0 = off)

CSourceHandler g_handler;
bool           g_seed_pending = false;   // first start while disconnected: seed (silently) once connected

int OnInit()
{
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   if(InpExpectedLogin != 0 && login != InpExpectedLogin)
   {
      Print("[CopierSource] REFUSING to start: attached to account ", login, " but InpExpectedLogin=", InpExpectedLogin);
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpReconcileMs != 0 && InpReconcileMs < 100)
   {
      Print("[CopierSource] REFUSING to start: InpReconcileMs must be 0 (off) or >= 100");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      Print("[CopierSource] WARNING: account is not in hedging mode; netting reversals (INOUT deals) are not mirrored");

   g_handler.Init(InpEventsFileBase);
   bool have_tracker = g_handler.LoadTracker();
   int  loaded = g_handler.TrackedCount();
   bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   if(!connected)
   {
      // The position list is empty until the account has synchronised; sweeping it now would report
      // every tracked position as closed. The timer takes over once connected.
      g_seed_pending = !have_tracker;
      Print("[CopierSource] terminal not connected yet - initial ", have_tracker ? "sweep" : "seeding",
            " skipped; the reconcile timer will do it once connected");
   }
   else if(have_tracker)
      g_handler.Reconcile();            // emits whatever changed while the EA was down
   else
      g_handler.SeedFromOpenPositions(); // no tracker yet: adopt the open positions silently
   if(InpReconcileMs > 0) EventSetMillisecondTimer(InpReconcileMs);

   string expected_login_note = (InpExpectedLogin != 0) ? (" | expected login " + IntegerToString(InpExpectedLogin)) : "";
   string reconcile_note = (InpReconcileMs > 0) ? (" | reconcile every " + IntegerToString(InpReconcileMs) + "ms") : " | reconcile OFF";
   Print("[CopierSource] started on account ", login, " @ ", AccountInfoString(ACCOUNT_SERVER),
         " | events -> Common\\Files\\", g_handler.CurrentFile(),
         " | tracking ", g_handler.TrackedCount(), " open position(s) (", loaded, " loaded from ",
         g_handler.TrackerFileName(), ")", reconcile_note, expected_login_note);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[CopierSource] stopped (reason ", reason, ")");
}

void OnTick() {}

// Event handlers are serialized by the terminal: OnTimer never runs concurrently with OnTradeTransaction.
void OnTimer()
{
   if(g_seed_pending)
   {
      if(!TerminalInfoInteger(TERMINAL_CONNECTED)) return;
      g_seed_pending = false;
      g_handler.SeedFromOpenPositions();
      Print("[CopierSource] connected: now tracking ", g_handler.TrackedCount(), " open position(s)");
      return;
   }
   g_handler.Reconcile();
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   g_handler.OnTransaction(trans);
}
