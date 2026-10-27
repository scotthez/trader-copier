# Account mirror on the Windows VPS: IconTech 4100074 → 1515095790 (demo)

Copies the trades **you place by hand** on 4100074 to 1515095790 at the same lot size, as fast as the
terminals allow, and times every copy. Copy-trading platform positions on 4100074 (the Holy Grail
copies: magic 2147483646, random-id comments) and the Wolves bot's `sig:` trades are never copied.
Nothing is ever sent to 4100074; the mirror only reads what CopierSource writes.

Both terminals and this program run on the VPS. No Telegram is used (`notify_trades: false`).

## 0. Get the code on the VPS

Install Git for Windows and Python 3.11+ (python.org, tick *Add python.exe to PATH*), then in a command prompt:

```
git clone https://github.com/scotthez/trader-copier.git C:\tg-mirror\trader-copier
```

Later updates: `cd C:\tg-mirror\trader-copier` → `git pull` (then restart run-mirror.bat; recompile an EA
only if its `.mq5`/`.mqh` changed). Paths below are relative to `C:\tg-mirror\trader-copier`.

## 1. IconTech 4100074 terminal (source): CopierSource

1. *File → Open Data Folder*, then copy from the repo:
   - `mql5/Include/CopyTrader` → `MQL5/Include/CopyTrader` (replace an older copy: this build
     adds the login/magic/comment the mirror needs)
   - `mql5/Experts/CopyTrader/CopierSource.mq5` → `MQL5/Experts/CopyTrader/`
2. MetaEditor (F4) → open `CopierSource.mq5` → F7 (0 errors).
3. Open a **new** chart (any symbol) and attach **CopierSource**. Inputs:
   `InpEventsFileBase = iconmirror`, `InpExpectedLogin = 4100074`, `InpReconcileMs = 2000`.
   Leave any SignalBridge already on this terminal where it is: CopierSource only reads.
4. The Experts tab must show `[CopierSource] started on account 4100074 … events -> Common\Files\iconmirror_events_YYYYMMDD.jsonl`.

## 2. 1515095790 terminal (destination): SignalBridge

1. *Tools → Options → Expert Advisors → Allow algorithmic trading*, and the *Algo Trading* button green.
   Put XAUUSD, EURUSD, GBPUSD, USDJPY (the names this broker uses) in Market Watch.
2. *File → Open Data Folder*, then copy `mql5/Include/SignalBridge` → `MQL5/Include/SignalBridge`
   and `mql5/Experts/SignalBridge/SignalBridge.mq5` → `MQL5/Experts/SignalBridge/`; compile `SignalBridge.mq5` (F7).
3. Attach **SignalBridge** to one chart. Inputs: `InpExpectedLogin = 1515095790`, `InpPollMs = 100`,
   `InpSymbols = XAUUSD,EURUSD,GBPUSD,USDJPY` (this broker's names), the rest default.
4. The Experts tab must show `[SignalBridge] started on account 1515095790 …`. The account must be a
   **hedging** account (SignalBridge refuses a netting one).

## 3. The mirror program

1. Double-click `deploy\windows\install.bat` (creates `.venv` and `config.yaml` in the repo folder;
   `config.yaml` is git-ignored, so `git pull` never overwrites it).
2. Edit `config.yaml`:
   - `bridge_dir`: the 1515095790 terminal's data folder + `/MQL5/Files/signalbridge`
     (forward slashes, e.g. `C:/Users/Administrator/AppData/Roaming/MetaQuotes/Terminal/D0E8…/MQL5/Files/signalbridge`).
   - `events_dir`: `C:/Users/<your Windows user>/AppData/Roaming/MetaQuotes/Terminal/Common/Files`
     (the folder that now holds `iconmirror_events_….jsonl`).
   - `symbols`: left side = IconTech names, right side = the destination's names.
3. Double-click `deploy\windows\status.bat`: expect `[mirror icon] source 4100074 … -> dest_demo | armed (demo)` and
   `dest_demo: pong in 0.1…s`. `NO STATE` = wrong `bridge_dir` or SignalBridge not running.
4. Double-click `deploy\windows\run-mirror.bat` and leave the window open (to start it with Windows: Task Scheduler →
   *At log on* → run-mirror.bat). A trade already open when the mirror first starts is not copied.

## 4. Test (demo)

- Place a minimum-lot trade by hand on 4100074 with an SL and TP → within a second or so the same
  symbol, side, lots, SL and TP appear on 1515095790 with comment `m:icon:<position>`.
- Move its SL/TP → the copy follows. Partly close → the copy closes the same fraction. Close → the copy closes.
- `deploy\windows\latency.bat` lists each step's timing in seconds: `src->read` (source trade → this program saw it;
  whole-second source clock, ±1 s), `read->send`, `send->result` (SignalBridge executed it), `src->result`.
- `.venv\Scripts\tg-trader --config config.yaml journal --n 50` (in the repo folder) shows every step,
  including Holy Grail positions skipped as `mirror_filtered`.

Stop: close the run-mirror window. Restarting is safe: nothing opens twice, and closes that happened
while it was stopped are applied when it starts.
