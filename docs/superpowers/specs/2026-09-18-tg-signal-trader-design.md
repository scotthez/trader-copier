# Telegram Signal Trader: Design Spec

Date: 2026-09-18

## Context

Scott is a member of two Telegram signal channels — **Lewis Inner Circle** and **WolvesVIP** — and
wants their trade calls executed automatically, each on its own live MT5 account:

| Provider | Account | Broker / server | Instruments seen |
|---|---|---|---|
| Lewis | 35186896 | PUPrime-Live 6 | NAS100, XAUUSD, GER40, EURGBP |
| Wolves | 4100074 | IconTech-Live | XAUUSD only |

This is a separate system from `mt5-copytrader` (which mirrors a live account to FTMO). The VPS is
**Linux, running MT5 terminals under Wine**, so the Windows-only `MetaTrader5` Python package is not
an option; execution has to happen inside the terminals, and the natural transport is files.

Two Telegram exports (Lewis: Mar–Sep 2026, ~390 entries; Wolves: 2023–Sep 2026, ~2,270 entries)
were analysed to fix the message formats below. They become the parser's regression corpus.

## Goal

Read both channels live, turn each entry message into a set of positions sized by risk on the
target account, manage them with a fixed TP ladder, and apply a small, unambiguous set of the
provider's management instructions — without ever letting an ambiguous or mis-parsed message move
money.

## Non-goals (v1)

- No trading of screenshots/images; text only.
- No "secure X %" partial-close or "Active ✅" (convert pending → market) automation. Both are
  classified and journaled, not executed, until a week of audit data says the wording is stable.
- No cross-provider netting or hedging logic; each provider is independent.
- No symbol other than those in each provider's mapping table (unmapped → refuse + log).
- No Windows support for the Python side (the VPS is Linux; macOS is for development).

## Architecture (approach B′)

```
Telegram ──MTProto──▶ tg-listener (Telethon) ──▶ SQLite inbox
                                                     │
                                                     ▼
                      tg-trader: parse → validate → classify (LLM) → SignalRun state machines
                                                     │ commands.jsonl / results.jsonl / state.json
                        ┌────────────────────────────┴────────────────────────────┐
                        ▼                                                         ▼
      PUPrime terminal (Wine) + SignalBridge EA                IconTech terminal (Wine) + SignalBridge EA
```

- **Python service (Linux, native):** all logic and state. Two systemd units:
  `tg-listener` (Telethon user session → SQLite `inbox`; nothing else) and `tg-trader`
  (everything downstream; freely restartable — state in SQLite + `state.json`).
- **`SignalBridge.mq5` (one identical build per terminal):** a thin, generic executor: tails
  `commands.jsonl`, executes via `CTrade` with the copier's proven primitives, appends
  `results.jsonl`, rewrites `state.json` every 500 ms.
- **Transport:** each terminal's `MQL5/Files/signalbridge/` directory, which is an ordinary
  directory on the Linux filesystem. Python is configured with each absolute path, so it does not
  matter whether terminals share a Wine prefix.

### Approaches considered and rejected

- **A — extend `CopierDestination` to execute Telegram events.** Rejected: the TP ladder must observe
  fills and then modify sibling legs, i.e. stateful logic on the terminal side; that would grow a
  mirroring EA into a signal manager in the harder-to-test language.
- **Python `MetaTrader5` package under Wine.** Rejected: Windows IPC under Wine is fragile; not for
  live money.
- **C — pure MQL5 signal EA with a thin Telegram relay.** Rejected: the MTProto listener cannot be
  MQL5 anyway, and LLM calls + ladder logic in MQL5 is the worst of both worlds for testing.

## Signal model and parsing

Every template parses to one normalised `Signal`:

```
Signal {
  id            "<provider>:<telegram_msg_id>"
  provider      lewis | wolves
  symbol        canonical: XAUUSD, NAS100, GER40, EURGBP …  ("Gold" → XAUUSD)
  side          BUY | SELL
  entry_type    MARKET | LIMIT
  entry_zone    [near, far] for LIMIT; empty for MARKET
  sl            price
  tps           [tp1, tp2, tp3, tp4]
  received_at   message time (UTC)
  raw_text
}
```

