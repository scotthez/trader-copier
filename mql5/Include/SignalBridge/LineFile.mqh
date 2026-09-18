#ifndef SIGNALBRIDGE_LINEFILE_MQH
#define SIGNALBRIDGE_LINEFILE_MQH
// Terminal-local (MQL5\Files) line files: byte-exact append and resumable tail.
// Same mechanics as mt5-copytrader's EventLog.mqh but without FILE_COMMON.

#define LINEFILE_READ  (FILE_READ|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE)
#define LINEFILE_WRITE (FILE_READ|FILE_WRITE|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE)

long LocalFileSize(const string file)
{
   if(!FileIsExist(file)) return 0;
   int h = FileOpen(file, LINEFILE_READ);
   if(h == INVALID_HANDLE) return 0;
   long size = (long)FileSize(h);
   FileClose(h);
   return size;
}

// Appends line + "\n". Retries a transient open failure 5 times (50 ms apart).
bool AppendLine(const string file, const string line)
{
   int h = INVALID_HANDLE;
   for(int attempt = 1; attempt <= 5; attempt++)
   {
      h = FileOpen(file, LINEFILE_WRITE);
      if(h != INVALID_HANDLE) break;
      if(attempt < 5) Sleep(50);
   }
   if(h == INVALID_HANDLE) { Print("AppendLine: cannot open ", file, " error=", GetLastError()); return false; }
   FileSeek(h, 0, SEEK_END);
   uchar bytes[];
   StringToCharArray(line + "\n", bytes, 0, -1, CP_UTF8);
   int n = ArraySize(bytes);
   if(n > 0 && bytes[n - 1] == 0) n--;
   uint written = FileWriteArray(h, bytes, 0, n);
   FileFlush(h);
   FileClose(h);
   return ((int)written == n);
}

class CLineTail
{
private:
   string m_file;
   long   m_offset;
public:
   CLineTail() { m_file = ""; m_offset = 0; }
   void   Init(const string file, const long offset) { m_file = file; m_offset = offset; }
   string File() { return m_file; }
   long   Offset() { return m_offset; }

   // Complete lines after the offset → lines[]; end_offsets[i] = byte just past line i's "\n".
   // Returns -1 if the file exists but cannot be opened, 0 if missing or nothing new.
   int ReadNewLines(string &lines[], long &end_offsets[])
   {
      if(m_file == "" || !FileIsExist(m_file)) return 0;
      int h = FileOpen(m_file, LINEFILE_READ);
      if(h == INVALID_HANDLE) return -1;
      long size = (long)FileSize(h);
      if(size <= m_offset) { FileClose(h); return 0; }
      int n = (int)(size - m_offset);
      uchar buf[]; ArrayResize(buf, n);
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
         start = i + 1; consumed = start;
         if(line == "") continue;
         int k = ArraySize(lines);
         ArrayResize(lines, k + 1); ArrayResize(end_offsets, k + 1);
         lines[k] = line; end_offsets[k] = m_offset + consumed;
         added++;
      }
      m_offset += consumed;
      return added;
   }
};
#endif
