# tg-signal-trader

Executes Telegram signal-channel trades on MT5 accounts through a file bridge. One provider → one
account: Lewis → PUPrime, Wolves → IconTech (both configurable; nothing is hardcoded). It can also
mirror your own trades from one MT5 account to another (see *Mirror one of my accounts to another*).

Design spec: `docs/superpowers/specs/2026-09-18-tg-signal-trader-design.md`.

## How it works

`tg-listener` (Telethon, your own Telegram account) writes every channel message into SQLite.
`tg-trader` parses entries with per-provider templates, validates the numbers, sizes four legs (one
per TP) at `risk_pct_per_leg` of the account balance, and writes commands to the terminal's
`MQL5/Files/signalbridge/commands.jsonl`. `SignalBridge.mq5` inside the terminal executes each
command once and writes `results.jsonl` and `state.json`. Free-form management messages ("Delete
this", "im BE", "Move SL 4412") go through a Claude classifier; only `close_all`, `cancel_pending`,
`move_sl` and `break_even` are executed, and only above the confidence threshold. Known boilerplate
(e.g. Lewis's "you can put your stop-loss to break-even if you wish" after TP1) is listed in
`ignore_patterns` and never reaches the model.

Entries are cross-checked: with `llm.entry_crosscheck: true` the model reads every template-parsed
entry independently and the trade only goes ahead if both agree on symbol, side, entry type, SL and
TP1–TP3 (a disagreement is journaled as `crosscheck:<field>`; if the model is unavailable the
template result stands and `entry_crosscheck_unavailable` is journaled). With
`llm.market_crosscheck_after: true` (the default) MARKET entries are not held for the model: they are
sent at once and checked straight after, and a disagreement closes every leg of that trade
(`crosscheck_failed_closing`). LIMIT entries are always checked before any order is placed.

Wolves TP typos: when a Wolves limit signal's TPs are out of order or on the wrong side of the entry,
and exactly one TP disagrees with the pips note written beside it (while at least two others match
theirs), that TP is replaced by the entry ± pips value (e.g. "TP2 4260 100pips" from 4280 → 4270).
The fix is journaled as `tp_corrected`; any other malformed ladder is still rejected.

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
   `ANTHROPIC_API_KEY` — or, with `llm.provider: openrouter` in the config, `OPENROUTER_API_KEY` and
   `OPENROUTER_MODEL` (e.g. `openai/gpt-5-mini`) instead. The `.env` next to the config file is loaded
   automatically for local runs; systemd uses `EnvironmentFile`.
4. `tg-trader resolve-chats` (interactive Telegram login the first time) → put the channel ids in
   `config.yaml`.
5. Checks: `tg-trader status` (state age < 1 s, right login, symbols visible) →
   `tg-trader bridge-ping lewis` (round trip through the EA, no order) →
   on a DEMO account `tg-trader bridge-test lewis --confirm` (min-lot open / modify / close).
6. Install `deploy/*.service`, `systemctl enable --now tg-listener tg-trader`.

## Phone alerts

When a terminal's `state.json` is missing or has not updated for `terminal_alert_sec` (default 60),
the trader journals `terminal_down` and sends a Telegram alert; `terminal_up` follows when it
recovers. A signal that arrives while a terminal is silent waits for it (up to `max_signal_age_sec`)
instead of being rejected as `no_quote`. Alerts come from a bot, because messages to your own Saved
Messages do not notify:

1. In Telegram, message @BotFather → `/newbot` → copy the token into `.env` as
   `TELEGRAM_ALERT_BOT_TOKEN=…`.
2. Open your new bot and press Start.
3. `tg-trader alert-test` prints your chat id → add `TELEGRAM_ALERT_CHAT_ID=…` to `.env`.
4. `tg-trader alert-test` again sends a test alert; restart `tg-trader`.

With the bot set up, every trade is also messaged (`notify_trades: true`, the default): placed (with
prices and lots), not placed (with the reason), filled, TP / SL hit, SL moved, cancelled, provider
instructions applied or ignored, and failures, as one message per trade per moment.

## Arming guard

Every provider has `expected_login` and `live`. The trader sends **no** command — no entries, no
ladder moves, no management — to a terminal whose `state.json` login differs from `expected_login`,
or whose account is `REAL` while `live` is `false`; it journals `arming_blocked` instead, and
`tg-trader status` shows `NOT ARMED`. Demo/contest accounts run with `live: false`. To go live on an
account, set both `expected_login: <number>` and `live: true` for that provider — a stale or
mistyped `bridge_dir` therefore cannot trade a real account. `bridge-test` refuses REAL accounts
unless you pass `--live`.

## Before real money

1. `tg-trader replay lewis <export dir>` and `... wolves ...` — review `tg-trader journal`.
2. `tg-trader run --dry-run` for 3–5 trading days; read the journal and `classifications` daily.
   `tg-trader classify-eval wolves <export dir> --n 40` shows how the configured model reads real
   management messages (costs cents) — use it to compare models before trusting one.
3. Demo accounts armed for a week.
4. Live with `risk_pct_per_leg: 0.25`, then 1.0.

## Mirror one of my accounts to another

`mirrors:` copies **your own** trades from one MT5 account (the source) to an account at another
broker (the destination): every open, partial close, close and SL/TP change on the source is placed on
the destination through its SignalBridge, and messaged through the alert bot. Which account is which
is purely config: swap `source.login`/`events_dir` and `destination` to reverse it.

What is mirrored: source positions whose magic is in `include_magics` (default `[0]`, trades you
placed by hand; add your own EAs' magics) and, if `symbols` is set, whose symbol is listed. Never
mirrored, whatever the config says: copy-trading platform positions (a random 20–24 character id as
the comment, or IconTech's copy magic 2147483646), this bot's provider legs (`sig:`), CopierDestination
copies (`copy:`) and mirror trades (`m:`). Those are journaled as `mirror_filtered`, without a message.
Positions already open when a mirror first starts are not copied.

Nothing is ever sent to the source account: the mirror only reads CopierSource's events file.

### Setup

1. **Source terminal** (e.g. IconTech 4100074): install `CopierSource` (copied here from
   `mt5-copytrader`: `mql5/Include/CopyTrader` → `MQL5/Include/`, `mql5/Experts/CopyTrader/CopierSource.mq5`
   → `MQL5/Experts/CopyTrader/`, compile with F7), attach it to one chart with `InpExpectedLogin` = the source login.
   It needs the 2026-10-27 build or later (its events carry `login`/`magic`/`comment`; older lines are
   ignored with a `source login unknown` alert). It writes
   `Common/Files/<InpEventsFileBase>_events_YYYYMMDD.jsonl`; under Wine, Common/Files is
   `<prefix>/drive_c/users/<user>/AppData/Roaming/MetaQuotes/Terminal/Common/Files` (on this Mac:
   `~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/<user>/…`).
2. **Destination terminal** (e.g. 1514988624): SignalBridge as in *VPS setup* step 2, with
   `InpExpectedLogin` = the destination login and every destination symbol in `InpSymbols`. It must be a
   hedging account (one destination position per source position).
3. Config (with or without `providers:`):

```yaml
accounts:                          # destinations that are not a Telegram provider's terminal
  ftmo_demo:
    bridge_dir: /abs/path/to/destination/MQL5/Files/signalbridge
    expected_login: 1514988624
    live: false                    # a REAL destination is refused until this is true
mirrors:
  icon:                            # 1-8 chars a-z 0-9 _; destination comments are m:icon:<source position>
    source:
      login: 4100074               # events written by any other account are ignored (and alerted once)
      events_dir: /abs/path/to/Common/Files
      events_base: copytrader      # CopierSource InpEventsFileBase
      include_magics: [0]          # 0 = trades placed by hand
      symbols: { XAUUSD.p: XAUUSD, EURUSD.p: EURUSD }   # source → destination name; omit for all symbols, same names
    destination: ftmo_demo         # a name under accounts: or providers:
    sizing: { mode: source_lots, multiplier: 1.0 }      # same lots; or balance_ratio (× dest/source balance), or fixed_lots + lots
    max_lots: 1.0                  # cap on any destination trade
    stop_loss: copy                # copy | none | { fixed_pips: 20 }
    take_profit: copy
    pip_size: { XAUUSD: 0.1 }      # needed by fixed_pips except on 3/5-digit FX (10 points)
    mirror_closes: proportional    # a source partial close closes the same fraction (rounded to the lot step)
    max_open_age_sec: 300          # an OPEN read later than this (terminal down) is not copied
notify_trades: true                # false = journal only, no Telegram messages
poll_interval_sec: 0.1
```

   On a Windows VPS, `deploy/windows/SETUP.md` walks through all of this from a `git clone`, with
   install/status/run/latency `.bat` files and a ready config (`deploy/windows/mirror-config.yaml`).
4. `tg-trader status` shows each mirror (`armed (demo)`, the events file it reads, open mirrored
   positions); `tg-trader run` (or `--dry-run`) runs mirrors and providers in one loop.

Sizing rounds to the nearest lot step, never below the broker minimum, never above `max_lots`.
Contract sizes differ between brokers (e.g. index CFDs); check one trade's value and adjust
`multiplier` if they do. A partial close that would leave less than one lot step to close (e.g. a
0.01-lot destination) is left for the final close; the source's CLOSE always closes whatever remains.
A destination position that closes by itself (its own SL/TP, or by hand) is reported and not reopened.
After three failed commands on one position the mirror stops touching it and says so (`NEEDS A HAND
CHECK` in `status`).

Restarts: each source position's destination ticket is kept in the `mirror_positions` table and saved
before any command, and the events cursor only moves past an event once it has been acted on, so a
replayed OPEN is re-sent with the same `cmd_id` (SignalBridge executes it once) or adopted by its
comment, and a close that arrived while the trader was down is applied when it starts.

### Latency

Every mirrored action journals `mirror_latency`; `tg-trader mirror-latency` prints the last 50:
source event → read by the trader (`src->read`; the source timestamp is whole seconds, so ±1 s, and
both terminals must be on the same clock), read → command written, written → SignalBridge result.
For the fastest copies: `poll_interval_sec: 0.1` in the config and `InpPollMs = 100` on the
destination SignalBridge. CopierSource writes each event from `OnTradeTransaction` as it happens.

### Demo checklist (destination on a DEMO account, `live: false`)

- [ ] `tg-trader status`: the mirror line says `armed (demo)` and names today's events file.
- [ ] Open a trade by hand on the source → message *Source opened … / Placing … / Placed …*; the
      destination shows the same symbol/side, the configured lots, SL/TP per the modes, comment `m:<name>:<id>`.
- [ ] Move the source SL/TP → destination follows (`copy` modes only).
- [ ] Partially close the source → the destination closes the same fraction; message shows P/L.
- [ ] Close the source → the destination closes the rest; `status` no longer lists it.
- [ ] A copy-platform or EA trade on the source → nothing on the destination, no message, a
      `mirror_filtered` line in `tg-trader journal`.
- [ ] Stop `tg-trader`, open and close a trade on the source, start it again → the close is applied,
      nothing opens twice (an open older than `max_open_age_sec` is reported as not mirrored).
- [ ] Close the destination terminal for a minute while opening a source trade → *terminal not
      responding* message, then the trade is placed when it returns (within `max_open_age_sec`).
- [ ] Log the destination terminal into a different account → *Not mirrored: … wrong account*.
- [ ] `tg-trader mirror-latency` shows the timings you expect.

## Developing

`pytest -q` (Python, no MT5 needed). MQL5 bridge tests on macOS via the Wine harness:
`scripts/mt5-setup-test-terminal.sh`, then `scripts/mt5-test.sh SignalBridgeUnitTests|SignalBridgeExecTests|SignalBridgeCoreTests`.
