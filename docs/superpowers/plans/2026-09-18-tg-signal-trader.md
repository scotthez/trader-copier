# Telegram Signal Trader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute Lewis Inner Circle and WolvesVIP Telegram signals on two live MT5 accounts (one per provider) with risk-based sizing and a fixed TP ladder, via a Linux Python service talking to Wine-hosted MT5 terminals through a file bridge.

**Architecture:** Part A is `SignalBridge.mq5`, a thin generic executor EA: it tails `commands.jsonl` in its terminal's `MQL5/Files/signalbridge/`, executes each command once via `CTrade`, appends `results.jsonl`, and rewrites `state.json` every 500 ms. Part B is the Python package `tg_signal_trader`: a Telethon listener that fills a SQLite inbox, and a trader loop that parses provider templates, validates arithmetically, classifies free-form management text with Claude, drives one `SignalRun` state machine per signal (4 legs, ladder), sizes legs from the bridge's `state.json`, and sends commands. All Python logic is testable against an in-memory `FakeBridge`.

**Tech Stack:** MQL5 (MetaTrader 5 build 5830+, `<Trade/Trade.mqh>`), Python 3.11+, `telethon`, `anthropic` (Python SDK, `client.messages.parse` structured outputs), `pydantic` v2, `pyyaml`, `pytest`, SQLite (stdlib). Dev harness on macOS via the MT5-bundled Wine (scripts copied from `mt5-copytrader`).

## Global Constraints

