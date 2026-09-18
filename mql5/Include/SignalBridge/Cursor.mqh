#ifndef SIGNALBRIDGE_CURSOR_MQH
#define SIGNALBRIDGE_CURSOR_MQH
// Destination read cursor: which daily events file is being tailed and how many bytes are done.
// Stored in the destination terminal's own MQL5\Files as <base>_cursor.txt (two lines).

struct ReadCursor
{
   string file;
   long   offset;
};

string CursorFileName(const string base) { return base + "_cursor.txt"; }

bool SaveCursor(const string base, const ReadCursor &c)
{
   int h = FileOpen(CursorFileName(base), FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE) return false;
   FileWrite(h, c.file);
   FileWrite(h, IntegerToString(c.offset));
   FileClose(h);
   return true;
}

bool LoadCursor(const string base, ReadCursor &c)
{
   c.file = "";
   c.offset = 0;
   if(!FileIsExist(CursorFileName(base))) return false;
   int h = FileOpen(CursorFileName(base), FILE_READ|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE) return false;
   c.file = FileReadString(h);
   string offset_line = FileIsEnding(h) ? "" : FileReadString(h);
   FileClose(h);
   StringTrimRight(offset_line);
   if(c.file == "" || offset_line == "") { c.file = ""; c.offset = 0; return false; }
   c.offset = StringToInteger(offset_line);
   return true;
}
#endif
