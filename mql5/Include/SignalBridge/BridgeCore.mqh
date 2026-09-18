#ifndef SIGNALBRIDGE_BRIDGECORE_MQH
#define SIGNALBRIDGE_BRIDGECORE_MQH
#include <SignalBridge/LineFile.mqh>
#include <SignalBridge/Cursor.mqh>
#include <SignalBridge/BridgeExecutor.mqh>
// The bridge loop without timers: tail commands.jsonl, execute each cmd_id once, append results.jsonl.

class CBridgeCore
{
private:
   string          m_dir;
   string          m_commands, m_results, m_cursor_base;
   CLineTail       m_tail;
   CBridgeExecutor m_exec;
   string          m_seen[];      // cmd_ids with a result line (loaded from results.jsonl + this session)
   int             m_executed;

   bool Seen(const string id) { for(int i = 0; i < ArraySize(m_seen); i++) if(m_seen[i] == id) return true; return false; }
   void Remember(const string id) { int n = ArraySize(m_seen); ArrayResize(m_seen, n + 1); m_seen[n] = id; }

   void LoadSeen()
   {
      ArrayResize(m_seen, 0);
      CLineTail t; t.Init(m_results, 0);
      string lines[]; long ends[];
      int n = t.ReadNewLines(lines, ends);
      for(int i = 0; i < n; i++) { string id; if(JsonGetString(lines[i], "cmd_id", id)) Remember(id); }
   }

   void WriteResult(const BridgeResult &r)
   {
      if(!AppendLine(m_results, ResultToJson(r))) Print("[SignalBridge] FAILED to append result for ", r.cmd_id);
      Remember(r.cmd_id);
   }

public:
   bool Init(const string dir, const ulong magic, const int deviation_points, const int max_retries)
   {
      m_dir = dir; m_commands = dir + "/commands.jsonl"; m_results = dir + "/results.jsonl"; m_cursor_base = dir + "/bridge";
      m_executed = 0;
      m_exec.Init(magic, deviation_points, max_retries);
      LoadSeen();
      ReadCursor c;
      if(!LoadCursor(m_cursor_base, c) || c.file != m_commands) { c.file = m_commands; c.offset = 0; }
      m_tail.Init(c.file, c.offset);
      Print("[SignalBridge] core: ", ArraySize(m_seen), " known cmd_ids, tailing ", m_commands, "@", c.offset);
      return true;
   }

   int ExecutedCount() { return m_executed; }

   int Poll()
   {
      string lines[]; long ends[];
      int n = m_tail.ReadNewLines(lines, ends);
      if(n < 0) { Print("[SignalBridge] cannot open ", m_commands, " error=", GetLastError()); return -1; }
      int done = 0;
      for(int i = 0; i < n; i++)
      {
         BridgeCommand c; BridgeResult r;
         if(!ParseCommand(lines[i], c))
         {
            // Deterministic id so a re-read (lost cursor) does not journal the same bad line twice.
            r.cmd_id = "invalid:" + IntegerToString(ends[i]); r.ok = false; r.retcode = 0;
            r.retcode_text = "unparseable command line"; r.position = 0; r.order = 0; r.fill_price = 0; r.attempts = 0;
            if(!Seen(r.cmd_id))
            {
               Print("[SignalBridge] unparseable command skipped: ", lines[i]);
               WriteResult(r); done++;
            }
         }
         else if(Seen(c.cmd_id))
         {
            Print("[SignalBridge] duplicate cmd_id ", c.cmd_id, " skipped");
         }
         else
         {
            m_exec.Execute(c, r);
            Print("[SignalBridge] ", c.type, " ", c.cmd_id, (r.ok ? " OK" : " FAILED"), " retcode=", r.retcode, " ", r.retcode_text,
                  (r.position > 0 ? " position=#" + IntegerToString((long)r.position) : ""), (r.order > 0 ? " order=#" + IntegerToString((long)r.order) : ""));
            WriteResult(r); done++; m_executed++;
         }
         ReadCursor cur; cur.file = m_commands; cur.offset = ends[i];
         SaveCursor(m_cursor_base, cur);   // after the result line, so a crash in between re-reads → duplicate → skipped
         if(IsStopped()) break;
      }
      return done;
   }
};
#endif