- Pure MQL5 in the bridge: no DLL imports, no network. Nothing hardcoded to a broker or account: account selection only via `InpExpectedLogin` (0 = any) and where the EA is attached; provider→terminal binding only via `bridge_dir` in `config.yaml`. The strings "PUPrime", "IconTech" may appear only in docs/config examples.
- Bridge directory: `<terminal>/MQL5/Files/signalbridge/` (terminal-local `MQL5\Files`, **not** `FILE_COMMON`). Files: `commands.jsonl` (Python appends), `results.jsonl` (EA appends exactly one line per command), `state.json` (EA rewrites every `InpPollMs`=500 ms via `state.tmp` + `FileMove`). Timestamps everywhere are the terminal machine's local time in `YYYY.MM.DD HH:MM:SS` (Python and the terminal share a clock on the VPS).
- Command types: `ping`, `open_market`, `open_pending`, `modify_sl`, `close`, `cancel`. A `cmd_id` is executed at most once (idempotent across EA restarts). Every order carries `InpMagic` and the command's `comment` (`sig:<signal_id>:L<n>`).
- Execution primitives (copied from `mt5-copytrader`'s `Replayer.mqh`, tested there): transient-only retries (`REQUOTE, REJECT, ERROR, TIMEOUT, PRICE_CHANGED, PRICE_OFF, TOO_MANY_REQUESTS, LOCKED, CONNECTION, DONE_PARTIAL`, retcode 0), stop normalisation to `SYMBOL_DIGITS`, `SetTypeFillingBySymbol`, partial-fill check after close, refuse non-hedging account and wrong login.
- Signal model: `id="<provider>:<telegram_msg_id>"`, `symbol` canonical (`Gold`→`XAUUSD`), `side`, `entry_type MARKET|LIMIT`, `entry_zone [near, far]`, `sl`, `tps` exactly 4 with `tp4 = tp3 + (tp3 − tp2)` when missing/OPEN.
- Validation (all must pass): SL strictly on the loss side; TPs strictly on the profit side and monotonic; SL distance within per-symbol `sl_range`; MARKET price within `market_entry_tolerance_pct` (0.3) of current and age ≤ `max_signal_age_sec` (120); LIMIT near price within `limit_max_distance_pct` (1.0) of current.
- Sizing per leg: `risk_pct_per_leg × balance ÷ (|entry − sl| ÷ tick_size × tick_value)`, floor to `volume_step`, clamp `[volume_min, volume_max]`. Default `risk_pct_per_leg` 1.0 for both providers.
- Ladder: L1 TP → nothing; L2 TP → L3, L4 SL to their own entry; L3 TP → L4 SL to TP1; any leg SL-hit while pendings exist → cancel pendings; SL moves one-way only. Pending TTL 24 h.
- Management actions executable in v1: exactly `close_all, cancel_pending, move_sl, break_even`. Reference = replied-to run, else the single ACTIVE run for the provider; two ACTIVE and not a reply → journal only.
- Classifier: `client.messages.parse` with a Pydantic schema; `confidence ≥ 0.8`; timeout 8 s; any error → `none`. Default model `claude-opus-5` (config `llm.model`; `claude-haiku-4-5` is the cheaper alternative the user may switch to).
- Guards: `max_open_signals` 2, `max_legs_open` 8, `daily_loss_stop_pct` 5.0, terminal stale > 5 s or `trade_allowed=false` → no placement.
- Tests: MQL5 via `scripts/mt5-test.sh <Name>` ending `TESTS COMPLETE: N passed, 0 failed`; Python via `pytest -q` all green. TDD per task. Never commit `*.ex5`, `*.log`, `.env`, `*.session`. Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

```
tg-signal-trader/
├── .gitignore  pyproject.toml  README.md  config.example.yaml  .env.example
├── scripts/                      # macOS Wine harness (copied from mt5-copytrader, REPO src = mql5/)
│   mt5-env.sh  mt5-setup-test-terminal.sh  mt5-sync.sh  mt5-compile.sh  mt5-test.sh
├── mql5/
│   ├── Include/SignalBridge/
│   │   ├── TestRunner.mqh        # copied verbatim from mt5-copytrader
│   │   ├── Json.mqh              # copied verbatim (flat reader/writer)
│   │   ├── JsonOut.mqh           # NEW: nested writer for state.json
│   │   ├── LineFile.mqh          # append + byte-offset tail for terminal-local files
│   │   ├── Cursor.mqh            # copied verbatim
│   │   ├── BridgeState.mqh       # WriteStateJson(): account/symbols/positions/orders/deals → state.json (atomic)
│   │   ├── BridgeExecutor.mqh    # CBridgeExecutor: executes one Command → Result (CTrade + retry gate)
│   │   └── BridgeCore.mqh        # CBridgeCore: tail commands, idempotency, results, cursor (no timer)
│   └── Experts/
│       ├── SignalBridge/SignalBridge.mq5
│       └── SignalBridgeTests/{SignalBridgeUnitTests,SignalBridgeExecTests,SignalBridgeCoreTests}.mq5
├── tg_signal_trader/
│   ├── __init__.py  config.py  models.py  normalize.py
│   ├── parsers/{__init__,lewis,wolves}.py
│   ├── validation.py  sizing.py  bridge.py  store.py  ladder.py  classifier.py  trader.py
│   ├── export.py                 # Telegram HTML export → messages (fixtures + --replay)
│   ├── listener.py  cli.py
├── deploy/{tg-listener.service,tg-trader.service}
├── fixtures/{lewis_messages.jsonl,wolves_messages.jsonl}
└── tests/  (one file per module)
```

## Dev harness facts (from mt5-copytrader, verified)

- Compile: `cd <install> && wine MetaEditor64.exe /compile:MQL5\... /log` (relative path only). Tester: `terminal64.exe /portable /config:config\tester.ini`, wait on the PID. Logs are UTF-16LE.
- Tester market is closed for the first ~70 s of the start date → test EAs wait 5 simulated minutes; one scenario step per `OnTick`; tester never emits `TRADE_TRANSACTION_POSITION`; `TERMINAL_CONNECTED` is always true.
- Terminal-local `MQL5\Files` in the tester is the agent sandbox — fine for tests.
- `~/Documents/clubshub/mt5-copytrader` is the source for copied files; use the exact paths given in each task.

---

# Part A — MQL5 bridge

### Task 1: Repo scaffold, harness, and copied includes with a green unit suite

**Files:**
- Create: `.gitignore` (already exists from the spec commit — extend), `scripts/*.sh` (5 files), `mql5/Include/SignalBridge/{TestRunner,Json,Cursor}.mqh`, `mql5/Include/SignalBridge/LineFile.mqh`, `mql5/Experts/SignalBridgeTests/SignalBridgeUnitTests.mq5`
- Source to copy from: `~/Documents/clubshub/mt5-copytrader/`

**Interfaces:**
- Produces (LineFile.mqh): `bool AppendLine(const string file, const string line)` (terminal-local, `FILE_BIN`, 5 retries), `long LocalFileSize(const string file)`, `class CLineTail { void Init(const string file, const long offset); string File(); long Offset(); int ReadNewLines(string &lines[], long &end_offsets[]); }` (returns −1 when the file exists but cannot be opened).
- Produces: `scripts/mt5-test.sh <Name>` running `mql5/Experts/SignalBridgeTests/<Name>.mq5`; `scripts/mt5-compile.sh <path relative to mql5/>`.

- [ ] **Step 1: Copy the harness and adapt paths**

```bash
cd ~/Documents/clubshub/tg-signal-trader
cp ~/Documents/clubshub/mt5-copytrader/scripts/*.sh scripts/ 2>/dev/null || { mkdir -p scripts && cp ~/Documents/clubshub/mt5-copytrader/scripts/*.sh scripts/; }
```
Then edit `scripts/mt5-sync.sh` so the loop reads `for sub in Include/SignalBridge Experts/SignalBridge Experts/SignalBridgeTests; do` and the source is `"$REPO_ROOT/mql5/$sub/"`. Edit `scripts/mt5-test.sh`: `Expert=SignalBridgeTests\\$name` and the compile call `"Experts/SignalBridgeTests/$name.mq5"`. Leave `mt5-env.sh`, `mt5-setup-test-terminal.sh`, `mt5-compile.sh` unchanged (they already resolve `REPO_ROOT` from the script location). Run `chmod +x scripts/*.sh`.

- [ ] **Step 2: Copy includes verbatim**

```bash
mkdir -p mql5/Include/SignalBridge mql5/Experts/SignalBridge mql5/Experts/SignalBridgeTests
S=~/Documents/clubshub/mt5-copytrader/src/MQL5/Include/CopyTrader
cp $S/TestRunner.mqh $S/Json.mqh $S/Cursor.mqh mql5/Include/SignalBridge/
```
In each copied file change the include guard prefix `COPYTRADER_` → `SIGNALBRIDGE_` and any `#include <CopyTrader/...>` → `#include <SignalBridge/...>` (Json.mqh has none; Cursor.mqh has none).

- [ ] **Step 3: Write the failing unit test EA**

`mql5/Experts/SignalBridgeTests/SignalBridgeUnitTests.mq5`:
```mql5
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
```

- [ ] **Step 4: Run to verify it fails**

Run: `scripts/mt5-setup-test-terminal.sh && scripts/mt5-test.sh SignalBridgeUnitTests`
Expected: compile error `file 'Include\SignalBridge\LineFile.mqh' not found`, exit 1.

- [ ] **Step 5: Create `mql5/Include/SignalBridge/LineFile.mqh`**

```mql5
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
```

- [ ] **Step 6: Run to verify it passes**

Run: `scripts/mt5-test.sh SignalBridgeUnitTests`
Expected: `TESTS COMPLETE: 22 passed, 0 failed`.

- [ ] **Step 7: Commit**

```bash
git add .gitignore scripts mql5
git commit -m "chore: MQL5 harness and shared includes for the signal bridge"
```

---

### Task 2: JsonOut.mqh — nested JSON writer for state.json

**Files:**
- Create: `mql5/Include/SignalBridge/JsonOut.mqh`
- Modify: `mql5/Experts/SignalBridgeTests/SignalBridgeUnitTests.mq5`

**Interfaces:**
- Produces: `class CJsonOut { void BeginObject(); void EndObject(); void BeginArray(); void EndArray(); void Key(const string key); void Str(const string v); void Num(const double v); void Int(const long v); void Bool(const bool v); string Text(); }` — call `Key()` then a value inside objects; call values directly inside arrays. Numbers serialise via `DoubleToString(v, 8)` with trailing zeros trimmed (`JsonTrimZeros` from Json.mqh).

- [ ] **Step 1: Add the failing test**

Add `#include <SignalBridge/JsonOut.mqh>` and, called from `OnInit()` after `Test_Cursor();`:
```mql5
void Test_JsonOut()
{
   CJsonOut j;
   j.BeginObject();
   j.Key("ts"); j.Str("2026.09.18 13:20:07");
   j.Key("account"); j.BeginObject(); j.Key("login"); j.Int(35186896); j.Key("hedging"); j.Bool(true); j.EndObject();
   j.Key("positions"); j.BeginArray();
      j.BeginObject(); j.Key("ticket"); j.Int(1); j.Key("volume"); j.Num(0.12); j.Key("comment"); j.Str("sig:a\"b"); j.EndObject();
      j.BeginObject(); j.Key("ticket"); j.Int(2); j.Key("volume"); j.Num(1.0); j.EndObject();
   j.EndArray();
   j.Key("orders"); j.BeginArray(); j.EndArray();
   j.EndObject();
   AssertEqStr("{\"ts\":\"2026.09.18 13:20:07\",\"account\":{\"login\":35186896,\"hedging\":true},\"positions\":[{\"ticket\":1,\"volume\":0.12,\"comment\":\"sig:a\\\"b\"},{\"ticket\":2,\"volume\":1}],\"orders\":[]}", j.Text(), "jsonout: nested document");
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/mt5-test.sh SignalBridgeUnitTests` — Expected: compile error, missing include.

- [ ] **Step 3: Create `mql5/Include/SignalBridge/JsonOut.mqh`**

```mql5
#ifndef SIGNALBRIDGE_JSONOUT_MQH
#define SIGNALBRIDGE_JSONOUT_MQH
#include <SignalBridge/Json.mqh>
// Streaming writer for nested JSON (objects/arrays). Tracks whether a comma is needed per level.

class CJsonOut
{
private:
   string m_buf;
   bool   m_need_comma[];   // per open container
   int    m_depth;
   void   Sep()
   {
      if(m_depth == 0) return;
      if(m_need_comma[m_depth - 1]) m_buf += ",";
      m_need_comma[m_depth - 1] = true;
   }
   void   Push() { m_depth++; ArrayResize(m_need_comma, m_depth); m_need_comma[m_depth - 1] = false; }
   void   Pop()  { if(m_depth > 0) { m_depth--; ArrayResize(m_need_comma, m_depth); } }
public:
   CJsonOut() { m_buf = ""; m_depth = 0; }
   void BeginObject() { Sep(); m_buf += "{"; Push(); }
   void EndObject()   { Pop(); m_buf += "}"; }
   void BeginArray()  { Sep(); m_buf += "["; Push(); }
   void EndArray()    { Pop(); m_buf += "]"; }
   // A key consumes the separator slot; the following value must not add another.
   void Key(const string key) { Sep(); m_buf += "\"" + JsonEscape(key) + "\":"; if(m_depth > 0) m_need_comma[m_depth - 1] = false; m_after_key = true; }
   void Str(const string v)   { Value(); m_buf += "\"" + JsonEscape(v) + "\""; }
   void Num(const double v)   { Value(); m_buf += JsonTrimZeros(DoubleToString(v, 8)); }
   void Int(const long v)     { Value(); m_buf += IntegerToString(v); }
   void Bool(const bool v)    { Value(); m_buf += (v ? "true" : "false"); }
   string Text() { return m_buf; }
private:
   bool m_after_key;
   void Value()
   {
      if(m_after_key) { m_after_key = false; if(m_depth > 0) m_need_comma[m_depth - 1] = true; return; }
      Sep();
   }
};
#endif
```
Note for the implementer: `BeginObject`/`BeginArray` after a `Key()` must also honour `m_after_key` — change their first statement from `Sep();` to `Value();` so `"account":{` does not get a stray comma. (The test above catches this.)

- [ ] **Step 4: Run to verify it passes**

Run: `scripts/mt5-test.sh SignalBridgeUnitTests` — Expected: `TESTS COMPLETE: 23 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
git add mql5
git commit -m "feat(bridge): nested JSON writer for state.json"
```

---

### Task 3: BridgeState.mqh — atomic state.json snapshot

**Files:**
- Create: `mql5/Include/SignalBridge/BridgeState.mqh`
- Create: `mql5/Experts/SignalBridgeTests/SignalBridgeExecTests.mq5` (this task adds the state test; Task 4 adds executor steps to the same EA)

**Interfaces:**
- Produces: `bool WriteStateJson(const string dir, const string &symbols[], const ulong magic)` — writes `<dir>/state.tmp` then `FileMove` → `<dir>/state.json`; returns false if any file op fails. Also `string DealEntryName(const long entry)` (`IN|OUT|INOUT|OUT_BY`), `string DealReasonName(const long reason)` (`CLIENT|MOBILE|WEB|EXPERT|SL|TP|SO|ROLLOVER|VMARGIN|SPLIT|OTHER`), `string PositionTypeName(const long t)` (`BUY|SELL`), `string OrderTypeName(const long t)` (`BUY|SELL|BUY_LIMIT|SELL_LIMIT|BUY_STOP|SELL_STOP|BUY_STOP_LIMIT|SELL_STOP_LIMIT|CLOSE_BY`), `string TimeStr(const datetime t)` → `TimeToString(t, TIME_DATE|TIME_SECONDS)`.
- Document shape (Python's `BridgeState` in Task 11 mirrors it exactly): `{"ts","account":{"login","balance","equity","margin_free","hedging"},"symbols":{"<name>":{"bid","ask","digits","point","volume_step","volume_min","volume_max","tick_value","tick_size","trade_allowed"}},"positions":[{"ticket","symbol","type","volume","price_open","sl","tp","comment","magic"}],"orders":[{"ticket","symbol","type","volume","price","sl","tp","comment","expiration"}],"deals_recent":[{"ticket","position_id","entry","reason","price","volume","time"}]}` — `deals_recent` = deals of the last 24 h, newest last, capped at 200 (oldest dropped).

- [ ] **Step 1: Write the failing test EA**

`mql5/Experts/SignalBridgeTests/SignalBridgeExecTests.mq5`:
```mql5
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
         AssertTrue(StringFind(s, "\"entry\":\"OUT\",\"reason\":\"CLIENT\"") > 0, "state: OUT deal with reason");
         break;
      default:
         TestSummary(); ExpertRemove(); return;
   }
   g_step++;
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/mt5-test.sh SignalBridgeExecTests` — Expected: compile error, missing `BridgeState.mqh`.

- [ ] **Step 3: Create `mql5/Include/SignalBridge/BridgeState.mqh`**

```mql5
#ifndef SIGNALBRIDGE_BRIDGESTATE_MQH
#define SIGNALBRIDGE_BRIDGESTATE_MQH
#include <SignalBridge/JsonOut.mqh>
// Snapshot of the account for the Python side. Written atomically: state.tmp then FileMove → state.json.

string TimeStr(const datetime t) { return TimeToString(t, TIME_DATE|TIME_SECONDS); }

string PositionTypeName(const long t) { return (t == POSITION_TYPE_BUY) ? "BUY" : "SELL"; }

string OrderTypeName(const long t)
{
   switch((int)t)
   {
      case ORDER_TYPE_BUY: return "BUY";                 case ORDER_TYPE_SELL: return "SELL";
      case ORDER_TYPE_BUY_LIMIT: return "BUY_LIMIT";     case ORDER_TYPE_SELL_LIMIT: return "SELL_LIMIT";
      case ORDER_TYPE_BUY_STOP: return "BUY_STOP";       case ORDER_TYPE_SELL_STOP: return "SELL_STOP";
      case ORDER_TYPE_BUY_STOP_LIMIT: return "BUY_STOP_LIMIT"; case ORDER_TYPE_SELL_STOP_LIMIT: return "SELL_STOP_LIMIT";
      case ORDER_TYPE_CLOSE_BY: return "CLOSE_BY";
   }
   return "UNKNOWN";
}

string DealEntryName(const long e)
{
   switch((int)e) { case DEAL_ENTRY_IN: return "IN"; case DEAL_ENTRY_OUT: return "OUT"; case DEAL_ENTRY_INOUT: return "INOUT"; case DEAL_ENTRY_OUT_BY: return "OUT_BY"; }
   return "UNKNOWN";
}

string DealReasonName(const long r)
{
   switch((int)r)
   {
      case DEAL_REASON_CLIENT: return "CLIENT";   case DEAL_REASON_MOBILE: return "MOBILE";  case DEAL_REASON_WEB: return "WEB";
      case DEAL_REASON_EXPERT: return "EXPERT";   case DEAL_REASON_SL: return "SL";          case DEAL_REASON_TP: return "TP";
      case DEAL_REASON_SO: return "SO";           case DEAL_REASON_ROLLOVER: return "ROLLOVER"; case DEAL_REASON_VMARGIN: return "VMARGIN";
      case DEAL_REASON_SPLIT: return "SPLIT";
   }
   return "OTHER";
}

bool WriteStateJson(const string dir, const string &symbols[], const ulong magic)
{
   CJsonOut j;
   j.BeginObject();
   j.Key("ts"); j.Str(TimeStr(TimeLocal()));
   j.Key("account"); j.BeginObject();
      j.Key("login");       j.Int(AccountInfoInteger(ACCOUNT_LOGIN));
      j.Key("balance");     j.Num(AccountInfoDouble(ACCOUNT_BALANCE));
      j.Key("equity");      j.Num(AccountInfoDouble(ACCOUNT_EQUITY));
      j.Key("margin_free"); j.Num(AccountInfoDouble(ACCOUNT_MARGIN_FREE));
      j.Key("hedging");     j.Bool(AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   j.EndObject();
   j.Key("symbols"); j.BeginObject();
   for(int i = 0; i < ArraySize(symbols); i++)
   {
      string s = symbols[i];
      if(!SymbolSelect(s, true)) continue;
      j.Key(s); j.BeginObject();
         j.Key("bid");           j.Num(SymbolInfoDouble(s, SYMBOL_BID));
         j.Key("ask");           j.Num(SymbolInfoDouble(s, SYMBOL_ASK));
         j.Key("digits");        j.Int(SymbolInfoInteger(s, SYMBOL_DIGITS));
         j.Key("point");         j.Num(SymbolInfoDouble(s, SYMBOL_POINT));
         j.Key("volume_step");   j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP));
         j.Key("volume_min");    j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN));
         j.Key("volume_max");    j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX));
         j.Key("tick_value");    j.Num(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE));
         j.Key("tick_size");     j.Num(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE));
         j.Key("trade_allowed"); j.Bool(SymbolInfoInteger(s, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_FULL && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED));
      j.EndObject();
   }
   j.EndObject();
   j.Key("positions"); j.BeginArray();
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      j.BeginObject();
         j.Key("ticket");     j.Int((long)ticket);
         j.Key("symbol");     j.Str(PositionGetString(POSITION_SYMBOL));
         j.Key("type");       j.Str(PositionTypeName(PositionGetInteger(POSITION_TYPE)));
         j.Key("volume");     j.Num(PositionGetDouble(POSITION_VOLUME));
         j.Key("price_open"); j.Num(PositionGetDouble(POSITION_PRICE_OPEN));
         j.Key("sl");         j.Num(PositionGetDouble(POSITION_SL));
         j.Key("tp");         j.Num(PositionGetDouble(POSITION_TP));
         j.Key("comment");    j.Str(PositionGetString(POSITION_COMMENT));
         j.Key("magic");      j.Int(PositionGetInteger(POSITION_MAGIC));
      j.EndObject();
   }
   j.EndArray();
   j.Key("orders"); j.BeginArray();
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      j.BeginObject();
         j.Key("ticket");     j.Int((long)ticket);
         j.Key("symbol");     j.Str(OrderGetString(ORDER_SYMBOL));
         j.Key("type");       j.Str(OrderTypeName(OrderGetInteger(ORDER_TYPE)));
         j.Key("volume");     j.Num(OrderGetDouble(ORDER_VOLUME_CURRENT));
         j.Key("price");      j.Num(OrderGetDouble(ORDER_PRICE_OPEN));
         j.Key("sl");         j.Num(OrderGetDouble(ORDER_SL));
         j.Key("tp");         j.Num(OrderGetDouble(ORDER_TP));
         j.Key("comment");    j.Str(OrderGetString(ORDER_COMMENT));
         j.Key("expiration"); j.Str(TimeStr((datetime)OrderGetInteger(ORDER_TIME_EXPIRATION)));
      j.EndObject();
   }
   j.EndArray();
   j.Key("deals_recent"); j.BeginArray();
   if(HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 3600))
   {
      int total = HistoryDealsTotal();
      int first = (total > 200) ? total - 200 : 0;
      for(int i = first; i < total; i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(d == 0) continue;
         long type = HistoryDealGetInteger(d, DEAL_TYPE);
         if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
         j.BeginObject();
            j.Key("ticket");      j.Int((long)d);
            j.Key("position_id"); j.Int(HistoryDealGetInteger(d, DEAL_POSITION_ID));
            j.Key("entry");       j.Str(DealEntryName(HistoryDealGetInteger(d, DEAL_ENTRY)));
            j.Key("reason");      j.Str(DealReasonName(HistoryDealGetInteger(d, DEAL_REASON)));
            j.Key("price");       j.Num(HistoryDealGetDouble(d, DEAL_PRICE));
            j.Key("volume");      j.Num(HistoryDealGetDouble(d, DEAL_VOLUME));
            j.Key("time");        j.Str(TimeStr((datetime)HistoryDealGetInteger(d, DEAL_TIME)));
         j.EndObject();
      }
   }
   j.EndArray();
   j.EndObject();

   string tmp = dir + "/state.tmp", final = dir + "/state.json";
   int h = FileOpen(tmp, FILE_WRITE|FILE_BIN);
   if(h == INVALID_HANDLE) { Print("WriteStateJson: cannot open ", tmp, " error=", GetLastError()); return false; }
   uchar bytes[]; StringToCharArray(j.Text(), bytes, 0, -1, CP_UTF8);
   int n = ArraySize(bytes); if(n > 0 && bytes[n - 1] == 0) n--;
   FileWriteArray(h, bytes, 0, n);
   FileClose(h);
   if(!FileMove(tmp, 0, final, FILE_REWRITE)) { Print("WriteStateJson: FileMove failed error=", GetLastError()); return false; }
   return true;
}
#endif
```

- [ ] **Step 4: Run to verify it passes**

Run: `scripts/mt5-test.sh SignalBridgeExecTests` — Expected: `TESTS COMPLETE: 16 passed, 0 failed`. If `"volume":0.1,` does not match because the tester fills 0.10 as `0.1` — it does; if `"reason":"CLIENT"` fails, print the deal reason the tester used and, if it is `EXPERT`, change the assertion to accept either (`StringFind(...CLIENT...) > 0 || StringFind(...EXPERT...) > 0`) and note it.

- [ ] **Step 5: Commit**

```bash
git add mql5
git commit -m "feat(bridge): atomic state.json snapshot of account, symbols, positions, orders, deals"
```

---

### Task 4: BridgeExecutor.mqh — one command → one result

**Files:**
- Create: `mql5/Include/SignalBridge/BridgeExecutor.mqh`
- Modify: `mql5/Experts/SignalBridgeTests/SignalBridgeExecTests.mq5` (append steps)

**Interfaces:**
- Produces: `struct BridgeCommand { string cmd_id; string type; string symbol; string side; double volume; double sl; double tp; string comment; double price; datetime expires_at; ulong position; ulong order; }`, `bool ParseCommand(const string json, BridgeCommand &c)` (false when `cmd_id`/`type` missing or `type` unknown), `struct BridgeResult { string cmd_id; bool ok; uint retcode; string retcode_text; ulong position; ulong order; double fill_price; int attempts; }`, `string ResultToJson(const BridgeResult &r)`, `class CBridgeExecutor { void Init(const ulong magic, const int deviation_points, const int max_retries); void Execute(const BridgeCommand &c, BridgeResult &r); }`.
- `Execute` never throws; unknown symbol / invalid volume / etc. come back as `ok=false` with a retcode.

- [ ] **Step 1: Append failing executor steps to the test EA**

Add `#include <SignalBridge/BridgeExecutor.mqh>` and globals `CBridgeExecutor g_exec; ulong g_pos = 0; ulong g_ord = 0;`. In `OnInit` add `g_exec.Init(424242, 50, 3);`. Replace the `default:` block with new cases (keep cases 0–4):
```mql5
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
         AssertTrue(StringFind(ResultToJson(r), "\"cmd_id\":\"s1:L1:1\",\"ok\":true,\"retcode\":10009") == 0, "exec: result json");
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/mt5-test.sh SignalBridgeExecTests` — Expected: compile error, missing `BridgeExecutor.mqh`.

- [ ] **Step 3: Create `mql5/Include/SignalBridge/BridgeExecutor.mqh`**

```mql5
#ifndef SIGNALBRIDGE_BRIDGEEXECUTOR_MQH
#define SIGNALBRIDGE_BRIDGEEXECUTOR_MQH
#include <Trade/Trade.mqh>
#include <SignalBridge/Json.mqh>
// Executes one bridge command via CTrade. Retry/normalisation rules are those of mt5-copytrader's Replayer.

struct BridgeCommand
{
   string cmd_id; string type; string symbol; string side;
   double volume; double sl; double tp; string comment; double price; datetime expires_at;
   ulong position; ulong order;
};

struct BridgeResult
{
   string cmd_id; bool ok; uint retcode; string retcode_text;
   ulong position; ulong order; double fill_price; int attempts;
};

bool ParseCommand(const string json, BridgeCommand &c)
{
   c.cmd_id = ""; c.type = ""; c.symbol = ""; c.side = ""; c.volume = 0; c.sl = 0; c.tp = 0; c.comment = "";
   c.price = 0; c.expires_at = 0; c.position = 0; c.order = 0;
   if(!JsonGetString(json, "cmd_id", c.cmd_id) || c.cmd_id == "") return false;
   if(!JsonGetString(json, "type", c.type)) return false;
   if(c.type != "ping" && c.type != "open_market" && c.type != "open_pending" && c.type != "modify_sl" && c.type != "close" && c.type != "cancel") return false;
   string s; long l; double d;
   if(JsonGetString(json, "symbol", s)) c.symbol = s;
   if(JsonGetString(json, "side", s)) c.side = s;
   if(JsonGetString(json, "comment", s)) c.comment = s;
   if(JsonGetDouble(json, "volume", d)) c.volume = d;
   if(JsonGetDouble(json, "sl", d)) c.sl = d;
   if(JsonGetDouble(json, "tp", d)) c.tp = d;
   if(JsonGetDouble(json, "price", d)) c.price = d;
   if(JsonGetString(json, "expires_at", s)) c.expires_at = StringToTime(s);
   if(JsonGetLong(json, "position", l)) c.position = (ulong)l;
   if(JsonGetLong(json, "order", l)) c.order = (ulong)l;
   return true;
}

string ResultToJson(const BridgeResult &r)
{
   CJsonWriter w;
   w.AddString("cmd_id", r.cmd_id);
   // CJsonWriter has no AddBool; write ok as a raw token via AddLong-free trick: build manually.
   string head = w.End();                           // {"cmd_id":"..."}
   head = StringSubstr(head, 0, StringLen(head) - 1); // drop closing brace
   return head + ",\"ok\":" + (r.ok ? "true" : "false") + ",\"retcode\":" + IntegerToString(r.retcode)
        + ",\"retcode_text\":\"" + JsonEscape(r.retcode_text) + "\",\"position\":" + IntegerToString((long)r.position)
        + ",\"order\":" + IntegerToString((long)r.order) + ",\"fill_price\":" + JsonTrimZeros(DoubleToString(r.fill_price, 8))
        + ",\"attempts\":" + IntegerToString(r.attempts) + "}";
}

class CBridgeExecutor
{
private:
   CTrade m_trade;
   int    m_max_retries;
   string m_tag;

   bool Accepted()
   {
      uint rc = m_trade.ResultRetcode();
      return (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_DONE_PARTIAL || rc == TRADE_RETCODE_PLACED);
   }
   bool IsRetryable(const uint rc)
   {
      switch(rc)
      {
         case 0: case TRADE_RETCODE_REQUOTE: case TRADE_RETCODE_REJECT: case TRADE_RETCODE_ERROR: case TRADE_RETCODE_TIMEOUT:
         case TRADE_RETCODE_PRICE_CHANGED: case TRADE_RETCODE_PRICE_OFF: case TRADE_RETCODE_TOO_MANY_REQUESTS:
         case TRADE_RETCODE_LOCKED: case TRADE_RETCODE_CONNECTION: case TRADE_RETCODE_DONE_PARTIAL:
            return true;
      }
      return false;
   }
   double NormalizeStop(const string symbol, const double price)
   {
      if(price <= 0) return 0.0;
      return NormalizeDouble(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   }
   void Fill(BridgeResult &r, const bool ok)
   {
      r.ok = ok; r.retcode = m_trade.ResultRetcode(); r.retcode_text = m_trade.ResultRetcodeDescription();
      r.order = m_trade.ResultOrder(); r.fill_price = m_trade.ResultPrice();
      ulong deal = m_trade.ResultDeal();
      if(deal > 0 && HistoryDealSelect(deal)) r.position = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      else if(ok && r.order > 0 && PositionSelectByTicket(r.order)) r.position = r.order;
   }

   // One attempt of each type. Returns true when accepted.
   bool TryOpenMarket(const BridgeCommand &c)
   {
      if(c.side != "BUY" && c.side != "SELL") return false;
      if(!SymbolSelect(c.symbol, true)) return false;
      bool is_buy = (c.side == "BUY");
      double price = is_buy ? SymbolInfoDouble(c.symbol, SYMBOL_ASK) : SymbolInfoDouble(c.symbol, SYMBOL_BID);
      m_trade.SetTypeFillingBySymbol(c.symbol);
      m_trade.PositionOpen(c.symbol, is_buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, c.volume, price,
                           NormalizeStop(c.symbol, c.sl), NormalizeStop(c.symbol, c.tp), c.comment);
      return Accepted();
   }
   bool TryOpenPending(const BridgeCommand &c)
   {
      if(c.side != "BUY" && c.side != "SELL") return false;
      if(!SymbolSelect(c.symbol, true)) return false;
      m_trade.SetTypeFillingBySymbol(c.symbol);
      ENUM_ORDER_TYPE_TIME tt = (c.expires_at > 0) ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
      double px = NormalizeStop(c.symbol, c.price), sl = NormalizeStop(c.symbol, c.sl), tp = NormalizeStop(c.symbol, c.tp);
      if(c.side == "BUY") m_trade.BuyLimit(c.volume, px, c.symbol, sl, tp, tt, c.expires_at, c.comment);
      else                m_trade.SellLimit(c.volume, px, c.symbol, sl, tp, tt, c.expires_at, c.comment);
      return Accepted();
   }
   bool TryModifySl(const BridgeCommand &c)
   {
      if(!PositionSelectByTicket(c.position)) return false;
      string sym = PositionGetString(POSITION_SYMBOL);
      double cur_tp = PositionGetDouble(POSITION_TP), cur_sl = PositionGetDouble(POSITION_SL);
      double sl = NormalizeStop(sym, c.sl);
      if(MathAbs(cur_sl - sl) < 1e-9) return true;   // no-op is success
      m_trade.PositionModify(c.position, sl, cur_tp);
      return Accepted() || m_trade.ResultRetcode() == TRADE_RETCODE_NO_CHANGES;
   }
   bool TryClose(const BridgeCommand &c)
   {
      if(!PositionSelectByTicket(c.position)) return false;
      m_trade.PositionClose(c.position);
      if(!Accepted()) return false;
      if(PositionSelectByTicket(c.position) && PositionGetDouble(POSITION_VOLUME) > 1e-8) return false;   // partial fill → retry
      return true;
   }
   bool TryCancel(const BridgeCommand &c)
   {
      if(!OrderSelect(c.order)) return false;
      m_trade.OrderDelete(c.order);
      return Accepted();
   }

public:
   void Init(const ulong magic, const int deviation_points, const int max_retries)
   {
      m_max_retries = (max_retries < 1) ? 1 : max_retries;
      m_tag = "[SignalBridge] ";
      m_trade.SetExpertMagicNumber(magic);
      m_trade.SetDeviationInPoints(deviation_points);
      m_trade.SetAsyncMode(false);
      m_trade.LogLevel(LOG_LEVEL_ERRORS);
   }

   void Execute(const BridgeCommand &c, BridgeResult &r)
   {
      r.cmd_id = c.cmd_id; r.ok = false; r.retcode = 0; r.retcode_text = ""; r.position = 0; r.order = 0; r.fill_price = 0; r.attempts = 0;
      if(c.type == "ping") { r.ok = true; r.retcode_text = "pong"; return; }
      for(int attempt = 1; attempt <= m_max_retries; attempt++)
      {
         r.attempts = attempt;
         bool ok = false;
         if(c.type == "open_market")       ok = TryOpenMarket(c);
         else if(c.type == "open_pending") ok = TryOpenPending(c);
         else if(c.type == "modify_sl")    ok = TryModifySl(c);
         else if(c.type == "close")        ok = TryClose(c);
         else if(c.type == "cancel")       ok = TryCancel(c);
         Fill(r, ok);
         if(ok) { if(c.type == "close" || c.type == "cancel" || c.type == "modify_sl") { r.position = c.position; r.order = c.order; } return; }
         if(!IsRetryable(r.retcode)) break;
         Print(m_tag, c.type, " ", c.cmd_id, " attempt ", attempt, "/", m_max_retries, " failed retcode=", r.retcode, " ", r.retcode_text);
         if(attempt < m_max_retries) Sleep(300 * attempt);
      }
      if(r.retcode_text == "") r.retcode_text = "precondition failed (symbol/position/order not found or invalid side)";
   }
};
#endif
```

- [ ] **Step 4: Run to verify it passes**

Run: `scripts/mt5-test.sh SignalBridgeExecTests` — Expected: `TESTS COMPLETE: 40 passed, 0 failed`. Known pitfall: if `exec: result json` fails on the retcode value, print `r.retcode` — the tester may report `10009` (DONE) or `10008`; accept either by asserting on `"ok\":true` and `"retcode\":100` prefix instead.

- [ ] **Step 5: Commit**

```bash
git add mql5
git commit -m "feat(bridge): command parser and CTrade executor with retry gate"
```

---

### Task 5: BridgeCore.mqh + SignalBridge.mq5 — tail, idempotency, results, cursor

**Files:**
- Create: `mql5/Include/SignalBridge/BridgeCore.mqh`, `mql5/Experts/SignalBridge/SignalBridge.mq5`, `mql5/Experts/SignalBridgeTests/SignalBridgeCoreTests.mq5`

**Interfaces:**
- Produces: `class CBridgeCore { bool Init(const string dir, const ulong magic, const int deviation_points, const int max_retries); int Poll(); int ExecutedCount(); }` — `Poll()` reads new command lines, skips any `cmd_id` already seen in `results.jsonl` (loaded at `Init`) or executed this session, executes the rest in order, appends a result line per command **before** persisting the cursor (so a crash between the two re-reads a command that is then skipped as a duplicate), returns the number executed this call (−1 if the commands file cannot be opened).
- Files inside `dir`: `commands.jsonl`, `results.jsonl`, `state.json`, `cursor.txt` (via `Cursor.mqh` with base `dir + "/bridge"` → `dir/bridge_cursor.txt`).
- EA inputs: `InpExpectedLogin` (0), `InpMagic` (903001), `InpPollMs` (500), `InpDeviationPoints` (20), `InpMaxRetries` (3), `InpSymbols` (`"XAUUSD"`), `InpBridgeDir` (`"signalbridge"`).

- [ ] **Step 1: Write the failing core test EA**

`mql5/Experts/SignalBridgeTests/SignalBridgeCoreTests.mq5`:
```mql5
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/mt5-test.sh SignalBridgeCoreTests` — Expected: compile error, missing `BridgeCore.mqh`.

- [ ] **Step 3: Create `mql5/Include/SignalBridge/BridgeCore.mqh`**

```mql5
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
            r.cmd_id = "invalid:" + IntegerToString(ends[i]); r.ok = false; r.retcode = 0;
            r.retcode_text = "unparseable command line"; r.position = 0; r.order = 0; r.fill_price = 0; r.attempts = 0;
            Print("[SignalBridge] unparseable command skipped: ", lines[i]);
            WriteResult(r); done++;
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `scripts/mt5-test.sh SignalBridgeCoreTests` — Expected: `TESTS COMPLETE: 19 passed, 0 failed`.

- [ ] **Step 5: Create the EA `mql5/Experts/SignalBridge/SignalBridge.mq5`**

```mql5
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
   for(int i = 0; i < n; i++) { StringTrimLeft(g_symbols[i]); StringTrimRight(g_symbols[i]); if(!SymbolSelect(g_symbols[i], true)) Print("[SignalBridge] WARNING: symbol '", g_symbols[i], "' not found on this account"); }
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
```

- [ ] **Step 6: Compile the EA and re-run all three MQL5 suites**

Run: `scripts/mt5-compile.sh Experts/SignalBridge/SignalBridge.mq5 && scripts/mt5-test.sh SignalBridgeUnitTests && scripts/mt5-test.sh SignalBridgeExecTests && scripts/mt5-test.sh SignalBridgeCoreTests`
Expected: `Result: 0 errors, 0 warnings`; 23 / 40 / 19 passed, 0 failed.

- [ ] **Step 7: Commit**

```bash
git add mql5
git commit -m "feat(bridge): SignalBridge EA with idempotent command loop"
```

---

# Part B — Python service

Python work happens in a virtualenv: `python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`. All `pytest` commands below assume the venv is active and are run from the repo root.

### Task 6: Python scaffold, models, config

**Files:**
- Create: `pyproject.toml`, `tg_signal_trader/__init__.py`, `tg_signal_trader/models.py`, `tg_signal_trader/config.py`, `config.example.yaml`, `.env.example`, `tests/test_models.py`, `tests/test_config.py`

**Interfaces:**
- Produces (models.py): enums `Provider("lewis","wolves")`, `Side("BUY","SELL")`, `EntryType("MARKET","LIMIT")`, `LegState("PLACING","PENDING_ORDER","OPEN","CLOSED_TP","CLOSED_SL","CLOSED_MANUAL","CANCELLED")`, `RunState("NEW","PLACING","ACTIVE","DONE","REJECTED")`; pydantic models `Signal`, `Leg`, `SignalRun`, `InboxMessage`; helpers `complete_tps(tps: list[float | None]) -> list[float]`, `make_signal_id(provider, msg_id) -> str`, `leg_comment(signal_id, n) -> str` = `f"sig:{signal_id}:L{n}"`.
- Produces (config.py): `ProviderConfig`, `LlmConfig`, `AppConfig` (pydantic), `load_config(path) -> AppConfig`, `Secrets.from_env() -> Secrets` (fields `telegram_api_id: int`, `telegram_api_hash: str`, `anthropic_api_key: str | None`).

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "tg-signal-trader"
version = "0.1.0"
description = "Executes Telegram trade signals on MT5 accounts through a file bridge"
requires-python = ">=3.11"
dependencies = [
  "telethon>=1.36,<2",
  "anthropic>=1.6,<2",
  "pydantic>=2.7,<3",
  "pyyaml>=6,<7",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
tg-trader = "tg_signal_trader.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["tg_signal_trader*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```
Create the venv and install: `python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`. Create empty `tg_signal_trader/__init__.py` containing `__version__ = "0.1.0"`.

- [ ] **Step 2: Write the failing tests**

`tests/test_models.py`:
```python
from datetime import datetime, timezone
import pytest
from tg_signal_trader.models import (Side, EntryType, LegState, RunState, Signal, Leg, SignalRun,
                                     complete_tps, make_signal_id, leg_comment)


def test_complete_tps_fills_open_tp4_by_last_increment():
    assert complete_tps([4381, 4386, 4391, None]) == [4381, 4386, 4391, 4396]
    assert complete_tps([4356.27, 4357.42, 4359.73, None]) == pytest.approx([4356.27, 4357.42, 4359.73, 4362.04])
    assert complete_tps([4381, 4386, 4391]) == [4381, 4386, 4391, 4396]


def test_complete_tps_keeps_explicit_tp4():
    assert complete_tps([1, 2, 3, 10]) == [1, 2, 3, 10]


def test_complete_tps_rejects_fewer_than_three():
    with pytest.raises(ValueError):
        complete_tps([1, 2])


def test_signal_id_and_comment():
    assert make_signal_id("wolves", 29501) == "wolves:29501"
    assert leg_comment("wolves:29501", 3) == "sig:wolves:29501:L3"


def test_signal_run_builds_four_legs():
    sig = Signal(id="lewis:1", provider="lewis", symbol="NAS100", side=Side.BUY, entry_type=EntryType.MARKET,
                 entry_zone=[], sl=29088.91, tps=[29212.15, 29240.59, 29306.95, 29373.31],
                 received_at=datetime(2026, 9, 17, 7, 45, 4, tzinfo=timezone.utc), raw_text="x", telegram_msg_id=1)
    run = SignalRun.from_signal(sig)
    assert run.state == RunState.NEW
    assert [l.n for l in run.legs] == [1, 2, 3, 4]
    assert [l.tp for l in run.legs] == sig.tps
    assert all(l.state == LegState.PLACING for l in run.legs)
    assert all(l.sl_current == 29088.91 for l in run.legs)
    assert run.open_legs() == [] and run.pending_legs() == []
    assert run.is_finished() is False
```

`tests/test_config.py`:
```python
import os, textwrap
import pytest
from tg_signal_trader.config import load_config, Secrets


def test_load_config_parses_providers(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
      providers:
        lewis:
          telegram_chat: -100123
          bridge_dir: /tmp/lewis
          risk_pct_per_leg: 1.0
          symbols: {NAS100: NAS100, XAUUSD: XAUUSD}
          sl_range: {NAS100: [5, 500], XAUUSD: [1, 60]}
        wolves:
          telegram_chat: -100456
          bridge_dir: /tmp/wolves
          symbols: {XAUUSD: GOLD}
          sl_range: {XAUUSD: [1, 60]}
      llm: {model: claude-opus-5}
      db_path: /tmp/x.sqlite
    """))
    cfg = load_config(p)
    assert set(cfg.providers) == {"lewis", "wolves"}
    lw = cfg.providers["lewis"]
    assert lw.risk_pct_per_leg == 1.0 and lw.max_signal_age_sec == 120 and lw.pending_ttl_hours == 24
    assert lw.management_actions == ["close_all", "cancel_pending", "move_sl", "break_even"]
    assert lw.max_open_signals == 2 and lw.max_legs_open == 8 and lw.daily_loss_stop_pct == 5.0
    assert cfg.providers["wolves"].symbols["XAUUSD"] == "GOLD"
    assert cfg.llm.model == "claude-opus-5" and cfg.llm.confidence_threshold == 0.8 and cfg.llm.timeout_sec == 8


def test_load_config_rejects_unknown_management_action(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("providers:\n  lewis:\n    telegram_chat: 1\n    bridge_dir: /tmp/x\n    symbols: {XAUUSD: XAUUSD}\n    sl_range: {XAUUSD: [1, 60]}\n    management_actions: [close_all, secure_half]\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Secrets.from_env()
    assert s.telegram_api_id == 12345 and s.telegram_api_hash == "abc" and s.anthropic_api_key is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest -q` — Expected: `ModuleNotFoundError: tg_signal_trader.models`.

- [ ] **Step 4: Write `tg_signal_trader/models.py`**

```python
"""Core data model: signals, legs, runs, inbox messages."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class Provider(str, Enum):
    LEWIS = "lewis"
    WOLVES = "wolves"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class EntryType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class LegState(str, Enum):
    PLACING = "PLACING"            # command sent, no result yet
    PENDING_ORDER = "PENDING_ORDER"
    OPEN = "OPEN"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CANCELLED = "CANCELLED"


class RunState(str, Enum):
    NEW = "NEW"
    PLACING = "PLACING"
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    REJECTED = "REJECTED"


FINISHED_LEG_STATES = {LegState.CLOSED_TP, LegState.CLOSED_SL, LegState.CLOSED_MANUAL, LegState.CANCELLED}


def make_signal_id(provider: str, msg_id: int) -> str:
    return f"{provider}:{msg_id}"


def leg_comment(signal_id: str, n: int) -> str:
    return f"sig:{signal_id}:L{n}"


def complete_tps(tps: list[float | None]) -> list[float]:
    """Return exactly four TPs. A missing/None TP4 is tp3 + (tp3 - tp2)."""
    given = [t for t in tps[:4] if t is not None]
    if len(given) < 3 or (len(tps) >= 4 and tps[3] is None and len(given) != 3):
        raise ValueError(f"need at least TP1..TP3, got {tps}")
    if len(given) == 4:
        return [float(t) for t in given]
    tp1, tp2, tp3 = (float(t) for t in given[:3])
    return [tp1, tp2, tp3, round(tp3 + (tp3 - tp2), 5)]


class InboxMessage(BaseModel):
    msg_id: int
    chat_id: int
    provider: str
    reply_to: int | None = None
    text: str = ""
    ts: datetime                 # message time, UTC
    status: str = "new"          # new | processed | stale


class Signal(BaseModel):
    id: str
    provider: str
    symbol: str                  # canonical (XAUUSD, NAS100, ...)
    side: Side
    entry_type: EntryType
    entry_zone: list[float] = Field(default_factory=list)   # [near, far] for LIMIT
    sl: float
    tps: list[float]             # exactly 4
    received_at: datetime
    raw_text: str
    telegram_msg_id: int
    parsed_by: str = "template"


class Leg(BaseModel):
    n: int
    tp: float
    state: LegState = LegState.PLACING
    cmd_id: str | None = None
    order_ticket: int | None = None
    position_ticket: int | None = None
    entry_price: float | None = None
    volume: float = 0.0
    sl_current: float
    reason: str = ""

    def comment(self, signal_id: str) -> str:
        return leg_comment(signal_id, self.n)


class SignalRun(BaseModel):
    signal: Signal
    legs: list[Leg]
    state: RunState = RunState.NEW
    be_applied: bool = False       # ladder: L2 TP → L3/L4 to break-even done
    tp1_applied: bool = False      # ladder: L3 TP → L4 to TP1 done
    pendings_cancelled: bool = False
    seq: int = 0                   # command sequence for cmd_ids
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_signal(cls, sig: Signal) -> "SignalRun":
        legs = [Leg(n=i + 1, tp=tp, sl_current=sig.sl) for i, tp in enumerate(sig.tps)]
        return cls(signal=sig, legs=legs, created_at=sig.received_at, updated_at=sig.received_at)

    @property
    def id(self) -> str:
        return self.signal.id

    def next_cmd_id(self, leg_n: int) -> str:
        self.seq += 1
        return f"{self.signal.id}:L{leg_n}:{self.seq}"

    def open_legs(self) -> list[Leg]:
        return [l for l in self.legs if l.state == LegState.OPEN]

    def pending_legs(self) -> list[Leg]:
        return [l for l in self.legs if l.state == LegState.PENDING_ORDER]

    def placing_legs(self) -> list[Leg]:
        return [l for l in self.legs if l.state == LegState.PLACING]

    def is_finished(self) -> bool:
        return all(l.state in FINISHED_LEG_STATES for l in self.legs)
```

- [ ] **Step 5: Write `tg_signal_trader/config.py` and the example files**

```python
"""config.yaml (no secrets) and environment secrets."""
from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml

ALLOWED_ACTIONS = ["close_all", "cancel_pending", "move_sl", "break_even"]


class ProviderConfig(BaseModel):
    telegram_chat: int
    bridge_dir: Path
    risk_pct_per_leg: float = 1.0
    symbols: dict[str, str]                         # canonical -> broker symbol
    sl_range: dict[str, tuple[float, float]]        # canonical -> (min, max) SL distance in price units
    market_entry_tolerance_pct: float = 0.3
    limit_max_distance_pct: float = 1.0
    max_signal_age_sec: int = 120
    pending_ttl_hours: int = 24
    management_actions: list[str] = Field(default_factory=lambda: list(ALLOWED_ACTIONS))
    max_open_signals: int = 2
    max_legs_open: int = 8
    daily_loss_stop_pct: float = 5.0

    @field_validator("management_actions")
    @classmethod
    def _known_actions(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ALLOWED_ACTIONS]
        if bad:
            raise ValueError(f"unknown management_actions {bad}; allowed: {ALLOWED_ACTIONS}")
        return v


class LlmConfig(BaseModel):
    model: str = "claude-opus-5"
    confidence_threshold: float = 0.8
    timeout_sec: float = 8.0


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    llm: LlmConfig = Field(default_factory=LlmConfig)
    db_path: Path = Path("tg_signal_trader.sqlite")
    session_path: Path = Path("tg_listener.session")
    poll_interval_sec: float = 0.5


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


class Secrets(BaseModel):
    telegram_api_id: int
    telegram_api_hash: str
    anthropic_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
                   telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
                   anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None)
```

`config.example.yaml`:
```yaml
providers:
  lewis:
    telegram_chat: 0                      # run `tg-trader resolve-chats` to fill in
    bridge_dir: /home/mt5/terminals/pupri/MQL5/Files/signalbridge
    risk_pct_per_leg: 1.0
    symbols: { NAS100: NAS100, XAUUSD: XAUUSD, GER40: GER40, EURGBP: EURGBP }
    sl_range: { XAUUSD: [1, 60], NAS100: [5, 500], GER40: [5, 400], EURGBP: [0.0005, 0.02] }
    market_entry_tolerance_pct: 0.3
    limit_max_distance_pct: 1.0
    max_signal_age_sec: 120
    pending_ttl_hours: 24
    management_actions: [close_all, cancel_pending, move_sl, break_even]
    max_open_signals: 2
    max_legs_open: 8
    daily_loss_stop_pct: 5.0
  wolves:
    telegram_chat: 0
    bridge_dir: /home/mt5/terminals/icontech/MQL5/Files/signalbridge
    risk_pct_per_leg: 1.0
    symbols: { XAUUSD: XAUUSD }
    sl_range: { XAUUSD: [1, 60] }
llm:
  model: claude-opus-5            # claude-haiku-4-5 is the cheaper alternative
  confidence_threshold: 0.8
  timeout_sec: 8
db_path: /var/lib/tg-signal-trader/trader.sqlite
session_path: /var/lib/tg-signal-trader/tg_listener.session
poll_interval_sec: 0.5
```
`.env.example`:
```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
ANTHROPIC_API_KEY=
```

- [ ] **Step 6: Run to verify they pass**

Run: `pytest -q` — Expected: `8 passed`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tg_signal_trader tests config.example.yaml .env.example
git commit -m "feat(py): package scaffold, data model, config loading"
```

---

### Task 7: Telegram export reader and provider fixtures

**Files:**
- Create: `tg_signal_trader/export.py`, `tests/test_export.py`, `tests/data/export_sample.html`, `fixtures/lewis_messages.jsonl`, `fixtures/wolves_messages.jsonl`
- Source exports (macOS only): `/Users/scottheslop/Downloads/Telegram Desktop/ChatExport_2026-09-18/` (Lewis) and `/Users/scottheslop/Downloads/Telegram Desktop/ChatExport_2026-09-14 (1)/` (Wolves)

**Interfaces:**
- Produces: `read_export(dir: Path, provider: str, chat_id: int = 0) -> list[InboxMessage]` — reads every `messages*.html` in numeric order (`messages.html`, `messages2.html`, …), one `InboxMessage` per `<div class="message default …" id="messageN">` with text (HTML unescaped, `<br>` → newline, tags stripped), `ts` parsed from the `title="DD.MM.YYYY HH:MM:SS UTC+00:00"` attribute as UTC, `reply_to` from the `In reply to` link (`#go_to_messageN`), service/photo-only messages included with empty text. `write_fixture(msgs, path)` / `read_fixture(path) -> list[InboxMessage]` as JSONL (one `InboxMessage.model_dump_json()` per line).

- [ ] **Step 1: Create a small sample export for the test**

`tests/data/export_sample.html` (hand-written, structure copied from a real export):
```html
<html><body><div class="page_body chat_page"><div class="history">
<div class="message service" id="message1"><div class="body details">9 March 2026</div></div>
<div class="message default clearfix" id="message8">
 <div class="pull_right date details" title="09.03.2026 14:45:06 UTC+00:00">14:45</div>
 <div class="from_name">Lewis Inner Circle 💎</div>
 <div class="text">🔵 BUY NAS100 NOW<br><br>Stop Loss: 24389<br><br>Take Profit Targets:<br>🎯 TP1: 24545<br>🎯 TP2: 24620<br>🎯 TP3: 24729<br>🎯 TP4: 24949</div>
</div>
<div class="message default clearfix joined" id="message11">
 <div class="pull_right date details" title="09.03.2026 14:54:11 UTC+00:00">14:54</div>
 <div class="reply_to details">In reply to <a href="#go_to_message8">this message</a></div>
 <div class="media_wrap clearfix"><a class="photo_wrap"><img class="photo" src="photos/photo_1.jpg"></a></div>
 <div class="text">TP1 HIT! ✔️</div>
</div>
<div class="message default clearfix" id="message12">
 <div class="pull_right date details" title="09.03.2026 16:27:29 UTC+00:00">16:27</div>
 <div class="from_name">Lewis Inner Circle 💎</div>
 <div class="media_wrap clearfix"><a class="photo_wrap"><img class="photo" src="photos/photo_2.jpg"></a></div>
</div>
</div></div></body></html>
```

- [ ] **Step 2: Write the failing test**

`tests/test_export.py`:
```python
from datetime import datetime, timezone
from pathlib import Path
from tg_signal_trader.export import read_export, write_fixture, read_fixture

DATA = Path(__file__).parent / "data"


def test_read_export_parses_messages(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    msgs = read_export(tmp_path, "lewis", chat_id=-100)
    assert [m.msg_id for m in msgs] == [8, 11, 12]
    m8 = msgs[0]
    assert m8.provider == "lewis" and m8.chat_id == -100
    assert m8.ts == datetime(2026, 3, 9, 14, 45, 6, tzinfo=timezone.utc)
    assert m8.text.startswith("🔵 BUY NAS100 NOW\n\nStop Loss: 24389")
    assert "🎯 TP4: 24949" in m8.text
    assert msgs[1].reply_to == 8 and msgs[1].text == "TP1 HIT! ✔️"
    assert msgs[2].text == "" and msgs[2].reply_to is None


def test_read_export_orders_files_numerically(tmp_path):
    body = (DATA / "export_sample.html").read_text()
    (tmp_path / "messages.html").write_text(body.replace('id="message8"', 'id="message1000"'))
    (tmp_path / "messages2.html").write_text(body.replace('id="message8"', 'id="message2000"'))
    (tmp_path / "messages10.html").write_text(body.replace('id="message8"', 'id="message3000"'))
    ids = [m.msg_id for m in read_export(tmp_path, "lewis")]
    assert ids.index(1000) < ids.index(2000) < ids.index(3000)


def test_fixture_round_trip(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    msgs = read_export(tmp_path, "lewis")
    write_fixture(msgs, tmp_path / "f.jsonl")
    back = read_fixture(tmp_path / "f.jsonl")
    assert back == msgs
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest -q tests/test_export.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Write `tg_signal_trader/export.py`**

```python
"""Telegram Desktop HTML export → InboxMessage list. Used for fixtures and `--replay`."""
from __future__ import annotations
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from .models import InboxMessage

_CHUNK = re.compile(r'(?=<div class="message (?:default|service))')
_ID = re.compile(r'id="message(\d+)"')
_DATE = re.compile(r'class="pull_right date details" title="([^"]+)"')
_TEXT = re.compile(r'<div class="text">(.*?)</div>\s*(?:<div|</div>)', re.S)
_REPLY = re.compile(r'In reply to <a href="#go_to_message(\d+)"')
_TAG = re.compile(r"<.*?>", re.S)


def _file_order(p: Path) -> int:
    m = re.fullmatch(r"messages(\d*)\.html", p.name)
    return int(m.group(1) or "1")


def parse_export_html(text: str, provider: str, chat_id: int) -> list[InboxMessage]:
    out: list[InboxMessage] = []
    for chunk in _CHUNK.split(text)[1:]:
        if chunk.startswith('<div class="message service'):
            continue
        mid = _ID.search(chunk)
        date = _DATE.search(chunk)
        if not mid or not date:
            continue
        ts = datetime.strptime(date.group(1)[:19], "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        t = _TEXT.search(chunk)
        body = ""
        if t:
            body = html.unescape(re.sub(r"<br\s*/?>", "\n", t.group(1)))
            body = _TAG.sub("", body).strip()
        r = _REPLY.search(chunk)
        out.append(InboxMessage(msg_id=int(mid.group(1)), chat_id=chat_id, provider=provider,
                                reply_to=int(r.group(1)) if r else None, text=body, ts=ts))
    return out


def read_export(directory: str | Path, provider: str, chat_id: int = 0) -> list[InboxMessage]:
    files = sorted(Path(directory).glob("messages*.html"), key=_file_order)
    msgs: list[InboxMessage] = []
    for f in files:
        msgs.extend(parse_export_html(f.read_text(encoding="utf-8"), provider, chat_id))
    return msgs


def write_fixture(msgs: list[InboxMessage], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(m.model_dump_json() + "\n")


def read_fixture(path: str | Path) -> list[InboxMessage]:
    with open(path, "r", encoding="utf-8") as f:
        return [InboxMessage.model_validate_json(line) for line in f if line.strip()]
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest -q tests/test_export.py` — Expected: `3 passed`.

- [ ] **Step 6: Generate the fixtures from the real exports (provider messages only)**

```bash
source .venv/bin/activate
python - <<'PY'
from pathlib import Path
from tg_signal_trader.export import read_export, write_fixture
lewis = read_export(Path("/Users/scottheslop/Downloads/Telegram Desktop/ChatExport_2026-09-18"), "lewis")
wolves = read_export(Path("/Users/scottheslop/Downloads/Telegram Desktop/ChatExport_2026-09-14 (1)"), "wolves")
Path("fixtures").mkdir(exist_ok=True)
write_fixture(lewis, "fixtures/lewis_messages.jsonl")
write_fixture(wolves, "fixtures/wolves_messages.jsonl")
print(len(lewis), len(wolves))
PY
```
Expected output roughly `2100 27000` (exact counts vary; record them in the commit message). These files are provider-published channel text (no private data) and are committed as the regression corpus.

- [ ] **Step 7: Commit**

```bash
git add tg_signal_trader/export.py tests/test_export.py tests/data fixtures
git commit -m "feat(py): Telegram export reader and provider message fixtures"
```

---

### Task 8: Normalisation and the Lewis parser

**Files:**
- Create: `tg_signal_trader/normalize.py`, `tg_signal_trader/parsers/__init__.py`, `tg_signal_trader/parsers/lewis.py`, `tests/test_parser_lewis.py`

**Interfaces:**
- Produces (normalize.py): `normalize_text(s: str) -> str` (NFKC, strip emoji/pictographs, collapse spaces, uppercase `SL`/`TP` labels: `Stop Loss:`→`SL:`, `Take Profit Targets:` removed, `TPn:`/`TPn` → `TPn:`, `–`/`—` → `-`, `@` kept), `canonical_symbol(s: str) -> str | None` (`GOLD`/`XAUUSD`→`XAUUSD`, `NAS100`/`US100`/`USTEC`→`NAS100`, `GER40`/`GER30`/`DE40`/`DAX`→`GER40`, `EURGBP`, `BTCUSD`, `ETHUSD`, `GBPUSD`, `EURUSD` pass through; else `None`), `parse_price(s) -> float`.
- Produces (parsers/__init__.py): `class ParseResult(BaseModel): signal: Signal | None; template: str | None; rejected_reason: str | None`; `Parser` protocol `parse(msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult` (`prev` = up to 3 earlier provider messages, newest last); `looks_like_entry(text) -> bool` (has an SL-like and a TP-like number); `get_parser(provider) -> Parser`.
- Produces (parsers/lewis.py): `LewisParser` with templates `lewis.trade_setup`, `lewis.now`, `lewis.now_split` (header in `prev[-1]` ≤ 60 s earlier + SL/TP body in `msg`), `lewis.limit_setup`.

- [ ] **Step 1: Write the failing tests**

`tests/test_parser_lewis.py`:
```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from tg_signal_trader.models import InboxMessage, Side, EntryType
from tg_signal_trader.normalize import normalize_text, canonical_symbol
from tg_signal_trader.parsers import get_parser, looks_like_entry
from tg_signal_trader.export import read_fixture

T0 = datetime(2026, 9, 17, 7, 45, 4, tzinfo=timezone.utc)


def msg(text, mid=1, ts=T0, reply_to=None):
    return InboxMessage(msg_id=mid, chat_id=-1, provider="lewis", text=text, ts=ts, reply_to=reply_to)


def test_normalize_text():
    assert normalize_text("🟡  SELL XAUUSD NOW! 🔽\nStop Loss: 4377.26\nTake Profit Targets:\n🎯 TP1: 4369.94") == "SELL XAUUSD NOW!\nSL: 4377.26\nTP1: 4369.94"
    assert normalize_text("Entry Zone: 5164 – 5160") == "Entry Zone: 5164 - 5160"
    assert canonical_symbol("Gold") == "XAUUSD" and canonical_symbol("US100") == "NAS100" and canonical_symbol("DE40") == "GER40"
    assert canonical_symbol("BANANA") is None


def test_trade_setup_template():
    text = "TRADE SETUP:\n🔵  BUY NAS100\n\nStop Loss: 29088.91\n\nTake Profit Targets:\n🎯 TP1: 29212.15\n🎯 TP2: 29240.59\n🎯 TP3: 29306.95\n🎯 TP4: 29373.31"
    r = get_parser("lewis").parse(msg(text, 3701), [])
    s = r.signal
    assert r.template == "lewis.trade_setup" and s is not None
    assert (s.symbol, s.side, s.entry_type) == ("NAS100", Side.BUY, EntryType.MARKET)
    assert s.sl == 29088.91 and s.tps == [29212.15, 29240.59, 29306.95, 29373.31]
    assert s.id == "lewis:3701" and s.telegram_msg_id == 3701 and s.parsed_by == "lewis.trade_setup"


def test_trade_idea_with_open_tp4():
    text = "TRADE IDEA:\n🟡  SELL XAUUSD\nStop Loss: 4686.64\nTake Profit Targets:\n🎯 TP1: 4669.85\n🎯 TP2: 4667.17\n🎯 TP3: 4661.79\n🎯 TP4: OPEN"
    s = get_parser("lewis").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.tps == pytest.approx([4669.85, 4667.17, 4661.79, 4656.41])


def test_now_template_single_message():
    text = "🔵 BUY NAS100 NOW\n\nStop Loss: 24389\n\nTake Profit Targets:\n🎯 TP1: 24545\n🎯 TP2: 24620\n🎯 TP3: 24729\n🎯 TP4: 24949"
    r = get_parser("lewis").parse(msg(text, 8), [])
    assert r.template == "lewis.now" and r.signal.entry_type == EntryType.MARKET and r.signal.sl == 24389


def test_now_split_across_two_messages():
    head = msg("🔵  SELL NAS100 NOW!", 599, T0)
    body = msg("Stop Loss: 26249.79\nTake Profit Targets:\n🎯 TP1: 26123.85\n🎯 TP2: 26103.70\n🎯 TP3: 26063.40\n🎯 TP4: 25962.65", 600, T0 + timedelta(seconds=4))
    r = get_parser("lewis").parse(body, [head])
    assert r.template == "lewis.now_split" and r.signal.side == Side.SELL and r.signal.symbol == "NAS100" and r.signal.id == "lewis:600"
    # too old a header → not combined
    stale = msg("🔵  SELL NAS100 NOW!", 599, T0 - timedelta(seconds=120))
    assert get_parser("lewis").parse(body, [stale]).signal is None


def test_limit_setup_template():
    text = "🟡 XAUUSD — BUY LIMIT SETUP\n\nEntry Zone: 5164 – 5160\n\nStop Loss: 5154\n\nTake Profit Targets:\n🎯 TP1: 5167\n🎯 TP2: 5174\n🎯 TP3: 5184\n🎯 TP4: 5204"
    r = get_parser("lewis").parse(msg(text, 16), [])
    s = r.signal
    assert r.template == "lewis.limit_setup" and s.entry_type == EntryType.LIMIT and s.entry_zone == [5164.0, 5160.0]
    assert s.side == Side.BUY and s.sl == 5154 and s.tps == [5167, 5174, 5184, 5204]


def test_non_entry_messages_do_not_parse():
    p = get_parser("lewis")
    for t in ["TP1 HIT! ✔️", "You can put your stop-loss to break-even if you wish", "Get ready for NAS100! 🚨", "Cancel this order!"]:
        assert p.parse(msg(t), []).signal is None
    assert looks_like_entry("Stop Loss: 1 TP1: 2") and not looks_like_entry("TP1 HIT!")


def test_corpus_counts_and_pinned_samples():
    p = get_parser("lewis")
    msgs = read_fixture(Path("fixtures/lewis_messages.jsonl"))
    by_template: dict[str, int] = {}
    parsed = {}
    for i, m in enumerate(msgs):
        r = p.parse(m, msgs[max(0, i - 3):i])
        if r.signal:
            by_template[r.template] = by_template.get(r.template, 0) + 1
            parsed[m.msg_id] = r.signal
    total = sum(by_template.values())
    # ~389 entry-like messages exist in the export; templates must cover the large majority.
    assert total >= 360, by_template
    assert by_template.get("lewis.trade_setup", 0) >= 250
    assert by_template.get("lewis.limit_setup", 0) >= 15
    assert by_template.get("lewis.now_split", 0) >= 10
    # pinned samples (message ids from the export)
    assert parsed[3743].symbol == "XAUUSD" and parsed[3743].side == Side.SELL and parsed[3743].sl == 4377.26 and parsed[3743].tps == [4369.94, 4368.76, 4366.42, 4360.56]
    assert parsed[3735].symbol == "NAS100" and parsed[3735].tps[0] == 29657.33
    assert parsed[16].entry_type == EntryType.LIMIT and parsed[16].entry_zone == [5164.0, 5160.0]
    assert parsed[600].side == Side.SELL and parsed[600].symbol == "NAS100"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_parser_lewis.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/normalize.py`**

```python
"""Text normalisation shared by all provider templates."""
from __future__ import annotations
import re
import unicodedata

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐⬆⬇✅❌️‍⃣]+")
_SYMBOL_ALIASES = {
    "GOLD": "XAUUSD", "XAUUSD": "XAUUSD",
    "NAS100": "NAS100", "US100": "NAS100", "USTEC": "NAS100", "NASDAQ": "NAS100", "NAS": "NAS100",
    "GER40": "GER40", "GER30": "GER40", "DE40": "GER40", "DE30": "GER40", "DAX": "GER40",
    "EURGBP": "EURGBP", "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "GBPJPY": "GBPJPY", "EURJPY": "EURJPY",
    "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD",
}


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = _EMOJI.sub(" ", s)
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"(?i)take profit targets?\s*:?", "", s)
    s = re.sub(r"(?i)stop\s*-?\s*loss\s*:?", "SL:", s)
    s = re.sub(r"(?i)\bSL\s*:?\s*(?=[\d@])", "SL: ", s)
    s = re.sub(r"(?i)\bTP\s*([1-4])(?!\d)\s*:?\s*", lambda m: f"TP{m.group(1)}: ", s)   # TP1..TP4 only; "TP 4353" is not an index
    s = re.sub(r"(?i)\bTP\s*:\s*", "TP: ", s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def canonical_symbol(s: str) -> str | None:
    return _SYMBOL_ALIASES.get(s.strip().upper())


def parse_price(s: str) -> float:
    return float(s.replace(",", "").strip())
```

- [ ] **Step 4: Write `tg_signal_trader/parsers/__init__.py`**

```python
"""Provider parsers: each turns an InboxMessage (+ a little context) into a normalised Signal."""
from __future__ import annotations
import re
from typing import Protocol
from pydantic import BaseModel
from ..models import InboxMessage, Signal
from ..normalize import normalize_text


class ParseResult(BaseModel):
    signal: Signal | None = None
    template: str | None = None
    rejected_reason: str | None = None


class Parser(Protocol):
    def parse(self, msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult: ...


_SL = re.compile(r"\bSL:\s*\d")
_TP = re.compile(r"\bTP\d?:\s*(\d|OPEN)", re.I)


def looks_like_entry(text: str) -> bool:
    n = normalize_text(text)
    return bool(_SL.search(n) and _TP.search(n))


def get_parser(provider: str) -> Parser:
    if provider == "lewis":
        from .lewis import LewisParser
        return LewisParser()
    if provider == "wolves":
        from .wolves import WolvesParser
        return WolvesParser()
    raise KeyError(provider)
```

- [ ] **Step 5: Write `tg_signal_trader/parsers/lewis.py`**

```python
"""Lewis Inner Circle templates."""
from __future__ import annotations
import re
from datetime import timedelta
from ..models import InboxMessage, Signal, Side, EntryType, complete_tps, make_signal_id
from ..normalize import normalize_text, canonical_symbol, parse_price
from . import ParseResult

_SL = re.compile(r"SL:\s*([\d.]+)")
_TP = re.compile(r"TP([1-4]):\s*([\d.]+|OPEN)", re.I)
_HEAD_SETUP = re.compile(r"^(?:TRADE SETUP|TRADE IDEA):\s*\n\s*(BUY|SELL)\s+([A-Z0-9]+)\b", re.I)
_HEAD_NOW = re.compile(r"^\s*(BUY|SELL)\s+([A-Z0-9]+)\s+NOW\b|^\s*([A-Z0-9]+)\s*-\s*(BUY|SELL)\s+NOW\b", re.I)
_HEAD_LIMIT = re.compile(r"^\s*([A-Z0-9]+)\s*-\s*(BUY|SELL)\s+LIMIT\s+SETUP", re.I)
_ZONE = re.compile(r"Entry Zone:\s*([\d.]+)\s*-\s*([\d.]+)", re.I)
SPLIT_WINDOW = timedelta(seconds=60)


def _levels(norm: str) -> tuple[float, list[float | None]] | None:
    sl = _SL.search(norm)
    tps: dict[int, float | None] = {}
    for m in _TP.finditer(norm):
        tps[int(m.group(1))] = None if m.group(2).upper() == "OPEN" else parse_price(m.group(2))
    if not sl or not all(k in tps for k in (1, 2, 3)):
        return None
    return parse_price(sl.group(1)), [tps.get(1), tps.get(2), tps.get(3), tps.get(4)]


def _build(msg: InboxMessage, template: str, symbol: str, side: str, entry_type: EntryType,
           zone: list[float], sl: float, tps: list[float | None]) -> ParseResult:
    sym = canonical_symbol(symbol)
    if sym is None:
        return ParseResult(template=template, rejected_reason=f"unknown symbol {symbol!r}")
    try:
        full = complete_tps(tps)
    except ValueError as e:
        return ParseResult(template=template, rejected_reason=str(e))
    sig = Signal(id=make_signal_id(msg.provider, msg.msg_id), provider=msg.provider, symbol=sym,
                 side=Side(side.upper()), entry_type=entry_type, entry_zone=zone, sl=sl, tps=full,
                 received_at=msg.ts, raw_text=msg.text, telegram_msg_id=msg.msg_id, parsed_by=template)
    return ParseResult(signal=sig, template=template)


class LewisParser:
    def parse(self, msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult:
        norm = normalize_text(msg.text)
        lv = _levels(norm)
        if lv is None:
            return ParseResult()
        sl, tps = lv
        m = _HEAD_SETUP.search(norm)
        if m:
            return _build(msg, "lewis.trade_setup", m.group(2), m.group(1), EntryType.MARKET, [], sl, tps)
        m = _HEAD_LIMIT.search(norm)
        if m:
            z = _ZONE.search(norm)
            if not z:
                return ParseResult(template="lewis.limit_setup", rejected_reason="no entry zone")
            return _build(msg, "lewis.limit_setup", m.group(1), m.group(2), EntryType.LIMIT,
                          [parse_price(z.group(1)), parse_price(z.group(2))], sl, tps)
        m = _HEAD_NOW.search(norm)
        if m:
            side, symbol = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            return _build(msg, "lewis.now", symbol, side, EntryType.MARKET, [], sl, tps)
        # SL/TP-only body: the header ("SELL NAS100 NOW!") was the previous message, posted moments earlier.
        if norm.startswith("SL:") and prev:
            head = prev[-1]
            if msg.ts - head.ts <= SPLIT_WINDOW:
                hm = _HEAD_NOW.search(normalize_text(head.text))
                if hm:
                    side, symbol = (hm.group(1), hm.group(2)) if hm.group(1) else (hm.group(4), hm.group(3))
                    return _build(msg, "lewis.now_split", symbol, side, EntryType.MARKET, [], sl, tps)
        return ParseResult(rejected_reason="levels found but no recognised header")
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest -q tests/test_parser_lewis.py` — Expected: `8 passed`. If the corpus assertion fails, print `by_template` and the first 10 messages that have levels but no template (`rejected_reason` set) and adjust the header regexes to cover them — never lower the thresholds without listing what was missed in the commit message.

- [ ] **Step 7: Commit**

```bash
git add tg_signal_trader/normalize.py tg_signal_trader/parsers tests/test_parser_lewis.py
git commit -m "feat(py): text normalisation and Lewis signal templates against the export corpus"
```

---

### Task 9: The Wolves parser

**Files:**
- Create: `tg_signal_trader/parsers/wolves.py`, `tests/test_parser_wolves.py`

**Interfaces:**
- Produces: `WolvesParser` with templates `wolves.at` (`BUY XAUUSD @4347` / `SELL Limit XAUUSD @4290 4295` + `SL` + `TP1..TP4 Open`), `wolves.block` (`Pair: XAUUSD / Side: Long / Buy [Limit] … / Entry: a [b] / TP: Open / SL: x / TP1 … 50pips`), `wolves.now` (`Gold buy now 4376 - 4372 / SL: / TP: / TP: / TP:`). Zone entries: `Entry: 4417 4413` → `[4417, 4413]`; `buy now 4376 - 4372` on a "now" template is MARKET (zone informational, ignored).

- [ ] **Step 1: Write the failing tests**

`tests/test_parser_wolves.py`:
```python
from datetime import datetime, timezone
from pathlib import Path
import pytest
from tg_signal_trader.models import InboxMessage, Side, EntryType
from tg_signal_trader.parsers import get_parser
from tg_signal_trader.export import read_fixture

T0 = datetime(2026, 9, 11, 15, 35, 48, tzinfo=timezone.utc)


def msg(text, mid=1):
    return InboxMessage(msg_id=mid, chat_id=-2, provider="wolves", text=text, ts=T0)


def test_at_template_market():
    text = "BUY XAUUSD @4347\n\nSL 4341\nTP1 4353\nTP2 4357\nTP3 4362\nTP4 Open"
    r = get_parser("wolves").parse(msg(text, 10), [])
    s = r.signal
    assert r.template == "wolves.at" and s.symbol == "XAUUSD" and s.side == Side.BUY and s.entry_type == EntryType.MARKET
    assert s.sl == 4341 and s.tps == [4353, 4357, 4362, 4367]


def test_at_template_limit_zone():
    text = "SELL Limit XAUUSD @4290 4295\n\nSL 4302\nTP1 4285\nTP2 4280\nTP3 4275\nTP4 Open"
    s = get_parser("wolves").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.entry_type == EntryType.LIMIT and s.entry_zone == [4290.0, 4295.0]
    assert s.tps == [4285, 4280, 4275, 4270]


def test_block_template():
    text = ("Gold 🏆\nPair: XAUUSD 📊\nSide: Long / Buy Limit 3rd entry\nEntry: 4417 4413\nTP: Open\nSL: 4400\n"
            "Note: past profits do not predict future profits\nRisk 0.5-1-2%\nTP1 4422 50pips ✅\nTP2 4427 100pips ✅\nTP3 4432 150pips ✅\nUse Proper Risk Management")
    r = get_parser("wolves").parse(msg(text, 29524), [])
    s = r.signal
    assert r.template == "wolves.block" and s.side == Side.BUY and s.entry_type == EntryType.LIMIT
    assert s.entry_zone == [4417.0, 4413.0] and s.sl == 4400 and s.tps == [4422, 4427, 4432, 4437]


def test_block_template_market_short():
    text = "Gold 🏆\nPair: XAUUSD 📊\nSide: Short / Sell\nEntry: 4402\nTP: Open\nSL: 4415\nTP1 4397 50pips ✅\nTP2 4392 100pips ✅\nTP3 4387 150pips ✅"
    s = get_parser("wolves").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.entry_type == EntryType.MARKET and s.entry_zone == [] and s.tps == [4397, 4392, 4387, 4382]


def test_now_template():
    text = "Gold sell now 2034 - 2037\n\nSL: 2040\n\nTP: 2032\nTP: 2030\nTP: 2028"
    r = get_parser("wolves").parse(msg(text), [])
    s = r.signal
    assert r.template == "wolves.now" and s.side == Side.SELL and s.entry_type == EntryType.MARKET
    assert s.sl == 2040 and s.tps == [2032, 2030, 2028, 2026]


def test_non_entry_messages():
    p = get_parser("wolves")
    for t in ["RUNNING 40PIPS 🤑🤑🤑", "Secure 50% and BE", "Move SL 4412", "Delete this", "I'm in 4364 enter now"]:
        assert p.parse(msg(t), []).signal is None


def test_corpus_counts_and_pinned_samples():
    p = get_parser("wolves")
    msgs = read_fixture(Path("fixtures/wolves_messages.jsonl"))
    by_template: dict[str, int] = {}
    parsed = {}
    for i, m in enumerate(msgs):
        r = p.parse(m, msgs[max(0, i - 3):i])
        if r.signal:
            by_template[r.template] = by_template.get(r.template, 0) + 1
            parsed[m.msg_id] = r.signal
    # ~2270 messages carry "SL:"; XAUUSD-only templates must cover most of them (crypto/FX ones are skipped by design).
    assert sum(by_template.values()) >= 1900, by_template
    assert by_template.get("wolves.block", 0) >= 1000 and by_template.get("wolves.now", 0) >= 600
    assert parsed[29603].side == Side.SELL and parsed[29603].entry_type == EntryType.LIMIT and parsed[29603].entry_zone == [4393.0, 4396.0] and parsed[29603].sl == 4408
    assert parsed[29546].side == Side.BUY and parsed[29546].entry_type == EntryType.MARKET and parsed[29546].tps == [4417, 4427, 4437, 4447]
    assert parsed[46].side == Side.SELL and parsed[46].sl == 2040 and parsed[46].tps[:3] == [2032, 2030, 2028]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_parser_wolves.py` — Expected: `ModuleNotFoundError` for `parsers.wolves`.

- [ ] **Step 3: Write `tg_signal_trader/parsers/wolves.py`**

```python
"""WolvesVIP templates (XAUUSD-only channel, several posting styles over time)."""
from __future__ import annotations
import re
from ..models import InboxMessage, Signal, Side, EntryType, complete_tps, make_signal_id
from ..normalize import normalize_text, canonical_symbol, parse_price
from . import ParseResult

_SL = re.compile(r"SL:\s*([\d.]+)")
_TPN = re.compile(r"TP([1-4]):\s*([\d.]+|OPEN)", re.I)
_TP_PLAIN = re.compile(r"^TP:\s*([\d.]+)", re.I | re.M)
_AT = re.compile(r"^\s*(BUY|SELL)\s+(LIMIT\s+)?([A-Z0-9]+)\s*@\s*([\d.]+)(?:\s+([\d.]+))?", re.I | re.M)
_PAIR = re.compile(r"Pair:\s*([A-Z0-9]+)", re.I)
_SIDE = re.compile(r"Side:\s*(?:Long|Short)?\s*/?\s*(Buy|Sell)(\s+Limit)?", re.I)
_ENTRY = re.compile(r"Entry:\s*([\d.]+)(?:\s+([\d.]+))?", re.I)
_NOW = re.compile(r"^\s*([A-Z]+)\s+(?:re-entry\s+|continue\s+)?(buy|sell)\s+now(?:\s+again)?(?:\s+slowly)?\s*([\d.]+)?\s*-?\s*([\d.]+)?", re.I | re.M)


def _sl(norm: str) -> float | None:
    m = _SL.search(norm)
    return parse_price(m.group(1)) if m else None


def _numbered_tps(norm: str) -> list[float | None] | None:
    tps: dict[int, float | None] = {}
    for m in _TPN.finditer(norm):
        tps[int(m.group(1))] = None if m.group(2).upper() == "OPEN" else parse_price(m.group(2))
    if not all(k in tps for k in (1, 2, 3)):
        return None
    return [tps.get(1), tps.get(2), tps.get(3), tps.get(4)]


def _plain_tps(norm: str) -> list[float | None] | None:
    vals = [parse_price(m.group(1)) for m in _TP_PLAIN.finditer(norm)]
    return [vals[0], vals[1], vals[2], vals[3] if len(vals) > 3 else None] if len(vals) >= 3 else None


def _build(msg: InboxMessage, template: str, symbol: str, side: str, entry_type: EntryType,
           zone: list[float], sl: float, tps: list[float | None]) -> ParseResult:
    sym = canonical_symbol(symbol)
    if sym is None:
        return ParseResult(template=template, rejected_reason=f"unknown symbol {symbol!r}")
    try:
        full = complete_tps(tps)
    except ValueError as e:
        return ParseResult(template=template, rejected_reason=str(e))
    sig = Signal(id=make_signal_id(msg.provider, msg.msg_id), provider=msg.provider, symbol=sym,
                 side=Side(side.upper()), entry_type=entry_type, entry_zone=zone, sl=sl, tps=full,
                 received_at=msg.ts, raw_text=msg.text, telegram_msg_id=msg.msg_id, parsed_by=template)
    return ParseResult(signal=sig, template=template)


class WolvesParser:
    def parse(self, msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult:
        norm = normalize_text(msg.text)
        sl = _sl(norm)
        if sl is None:
            return ParseResult()
        m = _AT.search(norm)
        if m:
            tps = _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.at", rejected_reason="TP1..TP3 missing")
            zone = [parse_price(m.group(4))] + ([parse_price(m.group(5))] if m.group(5) else [])
            limit = bool(m.group(2))
            return _build(msg, "wolves.at", m.group(3), m.group(1), EntryType.LIMIT if limit else EntryType.MARKET,
                          zone if limit else [], sl, tps)
        pair, side = _PAIR.search(norm), _SIDE.search(norm)
        if pair and side:
            tps = _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.block", rejected_reason="TP1..TP3 missing")
            e = _ENTRY.search(norm)
            zone = [parse_price(e.group(1))] + ([parse_price(e.group(2))] if e and e.group(2) else []) if e else []
            limit = bool(side.group(2))
            return _build(msg, "wolves.block", pair.group(1), side.group(1), EntryType.LIMIT if limit else EntryType.MARKET,
                          zone if limit else [], sl, tps)
        m = _NOW.search(norm)
        if m:
            tps = _plain_tps(norm) or _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.now", rejected_reason="fewer than 3 TPs")
            return _build(msg, "wolves.now", m.group(1), m.group(2), EntryType.MARKET, [], sl, tps)
        return ParseResult(rejected_reason="SL found but no recognised header")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_parser_wolves.py` — Expected: `7 passed`. Same rule as Task 8 if corpus thresholds miss: list what was missed, widen regexes, never lower thresholds silently.

- [ ] **Step 5: Commit**

```bash
git add tg_signal_trader/parsers/wolves.py tests/test_parser_wolves.py
git commit -m "feat(py): Wolves signal templates against the export corpus"
```

---

### Task 10: Validation and sizing

**Files:**
- Create: `tg_signal_trader/validation.py`, `tg_signal_trader/sizing.py`, `tests/test_validation.py`, `tests/test_sizing.py`

**Interfaces:**
- Produces (validation.py): `class Quote(BaseModel): bid: float; ask: float`; `validate_signal(sig: Signal, cfg: ProviderConfig, quote: Quote | None, now: datetime) -> list[str]` — returns the list of failed rules (empty = valid). Rules: `symbol_unmapped`, `sl_wrong_side`, `tp_wrong_side`, `tp_not_monotonic`, `sl_distance_out_of_range`, `stale` (age > max_signal_age_sec), `market_too_far` (MARKET: |mid − ref| / mid > tolerance; ref = zone[0] if given else mid — i.e. MARKET signals without a stated price only check age), `limit_too_far` (LIMIT: |near − mid| / mid > limit_max_distance_pct), `no_quote` (quote missing).
- Produces (sizing.py): `class SizingSpec(BaseModel): volume_step, volume_min, volume_max, tick_value, tick_size`; `leg_volume(balance: float, risk_pct: float, entry: float, sl: float, spec: SizingSpec) -> float` (floor to step, clamp); `sl_improves(side: Side, new_sl: float, current_sl: float) -> bool` (one-way rule; `current_sl == 0` counts as "no stop"); `sl_valid_vs_market(side, new_sl, quote) -> bool` (BUY: `new_sl < bid`; SELL: `new_sl > ask`).

- [ ] **Step 1: Write the failing tests**

`tests/test_validation.py`:
```python
from datetime import datetime, timedelta, timezone
from tg_signal_trader.models import Signal, Side, EntryType
from tg_signal_trader.config import ProviderConfig
from tg_signal_trader.validation import validate_signal, Quote

NOW = datetime(2026, 9, 18, 13, 20, 30, tzinfo=timezone.utc)
CFG = ProviderConfig(telegram_chat=1, bridge_dir="/tmp/x", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})


def sig(**kw):
    base = dict(id="w:1", provider="wolves", symbol="XAUUSD", side=Side.SELL, entry_type=EntryType.MARKET, entry_zone=[],
                sl=4377.26, tps=[4369.94, 4368.76, 4366.42, 4360.56], received_at=NOW - timedelta(seconds=20), raw_text="", telegram_msg_id=1)
    base.update(kw)
    return Signal(**base)


Q = Quote(bid=4372.0, ask=4372.4)


def test_valid_sell_market():
    assert validate_signal(sig(), CFG, Q, NOW) == []


def test_sl_and_tp_sides():
    assert "sl_wrong_side" in validate_signal(sig(sl=4360), CFG, Q, NOW)
    assert "tp_wrong_side" in validate_signal(sig(tps=[4380, 4368.76, 4366.42, 4360.56]), CFG, Q, NOW)
    assert "tp_not_monotonic" in validate_signal(sig(tps=[4369.94, 4370.5, 4366.42, 4360.56]), CFG, Q, NOW)
    buy = sig(side=Side.BUY, sl=4365, tps=[4375, 4377, 4380, 4385])
    assert validate_signal(buy, CFG, Q, NOW) == []
    assert "sl_wrong_side" in validate_signal(sig(side=Side.BUY, sl=4380, tps=[4375, 4377, 4380, 4385]), CFG, Q, NOW)


def test_sl_distance_range():
    assert "sl_distance_out_of_range" in validate_signal(sig(sl=4500), CFG, Q, NOW)          # 128 pts > 60
    assert "sl_distance_out_of_range" in validate_signal(sig(sl=4372.6), CFG, Q, NOW)        # 0.2 pt < 1


def test_stale_and_distance():
    assert "stale" in validate_signal(sig(received_at=NOW - timedelta(seconds=200)), CFG, Q, NOW)
    lim = sig(entry_type=EntryType.LIMIT, entry_zone=[4390, 4395], sl=4402, tps=[4385, 4380, 4375, 4370])
    assert validate_signal(lim, CFG, Q, NOW) == []
    far = sig(entry_type=EntryType.LIMIT, entry_zone=[4450, 4455], sl=4462, tps=[4445, 4440, 4435, 4430])
    assert "limit_too_far" in validate_signal(far, CFG, Q, NOW)
    mk = sig(entry_zone=[4347], side=Side.BUY, sl=4341, tps=[4353, 4357, 4362, 4367])
    assert "market_too_far" in validate_signal(mk, CFG, Q, NOW)   # 4347 vs 4372 is 0.57% > 0.3%


def test_unmapped_symbol_and_no_quote():
    assert "symbol_unmapped" in validate_signal(sig(symbol="NAS100"), CFG, Q, NOW)
    assert "no_quote" in validate_signal(sig(), CFG, None, NOW)
```

`tests/test_sizing.py`:
```python
import pytest
from tg_signal_trader.models import Side
from tg_signal_trader.sizing import SizingSpec, leg_volume, sl_improves, sl_valid_vs_market
from tg_signal_trader.validation import Quote

GOLD = SizingSpec(volume_step=0.01, volume_min=0.01, volume_max=50, tick_value=1.0, tick_size=0.01)   # $1 per 0.01 per lot
NAS = SizingSpec(volume_step=0.1, volume_min=0.1, volume_max=100, tick_value=0.1, tick_size=0.1)     # $1 per point per lot


def test_leg_volume_gold():
    # risk 1% of 10,000 = $100; SL 6.0 → 600 ticks × $1 = $600 per lot → 0.1666 → floor to 0.16
    assert leg_volume(10_000, 1.0, 4347, 4341, GOLD) == pytest.approx(0.16)


def test_leg_volume_nas():
    # $100 risk; SL 123.24 points → $123.24 per lot → 0.81 → floor to step 0.1 → 0.8
    assert leg_volume(10_000, 1.0, 29212.15, 29088.91, NAS) == pytest.approx(0.8)


def test_leg_volume_clamps():
    assert leg_volume(100, 1.0, 4347, 4341, GOLD) == pytest.approx(0.01)       # below min → min
    assert leg_volume(10_000_000, 1.0, 4347, 4346.9, GOLD) == pytest.approx(50)  # above max → max


def test_leg_volume_rejects_zero_distance():
    with pytest.raises(ValueError):
        leg_volume(10_000, 1.0, 4347, 4347, GOLD)


def test_sl_one_way():
    assert sl_improves(Side.BUY, 4350, 4341) and not sl_improves(Side.BUY, 4340, 4341) and sl_improves(Side.BUY, 4340, 0)
    assert sl_improves(Side.SELL, 4370, 4377) and not sl_improves(Side.SELL, 4378, 4377)


def test_sl_vs_market():
    q = Quote(bid=4372.0, ask=4372.4)
    assert sl_valid_vs_market(Side.BUY, 4371, q) and not sl_valid_vs_market(Side.BUY, 4373, q)
    assert sl_valid_vs_market(Side.SELL, 4373, q) and not sl_valid_vs_market(Side.SELL, 4372, q)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest -q tests/test_validation.py tests/test_sizing.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/validation.py`**

```python
"""Arithmetic validation of a parsed signal. Returns failed rule names; empty list = tradeable."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel
from .config import ProviderConfig
from .models import Signal, Side, EntryType


class Quote(BaseModel):
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def validate_signal(sig: Signal, cfg: ProviderConfig, quote: Quote | None, now: datetime) -> list[str]:
    fails: list[str] = []
    if sig.symbol not in cfg.symbols:
        fails.append("symbol_unmapped")
    buy = sig.side == Side.BUY
    ref = sig.entry_zone[0] if sig.entry_zone else (quote.mid if quote else None)
    if ref is not None:
        if (buy and sig.sl >= ref) or (not buy and sig.sl <= ref):
            fails.append("sl_wrong_side")
        if any((buy and tp <= ref) or (not buy and tp >= ref) for tp in sig.tps):
            fails.append("tp_wrong_side")
        dist = abs(ref - sig.sl)
        lo, hi = cfg.sl_range.get(sig.symbol, (0.0, float("inf")))
        if not (lo <= dist <= hi):
            fails.append("sl_distance_out_of_range")
    steps = [b - a for a, b in zip(sig.tps, sig.tps[1:])]
    if any((s <= 0) if buy else (s >= 0) for s in steps):
        fails.append("tp_not_monotonic")
    if (now - sig.received_at).total_seconds() > cfg.max_signal_age_sec:
        fails.append("stale")
    if quote is None:
        fails.append("no_quote")
    else:
        if sig.entry_type == EntryType.MARKET and sig.entry_zone:
            if abs(sig.entry_zone[0] - quote.mid) / quote.mid * 100 > cfg.market_entry_tolerance_pct:
                fails.append("market_too_far")
        if sig.entry_type == EntryType.LIMIT:
            near = sig.entry_zone[0] if sig.entry_zone else quote.mid
            if abs(near - quote.mid) / quote.mid * 100 > cfg.limit_max_distance_pct:
                fails.append("limit_too_far")
    return fails
```

- [ ] **Step 4: Write `tg_signal_trader/sizing.py`**

```python
"""Risk-based leg sizing and stop-loss rules."""
from __future__ import annotations
import math
from pydantic import BaseModel
from .models import Side
from .validation import Quote


class SizingSpec(BaseModel):
    volume_step: float
    volume_min: float
    volume_max: float
    tick_value: float      # account-currency value of one tick for one lot
    tick_size: float


def leg_volume(balance: float, risk_pct: float, entry: float, sl: float, spec: SizingSpec) -> float:
    dist = abs(entry - sl)
    if dist <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        raise ValueError("SL distance, tick_size and tick_value must be positive")
    risk_amount = balance * risk_pct / 100.0
    loss_per_lot = dist / spec.tick_size * spec.tick_value
    raw = risk_amount / loss_per_lot
    stepped = math.floor(raw / spec.volume_step + 1e-9) * spec.volume_step
    clamped = min(max(stepped, spec.volume_min), spec.volume_max)
    digits = max(0, -int(math.floor(math.log10(spec.volume_step))))
    return round(clamped, digits)


def sl_improves(side: Side, new_sl: float, current_sl: float) -> bool:
    if current_sl <= 0:
        return True
    return new_sl > current_sl if side == Side.BUY else new_sl < current_sl


def sl_valid_vs_market(side: Side, new_sl: float, quote: Quote) -> bool:
    return new_sl < quote.bid if side == Side.BUY else new_sl > quote.ask
```

- [ ] **Step 5: Run to verify they pass**

Run: `pytest -q` — Expected: all green (37 tests so far).

- [ ] **Step 6: Commit**

```bash
git add tg_signal_trader/validation.py tg_signal_trader/sizing.py tests/test_validation.py tests/test_sizing.py
git commit -m "feat(py): signal validation rules and risk-based leg sizing"
```

---

### Task 11: Bridge client (files) and FakeBridge

**Files:**
- Create: `tg_signal_trader/bridge.py`, `tests/test_bridge.py`

**Interfaces:**
- Produces: pydantic models mirroring `state.json`: `SymbolSpec` (+ `.quote() -> Quote`, `.sizing() -> SizingSpec`), `Account`, `Position`, `Order`, `Deal`, `BridgeState` (+ `ts_local() -> datetime` naive local, `age_sec(now_local) -> float`, `position_by_comment(c) -> Position | None`, `order_by_comment(c) -> Order | None`, `out_deals_for(position_id) -> list[Deal]`); `Command` (+ `to_line() -> str`, JSON with `None` fields omitted), `CommandResult`; `class Bridge(Protocol): send(cmd: Command) -> None; read_results() -> list[CommandResult]; read_state() -> BridgeState | None`; `FileBridge(dir: Path, results_offset: int = 0)` with attribute `results_offset` (advanced by `read_results`); `FakeBridge(now_local: datetime)` simulating an account, with test helpers `set_quote(symbol, bid, ask)`, `fill_pending(order_ticket)`, `hit_tp(position_ticket)`, `hit_sl(position_ticket)`, `close_manually(position_ticket)`, `expire_order(order_ticket)`, `fail_next(cmd_type, retcode, text)`, `sent: list[Command]`, `advance(seconds)`.
- Bridge time format: `"%Y.%m.%d %H:%M:%S"` local, naive.

- [ ] **Step 1: Write the failing tests**

`tests/test_bridge.py`:
```python
import json
from datetime import datetime
from pathlib import Path
from tg_signal_trader.bridge import (FileBridge, FakeBridge, BridgeState, Command, CommandResult, TS_FMT)

STATE = {"ts": "2026.09.18 13:20:07", "account": {"login": 1, "balance": 10000, "equity": 10050, "margin_free": 9000, "hedging": True},
         "symbols": {"XAUUSD": {"bid": 4372.0, "ask": 4372.4, "digits": 2, "point": 0.01, "volume_step": 0.01, "volume_min": 0.01,
                                "volume_max": 50, "tick_value": 1.0, "tick_size": 0.01, "trade_allowed": True}},
         "positions": [{"ticket": 11, "symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "price_open": 4370, "sl": 4360, "tp": 4380, "comment": "sig:w:1:L1", "magic": 903001}],
         "orders": [{"ticket": 12, "symbol": "XAUUSD", "type": "BUY_LIMIT", "volume": 0.1, "price": 4360, "sl": 4350, "tp": 4380, "comment": "sig:w:1:L2", "expiration": "2026.09.19 13:20:07"}],
         "deals_recent": [{"ticket": 5, "position_id": 11, "entry": "IN", "reason": "EXPERT", "price": 4370, "volume": 0.1, "time": "2026.09.18 13:10:00"}]}


def test_file_bridge_round_trip(tmp_path: Path):
    b = FileBridge(tmp_path)
    assert b.read_state() is None
    (tmp_path / "state.json").write_text(json.dumps(STATE))
    st = b.read_state()
    assert st.account.balance == 10000 and st.symbols["XAUUSD"].quote().mid == 4372.2
    assert st.ts_local() == datetime(2026, 9, 18, 13, 20, 7)
    assert st.age_sec(datetime(2026, 9, 18, 13, 20, 9)) == 2.0
    assert st.position_by_comment("sig:w:1:L1").ticket == 11 and st.order_by_comment("sig:w:1:L2").ticket == 12
    assert st.position_by_comment("nope") is None and st.out_deals_for(11) == []
    b.send(Command(cmd_id="w:1:L1:1", type="open_market", symbol="XAUUSD", side="BUY", volume=0.1, sl=4341, tp=4353, comment="sig:w:1:L1"))
    line = (tmp_path / "commands.jsonl").read_text()
    assert json.loads(line) == {"cmd_id": "w:1:L1:1", "type": "open_market", "symbol": "XAUUSD", "side": "BUY", "volume": 0.1, "sl": 4341, "tp": 4353, "comment": "sig:w:1:L1"}
    (tmp_path / "results.jsonl").write_text('{"cmd_id":"w:1:L1:1","ok":true,"retcode":10009,"retcode_text":"done","position":11,"order":11,"fill_price":4372.4,"attempts":1}\n{"cmd_id":"x","ok":fal')
    rs = b.read_results()
    assert len(rs) == 1 and rs[0].position == 11 and rs[0].ok and b.results_offset > 0
    assert b.read_results() == []                       # partial line not consumed
    (tmp_path / "results.jsonl").open("a").write('se}\n')
    assert b.read_results() == []                       # completed but unparseable → skipped, not raised
    assert b.read_state().age_sec(datetime(2026, 9, 18, 13, 20, 7)) == 0


def test_fake_bridge_simulates_lifecycle():
    fb = FakeBridge(now_local=datetime(2026, 9, 18, 13, 0, 0))
    fb.set_quote("XAUUSD", 4372.0, 4372.4)
    fb.send(Command(cmd_id="c1", type="ping"))
    fb.send(Command(cmd_id="c2", type="open_market", symbol="XAUUSD", side="BUY", volume=0.1, sl=4341, tp=4353, comment="sig:w:1:L1"))
    fb.send(Command(cmd_id="c3", type="open_pending", symbol="XAUUSD", side="SELL", volume=0.1, price=4390, sl=4402, tp=4385, comment="sig:w:2:L1", expires_at="2026.09.19 13:00:00"))
    rs = {r.cmd_id: r for r in fb.read_results()}
    assert rs["c1"].ok and rs["c2"].ok and rs["c2"].position > 0 and rs["c2"].fill_price == 4372.4 and rs["c3"].order > 0
    st = fb.read_state()
    assert len(st.positions) == 1 and len(st.orders) == 1 and st.position_by_comment("sig:w:1:L1").sl == 4341
    fb.send(Command(cmd_id="c4", type="modify_sl", position=rs["c2"].position, sl=4350))
    assert fb.read_results()[0].ok and fb.read_state().positions[0].sl == 4350
    fb.hit_tp(rs["c2"].position)
    st = fb.read_state()
    assert st.positions == [] and st.out_deals_for(rs["c2"].position)[0].reason == "TP"
    fb.fill_pending(rs["c3"].order)
    st = fb.read_state()
    assert st.orders == [] and st.position_by_comment("sig:w:2:L1").type == "SELL" and st.position_by_comment("sig:w:2:L1").price_open == 4390
    fb.fail_next("close", 10018, "Market closed")
    fb.send(Command(cmd_id="c5", type="close", position=st.position_by_comment("sig:w:2:L1").ticket))
    r = fb.read_results()[0]
    assert not r.ok and r.retcode == 10018
    fb.send(Command(cmd_id="c6", type="close", position=st.position_by_comment("sig:w:2:L1").ticket))
    assert fb.read_results()[0].ok and fb.read_state().positions == []
    fb.advance(3600)
    assert fb.read_state().ts_local() == datetime(2026, 9, 18, 14, 0, 0)
    assert [c.cmd_id for c in fb.sent] == ["c1", "c2", "c3", "c4", "c5", "c6"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_bridge.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/bridge.py`**

```python
"""Python side of the file bridge, plus an in-memory fake for tests."""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from pydantic import BaseModel, Field
from .validation import Quote
from .sizing import SizingSpec

TS_FMT = "%Y.%m.%d %H:%M:%S"


def fmt_ts(t: datetime) -> str:
    return t.strftime(TS_FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


class SymbolSpec(BaseModel):
    bid: float; ask: float; digits: int; point: float
    volume_step: float; volume_min: float; volume_max: float
    tick_value: float; tick_size: float; trade_allowed: bool

    def quote(self) -> Quote:
        return Quote(bid=self.bid, ask=self.ask)

    def sizing(self) -> SizingSpec:
        return SizingSpec(volume_step=self.volume_step, volume_min=self.volume_min, volume_max=self.volume_max,
                          tick_value=self.tick_value, tick_size=self.tick_size)


class Account(BaseModel):
    login: int; balance: float; equity: float; margin_free: float; hedging: bool


class Position(BaseModel):
    ticket: int; symbol: str; type: str; volume: float; price_open: float; sl: float; tp: float
    comment: str = ""; magic: int = 0


class Order(BaseModel):
    ticket: int; symbol: str; type: str; volume: float; price: float; sl: float; tp: float
    comment: str = ""; expiration: str = ""


class Deal(BaseModel):
    ticket: int; position_id: int; entry: str; reason: str; price: float; volume: float; time: str


class BridgeState(BaseModel):
    ts: str
    account: Account
    symbols: dict[str, SymbolSpec] = Field(default_factory=dict)
    positions: list[Position] = Field(default_factory=list)
    orders: list[Order] = Field(default_factory=list)
    deals_recent: list[Deal] = Field(default_factory=list)

    def ts_local(self) -> datetime:
        return parse_ts(self.ts)

    def age_sec(self, now_local: datetime) -> float:
        return (now_local - self.ts_local()).total_seconds()

    def position_by_comment(self, comment: str) -> Position | None:
        return next((p for p in self.positions if p.comment == comment), None)

    def order_by_comment(self, comment: str) -> Order | None:
        return next((o for o in self.orders if o.comment == comment), None)

    def out_deals_for(self, position_id: int) -> list[Deal]:
        return [d for d in self.deals_recent if d.position_id == position_id and d.entry in ("OUT", "OUT_BY")]


class Command(BaseModel):
    cmd_id: str
    type: str
    symbol: str | None = None; side: str | None = None; volume: float | None = None
    sl: float | None = None; tp: float | None = None; comment: str | None = None
    price: float | None = None; expires_at: str | None = None
    position: int | None = None; order: int | None = None

    def to_line(self) -> str:
        return json.dumps(self.model_dump(exclude_none=True), separators=(",", ":"))


class CommandResult(BaseModel):
    cmd_id: str; ok: bool; retcode: int = 0; retcode_text: str = ""
    position: int = 0; order: int = 0; fill_price: float = 0.0; attempts: int = 0


class Bridge(Protocol):
    def send(self, cmd: Command) -> None: ...
    def read_results(self) -> list[CommandResult]: ...
    def read_state(self) -> BridgeState | None: ...


class FileBridge:
    """Talks to one SignalBridge EA through its MQL5/Files/signalbridge directory."""

    def __init__(self, directory: str | Path, results_offset: int = 0):
        self.dir = Path(directory)
        self.results_offset = results_offset

    def send(self, cmd: Command) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "commands.jsonl", "ab") as f:
            f.write((cmd.to_line() + "\n").encode("utf-8"))
            f.flush()

    def read_results(self) -> list[CommandResult]:
        p = self.dir / "results.jsonl"
        if not p.exists():
            return []
        with open(p, "rb") as f:
            f.seek(self.results_offset)
            data = f.read()
        out: list[CommandResult] = []
        consumed = 0
        while True:
            nl = data.find(b"\n", consumed)
            if nl < 0:
                break
            line = data[consumed:nl].decode("utf-8", errors="replace").strip()
            consumed = nl + 1
            if not line:
                continue
            try:
                out.append(CommandResult.model_validate_json(line))
            except ValueError:
                continue    # journaled by the caller via a missing result, never raised
        self.results_offset += consumed
        return out

    def read_state(self) -> BridgeState | None:
        p = self.dir / "state.json"
        try:
            return BridgeState.model_validate_json(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class FakeBridge:
    """In-memory stand-in for a terminal + SignalBridge EA. Executes commands immediately."""

    def __init__(self, now_local: datetime, balance: float = 10_000.0, login: int = 1):
        self.now = now_local
        self.account = Account(login=login, balance=balance, equity=balance, margin_free=balance, hedging=True)
        self.symbols: dict[str, SymbolSpec] = {}
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.deals: list[Deal] = []
        self.sent: list[Command] = []
        self._results: list[CommandResult] = []
        self._fail: dict[str, tuple[int, str]] = {}
        self._ticket = 1000
        self._pending_meta: dict[int, str] = {}   # order ticket -> side

    # --- test helpers -------------------------------------------------
    def set_quote(self, symbol: str, bid: float, ask: float, **spec) -> None:
        base = dict(digits=2, point=0.01, volume_step=0.01, volume_min=0.01, volume_max=50, tick_value=1.0, tick_size=0.01, trade_allowed=True)
        base.update(spec)
        self.symbols[symbol] = SymbolSpec(bid=bid, ask=ask, **base)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def fail_next(self, cmd_type: str, retcode: int, text: str) -> None:
        self._fail[cmd_type] = (retcode, text)

    def _next_ticket(self) -> int:
        self._ticket += 1
        return self._ticket

    def _pos(self, ticket: int) -> Position | None:
        return next((p for p in self.positions if p.ticket == ticket), None)

    def _close_position(self, ticket: int, reason: str, price: float | None = None) -> None:
        p = self._pos(ticket)
        if p is None:
            return
        self.positions.remove(p)
        px = price if price is not None else (self.symbols[p.symbol].bid if p.type == "BUY" else self.symbols[p.symbol].ask)
        self.deals.append(Deal(ticket=self._next_ticket(), position_id=ticket, entry="OUT", reason=reason, price=px, volume=p.volume, time=fmt_ts(self.now)))

    def hit_tp(self, ticket: int) -> None:
        p = self._pos(ticket); self._close_position(ticket, "TP", p.tp if p else None)

    def hit_sl(self, ticket: int) -> None:
        p = self._pos(ticket); self._close_position(ticket, "SL", p.sl if p else None)

    def close_manually(self, ticket: int) -> None:
        self._close_position(ticket, "CLIENT")

    def fill_pending(self, order_ticket: int) -> None:
        o = next((o for o in self.orders if o.ticket == order_ticket), None)
        if o is None:
            return
        self.orders.remove(o)
        side = "BUY" if o.type.startswith("BUY") else "SELL"
        self.positions.append(Position(ticket=order_ticket, symbol=o.symbol, type=side, volume=o.volume, price_open=o.price, sl=o.sl, tp=o.tp, comment=o.comment, magic=903001))
        self.deals.append(Deal(ticket=self._next_ticket(), position_id=order_ticket, entry="IN", reason="EXPERT", price=o.price, volume=o.volume, time=fmt_ts(self.now)))

    def expire_order(self, order_ticket: int) -> None:
        self.orders = [o for o in self.orders if o.ticket != order_ticket]

    # --- Bridge protocol -----------------------------------------------
    def send(self, cmd: Command) -> None:
        self.sent.append(cmd)
        if cmd.type in self._fail:
            rc, text = self._fail.pop(cmd.type)
            self._results.append(CommandResult(cmd_id=cmd.cmd_id, ok=False, retcode=rc, retcode_text=text, attempts=1))
            return
        r = CommandResult(cmd_id=cmd.cmd_id, ok=True, retcode=10009, retcode_text="done", attempts=1)
        if cmd.type == "ping":
            r.attempts = 0
        elif cmd.type == "open_market":
            spec = self.symbols.get(cmd.symbol or "")
            if spec is None:
                r = CommandResult(cmd_id=cmd.cmd_id, ok=False, retcode=10014, retcode_text="unknown symbol", attempts=1)
            else:
                px = spec.ask if cmd.side == "BUY" else spec.bid
                t = self._next_ticket()
                self.positions.append(Position(ticket=t, symbol=cmd.symbol, type=cmd.side, volume=cmd.volume, price_open=px, sl=cmd.sl or 0, tp=cmd.tp or 0, comment=cmd.comment or "", magic=903001))
                self.deals.append(Deal(ticket=self._next_ticket(), position_id=t, entry="IN", reason="EXPERT", price=px, volume=cmd.volume, time=fmt_ts(self.now)))
                r.position = t; r.order = t; r.fill_price = px
        elif cmd.type == "open_pending":
            t = self._next_ticket()
            self.orders.append(Order(ticket=t, symbol=cmd.symbol, type=f"{cmd.side}_LIMIT", volume=cmd.volume, price=cmd.price, sl=cmd.sl or 0, tp=cmd.tp or 0, comment=cmd.comment or "", expiration=cmd.expires_at or ""))
            r.order = t
        elif cmd.type == "modify_sl":
            p = self._pos(cmd.position or 0)
            if p is None:
                r = CommandResult(cmd_id=cmd.cmd_id, ok=False, retcode=10036, retcode_text="position closed", attempts=1)
            else:
                p.sl = cmd.sl; r.position = p.ticket
        elif cmd.type == "close":
            p = self._pos(cmd.position or 0)
            if p is None:
                r = CommandResult(cmd_id=cmd.cmd_id, ok=False, retcode=10036, retcode_text="position closed", attempts=1)
            else:
                self._close_position(p.ticket, "EXPERT"); r.position = cmd.position
        elif cmd.type == "cancel":
            before = len(self.orders)
            self.orders = [o for o in self.orders if o.ticket != cmd.order]
            if len(self.orders) == before:
                r = CommandResult(cmd_id=cmd.cmd_id, ok=False, retcode=10013, retcode_text="order not found", attempts=1)
            else:
                r.order = cmd.order
        self._results.append(r)

    def read_results(self) -> list[CommandResult]:
        out, self._results = self._results, []
        return out

    def read_state(self) -> BridgeState | None:
        return BridgeState(ts=fmt_ts(self.now), account=self.account, symbols=dict(self.symbols),
                           positions=[p.model_copy() for p in self.positions], orders=[o.model_copy() for o in self.orders],
                           deals_recent=list(self.deals[-200:]))
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_bridge.py` — Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tg_signal_trader/bridge.py tests/test_bridge.py
git commit -m "feat(py): file bridge client, state models, and in-memory FakeBridge"
```

---

### Task 12: SQLite store

**Files:**
- Create: `tg_signal_trader/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `class Store: __init__(path: str | Path)` (creates tables; `":memory:"` allowed); `add_inbox(msg: InboxMessage) -> bool` (False if duplicate `(provider, msg_id)`); `new_inbox(provider) -> list[InboxMessage]` (status `new`, ascending `msg_id`); `set_inbox_status(provider, msg_id, status)`; `recent_inbox(provider, before_msg_id, n=3) -> list[InboxMessage]` (the n messages with smaller ids, ascending); `save_run(run: SignalRun)`; `get_run(run_id) -> SignalRun | None`; `runs(provider, states: list[RunState] | None = None) -> list[SignalRun]` (newest first); `run_by_msg_id(provider, msg_id) -> SignalRun | None`; `journal(provider, kind, detail: dict, run_id: str | None = None)`; `journal_tail(n=50) -> list[dict]`; `save_classification(provider, msg_id, text, action, price, confidence, reason, executed: bool)`; `kv_get(key) -> str | None`; `kv_set(key, value)`.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:
```python
from datetime import datetime, timezone
from tg_signal_trader.models import InboxMessage, Signal, SignalRun, Side, EntryType, RunState
from tg_signal_trader.store import Store

T = datetime(2026, 9, 18, 13, 20, 7, tzinfo=timezone.utc)


def sig(mid=1, provider="wolves"):
    return Signal(id=f"{provider}:{mid}", provider=provider, symbol="XAUUSD", side=Side.BUY, entry_type=EntryType.MARKET,
                  entry_zone=[], sl=4341, tps=[4353, 4357, 4362, 4367], received_at=T, raw_text="x", telegram_msg_id=mid)


def test_inbox_dedupe_order_and_status():
    s = Store(":memory:")
    assert s.add_inbox(InboxMessage(msg_id=5, chat_id=-1, provider="wolves", text="b", ts=T))
    assert s.add_inbox(InboxMessage(msg_id=3, chat_id=-1, provider="wolves", text="a", ts=T))
    assert not s.add_inbox(InboxMessage(msg_id=5, chat_id=-1, provider="wolves", text="dup", ts=T))
    assert [m.msg_id for m in s.new_inbox("wolves")] == [3, 5]
    s.set_inbox_status("wolves", 3, "processed")
    assert [m.msg_id for m in s.new_inbox("wolves")] == [5]
    assert [m.msg_id for m in s.recent_inbox("wolves", before_msg_id=5, n=3)] == [3]
    assert s.new_inbox("lewis") == []


def test_runs_round_trip_and_queries():
    s = Store(":memory:")
    r1 = SignalRun.from_signal(sig(1)); r2 = SignalRun.from_signal(sig(2))
    r2.state = RunState.ACTIVE
    s.save_run(r1); s.save_run(r2)
    back = s.get_run("wolves:1")
    assert back == r1 and s.get_run("nope") is None
    assert [r.id for r in s.runs("wolves")] == ["wolves:2", "wolves:1"]
    assert [r.id for r in s.runs("wolves", [RunState.ACTIVE])] == ["wolves:2"]
    assert s.run_by_msg_id("wolves", 2).id == "wolves:2" and s.run_by_msg_id("wolves", 9) is None
    r1.state = RunState.DONE; s.save_run(r1)
    assert s.get_run("wolves:1").state == RunState.DONE


def test_journal_classifications_kv():
    s = Store(":memory:")
    s.journal("wolves", "signal_rejected", {"reasons": ["stale"]}, run_id="wolves:1")
    s.journal("lewis", "command_sent", {"cmd_id": "x"})
    tail = s.journal_tail(10)
    assert [e["kind"] for e in tail] == ["command_sent", "signal_rejected"] and tail[1]["detail"]["reasons"] == ["stale"]
    s.save_classification("wolves", 7, "Delete this", "close_all", None, 0.95, "provider says delete", True)
    assert s.kv_get("k") is None
    s.kv_set("k", "v"); s.kv_set("k", "w")
    assert s.kv_get("k") == "w"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_store.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/store.py`**

```python
"""SQLite persistence: inbox, runs, journal, classifications, kv."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .models import InboxMessage, SignalRun, RunState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (provider TEXT, msg_id INTEGER, chat_id INTEGER, reply_to INTEGER, text TEXT, ts TEXT, status TEXT, PRIMARY KEY(provider, msg_id));
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, provider TEXT, state TEXT, telegram_msg_id INTEGER, data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, kind TEXT, run_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS classifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, msg_id INTEGER, text TEXT, action TEXT, price REAL, confidence REAL, reason TEXT, executed INTEGER);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None)   # autocommit
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # inbox
    def add_inbox(self, m: InboxMessage) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?)",
                                (m.provider, m.msg_id, m.chat_id, m.reply_to, m.text, m.ts.isoformat(), m.status))
        return cur.rowcount == 1

    def _row_msg(self, r: sqlite3.Row) -> InboxMessage:
        return InboxMessage(provider=r["provider"], msg_id=r["msg_id"], chat_id=r["chat_id"], reply_to=r["reply_to"],
                            text=r["text"], ts=datetime.fromisoformat(r["ts"]), status=r["status"])

    def new_inbox(self, provider: str) -> list[InboxMessage]:
        rows = self.conn.execute("SELECT * FROM inbox WHERE provider=? AND status='new' ORDER BY msg_id", (provider,))
        return [self._row_msg(r) for r in rows]

    def set_inbox_status(self, provider: str, msg_id: int, status: str) -> None:
        self.conn.execute("UPDATE inbox SET status=? WHERE provider=? AND msg_id=?", (status, provider, msg_id))

    def recent_inbox(self, provider: str, before_msg_id: int, n: int = 3) -> list[InboxMessage]:
        rows = self.conn.execute("SELECT * FROM inbox WHERE provider=? AND msg_id<? ORDER BY msg_id DESC LIMIT ?", (provider, before_msg_id, n))
        return [self._row_msg(r) for r in rows][::-1]

    # runs
    def save_run(self, run: SignalRun) -> None:
        run.updated_at = datetime.now(timezone.utc)
        self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                          (run.id, run.signal.provider, run.state.value, run.signal.telegram_msg_id, run.model_dump_json(), run.updated_at.isoformat()))

    def get_run(self, run_id: str) -> SignalRun | None:
        r = self.conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return SignalRun.model_validate_json(r["data"]) if r else None

    def runs(self, provider: str, states: list[RunState] | None = None) -> list[SignalRun]:
        if states:
            q = f"SELECT data FROM runs WHERE provider=? AND state IN ({','.join('?' * len(states))}) ORDER BY telegram_msg_id DESC"
            rows = self.conn.execute(q, (provider, *[s.value for s in states]))
        else:
            rows = self.conn.execute("SELECT data FROM runs WHERE provider=? ORDER BY telegram_msg_id DESC", (provider,))
        return [SignalRun.model_validate_json(r["data"]) for r in rows]

    def run_by_msg_id(self, provider: str, msg_id: int) -> SignalRun | None:
        r = self.conn.execute("SELECT data FROM runs WHERE provider=? AND telegram_msg_id=?", (provider, msg_id)).fetchone()
        return SignalRun.model_validate_json(r["data"]) if r else None

    # journal / classifications / kv
    def journal(self, provider: str, kind: str, detail: dict, run_id: str | None = None) -> None:
        self.conn.execute("INSERT INTO journal (ts, provider, kind, run_id, detail) VALUES (?,?,?,?,?)",
                          (_now(), provider, kind, run_id, json.dumps(detail, default=str)))

    def journal_tail(self, n: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (n,))
        return [{"ts": r["ts"], "provider": r["provider"], "kind": r["kind"], "run_id": r["run_id"], "detail": json.loads(r["detail"])} for r in rows]

    def save_classification(self, provider: str, msg_id: int, text: str, action: str, price: float | None,
                            confidence: float, reason: str, executed: bool) -> None:
        self.conn.execute("INSERT INTO classifications (ts, provider, msg_id, text, action, price, confidence, reason, executed) VALUES (?,?,?,?,?,?,?,?,?)",
                          (_now(), provider, msg_id, text, action, price, confidence, reason, int(executed)))

    def kv_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_store.py` — Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tg_signal_trader/store.py tests/test_store.py
git commit -m "feat(py): SQLite store for inbox, runs, journal, classifications"
```

---

### Task 13: Ladder — placement, fills, TP ladder, management actions

**Files:**
- Create: `tg_signal_trader/ladder.py`, `tests/test_ladder.py`
- Modify: `tg_signal_trader/models.py` — add to `Leg`: `inflight_cmd: str | None = None`, `inflight_kind: str | None = None`, `inflight_sl: float | None = None`, `missing_since: datetime | None = None`.

**Interfaces:**
- Produces: `place_run(run, cfg, state, bridge, now_local) -> list[dict]` (sizes each leg from `state`, sends `open_market`/`open_pending`, sets legs PLACING, run PLACING; returns journal events `{"kind":..., ...}`); `apply_results(run, results) -> list[dict]` (matches `inflight_cmd`; open ok → OPEN/PENDING_ORDER with tickets and `entry_price`; open fail → CANCELLED with reason; modify ok → `sl_current`; close ok → CLOSED_MANUAL; cancel ok → CANCELLED; failures journaled, state unchanged; recomputes `run.state`); `sync_run(run, cfg, state, bridge, now_local) -> list[dict]` (fill/close detection from state, pending expiry backstop, ladder moves, cancel-pendings-on-SL, `run.state`); `apply_action(run, action, price, state, bridge, cfg) -> list[dict]` for `close_all | cancel_pending | move_sl | break_even`.
- Every command sent is recorded on its leg (`inflight_*`) and gets a journal event `{"kind": "command_sent", "cmd_id", "type", "leg"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ladder.py`:
```python
from datetime import datetime, timezone
import pytest
from tg_signal_trader.models import Signal, SignalRun, Side, EntryType, LegState, RunState
from tg_signal_trader.config import ProviderConfig
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.ladder import place_run, apply_results, sync_run, apply_action

NOW = datetime(2026, 9, 18, 13, 0, 0)
CFG = ProviderConfig(telegram_chat=1, bridge_dir="/tmp/x", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)}, pending_ttl_hours=24)


def mk(side=Side.BUY, entry_type=EntryType.MARKET, zone=None, sl=4341.0, tps=(4353.0, 4357.0, 4362.0, 4367.0), mid=1):
    sig = Signal(id=f"wolves:{mid}", provider="wolves", symbol="XAUUSD", side=side, entry_type=entry_type, entry_zone=zone or [],
                 sl=sl, tps=list(tps), received_at=datetime(2026, 9, 18, 12, 59, 50, tzinfo=timezone.utc), raw_text="", telegram_msg_id=mid)
    return SignalRun.from_signal(sig)


def bridge():
    fb = FakeBridge(now_local=NOW, balance=10_000)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    return fb


def pump(run, fb):
    """Deliver results, then sync against state — one trader tick."""
    ev = apply_results(run, fb.read_results())
    ev += sync_run(run, CFG, fb.read_state(), fb, fb.now)
    return ev


def test_market_placement_sizes_and_opens_four_legs():
    fb, run = bridge(), mk()
    ev = place_run(run, CFG, fb.read_state(), fb, fb.now)
    assert run.state == RunState.PLACING and len(fb.sent) == 4 and all(c.type == "open_market" for c in fb.sent)
    # $100 risk / (6.0 / 0.01 * 1.0 = $600 per lot) = 0.1666 → 0.16 lots per leg
    assert [c.volume for c in fb.sent] == [0.16] * 4 and [c.tp for c in fb.sent] == [4353, 4357, 4362, 4367] and fb.sent[0].sl == 4341
    assert fb.sent[2].comment == "sig:wolves:1:L3" and fb.sent[0].cmd_id == "wolves:1:L1:1"
    pump(run, fb)
    assert run.state == RunState.ACTIVE and [l.state for l in run.legs] == [LegState.OPEN] * 4
    assert all(l.position_ticket and l.entry_price == 4347.0 for l in run.legs)
    assert [e["kind"] for e in ev][:1] == ["command_sent"]


def test_ladder_moves_be_after_tp2_and_tp1_after_tp3():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.hit_tp(run.legs[0].position_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.CLOSED_TP and [l.sl_current for l in run.legs[1:]] == [4341] * 3 and not run.be_applied
    fb.hit_tp(run.legs[1].position_ticket); pump(run, fb)
    assert run.be_applied and [c.type for c in fb.sent[4:]] == ["modify_sl", "modify_sl"]
    pump(run, fb)                              # deliver modify results
    assert run.legs[2].sl_current == 4347.0 and run.legs[3].sl_current == 4347.0   # own entry (break-even)
    st = fb.read_state()
    assert st.position_by_comment("sig:wolves:1:L3").sl == 4347.0
    fb.hit_tp(run.legs[2].position_ticket); pump(run, fb); pump(run, fb)
    assert run.tp1_applied and run.legs[3].sl_current == 4353.0                    # TP1 price
    fb.hit_tp(run.legs[3].position_ticket); pump(run, fb)
    assert run.state == RunState.DONE and run.is_finished()


def test_sl_hit_and_manual_close_are_classified():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.hit_sl(run.legs[0].position_ticket); fb.close_manually(run.legs[1].position_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.CLOSED_SL and run.legs[1].state == LegState.CLOSED_MANUAL


def test_sl_moves_are_one_way():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    # Simulate the broker already having a better stop on L3 (e.g. moved by hand) than break-even
    fb.read_state().position_by_comment("sig:wolves:1:L3")  # exists
    run.legs[2].sl_current = 4350.0
    fb.hit_tp(run.legs[0].position_ticket); fb.hit_tp(run.legs[1].position_ticket); pump(run, fb)
    sent = [c for c in fb.sent if c.type == "modify_sl"]
    assert len(sent) == 1 and sent[0].comment is None and sent[0].position == run.legs[3].position_ticket   # L3 not worsened


def test_pending_placement_fill_expiry_and_cancel_on_sl():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0, 4395.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=2)
    place_run(run, CFG, fb.read_state(), fb, fb.now)
    assert all(c.type == "open_pending" and c.price == 4390.0 and c.expires_at == "2026.09.19 12:59:50" for c in fb.sent)
    # sizing uses the zone price: $100 / (12.0/0.01*1) = 0.0833 → 0.08
    assert fb.sent[0].volume == pytest.approx(0.08)
    pump(run, fb)
    assert [l.state for l in run.legs] == [LegState.PENDING_ORDER] * 4 and run.state == RunState.ACTIVE
    fb.fill_pending(run.legs[0].order_ticket); fb.fill_pending(run.legs[1].order_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.OPEN and run.legs[0].entry_price == 4390.0 and run.legs[0].position_ticket == run.legs[0].order_ticket
    fb.expire_order(run.legs[2].order_ticket); pump(run, fb)
    assert run.legs[2].state == LegState.CANCELLED and "gone" in run.legs[2].reason
    fb.hit_sl(run.legs[0].position_ticket); pump(run, fb)
    assert run.pendings_cancelled and any(c.type == "cancel" and c.order == run.legs[3].order_ticket for c in fb.sent)
    pump(run, fb)
    assert run.legs[3].state == LegState.CANCELLED


def test_pending_ttl_backstop_cancels_after_expiry():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=3)
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.advance(24 * 3600 + 120); pump(run, fb)
    assert sum(1 for c in fb.sent if c.type == "cancel") == 4


def test_failed_leg_does_not_block_others():
    fb, run = bridge(), mk()
    fb.fail_next("open_market", 10019, "No money")
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    assert run.legs[0].state == LegState.CANCELLED and "No money" in run.legs[0].reason
    assert [l.state for l in run.legs[1:]] == [LegState.OPEN] * 3 and run.state == RunState.ACTIVE


def test_management_actions():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    st = fb.read_state()
    assert apply_action(run, "move_sl", 4400.0, st, fb, CFG)[0]["kind"] == "action_rejected"      # BUY: SL above bid is invalid
    assert apply_action(run, "move_sl", 4340.0, st, fb, CFG)[0]["kind"] == "action_rejected"      # worse than current 4341
    ev = apply_action(run, "move_sl", 4345.0, st, fb, CFG); pump(run, fb)
    assert all(l.sl_current == 4345.0 for l in run.legs) and len([e for e in ev if e["kind"] == "command_sent"]) == 4
    apply_action(run, "break_even", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert all(l.sl_current == 4347.0 for l in run.legs)
    apply_action(run, "close_all", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert run.state == RunState.DONE and all(l.state == LegState.CLOSED_MANUAL for l in run.legs) and fb.read_state().positions == []


def test_cancel_pending_action_keeps_open_legs():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=4)
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.fill_pending(run.legs[0].order_ticket); pump(run, fb)
    apply_action(run, "cancel_pending", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert run.legs[0].state == LegState.OPEN and [l.state for l in run.legs[1:]] == [LegState.CANCELLED] * 3 and run.state == RunState.ACTIVE
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_ladder.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Add the inflight/missing fields to `Leg` in `models.py`**

```python
    inflight_cmd: str | None = None
    inflight_kind: str | None = None
    inflight_sl: float | None = None
    missing_since: datetime | None = None
```
(inside `class Leg`, after `reason`). Re-run `pytest -q tests/test_models.py tests/test_store.py` — still green.

- [ ] **Step 4: Write `tg_signal_trader/ladder.py`**

```python
"""SignalRun state machine: placement, fill/close detection, the TP ladder, management actions."""
from __future__ import annotations
from datetime import datetime, timedelta
from .bridge import Bridge, BridgeState, Command, CommandResult, fmt_ts, parse_ts
from .config import ProviderConfig
from .models import Leg, LegState, RunState, Side, EntryType, SignalRun
from .sizing import leg_volume, sl_improves, sl_valid_vs_market

MISSING_GRACE_SEC = 30      # position absent from state without a deal → CLOSED_MANUAL after this


def _ev(kind: str, run: SignalRun, **detail) -> dict:
    return {"kind": kind, "run_id": run.id, **detail}


def _send(run: SignalRun, leg: Leg, bridge: Bridge, cmd_type: str, **fields) -> dict:
    cmd = Command(cmd_id=run.next_cmd_id(leg.n), type=cmd_type, **fields)
    leg.inflight_cmd, leg.inflight_kind = cmd.cmd_id, cmd_type
    leg.inflight_sl = fields.get("sl") if cmd_type == "modify_sl" else None
    bridge.send(cmd)
    return _ev("command_sent", run, cmd_id=cmd.cmd_id, type=cmd_type, leg=leg.n, fields={k: v for k, v in fields.items() if v is not None})


def _recompute_state(run: SignalRun) -> None:
    if run.state == RunState.REJECTED:
        return
    if run.is_finished():
        run.state = RunState.DONE
    elif run.placing_legs() and not run.open_legs() and not run.pending_legs():
        run.state = RunState.PLACING
    else:
        run.state = RunState.ACTIVE


def place_run(run: SignalRun, cfg: ProviderConfig, state: BridgeState, bridge: Bridge, now_local: datetime) -> list[dict]:
    sig = run.signal
    broker_symbol = cfg.symbols[sig.symbol]
    spec = state.symbols[broker_symbol]
    is_buy = sig.side == Side.BUY
    if sig.entry_type == EntryType.LIMIT:
        entry_ref = sig.entry_zone[0]
    else:
        entry_ref = spec.ask if is_buy else spec.bid
    events: list[dict] = []
    expires = fmt_ts(sig.received_at.astimezone().replace(tzinfo=None) + timedelta(hours=cfg.pending_ttl_hours)) if sig.entry_type == EntryType.LIMIT else None
    for leg in run.legs:
        try:
            leg.volume = leg_volume(state.account.balance, cfg.risk_pct_per_leg, entry_ref, sig.sl, spec.sizing())
        except ValueError as e:
            leg.state, leg.reason = LegState.CANCELLED, f"sizing: {e}"
            events.append(_ev("leg_cancelled", run, leg=leg.n, reason=leg.reason)); continue
        comment = leg.comment(sig.id)
        if sig.entry_type == EntryType.MARKET:
            events.append(_send(run, leg, bridge, "open_market", symbol=broker_symbol, side=sig.side.value, volume=leg.volume,
                                sl=sig.sl, tp=leg.tp, comment=comment))
        else:
            events.append(_send(run, leg, bridge, "open_pending", symbol=broker_symbol, side=sig.side.value, volume=leg.volume,
                                price=entry_ref, sl=sig.sl, tp=leg.tp, comment=comment, expires_at=expires))
    run.state = RunState.PLACING
    if not run.placing_legs():
        _recompute_state(run)
    return events


def apply_results(run: SignalRun, results: list[CommandResult]) -> list[dict]:
    events: list[dict] = []
    by_id = {r.cmd_id: r for r in results}
    for leg in run.legs:
        r = by_id.get(leg.inflight_cmd or "")
        if r is None:
            continue
        kind = leg.inflight_kind
        leg.inflight_cmd = leg.inflight_kind = None
        if not r.ok:
            events.append(_ev("command_failed", run, cmd_id=r.cmd_id, type=kind, leg=leg.n, retcode=r.retcode, text=r.retcode_text))
            if kind in ("open_market", "open_pending"):
                leg.state, leg.reason = LegState.CANCELLED, f"{kind} failed: {r.retcode} {r.retcode_text}"
            continue
        if kind == "open_market":
            leg.state, leg.position_ticket, leg.entry_price = LegState.OPEN, r.position or r.order, r.fill_price or None
        elif kind == "open_pending":
            leg.state, leg.order_ticket = LegState.PENDING_ORDER, r.order
        elif kind == "modify_sl" and leg.inflight_sl is not None:
            leg.sl_current = leg.inflight_sl
        elif kind == "close":
            leg.state, leg.reason = LegState.CLOSED_MANUAL, "closed by command"
        elif kind == "cancel":
            leg.state, leg.reason = LegState.CANCELLED, "cancelled by command"
        leg.inflight_sl = None
    _recompute_state(run)
    return events


def _detect(run: SignalRun, state: BridgeState, now_local: datetime) -> list[dict]:
    events: list[dict] = []
    for leg in run.legs:
        comment = leg.comment(run.signal.id)
        if leg.state == LegState.PENDING_ORDER and leg.inflight_cmd is None:
            order = next((o for o in state.orders if o.ticket == leg.order_ticket), None)
            if order is None:
                pos = state.position_by_comment(comment)
                if pos is not None:
                    leg.state, leg.position_ticket, leg.entry_price = LegState.OPEN, pos.ticket, pos.price_open
                    events.append(_ev("leg_filled", run, leg=leg.n, price=pos.price_open))
                else:
                    leg.state, leg.reason = LegState.CANCELLED, "pending order gone (expired or cancelled at broker)"
                    events.append(_ev("leg_cancelled", run, leg=leg.n, reason=leg.reason))
        elif leg.state == LegState.OPEN and leg.inflight_cmd is None:
            pos = next((p for p in state.positions if p.ticket == leg.position_ticket), None)
            if pos is not None:
                leg.missing_since = None
                if leg.entry_price is None:
                    leg.entry_price = pos.price_open
                continue
            deals = state.out_deals_for(leg.position_ticket or -1)
            if deals:
                reason = deals[-1].reason
                leg.state = LegState.CLOSED_TP if reason == "TP" else LegState.CLOSED_SL if reason == "SL" else LegState.CLOSED_MANUAL
                leg.reason = f"deal reason {reason}"
                events.append(_ev("leg_closed", run, leg=leg.n, state=leg.state.value, price=deals[-1].price))
            elif leg.missing_since is None:
                leg.missing_since = now_local
            elif (now_local - leg.missing_since).total_seconds() > MISSING_GRACE_SEC:
                leg.state, leg.reason = LegState.CLOSED_MANUAL, "position vanished without a deal"
                events.append(_ev("leg_closed", run, leg=leg.n, state=leg.state.value))
    return events


def _modify(run: SignalRun, leg: Leg, bridge: Bridge, new_sl: float, why: str) -> dict | None:
    if not sl_improves(run.signal.side, new_sl, leg.sl_current):
        return _ev("sl_move_skipped", run, leg=leg.n, proposed=new_sl, current=leg.sl_current, why=why)
    return _send(run, leg, bridge, "modify_sl", position=leg.position_ticket, sl=new_sl)


def _ladder(run: SignalRun, bridge: Bridge) -> list[dict]:
    events: list[dict] = []
    legs = run.legs
    if len(legs) >= 2 and legs[1].state == LegState.CLOSED_TP and not run.be_applied:
        for leg in legs[2:]:
            if leg.state == LegState.OPEN and leg.entry_price:
                ev = _modify(run, leg, bridge, leg.entry_price, "break-even after TP2")
                if ev: events.append(ev)
        run.be_applied = True
    if len(legs) >= 3 and legs[2].state == LegState.CLOSED_TP and not run.tp1_applied:
        for leg in legs[3:]:
            if leg.state == LegState.OPEN:
                ev = _modify(run, leg, bridge, run.signal.tps[0], "SL to TP1 after TP3")
                if ev: events.append(ev)
        run.tp1_applied = True
    if any(l.state == LegState.CLOSED_SL for l in legs) and run.pending_legs() and not run.pendings_cancelled:
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    return events


def _expiry_backstop(run: SignalRun, cfg: ProviderConfig, bridge: Bridge, now_local: datetime) -> list[dict]:
    if run.signal.entry_type != EntryType.LIMIT:
        return []
    deadline = run.signal.received_at.astimezone().replace(tzinfo=None) + timedelta(hours=cfg.pending_ttl_hours, minutes=1)
    if now_local < deadline:
        return []
    return [_send(run, leg, bridge, "cancel", order=leg.order_ticket) for leg in run.pending_legs() if leg.inflight_cmd is None]


def sync_run(run: SignalRun, cfg: ProviderConfig, state: BridgeState, bridge: Bridge, now_local: datetime) -> list[dict]:
    events = _detect(run, state, now_local)
    events += _ladder(run, bridge)
    events += _expiry_backstop(run, cfg, bridge, now_local)
    _recompute_state(run)
    return events


def apply_action(run: SignalRun, action: str, price: float | None, state: BridgeState, bridge: Bridge, cfg: ProviderConfig) -> list[dict]:
    events: list[dict] = []
    if action == "close_all":
        for leg in run.open_legs():
            events.append(_send(run, leg, bridge, "close", position=leg.position_ticket))
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    elif action == "cancel_pending":
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    elif action == "break_even":
        for leg in run.open_legs():
            if leg.entry_price:
                ev = _modify(run, leg, bridge, leg.entry_price, "break-even by provider")
                if ev: events.append(ev)
    elif action == "move_sl":
        if price is None:
            return [_ev("action_rejected", run, action=action, why="no price")]
        quote = state.symbols[cfg.symbols[run.signal.symbol]].quote()
        if not sl_valid_vs_market(run.signal.side, price, quote):
            return [_ev("action_rejected", run, action=action, why=f"SL {price} on the wrong side of market {quote.bid}/{quote.ask}")]
        if not any(sl_improves(run.signal.side, price, l.sl_current) for l in run.open_legs()):
            return [_ev("action_rejected", run, action=action, why=f"SL {price} does not improve any leg")]
        for leg in run.open_legs():
            ev = _modify(run, leg, bridge, price, "provider move_sl")
            if ev: events.append(ev)
    else:
        return [_ev("action_rejected", run, action=action, why="unknown action")]
    return events
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest -q tests/test_ladder.py` — Expected: `9 passed`. If `test_pending_placement…` fails on `expires_at`, note the tz handling: `received_at` is UTC-aware, `astimezone()` converts to the machine's local zone before `replace(tzinfo=None)`; on a machine whose local zone is not UTC the expected string differs — set `TZ=UTC` for the test run (`TZ=UTC pytest -q`) and add that to `pyproject.toml`'s pytest config as `env` is not available; instead put `os.environ.setdefault("TZ", "UTC"); time.tzset()` at the top of `tests/conftest.py`.

- [ ] **Step 6: Commit**

```bash
git add tg_signal_trader/models.py tg_signal_trader/ladder.py tests/test_ladder.py tests/conftest.py
git commit -m "feat(py): signal run state machine with placement, fills, TP ladder and management actions"
```

---

### Task 14: LLM classifier (management messages + entry fallback)

**Files:**
- Create: `tg_signal_trader/classifier.py`, `tests/test_classifier.py`

**Interfaces:**
- Produces: `class Classification(BaseModel): action: Literal["none","close_all","cancel_pending","move_sl","break_even"]; price: float | None = None; confidence: float; reason: str = ""`; `class EntryExtraction(BaseModel): symbol: str; side: Literal["BUY","SELL"]; entry_type: Literal["MARKET","LIMIT"]; entry_zone: list[float]; sl: float; tps: list[float | None]; confidence: float`; `class RunContext(BaseModel): symbol, side, entry_type, entry_zone: list[float], sl, open_legs: int, pending_legs: int, sl_current: float`; `class Classifier(Protocol): classify_management(text, provider, ctx: RunContext | None, recent: list[str]) -> Classification; extract_entry(text, provider) -> EntryExtraction | None`; `ClaudeClassifier(model: str, timeout_sec: float, api_key: str | None = None, client=None)` (a `client` argument allows injecting a fake with `.with_options(...).messages.parse(...)`); `MockClassifier(management: dict[str, Classification] | None, entries: dict[str, EntryExtraction] | None)` returning `Classification(action="none", confidence=0)` for unknown text; `NONE = Classification(action="none", confidence=0.0, reason="")`.
- Any exception from the SDK (`anthropic.APITimeoutError`, `APIConnectionError`, `RateLimitError`, `APIStatusError`) or a schema failure returns `Classification(action="none", confidence=0, reason="error: <ExceptionName>")` / `None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_classifier.py`:
```python
import anthropic, httpx, pytest
from tg_signal_trader.classifier import ClaudeClassifier, MockClassifier, Classification, EntryExtraction, RunContext, NONE

CTX = RunContext(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, open_legs=3, pending_legs=0, sl_current=4341)


class FakeMessages:
    def __init__(self, outcome): self.outcome = outcome; self.calls = []
    def parse(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception): raise self.outcome
        class R: parsed_output = self.outcome
        return R()


class FakeClient:
    def __init__(self, outcome): self.messages = FakeMessages(outcome); self.opts = None
    def with_options(self, **kw): self.opts = kw; return self


def test_management_happy_path_passes_context_and_uses_parse():
    c = FakeClient(Classification(action="move_sl", price=4412, confidence=0.93, reason="explicit SL price"))
    cl = ClaudeClassifier(model="claude-opus-5", timeout_sec=8, client=c)
    out = cl.classify_management("Move SL 4412", "wolves", CTX, ["RUNNING 40PIPS"])
    assert out.action == "move_sl" and out.price == 4412 and out.confidence == 0.93
    kw = c.messages.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["output_format"] is Classification and c.opts == {"timeout": 8, "max_retries": 1}
    user = kw["messages"][0]["content"]
    assert "Move SL 4412" in user and "XAUUSD" in user and "RUNNING 40PIPS" in user and "wolves" in user


def test_management_errors_become_none():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in [anthropic.APITimeoutError(request=req), anthropic.APIConnectionError(request=req),
                anthropic.RateLimitError("rl", response=httpx.Response(429, request=req), body=None),
                anthropic.APIStatusError("boom", response=httpx.Response(500, request=req), body=None), ValueError("schema")]:
        out = ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(exc)).classify_management("x", "wolves", None, [])
        assert out.action == "none" and out.confidence == 0 and out.reason.startswith("error:")