**TP4 rule:** if TP4 is missing or `OPEN`, `tp4 = tp3 + (tp3 − tp2)` (repeat the last increment).
Examples: Wolves TP1 4381 / TP2 4386 / TP3 4391 → TP4 4396; Lewis XAUUSD TP1 4356.27 / TP2 4357.42 /
TP3 4359.73 → TP4 4362.04.

**Templates** (regex, per provider, tried in order, first match wins; emoji, extra spaces and the
`SL:` / `SL` / `Stop Loss:` and `TP4: OPEN` / `TP: Open` variants are normalised first):

- Lewis: `TRADE SETUP:` / `TRADE IDEA:` block (`🔵  BUY NAS100` / `Stop Loss:` / `TP1..TP4`);
  `🔵 BUY NAS100 NOW!` with SL/TPs in the same message or in the *next* provider message within
  60 s (his April 2026 split-post style); `XAUUSD — BUY LIMIT SETUP / Entry Zone: a – b`.
- Wolves: `BUY XAUUSD @4347` / `SELL Limit XAUUSD @4290 4295` then `SL 4341` / `TP1..TP4 Open`
  (current); `Pair: XAUUSD / Side: Long / Buy [Limit] [2nd entry] / Entry: a [b] / SL: / TP1 …`
  (export style); `Gold buy now a - b / SL: / TP: / TP: / TP:` (older).

A `2nd entry` / `3rd entry` is an independent signal with its own legs.

**Arithmetic validation** (all must pass, otherwise the signal is REJECTED with a journaled reason
and never traded): SL strictly on the loss side of entry; every TP strictly on the profit side and
monotonic; SL distance within the per-symbol `sl_range`; MARKET entry within
`market_entry_tolerance_pct` of the current price from `state.json` and message age
≤ `max_signal_age_sec`; LIMIT zone within a per-symbol distance of the current price.

**LLM fallback for entries:** a provider message with SL-like and TP-like numbers that matches no
template is sent to the classifier with the *entry* schema; the result passes through the same
validation and is journaled as `parsed_by=llm`. Intended to be rare; every use is reviewed and
usually becomes a new template.

## SignalRun state machine and TP ladder

```
Leg { n: 1..4, tp, state: PENDING_ORDER | OPEN | CLOSED_TP | CLOSED_SL | CLOSED_MANUAL | CANCELLED,
      order_ticket?, position_ticket?, entry_price?, volume, sl_current }
SignalRun { signal, legs[4], state: NEW → PLACING → ACTIVE → DONE | REJECTED }
```

Persisted to SQLite after every transition; a restart resumes and first **reconciles** every ACTIVE
run against `state.json` (positions/orders found by comment `sig:<id>:L<n>`).

**Sizing (per leg):** `risk_pct_per_leg × balance ÷ (|entry − sl| ÷ tick_size × tick_value)`,
rounded down to `volume_step`, clamped to `[volume_min, volume_max]`, using the account balance and
symbol specs from `state.json`. Both providers start at **1.0 % per leg** (4 % per 4-TP signal).

**Placement:** MARKET → four `open_market` commands (own TP each, shared SL, comment
`sig:<id>:L<n>`). LIMIT → four `open_pending` limit orders at the **near** zone price with
`expires_at = received_at + pending_ttl_hours` (24 h); the bridge sets the broker expiration and
Python cancels on its own clock as a backstop. A leg rejected after the bridge's retries becomes
`CANCELLED` with the reason; the other legs proceed.

**Ladder** (Python polls `state.json` every 500 ms; a vanished position is resolved against
`deals_recent` by `DEAL_REASON`):

| Event | Action |
|---|---|
| L1 `CLOSED_TP` | nothing |
| L2 `CLOSED_TP` | L3 and L4 SL → each leg's **own entry price** (break-even) |
| L3 `CLOSED_TP` | L4 SL → **TP1 price** |
| any leg `CLOSED_SL` while other legs are still pending | cancel remaining pendings |
| L4 closed / SL hit | run → DONE |

SL moves are **one-way**: a proposed SL is sent only if it locks in more than the current SL.

