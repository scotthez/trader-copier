#property version "1.00"
#include <SignalBridge/TestRunner.mqh>
#include <SignalBridge/Json.mqh>
#include <SignalBridge/LineFile.mqh>
#include <SignalBridge/BridgeCore.mqh>

int      g_step = 0;
datetime g_first_tick = 0;
string   g_dir = "sbtest_core";
CBridgeCore g_core;

int CountResults(string &lines[])
{
   ArrayResize(lines, 0);
   CLineTail t; t.Init(g_dir + "/results.jsonl", 0);
   long ends[]; return t.ReadNewLines(lines, ends);
}

int OnInit()
{
   FileDelete(g_dir + "/commands.jsonl"); FileDelete(g_dir + "/results.jsonl");
   FileDelete(g_dir + "/bridge_cursor.txt"); FileDelete(g_dir + "/state.json");
   AssertTrue(g_core.Init(g_dir, 424242, 50, 3), "core: init");
   return INIT_SUCCEEDED;
}

void OnTick()
{
   if(g_first_tick == 0) g_first_tick = TimeCurrent();
   if(TimeCurrent() < g_first_tick + 300) return;
   string lines[]; string s;
   switch(g_step)
   {
      case 0:
         AssertEqLong(0, g_core.Poll(), "core: nothing to do on empty file");
         AppendLine(g_dir + "/commands.jsonl", "{\"cmd_id\":\"p1\",\"type\":\"ping\"}");
         AppendLine(g_dir + "/commands.jsonl", "{\"cmd_id\":\"o1\",\"type\":\"open_market\",\"symbol\":\"" + _Symbol + "\",\"side\":\"BUY\",\"volume\":0.10,\"sl\":0,\"tp\":0,\"comment\":\"sig:c:L1\"}");
         AppendLine(g_dir + "/commands.jsonl", "not json at all");
         AssertEqLong(3, g_core.Poll(), "core: three lines processed (one invalid)");
         AssertEqLong(3, CountResults(lines), "core: one result per line");
         AssertTrue(StringFind(lines[0], "\"cmd_id\":\"p1\",\"ok\":true") == 1, "core: ping result");
         AssertTrue(StringFind(lines[1], "\"cmd_id\":\"o1\",\"ok\":true") == 1, "core: open result");
         AssertTrue(StringFind(lines[2], "\"ok\":false") > 0 && StringFind(lines[2], "unparseable") > 0, "core: invalid line journaled as failed result");
         AssertEqLong(1, PositionsTotal(), "core: position opened");
         break;
      case 1: {
         // Duplicate cmd_id must not execute again; a fresh core (restart) must skip it too.
         AppendLine(g_dir + "/commands.jsonl", "{\"cmd_id\":\"o1\",\"type\":\"open_market\",\"symbol\":\"" + _Symbol + "\",\"side\":\"BUY\",\"volume\":0.10,\"sl\":0,\"tp\":0,\"comment\":\"sig:c:L1\"}");
         AssertEqLong(0, g_core.Poll(), "core: duplicate cmd_id skipped");
         AssertEqLong(1, PositionsTotal(), "core: still one position");
         CBridgeCore fresh;
         FileDelete(g_dir + "/bridge_cursor.txt");            // simulate lost cursor: everything is re-read
         AssertTrue(fresh.Init(g_dir, 424242, 50, 3), "core: fresh init");
         AssertEqLong(0, fresh.Poll(), "core: fresh core re-reads but skips all known cmd_ids");
         AssertEqLong(1, PositionsTotal(), "core: no duplicate position after restart");
         AssertEqLong(3, CountResults(lines), "core: no extra result lines");
         break; }
      case 2: {
         ulong pos = PositionGetTicket(0);
         AppendLine(g_dir + "/commands.jsonl", "{\"cmd_id\":\"c1\",\"type\":\"close\",\"position\":" + IntegerToString((long)pos) + "}");
         AssertEqLong(1, g_core.Poll(), "core: close processed");
         AssertEqLong(0, PositionsTotal(), "core: flat");
         ReadCursor cur; AssertTrue(LoadCursor(g_dir + "/bridge", cur) && cur.offset == LocalFileSize(g_dir + "/commands.jsonl"), "core: cursor at EOF");
         break; }
      default:
         TestSummary(); ExpertRemove(); return;
   }
   g_step++;
}