def test_entry_extraction_and_error():
    ex = EntryExtraction(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.9)
    assert ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(ex)).extract_entry("buy gold 4347 sl 4341 tp 4353 4357 4362", "wolves") == ex
    req = httpx.Request("POST", "https://x")
    assert ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(anthropic.APITimeoutError(request=req))).extract_entry("x", "wolves") is None


def test_mock_classifier():
    m = MockClassifier(management={"Delete this": Classification(action="close_all", confidence=0.95)})
    assert m.classify_management("Delete this", "wolves", None, []).action == "close_all"
    assert m.classify_management("hello", "wolves", None, []) == NONE
    assert m.extract_entry("x", "wolves") is None


def test_schema_rejects_unknown_action():
    with pytest.raises(ValueError):
        Classification(action="secure_half", confidence=1.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_classifier.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/classifier.py`**

```python
"""Claude-backed classification of free-form provider messages (management) and entry fallback."""
from __future__ import annotations
from typing import Literal, Protocol
import anthropic
from pydantic import BaseModel, Field

Action = Literal["none", "close_all", "cancel_pending", "move_sl", "break_even"]


class Classification(BaseModel):
    action: Action
    price: float | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class EntryExtraction(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_type: Literal["MARKET", "LIMIT"]
    entry_zone: list[float] = Field(default_factory=list)
    sl: float
    tps: list[float | None]
    confidence: float = Field(ge=0, le=1)


class RunContext(BaseModel):
    symbol: str; side: str; entry_type: str; entry_zone: list[float] = Field(default_factory=list)
    sl: float; open_legs: int; pending_legs: int; sl_current: float


NONE = Classification(action="none", confidence=0.0, reason="")

MANAGEMENT_SYSTEM = """You classify messages from a Telegram trading-signal provider into ONE management action
for a copy-trading robot. The robot already opened the trade described in the context. Be conservative:
if the message is commentary, hype, a profit screenshot caption, a question, or ambiguous, answer "none".
Actions:
- close_all: the provider closes/exits the whole trade now ("delete this", "out at BE", "closing", "secure 100%", "cancel and close").
- cancel_pending: cancel unfilled pending/limit orders only ("cancel this limit", "delete the pending", "price ran, cancel").
- move_sl: move the stop loss to an explicit price the message states (put it in "price").
- break_even: move the stop loss to the entry price ("BE", "break even", "risk free", "SL to entry").
Typos and slang are common ("delte", "im be now", "sl 4412 guys"). "TP hit", "running +40 pips", "secure some profits" are NOT actions → "none".
confidence is your probability that the action is what the provider means for THIS trade."""

ENTRY_SYSTEM = """Extract a trade signal from a Telegram message if, and only if, it clearly states a direction, a symbol,
a stop loss and at least three take-profit levels. Use MARKET unless the message says limit/zone/pending, in which case
entry_type is LIMIT and entry_zone holds the stated price(s). tps must list TP1..TP4 in order, using null for an "open" TP.
If any of side, symbol, sl or three TPs is missing, set confidence to 0."""


class Classifier(Protocol):
    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification: ...
    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None: ...


class ClaudeClassifier:
    def __init__(self, model: str, timeout_sec: float, api_key: str | None = None, client=None):
        self.model, self.timeout = model, timeout_sec
        self.client = client or anthropic.Anthropic(api_key=api_key)

    def _parse(self, system: str, user: str, schema):
        return self.client.with_options(timeout=self.timeout, max_retries=1).messages.parse(
            model=self.model, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user}], output_format=schema).parsed_output

    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification:
        context = ctx.model_dump_json() if ctx else "no open trade"
        user = (f"Provider: {provider}\nOpen trade context: {context}\nRecent provider messages (oldest first):\n"
                + "\n".join(f"- {r}" for r in recent) + f"\n\nMessage to classify:\n{text}")
        try:
            return self._parse(MANAGEMENT_SYSTEM, user, Classification)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.APIStatusError, ValueError) as e:
            return Classification(action="none", confidence=0.0, reason=f"error: {type(e).__name__}")

    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None:
        try:
            return self._parse(ENTRY_SYSTEM, f"Provider: {provider}\nMessage:\n{text}", EntryExtraction)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.APIStatusError, ValueError):
            return None


