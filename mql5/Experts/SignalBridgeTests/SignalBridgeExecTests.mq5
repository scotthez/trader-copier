#property version "1.00"
#include <Trade/Trade.mqh>
#include <SignalBridge/TestRunner.mqh>
#include <SignalBridge/Json.mqh>
#include <SignalBridge/LineFile.mqh>
#include <SignalBridge/BridgeState.mqh>

int      g_step = 0;
datetime g_first_tick = 0;
string   g_dir = "sbtest_exec";
string   g_symbols[];

// Reads <dir>/state.json as one string (it is a single line).
string ReadState()
{
   int h = FileOpen(g_dir + "/state.json", FILE_READ|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return "";
   int n = (int)FileSize(h);
   uchar b[]; ArrayResize(b, n); FileReadArray(h, b, 0, n); FileClose(h);
   return CharArrayToString(b, 0, n, CP_UTF8);
}

int OnInit()
{
   ArrayResize(g_symbols, 1); g_symbols[0] = _Symbol;
   FileDelete(g_dir + "/state.json"); FileDelete(g_dir + "/state.tmp");
   return INIT_SUCCEEDED;
}

void OnTick()
{
   if(g_first_tick == 0) g_first_tick = TimeCurrent();
   if(TimeCurrent() < g_first_tick + 300) return;   // tester market closed at day start
   CTrade t; t.SetExpertMagicNumber(777);
   string s;
   switch(g_step)
   {
      case 0:
         AssertTrue(WriteStateJson(g_dir, g_symbols, 777), "state: write ok");
         AssertTrue(!FileIsExist(g_dir + "/state.tmp"), "state: tmp renamed away");
         s = ReadState();
         AssertTrue(StringFind(s, "\"ts\":\"") == 1, "state: starts with ts");
         AssertTrue(StringFind(s, "\"login\":") > 0 && StringFind(s, "\"hedging\":true") > 0, "state: account block");
         AssertTrue(StringFind(s, "\"" + _Symbol + "\":{\"bid\":") > 0, "state: symbol block");
         AssertTrue(StringFind(s, "\"trade_allowed\":true") > 0, "state: trade_allowed");
         AssertTrue(StringFind(s, "\"positions\":[]") > 0, "state: no positions yet");
         AssertTrue(StringFind(s, "\"orders\":[]") > 0, "state: no orders yet");
         break;
      case 1:
         t.SetTypeFillingBySymbol(_Symbol);
         AssertTrue(t.PositionOpen(_Symbol, ORDER_TYPE_BUY, 0.10, SymbolInfoDouble(_Symbol, SYMBOL_ASK), 0, 0, "sig:t:L1"), "state: open test position");
         break;
      case 2:
         AssertTrue(WriteStateJson(g_dir, g_symbols, 777), "state: write with position");
         s = ReadState();
         AssertTrue(StringFind(s, "\"type\":\"BUY\",\"volume\":0.1,") > 0, "state: position serialised");
         AssertTrue(StringFind(s, "\"comment\":\"sig:t:L1\",\"magic\":777") > 0, "state: comment+magic");
         AssertTrue(StringFind(s, "\"entry\":\"IN\"") > 0, "state: IN deal in deals_recent");
         break;
      case 3:
         AssertTrue(t.PositionClose(PositionGetTicket(0)), "state: close test position");
         break;
      case 4:
         AssertTrue(WriteStateJson(g_dir, g_symbols, 777), "state: write after close");
         s = ReadState();
         AssertTrue(StringFind(s, "\"positions\":[]") > 0, "state: positions empty again");
         AssertTrue(StringFind(s, "\"entry\":\"OUT\",\"reason\":\"CLIENT\"") > 0 || StringFind(s, "\"entry\":\"OUT\",\"reason\":\"EXPERT\"") > 0, "state: OUT deal with reason");   // tester reports EA closes as EXPERT
         break;
      default:
         TestSummary(); ExpertRemove(); return;
   }
   g_step++;
}
