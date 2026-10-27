#ifndef COPYTRADER_EVENTLOG_MQH
#define COPYTRADER_EVENTLOG_MQH
// The shared events file: <base>_events_YYYYMMDD.jsonl in the terminal Common\Files folder.
// Appends and tails are byte-oriented (FILE_BIN) so offsets can be persisted and resumed exactly.

#define EVENTLOG_FLAGS_READ  (FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE)
#define EVENTLOG_FLAGS_WRITE (FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE)

string EventsFileName(const string base, const datetime day)
{
   MqlDateTime dt;
   TimeToStruct(day, dt);
   return StringFormat("%s_events_%04d%02d%02d.jsonl", base, dt.year, dt.mon, dt.day);
}

// Inverse of EventsFileName: the day (midnight) encoded in "<base>_events_YYYYMMDD.jsonl", or 0 if the
// name does not have that shape.
datetime EventsFileDay(const string base, const string file)
{
   string prefix = base + "_events_";
   string suffix = ".jsonl";
   int plen = StringLen(prefix), slen = StringLen(suffix);
   if(StringLen(file) != plen + 8 + slen) return 0;
   if(StringSubstr(file, 0, plen) != prefix || StringSubstr(file, plen + 8) != suffix) return 0;
   string ymd = StringSubstr(file, plen, 8);
   for(int i = 0; i < 8; i++) { ushort c = StringGetCharacter(ymd, i); if(c < '0' || c > '9') return 0; }
   return StringToTime(StringSubstr(ymd, 0, 4) + "." + StringSubstr(ymd, 4, 2) + "." + StringSubstr(ymd, 6, 2) + " 00:00:00");
}

// Midnight of the given time.
datetime DayStart(const datetime t) { return t - (t % 86400); }

// The next existing daily file strictly after `current_file` up to and including the file for `now`'s day,
// or "" if there is none yet. Walks forward one day at a time so no day is skipped.
string NextEventsFile(const string base, const string current_file, const datetime now)
{
   datetime cur = EventsFileDay(base, current_file);
   datetime today = DayStart(now);
   if(cur == 0) return FileIsExist(EventsFileName(base, now), FILE_COMMON) ? EventsFileName(base, now) : "";
   for(datetime d = cur + 86400; d <= today; d += 86400)
   {
      string f = EventsFileName(base, d);
      if(FileIsExist(f, FILE_COMMON)) return f;
   }
   return "";
}

string FailedEventsFileName(const string base) { return base + "_failed_events.jsonl"; }

long CommonFileSize(const string file)
{
   if(!FileIsExist(file, FILE_COMMON)) return 0;
   int h = FileOpen(file, EVENTLOG_FLAGS_READ);
   if(h == INVALID_HANDLE) return 0;
   long size = (long)FileSize(h);
   FileClose(h);
   return size;
}

// Appends line + "\n". FILE_READ|FILE_WRITE opens without truncating; we seek to the end.
// A transient open failure (the other terminal or an editor holding the file) is retried a few
// times with a short pause (Sleep is legal in EAs and emulated in the tester); only the final
// failure is logged.
#define APPENDLINE_ATTEMPTS 5
bool AppendLineCommon(const string file, const string line)
{
   int h = INVALID_HANDLE;
   for(int attempt = 1; attempt <= APPENDLINE_ATTEMPTS; attempt++)
   {
      h = FileOpen(file, EVENTLOG_FLAGS_WRITE);
      if(h != INVALID_HANDLE) break;
      if(attempt == APPENDLINE_ATTEMPTS)
      {
         Print("AppendLineCommon: cannot open ", file, " after ", APPENDLINE_ATTEMPTS, " attempts, error=", GetLastError());
         return false;
      }
      Sleep(50);
   }
   FileSeek(h, 0, SEEK_END);
   uchar bytes[];
   StringToCharArray(line + "\n", bytes, 0, -1, CP_UTF8);
   int n = ArraySize(bytes);
   if(n > 0 && bytes[n - 1] == 0) n--;   // drop the terminating zero StringToCharArray adds
   uint written = FileWriteArray(h, bytes, 0, n);
   FileFlush(h);
   FileClose(h);
   return ((int)written == n);
}

class CEventTail
{
private:
   string m_file;
   long   m_offset;
public:
   CEventTail() { m_file = ""; m_offset = 0; }
   void   Init(const string file, const long offset) { m_file = file; m_offset = offset; }
   string File() { return m_file; }
   long   Offset() { return m_offset; }

   // Reads every complete new line after the current offset. For each line appended to lines[],
   // end_offsets[] receives the byte offset just past its "\n". A trailing partial line is left
   // for the next call. Returns the number of lines added (0 when nothing is new or the file does
   // not exist yet), or -1 when the file exists but cannot be opened (locked, permissions) so the
   // caller can tell "nothing new" from "cannot read"; GetLastError() then holds the reason.
   int ReadNewLines(string &lines[], long &end_offsets[])
   {
      if(m_file == "" || !FileIsExist(m_file, FILE_COMMON)) return 0;
      int h = FileOpen(m_file, EVENTLOG_FLAGS_READ);
      if(h == INVALID_HANDLE) return -1;
      long size = (long)FileSize(h);
      if(size <= m_offset) { FileClose(h); return 0; }
      int n = (int)(size - m_offset);
      uchar buf[];
      ArrayResize(buf, n);
      FileSeek(h, m_offset, SEEK_SET);
      int got = (int)FileReadArray(h, buf, 0, n);
      FileClose(h);

      int added = 0, start = 0, consumed = 0;
      for(int i = 0; i < got; i++)
      {
         if(buf[i] != '\n') continue;
         int len = i - start;
         if(len > 0 && buf[i - 1] == '\r') len--;
         string line = (len > 0) ? CharArrayToString(buf, start, len, CP_UTF8) : "";
         start = i + 1;
         consumed = start;
         if(line == "") continue;
         int k = ArraySize(lines);
         ArrayResize(lines, k + 1);
         ArrayResize(end_offsets, k + 1);
         lines[k] = line;
         end_offsets[k] = m_offset + consumed;
         added++;
      }
      m_offset += consumed;
      return added;
   }
};
#endif