class MockClassifier:
    def __init__(self, management: dict[str, Classification] | None = None, entries: dict[str, EntryExtraction] | None = None):
        self.management = management or {}
        self.entries = entries or {}
        self.calls: list[tuple[str, str]] = []

    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification:
        self.calls.append(("management", text))
        return self.management.get(text, NONE)

    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None:
        self.calls.append(("entry", text))
        return self.entries.get(text)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_classifier.py` — Expected: `5 passed`. If constructing `anthropic.RateLimitError`/`APIStatusError` in the test fails on the installed SDK's signature, construct them with keyword `message=` per the SDK's `_exceptions.py` and note it.

- [ ] **Step 5: Commit**

```bash
git add tg_signal_trader/classifier.py tests/test_classifier.py
git commit -m "feat(py): Claude classifier for management messages and entry fallback"
```

---

### Task 15: Trader orchestration (inbox → runs, guards, reference resolution)

**Files:**
- Create: `tg_signal_trader/trader.py`, `tests/test_trader.py`

**Interfaces:**
- Produces: `class DryRunBridge(inner: Bridge, store: Store, provider: str)` — `send` journals `{"kind":"dry_run_command", ...}` and fabricates an `ok` result so state machines advance (positions never appear in `state`, so runs stay PLACING→ACTIVE with fake tickets); `read_state` passes through. `class Trader: __init__(cfg: AppConfig, store: Store, bridges: dict[str, Bridge], classifier: Classifier, now_utc=None, now_local=None)` (clock callables injectable); `tick() -> None` (one loop iteration over all providers); `process_inbox(provider)`, `sync(provider)`; `resolve_reference(provider, msg: InboxMessage) -> SignalRun | None`; `guard_reasons(provider, state: BridgeState) -> list[str]`; `startup_reconcile()` (ACTIVE runs: adopt tickets by comment; PLACING legs whose comment is already in state → OPEN/PENDING).
- Daily loss tracking: kv key `sod_equity:<provider>:<YYYY-MM-DD local>` set to `state.account.equity` when first seen that day; `loss_pct = (sod − equity)/sod×100`; block when `≥ daily_loss_stop_pct` (journal `guard_blocked` once per signal).

- [ ] **Step 1: Write the failing tests**

`tests/test_trader.py`:
```python
from datetime import datetime, timedelta, timezone
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.models import InboxMessage, LegState, RunState
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.store import Store
from tg_signal_trader.classifier import MockClassifier, Classification, EntryExtraction
from tg_signal_trader.trader import Trader, DryRunBridge

