#property version "1.00"
#include <SignalBridge/TestRunner.mqh>
#include <SignalBridge/Json.mqh>
#include <SignalBridge/Cursor.mqh>
#include <SignalBridge/LineFile.mqh>

void Test_Json()
{
   CJsonWriter w;
   w.AddString("type", "ping");
   w.AddLong("ticket", 42);
   w.AddDouble("sl", 4341.5);
   AssertEqStr("{\"type\":\"ping\",\"ticket\":42,\"sl\":4341.5}", w.End(), "json: writer");
   string s; long l; double d;
   AssertTrue(JsonGetString("{\"cmd_id\":\"a:L1:1\",\"type\":\"close\"}", "cmd_id", s) && s == "a:L1:1", "json: get string");
   AssertTrue(JsonGetLong("{\"position\":123456}", "position", l) && l == 123456, "json: get long");
   AssertTrue(JsonGetDouble("{\"volume\":0.12}", "volume", d) && d == 0.12, "json: get double");
   AssertTrue(!JsonGetString("{\"a\":1}", "b", s), "json: missing key");
}

void Test_LineFile()
{
   string file = "sbtest_lines.jsonl";
   FileDelete(file);
   AssertEqLong(0, LocalFileSize(file), "lines: missing size 0");
   AssertTrue(AppendLine(file, "{\"a\":1}"), "lines: append 1");
   AssertTrue(AppendLine(file, "{\"b\":2}"), "lines: append 2");
   AssertEqLong(16, LocalFileSize(file), "lines: size");
   CLineTail t; t.Init(file, 0);
   string lines[]; long ends[];
   AssertEqLong(2, t.ReadNewLines(lines, ends), "lines: read 2");
   AssertEqStr("{\"a\":1}", lines[0], "lines: first");
   AssertEqLong(8, ends[0], "lines: end offset 1");
   AssertEqLong(16, t.Offset(), "lines: offset advanced");
   string more[]; long mends[];
   AssertEqLong(0, t.ReadNewLines(more, mends), "lines: nothing new");
   // partial line is not consumed
   int h = FileOpen(file, FILE_READ|FILE_WRITE|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE);
   FileSeek(h, 0, SEEK_END);
   uchar p[]; StringToCharArray("{\"c\":3}", p, 0, 7, CP_UTF8); FileWriteArray(h, p, 0, 7); FileClose(h);
   AssertEqLong(0, t.ReadNewLines(more, mends), "lines: partial not returned");
   AssertTrue(AppendLine(file, ""), "lines: complete partial");
   AssertEqLong(1, t.ReadNewLines(more, mends), "lines: completed returned");
   AssertEqStr("{\"c\":3}", more[0], "lines: completed content");
   CLineTail resumed; resumed.Init(file, 8);
   string rest[]; long rends[];
   AssertEqLong(2, resumed.ReadNewLines(rest, rends), "lines: resume from offset");
   CLineTail missing; missing.Init("sbtest_nope.jsonl", 0);
   AssertEqLong(0, missing.ReadNewLines(rest, rends), "lines: missing file 0");
   FileDelete(file);
}

void Test_Cursor()
{
   FileDelete(CursorFileName("sbtest"));
   ReadCursor c; c.file = "commands.jsonl"; c.offset = 999;
   AssertTrue(SaveCursor("sbtest", c), "cursor: save");
   ReadCursor back;
   AssertTrue(LoadCursor("sbtest", back) && back.offset == 999 && back.file == "commands.jsonl", "cursor: round-trip");
   FileDelete(CursorFileName("sbtest"));
}

int OnInit()
{
   Test_Json();
   Test_LineFile();
   Test_Cursor();
   TestSummary();
   return INIT_FAILED;
}
void OnTick() {}
