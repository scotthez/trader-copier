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
`move_sl` and `break_even` are executed, and only above the confidence threshold. Known boilerplate
(e.g. Lewis's "you can put your stop-loss to break-even if you wish" after TP1) is listed in
`ignore_patterns` and never reaches the model.

Entries are cross-checked: with `llm.entry_crosscheck: true` the model reads every template-parsed
entry independently and the trade only goes ahead if both agree on symbol, side, entry type, SL and
TP1–TP3 (a disagreement is journaled as `crosscheck:<field>`; if the model is unavailable the
template result stands and `entry_crosscheck_unavailable` is journaled).

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

## Developing

`pytest -q` (Python, no MT5 needed). MQL5 bridge tests on macOS via the Wine harness:
`scripts/mt5-setup-test-terminal.sh`, then `scripts/mt5-test.sh SignalBridgeUnitTests|SignalBridgeExecTests|SignalBridgeCoreTests`.