UTC0 = datetime(2026, 9, 18, 13, 0, 0, tzinfo=timezone.utc)
LOCAL0 = datetime(2026, 9, 18, 13, 0, 0)
ENTRY = "BUY XAUUSD @4347\n\nSL 4341\nTP1 4353\nTP2 4357\nTP3 4362\nTP4 Open"


def make(management=None, entries=None, **provider_overrides):
    pc = dict(telegram_chat=-2, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})
    pc.update(provider_overrides)
    cfg = AppConfig(providers={"wolves": ProviderConfig(**pc)}, db_path=":memory:")
    store = Store(":memory:")
    fb = FakeBridge(now_local=LOCAL0, balance=10_000)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    clock = {"utc": UTC0, "local": LOCAL0}
    tr = Trader(cfg, store, {"wolves": fb}, MockClassifier(management, entries), now_utc=lambda: clock["utc"], now_local=lambda: clock["local"])
    return tr, store, fb, clock


def inbox(store, text, mid, ts=None, reply_to=None):
    store.add_inbox(InboxMessage(msg_id=mid, chat_id=-2, provider="wolves", text=text, ts=ts or UTC0 - timedelta(seconds=5), reply_to=reply_to))


def test_entry_message_becomes_active_run():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 10)
    tr.tick(); tr.tick()
    run = store.get_run("wolves:10")
    assert run.state == RunState.ACTIVE and [l.state for l in run.legs] == [LegState.OPEN] * 4
    assert store.new_inbox("wolves") == [] and any(e["kind"] == "signal_accepted" for e in store.journal_tail())


