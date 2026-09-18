#property version "1.00"
#include <Trade/Trade.mqh>
#include <SignalBridge/TestRunner.mqh>
#include <SignalBridge/Json.mqh>
#include <SignalBridge/LineFile.mqh>
#include <SignalBridge/BridgeState.mqh>
#include <SignalBridge/BridgeExecutor.mqh>

int      g_step = 0;
datetime g_first_tick = 0;
string   g_dir = "sbtest_exec";
string   g_symbols[];
CBridgeExecutor g_exec;
ulong    g_pos = 0;
ulong    g_ord = 0;

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
   g_exec.Init(424242, 50, 3);
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
      case 5: { // open_market
         BridgeCommand c; BridgeResult r;
         AssertTrue(ParseCommand("{\"cmd_id\":\"s1:L1:1\",\"type\":\"open_market\",\"symbol\":\"" + _Symbol + "\",\"side\":\"BUY\",\"volume\":0.10,\"sl\":" + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - 500 * _Point, _Digits) + ",\"tp\":" + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_ASK) + 500 * _Point, _Digits) + ",\"comment\":\"sig:s1:L1\"}", c), "exec: parse open_market");
         g_exec.Execute(c, r);
         AssertTrue(r.ok, "exec: open_market ok");
         AssertEqStr("s1:L1:1", r.cmd_id, "exec: result cmd_id");
         AssertTrue(r.position > 0 && PositionSelectByTicket(r.position), "exec: position ticket returned");
         AssertEqDbl(0.10, PositionGetDouble(POSITION_VOLUME), 1e-9, "exec: volume");
         AssertEqStr("sig:s1:L1", PositionGetString(POSITION_COMMENT), "exec: comment");
         AssertTrue(r.fill_price > 0, "exec: fill price");
         AssertTrue(StringFind(ResultToJson(r), "{\"cmd_id\":\"s1:L1:1\",\"ok\":true,\"retcode\":100") == 0, "exec: result json");
         g_pos = r.position;
         break; }
      case 6: { // modify_sl
         BridgeCommand c; BridgeResult r;
         double new_sl = NormalizeDouble(SymbolInfoDouble(_Symbol, SYMBOL_BID) - 300 * _Point, _Digits);
         ParseCommand("{\"cmd_id\":\"s1:L1:2\",\"type\":\"modify_sl\",\"position\":" + IntegerToString((long)g_pos) + ",\"sl\":" + DoubleToString(new_sl, _Digits) + "}", c);
         g_exec.Execute(c, r);
         AssertTrue(r.ok, "exec: modify_sl ok");
         PositionSelectByTicket(g_pos);
         AssertEqDbl(new_sl, PositionGetDouble(POSITION_SL), 1e-9, "exec: sl applied");
         break; }
      case 7: { // close
         BridgeCommand c; BridgeResult r;
         ParseCommand("{\"cmd_id\":\"s1:L1:3\",\"type\":\"close\",\"position\":" + IntegerToString((long)g_pos) + "}", c);
         g_exec.Execute(c, r);
         AssertTrue(r.ok, "exec: close ok");
         AssertTrue(!PositionSelectByTicket(g_pos), "exec: position gone");
         break; }
      case 8: { // open_pending + cancel
         BridgeCommand c; BridgeResult r;
         double px = NormalizeDouble(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - 300 * _Point, _Digits);
         ParseCommand("{\"cmd_id\":\"s2:L1:1\",\"type\":\"open_pending\",\"symbol\":\"" + _Symbol + "\",\"side\":\"BUY\",\"volume\":0.10,\"price\":" + DoubleToString(px, _Digits) + ",\"sl\":" + DoubleToString(px - 500 * _Point, _Digits) + ",\"tp\":" + DoubleToString(px + 500 * _Point, _Digits) + ",\"comment\":\"sig:s2:L1\",\"expires_at\":\"" + TimeToString(TimeCurrent() + 86400, TIME_DATE|TIME_SECONDS) + "\"}", c);
         g_exec.Execute(c, r);
         AssertTrue(r.ok, "exec: open_pending ok");
         AssertTrue(r.order > 0 && OrderSelect(r.order), "exec: order ticket returned");
         AssertEqLong(ORDER_TYPE_BUY_LIMIT, OrderGetInteger(ORDER_TYPE), "exec: buy limit type");
         AssertTrue(OrderGetInteger(ORDER_TIME_EXPIRATION) > TimeCurrent(), "exec: expiration set");
         g_ord = r.order;
         break; }
      case 9: { BridgeCommand c; BridgeResult r;
         ParseCommand("{\"cmd_id\":\"s2:L1:2\",\"type\":\"cancel\",\"order\":" + IntegerToString((long)g_ord) + "}", c);
         g_exec.Execute(c, r);
         AssertTrue(r.ok, "exec: cancel ok");
         AssertTrue(!OrderSelect(g_ord), "exec: order gone");
         break; }
      case 10: { // failures are results, not crashes
         BridgeCommand c; BridgeResult r;
         ParseCommand("{\"cmd_id\":\"s3:L1:1\",\"type\":\"open_market\",\"symbol\":\"NOPE_XYZ\",\"side\":\"BUY\",\"volume\":0.10,\"sl\":0,\"tp\":0,\"comment\":\"x\"}", c);
         g_exec.Execute(c, r);
         AssertTrue(!r.ok, "exec: unknown symbol fails");
         ParseCommand("{\"cmd_id\":\"s3:L1:2\",\"type\":\"close\",\"position\":999999999}", c);
         g_exec.Execute(c, r);
         AssertTrue(!r.ok, "exec: close unknown position fails");
         AssertTrue(!ParseCommand("{\"cmd_id\":\"x\",\"type\":\"teleport\"}", c), "exec: unknown type rejected");
         AssertTrue(!ParseCommand("{\"type\":\"ping\"}", c), "exec: missing cmd_id rejected");
         ParseCommand("{\"cmd_id\":\"p1\",\"type\":\"ping\"}", c);
         g_exec.Execute(c, r);
         AssertTrue(r.ok && r.attempts == 0, "exec: ping ok without trading");
         AssertEqLong(0, PositionsTotal(), "exec: flat at end");
         break; }
      default:
         TestSummary(); ExpertRemove(); return;
   }
   g_step++;
}