**Management actions** (from the classifier; `management_actions` config lists which are
executable — v1 is exactly this set):

| Action | Effect on the referenced run |
|---|---|
| `close_all` | close open legs, cancel pendings → DONE |
| `cancel_pending` | cancel pendings only |
| `move_sl(price)` | all open legs, one-way rule, price must be on the correct side of entry |
| `break_even` | each open leg's SL → its own entry |

**Reference resolution:** a Telegram *reply* targets the run of the replied-to message; otherwise
the single most recent ACTIVE run for that provider. Two ACTIVE runs and not a reply → journaled,
not applied (ambiguity never moves money).

## Bridge protocol

Directory: `<terminal>/MQL5/Files/signalbridge/`. Files:

- **`commands.jsonl`** — Python appends; EA tails with a persisted byte-offset cursor (the copier's
  `EventLog.mqh` mechanics). One JSON object per line with a unique `cmd_id`
  (`<signal_id>:L<n>:<seq>`). Types: `open_market {symbol, side, volume, sl, tp, comment}`,
  `open_pending {…, price, expires_at}`, `modify_sl {position, sl}`, `close {position}`,
  `cancel {order}`.
- **`results.jsonl`** — EA appends exactly one line per command:
  `{cmd_id, ok, retcode, retcode_text, position?, order?, fill_price?, attempts}`.
- **`state.json`** — EA rewrites every 500 ms via `state.tmp` + `FileMove` (never torn):
  `{ts, account{login, balance, equity, margin_free, hedging}, symbols{<name>{bid, ask, digits,
  point, volume_step, volume_min, volume_max, tick_value, tick_size, trade_allowed}},
  positions[{ticket, symbol, type, volume, price_open, sl, tp, comment, magic}],
  orders[{ticket, symbol, type, volume, price, sl, tp, comment, expiration}],
  deals_recent[≤200: {ticket, position_id, entry, reason, price, volume, time}]}`.

**`SignalBridge.mq5` rules** (inputs: `InpMagic`, `InpExpectedLogin`, `InpPollMs`,
`InpDeviationPoints`, `InpMaxRetries`, `InpSymbols` — comma list kept in Market Watch and `state`):

- Idempotent: a `cmd_id` already in `results.jsonl` is never re-executed; the cursor is persisted
  after every result line.
- Execution reuses the copier's primitives: transient-only retries, stop normalisation to
  `SYMBOL_DIGITS`, `SetTypeFillingBySymbol`, partial-fill check on close, `IsStopped` hold.
- Every order carries `InpMagic` + the comment, so Python can reconcile by comment.
- Refuses to start on the wrong login or a non-hedging account.

**Python `BridgeClient`** (one per terminal; `FakeBridge` implements the same interface for tests).
`state.json` is authoritative for what is open; `results.jsonl` only accelerates learning tickets.
If `state.ts` is older than 5 s the terminal is **stale**: no new placements; management commands
still queue.

## Configuration and guards

`config.yaml` (no secrets):

```yaml
providers:
  lewis:
    telegram_chat: <channel id>          # resolved by title on first run, then stored
    bridge_dir: /abs/path/to/PUPrime/MQL5/Files/signalbridge
    risk_pct_per_leg: 1.0
    symbols: { NAS100: NAS100, XAUUSD: XAUUSD, GER40: GER40, EURGBP: EURGBP }   # canonical → broker
    sl_range: { XAUUSD: [1, 60], NAS100: [5, 500], GER40: [5, 400], EURGBP: [0.0005, 0.02] }
    market_entry_tolerance_pct: 0.3
    max_signal_age_sec: 120
    pending_ttl_hours: 24
    management_actions: [close_all, cancel_pending, move_sl, break_even]
    max_open_signals: 2
    max_legs_open: 8
    daily_loss_stop_pct: 5.0
  wolves: { …, bridge_dir: …/IconTech/…, symbols: { XAUUSD: XAUUSD }, sl_range: { XAUUSD: [1, 60] }, … }
llm: { model: <pinned at implementation from the current Claude API reference>, confidence_threshold: 0.8, timeout_sec: 8 }
```