def test_stale_entry_is_not_traded():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 11, ts=UTC0 - timedelta(seconds=500))
    tr.tick()
    assert store.get_run("wolves:11").state == RunState.REJECTED and fb.sent == []
    assert any(e["kind"] == "signal_rejected" and "stale" in e["detail"]["reasons"] for e in store.journal_tail())


def test_guards_block_placement():
    tr, store, fb, clock = make(max_open_signals=1)
    inbox(store, ENTRY, 12); tr.tick(); tr.tick()
    inbox(store, ENTRY.replace("4347", "4348"), 13); tr.tick()
    assert store.get_run("wolves:13").state == RunState.REJECTED
    assert any(e["kind"] == "guard_blocked" and "max_open_signals" in e["detail"]["reasons"] for e in store.journal_tail())
    # stale terminal
    clock["local"] = LOCAL0 + timedelta(seconds=30)
    inbox(store, ENTRY, 14, ts=clock["utc"]); tr.tick()
    assert "terminal_stale" in tr.guard_reasons("wolves", fb.read_state())


def test_daily_loss_stop():
    tr, store, fb, _ = make(daily_loss_stop_pct=5.0)
    tr.tick()                                   # records start-of-day equity 10,000
    fb.account.equity = 9_400
    inbox(store, ENTRY, 15); tr.tick()
    assert store.get_run("wolves:15").state == RunState.REJECTED and fb.sent == []


def test_management_reply_applies_to_referenced_run():
    tr, store, fb, _ = make(management={"Delete this": Classification(action="close_all", confidence=0.95, reason="delete")})
    inbox(store, ENTRY, 20); tr.tick(); tr.tick()
    inbox(store, "Delete this", 21, reply_to=20); tr.tick(); tr.tick()
    assert store.get_run("wolves:20").state == RunState.DONE and fb.read_state().positions == []


def test_management_without_reply_uses_single_active_run_only():
    tr, store, fb, _ = make(management={"im be now": Classification(action="break_even", confidence=0.9)}, max_open_signals=3)
    inbox(store, ENTRY, 30); tr.tick(); tr.tick()
    inbox(store, "im be now", 31); tr.tick(); tr.tick()
    assert all(l.sl_current == 4347.0 for l in store.get_run("wolves:30").legs)
    inbox(store, ENTRY.replace("4347", "4348"), 32); tr.tick(); tr.tick()
    inbox(store, "im be now", 33); tr.tick()
    assert any(e["kind"] == "management_ambiguous" for e in store.journal_tail())


def test_low_confidence_and_disallowed_actions_are_journal_only():
    tr, store, fb, _ = make(management={"maybe close": Classification(action="close_all", confidence=0.5)}, management_actions=["move_sl"])
    inbox(store, ENTRY, 40); tr.tick(); tr.tick()
    inbox(store, "maybe close", 41, reply_to=40); tr.tick()
    assert store.get_run("wolves:40").state == RunState.ACTIVE
    kinds = [e["kind"] for e in store.journal_tail()]
    assert "management_skipped" in kinds


def test_llm_entry_fallback_goes_through_validation():
    ex = EntryExtraction(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.9)
    tr, store, fb, _ = make(entries={"gold long sl 4341 tp: 4353 tp: 4357 tp: 4362": ex})
    inbox(store, "gold long sl 4341 tp: 4353 tp: 4357 tp: 4362", 50); tr.tick(); tr.tick()
    run = store.get_run("wolves:50")
    assert run is not None and run.signal.parsed_by == "llm" and run.state == RunState.ACTIVE


def test_startup_reconcile_adopts_by_comment():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 60); tr.tick()
    run = store.get_run("wolves:60")
    # Simulate a crash after the commands were sent but before their results were read:
    # the positions exist on the account, the run still thinks it is placing.
    for leg in run.legs:
        leg.state, leg.position_ticket, leg.entry_price, leg.inflight_cmd, leg.inflight_kind = LegState.PLACING, None, None, None, None
    run.state = RunState.PLACING
    store.save_run(run)
    tr2 = Trader(tr.cfg, store, {"wolves": fb}, MockClassifier(), now_utc=tr.now_utc, now_local=tr.now_local)
    tr2.startup_reconcile()
    run = store.get_run("wolves:60")
    assert run.state == RunState.ACTIVE and all(l.state == LegState.OPEN and l.position_ticket for l in run.legs)


def test_dry_run_bridge_never_sends():
    tr, store, fb, _ = make()
    tr.bridges["wolves"] = DryRunBridge(fb, store, "wolves")
    inbox(store, ENTRY, 70); tr.tick(); tr.tick()
    assert fb.sent == [] and sum(1 for e in store.journal_tail() if e["kind"] == "dry_run_command") == 4
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_trader.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/trader.py`**

```python
"""The trader loop: inbox → signals/management → runs → bridge commands."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Callable
from .bridge import Bridge, BridgeState, Command, CommandResult
from .classifier import Classifier, RunContext
from .config import AppConfig, ProviderConfig
from .ladder import place_run, apply_results, sync_run, apply_action
from .models import InboxMessage, LegState, RunState, Signal, SignalRun, Side, EntryType, complete_tps, make_signal_id
from .normalize import canonical_symbol
from .parsers import get_parser, looks_like_entry
from .store import Store
from .validation import validate_signal

STALE_STATE_SEC = 5.0


class DryRunBridge:
    """Journals commands instead of sending them; fabricates ok results so state machines advance."""

    def __init__(self, inner: Bridge, store: Store, provider: str):
        self.inner, self.store, self.provider = inner, store, provider
        self._results: list[CommandResult] = []
        self._ticket = 900_000

    def send(self, cmd: Command) -> None:
        self.store.journal(self.provider, "dry_run_command", cmd.model_dump(exclude_none=True))
        self._ticket += 1
        self._results.append(CommandResult(cmd_id=cmd.cmd_id, ok=True, retcode=0, retcode_text="dry-run",
                                           position=self._ticket, order=self._ticket, fill_price=cmd.price or 0.0))

    def read_results(self) -> list[CommandResult]:
        out, self._results = self._results, []
        return out

    def read_state(self) -> BridgeState | None:
        return self.inner.read_state()


class Trader:
    def __init__(self, cfg: AppConfig, store: Store, bridges: dict[str, Bridge], classifier: Classifier,
                 now_utc: Callable[[], datetime] | None = None, now_local: Callable[[], datetime] | None = None):
        self.cfg, self.store, self.bridges, self.classifier = cfg, store, bridges, classifier
        self.now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self.now_local = now_local or datetime.now
        self.parsers = {p: get_parser(p) for p in cfg.providers}

    # ---- loop -----------------------------------------------------------
    def tick(self) -> None:
        for provider in self.cfg.providers:
            state = self.bridges[provider].read_state()
            if state is not None:
                self._track_start_of_day(provider, state)
            self.process_inbox(provider, state)
            self.sync(provider, state)

    def sync(self, provider: str, state: BridgeState | None) -> None:
        bridge, cfg = self.bridges[provider], self.cfg.providers[provider]
        results = bridge.read_results()
        for run in self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE]):
            events = apply_results(run, results)
            if state is not None:
                events += sync_run(run, cfg, state, bridge, self.now_local())
            for e in events:
                self.store.journal(provider, e.pop("kind"), e, run_id=run.id)
            self.store.save_run(run)

    # ---- inbox -----------------------------------------------------------
    def process_inbox(self, provider: str, state: BridgeState | None) -> None:
        cfg, parser = self.cfg.providers[provider], self.parsers[provider]
        for msg in self.store.new_inbox(provider):
            prev = self.store.recent_inbox(provider, msg.msg_id, 3)
            result = parser.parse(msg, prev)
            sig = result.signal
            if sig is None and looks_like_entry(msg.text):
                sig = self._llm_entry(msg, provider)
            if sig is not None:
                self._handle_signal(provider, sig, state)
            elif looks_like_entry(msg.text):
                self.store.journal(provider, "signal_unparsed", {"msg_id": msg.msg_id, "reason": result.rejected_reason or "llm declined", "text": msg.text[:300]})
            elif msg.text.strip():
                self._handle_management(provider, msg, state, prev)
            self.store.set_inbox_status(provider, msg.msg_id, "processed")

    def _llm_entry(self, msg: InboxMessage, provider: str) -> Signal | None:
        ex = self.classifier.extract_entry(msg.text, provider)
        if ex is None or ex.confidence < self.cfg.llm.confidence_threshold:
            self.store.journal(provider, "llm_entry_rejected", {"msg_id": msg.msg_id, "extraction": ex.model_dump() if ex else None})
            return None
        sym = canonical_symbol(ex.symbol)
        if sym is None:
            return None
        try:
            tps = complete_tps(ex.tps)
        except ValueError:
            return None
        return Signal(id=make_signal_id(provider, msg.msg_id), provider=provider, symbol=sym, side=Side(ex.side),
                      entry_type=EntryType(ex.entry_type), entry_zone=ex.entry_zone, sl=ex.sl, tps=tps,
                      received_at=msg.ts, raw_text=msg.text, telegram_msg_id=msg.msg_id, parsed_by="llm")

    # ---- signals --------------------------------------------------------
    def guard_reasons(self, provider: str, state: BridgeState | None) -> list[str]:
        cfg = self.cfg.providers[provider]
        reasons: list[str] = []
        if state is None:
            return ["no_state"]
        if state.age_sec(self.now_local()) > STALE_STATE_SEC:
            reasons.append("terminal_stale")
        live = self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE])
        if len(live) >= cfg.max_open_signals:
            reasons.append("max_open_signals")
        if sum(len(r.open_legs()) + len(r.pending_legs()) + len(r.placing_legs()) for r in live) >= cfg.max_legs_open:
            reasons.append("max_legs_open")
        sod = self.store.kv_get(self._sod_key(provider))
        if sod and float(sod) > 0 and (float(sod) - state.account.equity) / float(sod) * 100 >= cfg.daily_loss_stop_pct:
            reasons.append("daily_loss_stop")
        return reasons

    def _sod_key(self, provider: str) -> str:
        return f"sod_equity:{provider}:{self.now_local().date().isoformat()}"

    def _track_start_of_day(self, provider: str, state: BridgeState) -> None:
        key = self._sod_key(provider)
        if self.store.kv_get(key) is None:
            self.store.kv_set(key, str(state.account.equity))

    def _handle_signal(self, provider: str, sig: Signal, state: BridgeState | None) -> None:
        cfg, bridge = self.cfg.providers[provider], self.bridges[provider]
        run = SignalRun.from_signal(sig)
        broker_symbol = cfg.symbols.get(sig.symbol)
        spec = state.symbols.get(broker_symbol) if (state and broker_symbol) else None
        fails = validate_signal(sig, cfg, spec.quote() if spec else None, self.now_utc())
        if spec is not None and not spec.trade_allowed:
            fails.append("trade_not_allowed")
        if fails:
            run.state = RunState.REJECTED
            self.store.save_run(run)
            self.store.journal(provider, "signal_rejected", {"reasons": fails, "signal": sig.model_dump(mode="json")}, run_id=run.id)
            return
        guards = self.guard_reasons(provider, state)
        if guards:
            run.state = RunState.REJECTED
            self.store.save_run(run)
            self.store.journal(provider, "guard_blocked", {"reasons": guards, "signal": sig.model_dump(mode="json")}, run_id=run.id)
            return
        events = place_run(run, cfg, state, bridge, self.now_local())
        self.store.save_run(run)
        self.store.journal(provider, "signal_accepted", {"signal": sig.model_dump(mode="json"), "volumes": [l.volume for l in run.legs]}, run_id=run.id)
        for e in events:
            self.store.journal(provider, e.pop("kind"), e, run_id=run.id)

    # ---- management -----------------------------------------------------
    def resolve_reference(self, provider: str, msg: InboxMessage) -> SignalRun | None:
        if msg.reply_to is not None:
            run = self.store.run_by_msg_id(provider, msg.reply_to)
            if run is not None and run.state in (RunState.PLACING, RunState.ACTIVE):
                return run
        active = self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE])
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            self.store.journal(provider, "management_ambiguous", {"msg_id": msg.msg_id, "active_runs": [r.id for r in active], "text": msg.text[:300]})
        return None

    def _handle_management(self, provider: str, msg: InboxMessage, state: BridgeState | None, prev: list[InboxMessage]) -> None:
        cfg, bridge = self.cfg.providers[provider], self.bridges[provider]
        run = self.resolve_reference(provider, msg)
        if run is None:
            self.store.journal(provider, "management_no_reference", {"msg_id": msg.msg_id, "text": msg.text[:300]})
            return
        ctx = RunContext(symbol=run.signal.symbol, side=run.signal.side.value, entry_type=run.signal.entry_type.value,
                         entry_zone=run.signal.entry_zone, sl=run.signal.sl, open_legs=len(run.open_legs()),
                         pending_legs=len(run.pending_legs()), sl_current=max((l.sl_current for l in run.open_legs()), default=run.signal.sl))
        c = self.classifier.classify_management(msg.text, provider, ctx, [p.text[:200] for p in prev])
        executable = c.action != "none" and c.confidence >= self.cfg.llm.confidence_threshold and c.action in cfg.management_actions and state is not None
        self.store.save_classification(provider, msg.msg_id, msg.text, c.action, c.price, c.confidence, c.reason, executable)
        if not executable:
            self.store.journal(provider, "management_skipped", {"msg_id": msg.msg_id, "classification": c.model_dump()}, run_id=run.id)
            return
        events = apply_action(run, c.action, c.price, state, bridge, cfg)
        self.store.save_run(run)
        self.store.journal(provider, "management_applied", {"msg_id": msg.msg_id, "classification": c.model_dump()}, run_id=run.id)
        for e in events:
            self.store.journal(provider, e.pop("kind"), e, run_id=run.id)

    # ---- startup --------------------------------------------------------
    def startup_reconcile(self) -> None:
        for provider in self.cfg.providers:
            state = self.bridges[provider].read_state()
            if state is None:
                continue
            for run in self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE]):
                changed = False
                for leg in run.legs:
                    if leg.state not in (LegState.PLACING, LegState.PENDING_ORDER, LegState.OPEN):
                        continue
                    comment = leg.comment(run.signal.id)
                    pos, order = state.position_by_comment(comment), state.order_by_comment(comment)
                    if pos is not None and leg.state != LegState.OPEN:
                        leg.state, leg.position_ticket, leg.entry_price, leg.inflight_cmd, leg.inflight_kind = LegState.OPEN, pos.ticket, pos.price_open, None, None
                        changed = True
                    elif order is not None and leg.state == LegState.PLACING:
                        leg.state, leg.order_ticket, leg.inflight_cmd, leg.inflight_kind = LegState.PENDING_ORDER, order.ticket, None, None
                        changed = True
                if changed:
                    from .ladder import _recompute_state
                    _recompute_state(run)
                    self.store.save_run(run)
                    self.store.journal(provider, "reconciled", {"legs": [l.state.value for l in run.legs]}, run_id=run.id)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_trader.py` — Expected: `10 passed`. Then `pytest -q` — everything green.

- [ ] **Step 5: Commit**

```bash
git add tg_signal_trader/trader.py tests/test_trader.py
git commit -m "feat(py): trader loop with validation, guards, management routing and startup reconcile"
```

---

### Task 16: Telegram listener and replay

**Files:**
- Create: `tg_signal_trader/listener.py`, `tg_signal_trader/replay.py`, `tests/test_replay.py`

**Interfaces:**
- Produces (listener.py): `async def run_listener(cfg: AppConfig, secrets: Secrets, store: Store) -> None` (Telethon `TelegramClient(str(cfg.session_path), api_id, api_hash)`, `await client.start()`, `events.NewMessage(chats=[...])` → `store.add_inbox(...)` with `ts = message.date` (UTC-aware), `reply_to = message.reply_to_msg_id`, `text = message.message or ""`; `await client.run_until_disconnected()`), `async def resolve_chats(cfg, secrets) -> list[tuple[str, int]]` (iterates `client.iter_dialogs()` and returns `(title, id)` for channels/groups).
- Produces (replay.py): `replay(cfg: AppConfig, store: Store, classifier, provider: str, export_dir: Path, bridge: Bridge | None = None) -> dict` — loads the export into the inbox with `status="new"`, runs a `Trader` over it with the provider's `max_signal_age_sec` disabled (set to a huge value) and a clock that follows each message's timestamp, using `bridge` or a `FakeBridge` seeded with a quote taken from each signal's own prices (mid = entry or first TP ± SL midpoint) so validation is exercised; returns counts `{"messages", "signals_accepted", "signals_rejected", "management_applied", "management_skipped", "unparsed"}`.

- [ ] **Step 1: Write the failing test**

`tests/test_replay.py`:
```python
from pathlib import Path
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.store import Store
from tg_signal_trader.classifier import MockClassifier
from tg_signal_trader.replay import replay

DATA = Path(__file__).parent / "data"


def test_replay_export_produces_counts(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    cfg = AppConfig(providers={"lewis": ProviderConfig(telegram_chat=-1, bridge_dir="/tmp/l", symbols={"NAS100": "NAS100"}, sl_range={"NAS100": (5, 500)})})
    store = Store(":memory:")
    counts = replay(cfg, store, MockClassifier(), "lewis", tmp_path)
    assert counts["messages"] == 3 and counts["signals_accepted"] == 1
    run = store.get_run("lewis:8")
    assert run is not None and run.signal.symbol == "NAS100" and [l.volume for l in run.legs][0] > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_replay.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/listener.py`**

```python
"""Telethon user-session listener: every message from the configured channels → SQLite inbox."""
from __future__ import annotations
import logging
from telethon import TelegramClient, events
from .config import AppConfig, Secrets
from .models import InboxMessage
from .store import Store

log = logging.getLogger("tg-listener")


def _client(cfg: AppConfig, secrets: Secrets) -> TelegramClient:
    return TelegramClient(str(cfg.session_path), secrets.telegram_api_id, secrets.telegram_api_hash)


async def run_listener(cfg: AppConfig, secrets: Secrets, store: Store) -> None:
    chats = {p.telegram_chat: name for name, p in cfg.providers.items() if p.telegram_chat}
    if not chats:
        raise SystemExit("no provider has a telegram_chat id; run `tg-trader resolve-chats` first")
    client = _client(cfg, secrets)
    await client.start()          # first run: interactive phone/code/2FA prompt; then the session file is enough

    @client.on(events.NewMessage(chats=list(chats)))
    async def on_message(event):
        m = event.message
        provider = chats.get(event.chat_id)
        if provider is None:
            return
        msg = InboxMessage(msg_id=m.id, chat_id=event.chat_id, provider=provider, reply_to=m.reply_to_msg_id,
                           text=m.message or "", ts=m.date)
        if store.add_inbox(msg):
            log.info("%s #%d %s", provider, m.id, (m.message or "")[:80].replace("\n", " | "))

    log.info("listening on %s", chats)
    await client.run_until_disconnected()


async def resolve_chats(cfg: AppConfig, secrets: Secrets) -> list[tuple[str, int]]:
    client = _client(cfg, secrets)
    await client.start()
    out: list[tuple[str, int]] = []
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            out.append((d.name, d.id))
    await client.disconnect()
    return out
```

- [ ] **Step 4: Write `tg_signal_trader/replay.py`**

```python
"""Replay a Telegram export through the full pipeline against a FakeBridge (or a real one in dry-run)."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from .bridge import Bridge, FakeBridge
from .classifier import Classifier
from .config import AppConfig
from .export import read_export
from .models import RunState
from .parsers import get_parser
from .store import Store
from .trader import Trader