Secrets (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ANTHROPIC_API_KEY`) via environment/`.env`;
the Telethon `.session` file is mode 0600.

**Guards checked before every placement (a "no" is journaled, never silent):** `max_open_signals`
per provider; `max_legs_open`; `daily_loss_stop_pct` of start-of-day balance (realised + floating
for that provider's account — no new signals until the next day; existing ladders continue);
terminal stale or `trade_allowed=false`.

## LLM management classifier

- Input: message text, provider, compact context (referenced run: symbol, side, entry, SL, open legs;
  last 3 provider messages).
- Output must validate against
  `{"action": "none"|"close_all"|"cancel_pending"|"move_sl"|"break_even", "price": number|null,
  "confidence": 0..1, "reason": string}`; schema failure = `none`.
- Executable only if `action ∈ management_actions` and `confidence ≥ threshold`; otherwise journaled.
- `move_sl` price is re-validated by the state machine (correct side, one-way).
- API error/timeout → `none`, journaled. Management is never blocked on the LLM: the worst case is a
  missed instruction, never a wrong trade.
- Every classification is stored (`classifications` table: input, output, executed?) for weekly
  audit and for widening `management_actions` from data.
- Expected volume ~10–40 messages/day; a cheap, fast model is sufficient.

## Operations on the VPS

- Two additional Wine MT5 instances (PUPrime, IconTech), each a separate portable install so all
  terminals run concurrently; setup scripted. `SignalBridge.ex5` on one chart each with
  `InpExpectedLogin` set.
- `tg-listener` first run prompts once for phone + code (+ 2FA); the session persists.
- `tg-trader` start: reconcile ACTIVE runs → mark inbox messages older than `max_signal_age_sec`
  as `stale` (never traded; management still applied to ACTIVE runs) → process.
- Modes: `--dry-run` (full pipeline, commands go to the journal instead of `commands.jsonl`);
  `--replay <export.html>` (feeds a Telegram export through parser + classifier).
- Observability: `journal` table (parsed/rejected, command/result, ladder transition,
  classification, guard); `status` CLI; optional Telegram DM alerts (signal rejected, command
  failed after retries, terminal stale > 60 s, daily stop triggered).
- Tooling: Python 3.11+, `telethon`, `anthropic`, `pydantic`, `pyyaml`, `pytest`; SQLite (stdlib).
  Repo layout: `tg_signal_trader/` (package), `mql5/` (bridge EA + its tests, reusing
  `mt5-copytrader`'s `scripts/` harness), `tests/`, `fixtures/` (the two exports, redacted to
  provider messages only).

## Testing

**Python (pytest, no MT5):**
- Parser corpus: every historical entry from both exports plus Scott's two hand-written Wolves
  examples; asserts parsed/rejected counts per template and pins ~30 hand-checked signals to exact
  fields.
- Validation: pass and fail case per rule.
- State machine against `FakeBridge`: full ladder; SL hit cancels pendings; one leg rejected does not
  block others; pending expiry; every management action incl. the one-way SL rule; reply vs
  most-recent resolution and the two-active rule; restart mid-run resumes and reconciles by comment.
- Sizing with real XAUUSD/NAS100 specs; rounding/clamping; daily-loss and max-open guards.
- Classifier with a mocked LLM client: schema enforcement, threshold, disallowed actions,
  timeout → `none`. Opt-in manual live test over ~40 real Wolves management messages reporting a
  confusion matrix.

**MQL5 bridge (Strategy Tester via the existing Wine harness):** `SignalBridgeTests.mq5` feeds a
`commands.jsonl` and asserts real simulated fills: market open with SL/TP, pending with expiration,
modify_sl, close, cancel, idempotent re-run of a `cmd_id`, one result per command, atomic and
parseable `state.json`, refusal on wrong login.

**Before real money (README checklist):**
1. `--replay` both exports; review every reject.
2. `--dry-run` against the live channels for 3–5 trading days; read the journal daily.
3. Arm on a **demo** account per terminal for a week. This needs a PUPrime demo and an IconTech demo
   (or any two demo terminals) on the VPS — the FTMO/copier terminals are not part of this system and
   need not be set up first.
4. Go live with `risk_pct_per_leg: 0.25`; raise to 1.0 after a full week of correct ladders.