def replay(cfg: AppConfig, store: Store, classifier: Classifier, provider: str, export_dir: Path, bridge: Bridge | None = None) -> dict:
    pcfg = cfg.providers[provider].model_copy(update={"max_signal_age_sec": 10**9, "max_open_signals": 10**6, "max_legs_open": 10**6, "daily_loss_stop_pct": 10**6})
    cfg = cfg.model_copy(update={"providers": {provider: pcfg}})
    msgs = read_export(export_dir, provider, chat_id=pcfg.telegram_chat)
    for m in msgs:
        store.add_inbox(m)
    parser = get_parser(provider)
    fb = FakeBridge(now_local=msgs[0].ts.replace(tzinfo=None) if msgs else datetime.now())
    for sym in pcfg.symbols.values():
        fb.set_quote(sym, 1.0, 1.0, tick_value=1.0, tick_size=0.01)
    clock = {"utc": msgs[0].ts if msgs else None}
    trader = Trader(cfg, store, {provider: bridge or fb}, classifier,
                    now_utc=lambda: clock["utc"], now_local=lambda: clock["utc"].replace(tzinfo=None))
    counts = {"messages": len(msgs), "signals_accepted": 0, "signals_rejected": 0, "management_applied": 0, "management_skipped": 0, "unparsed": 0}
    for i, m in enumerate(msgs):
        clock["utc"] = m.ts + timedelta(seconds=1)
        fb.now = clock["utc"].replace(tzinfo=None)
        r = parser.parse(m, msgs[max(0, i - 3):i])
        if r.signal and bridge is None:
            # Seed the fake quote around the signal's own price so validation is exercised meaningfully.
            ref = r.signal.entry_zone[0] if r.signal.entry_zone else (r.signal.tps[0] + r.signal.sl) / 2
            fb.set_quote(pcfg.symbols.get(r.signal.symbol, "?"), ref - 0.1, ref + 0.1, tick_value=1.0, tick_size=0.01)
        trader.tick()
    for e in store.journal_tail(10**6):
        if e["kind"] in counts:
            counts[e["kind"]] += 1
    counts["signals_rejected"] = sum(1 for r in store.runs(provider) if r.state == RunState.REJECTED)
    counts["signals_accepted"] = sum(1 for r in store.runs(provider) if r.state != RunState.REJECTED)
    counts["unparsed"] = sum(1 for e in store.journal_tail(10**6) if e["kind"] == "signal_unparsed")
    return counts
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest -q tests/test_replay.py` — Expected: `1 passed`; `pytest -q` all green. The listener has no unit test (it needs a live Telegram session); it is exercised by the `resolve-chats` and `listen` CLI commands in Task 17.

- [ ] **Step 6: Commit**

```bash
git add tg_signal_trader/listener.py tg_signal_trader/replay.py tests/test_replay.py
git commit -m "feat(py): Telethon listener and export replay"
```

---

### Task 17: CLI, ops commands, systemd units, README

**Files:**
- Create: `tg_signal_trader/cli.py`, `tests/test_cli.py`, `deploy/tg-listener.service`, `deploy/tg-trader.service`, `README.md`

**Interfaces:**
- Produces: console script `tg-trader` with subcommands (all take `--config PATH`, default `config.yaml`):
  `listen` · `run [--dry-run] [--once]` · `status` · `bridge-ping <provider> [--timeout 10]` · `bridge-test <provider> --confirm` (min-lot market order with SL/TP 1 % away, modify SL, close; refuses without `--confirm` and prints the account login it will trade on) · `replay <provider> <export_dir>` · `resolve-chats` · `journal [--n 50]`.
  Library functions used by tests: `bridge_ping(bridge, timeout_sec, now_local, sleep) -> float | None` (round-trip seconds or None), `status_lines(cfg, store, bridges, now_local) -> list[str]`, `bridge_test(bridge, symbol, now_local, sleep) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:
```python
from datetime import datetime, timezone
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.store import Store
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.cli import bridge_ping, status_lines, bridge_test

LOCAL0 = datetime(2026, 9, 18, 13, 0, 0)


def fb():
    b = FakeBridge(now_local=LOCAL0, balance=12_345)
    b.set_quote("XAUUSD", 4346.8, 4347.0)
    return b


def test_bridge_ping_round_trip():
    b = fb()
    assert bridge_ping(b, timeout_sec=1, now_local=lambda: LOCAL0, sleep=lambda s: None) is not None
    assert b.sent[-1].type == "ping"


def test_bridge_ping_times_out_when_no_ea():
    class Silent(FakeBridge):
        def send(self, cmd): self.sent.append(cmd)   # never answers
    b = Silent(now_local=LOCAL0)
    assert bridge_ping(b, timeout_sec=0.01, now_local=lambda: LOCAL0, sleep=lambda s: None) is None


def test_status_lines_report_age_balance_symbols():
    cfg = AppConfig(providers={"wolves": ProviderConfig(telegram_chat=1, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})})
    lines = status_lines(cfg, Store(":memory:"), {"wolves": fb()}, now_local=lambda: LOCAL0)
    text = "\n".join(lines)
    assert "wolves" in text and "12345" in text and "XAUUSD" in text and "age 0.0s" in text and "OK" in text


def test_bridge_test_opens_modifies_closes():
    b = fb()
    lines = bridge_test(b, "XAUUSD", now_local=lambda: LOCAL0, sleep=lambda s: None)
    assert [c.type for c in b.sent] == ["open_market", "modify_sl", "close"] and b.read_state().positions == []
    assert b.sent[0].volume == 0.01 and all("OK" in l for l in lines)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest -q tests/test_cli.py` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tg_signal_trader/cli.py`**

```python
"""tg-trader command line."""
from __future__ import annotations
import argparse, asyncio, logging, os, sys, time
from datetime import datetime
from pathlib import Path
from typing import Callable
from .bridge import Bridge, Command, FileBridge
from .classifier import ClaudeClassifier, MockClassifier
from .config import AppConfig, Secrets, load_config
from .models import RunState
from .store import Store
from .trader import Trader, DryRunBridge

log = logging.getLogger("tg-trader")


def _bridges(cfg: AppConfig, store: Store) -> dict[str, Bridge]:
    out: dict[str, Bridge] = {}
    for name, p in cfg.providers.items():
        off = store.kv_get(f"results_offset:{name}")
        out[name] = FileBridge(p.bridge_dir, results_offset=int(off) if off else 0)
    return out


def _persist_offsets(bridges: dict[str, Bridge], store: Store) -> None:
    for name, b in bridges.items():
        if isinstance(b, FileBridge):
            store.kv_set(f"results_offset:{name}", str(b.results_offset))


def bridge_ping(bridge: Bridge, timeout_sec: float, now_local: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep) -> float | None:
    cmd_id = f"ping:{now_local().strftime('%Y%m%d%H%M%S%f')}"
    t0 = time.perf_counter()
    bridge.send(Command(cmd_id=cmd_id, type="ping"))
    deadline = time.perf_counter() + timeout_sec
    while True:
        for r in bridge.read_results():
            if r.cmd_id == cmd_id:
                return time.perf_counter() - t0
        if time.perf_counter() >= deadline:
            return None
        sleep(0.1)


def status_lines(cfg: AppConfig, store: Store, bridges: dict[str, Bridge], now_local: Callable[[], datetime] = datetime.now) -> list[str]:
    lines: list[str] = []
    for name, p in cfg.providers.items():
        st = bridges[name].read_state()
        if st is None:
            lines.append(f"[{name}] NO STATE at {p.bridge_dir} — is SignalBridge attached?")
            continue
        age = st.age_sec(now_local())
        flag = "OK" if age <= 5 else "STALE"
        lines.append(f"[{name}] {flag} age {age:.1f}s | login {st.account.login} | balance {st.account.balance:g} equity {st.account.equity:g} | hedging {st.account.hedging}")
        for sym, s in st.symbols.items():
            lines.append(f"    {sym}: bid {s.bid} ask {s.ask} step {s.volume_step} min {s.volume_min} tick_value {s.tick_value} trade_allowed {s.trade_allowed}")
        lines.append(f"    positions {len(st.positions)} orders {len(st.orders)}")
        for run in store.runs(name, [RunState.PLACING, RunState.ACTIVE]):
            legs = " ".join(f"L{l.n}:{l.state.value}" for l in run.legs)
            lines.append(f"    run {run.id} {run.signal.symbol} {run.signal.side.value} {run.state.value} | {legs}")
        sod = store.kv_get(f"sod_equity:{name}:{now_local().date().isoformat()}")
        if sod:
            lines.append(f"    day P&L {(st.account.equity - float(sod)):+.2f} vs stop {p.daily_loss_stop_pct}% of {float(sod):g}")
    return lines


def bridge_test(bridge: Bridge, symbol: str, now_local: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Places a minimum-lot market BUY with SL/TP 1% away, moves the SL, closes it. Demo accounts only."""
    st = bridge.read_state()
    spec = st.symbols[symbol]
    stamp = now_local().strftime("%Y%m%d%H%M%S")
    out: list[str] = []

    def wait(cmd_id: str):
        deadline = time.perf_counter() + 15
        while time.perf_counter() < deadline:
            for r in bridge.read_results():
                if r.cmd_id == cmd_id:
                    return r
            sleep(0.2)
        return None

    def step(cmd: Command) -> object:
        bridge.send(cmd)
        r = wait(cmd.cmd_id)
        out.append(f"{cmd.type}: {'OK' if r and r.ok else 'FAILED'} {r.retcode_text if r else 'no result (EA not running?)'}")
        return r

    r = step(Command(cmd_id=f"test:{stamp}:1", type="open_market", symbol=symbol, side="BUY", volume=spec.volume_min,
                     sl=round(spec.ask * 0.99, spec.digits), tp=round(spec.ask * 1.01, spec.digits), comment="sig:bridge-test:L1"))
    if not (r and r.ok):
        return out
    step(Command(cmd_id=f"test:{stamp}:2", type="modify_sl", position=r.position, sl=round(spec.ask * 0.995, spec.digits)))
    step(Command(cmd_id=f"test:{stamp}:3", type="close", position=r.position))
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="tg-trader")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listen")
    r = sub.add_parser("run"); r.add_argument("--dry-run", action="store_true"); r.add_argument("--once", action="store_true")
    sub.add_parser("status")
    pp = sub.add_parser("bridge-ping"); pp.add_argument("provider"); pp.add_argument("--timeout", type=float, default=10)
    bt = sub.add_parser("bridge-test"); bt.add_argument("provider"); bt.add_argument("--confirm", action="store_true")
    rp = sub.add_parser("replay"); rp.add_argument("provider"); rp.add_argument("export_dir")
    sub.add_parser("resolve-chats")
    jn = sub.add_parser("journal"); jn.add_argument("--n", type=int, default=50)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    store = Store(cfg.db_path)

    if a.cmd == "listen":
        from .listener import run_listener
        asyncio.run(run_listener(cfg, Secrets.from_env(), store)); return 0
    if a.cmd == "resolve-chats":
        from .listener import resolve_chats
        for title, cid in asyncio.run(resolve_chats(cfg, Secrets.from_env())):
            print(f"{cid:>16}  {title}")
        return 0
    if a.cmd == "status":
        print("\n".join(status_lines(cfg, store, _bridges(cfg, store)))); return 0
    if a.cmd == "journal":
        for e in reversed(store.journal_tail(a.n)):
            print(e["ts"], e["provider"], e["kind"], e["run_id"] or "", e["detail"])
        return 0
    if a.cmd == "bridge-ping":
        rtt = bridge_ping(_bridges(cfg, store)[a.provider], a.timeout)
        print(f"{a.provider}: {'pong in %.3fs' % rtt if rtt is not None else 'NO RESPONSE — is SignalBridge attached and Algo Trading on?'}")
        return 0 if rtt is not None else 1
    if a.cmd == "bridge-test":
        b = _bridges(cfg, store)[a.provider]
        st = b.read_state()
        if st is None:
            print("no state.json — EA not running"); return 1
        print(f"This places a REAL minimum-lot BUY on account {st.account.login} (balance {st.account.balance:g}).")
        if not a.confirm:
            print("Re-run with --confirm on a DEMO account."); return 1
        sym = next(iter(cfg.providers[a.provider].symbols.values()))
        print("\n".join(bridge_test(b, sym))); return 0
    if a.cmd == "replay":
        from .replay import replay
        counts = replay(cfg, store, MockClassifier(), a.provider, Path(a.export_dir))
        print(counts); return 0
    if a.cmd == "run":
        secrets = Secrets.from_env()
        classifier = ClaudeClassifier(cfg.llm.model, cfg.llm.timeout_sec, api_key=secrets.anthropic_api_key)
        bridges = _bridges(cfg, store)
        if a.dry_run:
            bridges = {n: DryRunBridge(b, store, n) for n, b in bridges.items()}
            log.warning("DRY RUN: commands are journaled, not sent")
        trader = Trader(cfg, store, bridges, classifier)
        trader.startup_reconcile()
        while True:
            trader.tick()
            _persist_offsets(bridges if not a.dry_run else {n: b.inner for n, b in bridges.items()}, store)
            if a.once:
                return 0
            time.sleep(cfg.poll_interval_sec)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest -q tests/test_cli.py` — Expected: `4 passed`; `pytest -q` all green.

- [ ] **Step 5: Write the systemd units**

`deploy/tg-listener.service`:
```ini
[Unit]
Description=tg-signal-trader Telegram listener
After=network-online.target
Wants=network-online.target

[Service]
User=mt5
WorkingDirectory=/opt/tg-signal-trader
EnvironmentFile=/etc/tg-signal-trader.env
ExecStart=/opt/tg-signal-trader/.venv/bin/tg-trader --config /etc/tg-signal-trader/config.yaml listen
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
`deploy/tg-trader.service`: same with `Description=tg-signal-trader trader loop`, `ExecStart=... run`, and `After=tg-listener.service`.

- [ ] **Step 6: Write `README.md`**

````markdown
# tg-signal-trader

Executes Telegram signal-channel trades on MT5 accounts through a file bridge. One provider → one
account: Lewis → PUPrime, Wolves → IconTech (both configurable; nothing is hardcoded).

Design spec: `docs/superpowers/specs/2026-09-18-tg-signal-trader-design.md`.

## How it works

`tg-listener` (Telethon, your own Telegram account) writes every channel message into SQLite.
`tg-trader` parses entries with per-provider templates, validates the numbers, sizes four legs (one
per TP) at `risk_pct_per_leg` of the account balance, and writes commands to the terminal's
`MQL5/Files/signalbridge/commands.jsonl`. `SignalBridge.mq5` inside the terminal executes each
command once and writes `results.jsonl` and `state.json`. Free-form management messages ("Delete
this", "im BE", "Move SL 4412") go through a Claude classifier; only `close_all`, `cancel_pending`,
`move_sl` and `break_even` are executed, and only above the confidence threshold.

TP ladder: L1 hit → nothing; L2 hit → L3/L4 stop to their entry; L3 hit → L4 stop to TP1; stops only
ever move one way. `TP4: OPEN` → TP4 = TP3 + (TP3 − TP2). Limit/zone signals become pending orders
at the near price for 24 h.

## VPS setup (Linux + Wine terminals)

1. Two extra portable MT5 installs (one per provider account), each logged into its account with
   Algo Trading enabled and the traded symbols in Market Watch.
2. Copy `mql5/Include/SignalBridge` into each terminal's `MQL5/Include/` and
   `mql5/Experts/SignalBridge` into `MQL5/Experts/`; compile `SignalBridge.mq5` (MetaEditor, F7);
   attach it to one chart with `InpExpectedLogin` = that account and `InpSymbols` = the symbols.
   The Experts tab must show `started on account …`.
3. `python3 -m venv .venv && .venv/bin/pip install -e .`; copy `config.example.yaml` → your config
   with each provider's `bridge_dir` = absolute Linux path of that terminal's
   `MQL5/Files/signalbridge` (find the data folder via *File → Open Data Folder*, then translate to
   the Wine path); `.env` with `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (my.telegram.org) and
   `ANTHROPIC_API_KEY`.
4. `tg-trader resolve-chats` (interactive Telegram login the first time) → put the channel ids in
   `config.yaml`.
5. Checks: `tg-trader status` (state age < 1 s, right login, symbols visible) →
   `tg-trader bridge-ping lewis` (round trip through the EA, no order) →
   on a DEMO account `tg-trader bridge-test lewis --confirm` (min-lot open / modify / close).
6. Install `deploy/*.service`, `systemctl enable --now tg-listener tg-trader`.

## Before real money

1. `tg-trader replay lewis <export dir>` and `... wolves ...` — review `tg-trader journal`.
2. `tg-trader run --dry-run` for 3–5 trading days; read the journal and `classifications` daily.
3. Demo accounts armed for a week.
4. Live with `risk_pct_per_leg: 0.25`, then 1.0.

## Developing

`pytest -q` (Python, no MT5 needed). MQL5 bridge tests on macOS via the Wine harness:
`scripts/mt5-setup-test-terminal.sh`, then `scripts/mt5-test.sh SignalBridgeUnitTests|SignalBridgeExecTests|SignalBridgeCoreTests`.
````

- [ ] **Step 7: Run everything once**

Run: `pytest -q` (all green) and the three MQL5 suites + EA compile from Task 5.

- [ ] **Step 8: Commit**

```bash
git add tg_signal_trader/cli.py tests/test_cli.py deploy README.md
git commit -m "feat: tg-trader CLI with status/ping/test ops, systemd units, README"
```

---

## Self-review notes

- **Spec coverage:** architecture B′ (Tasks 5, 11, 15–17); signal model + templates + TP4 rule (6, 8, 9); validation (10); ladder incl. one-way SL, cancel-on-SL, expiry (13); management actions, reference resolution, two-active rule (13, 15); bridge protocol incl. idempotency, atomic state, refusal on login/netting (3–5); config + guards incl. daily loss (6, 15); classifier contract with schema/threshold/timeout/journal (14, 15); ops: two services, dry-run, replay, status, alerts (16, 17 — Telegram DM alerts are **not** implemented in v1; the journal + `status` cover observability; noted as a follow-up); tests per spec (all tasks). Demo checklist → README (17).
- **Deviation:** the spec's `Leg` states gain `PLACING` (command sent, no result yet) and per-leg inflight bookkeeping — needed to match results to legs.
- **Type consistency:** `Bridge.send/read_results/read_state` used identically in `ladder.py`, `trader.py`, `cli.py`, `replay.py`; `Command`/`CommandResult` field names match `BridgeExecutor.mqh`'s `ParseCommand`/`ResultToJson`; `state.json` keys match `BridgeState`; `leg_comment` format `sig:<id>:L<n>` is what `FindMirroredPosition`-style reconciliation and the bridge test EAs use.
