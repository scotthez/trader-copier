"""Account -> account trade mirror: CopierSource events on one MT5 account become trades on another
account's SignalBridge, with config-driven sizing and SL/TP, and a Telegram message for each step.

Only the account owner's own trades are mirrored. A source position is mirrored when its magic is in
`include_magics` (0 = placed by hand), its symbol is allowed, and its comment does not mark it as a
copy-trading platform's, a signal bot's or a mirror's own trade. Copy-trading platform positions are
mirrored only when `source.include_copy_trades` is set (demo/learning only); the signal bot, copier
and mirror exclusions are not configurable.

Nothing is ever sent to the source account: the source side is read-only (its events file).

State per mirrored source position lives in the store's mirror_positions table and is saved before
every command, so a restart never opens a source position twice (a replayed OPEN re-sends the same
cmd_id, which SignalBridge executes at most once) and never misses a close (the events cursor only
moves past an event once it has been acted on).
"""
from __future__ import annotations
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from pydantic import BaseModel, ValidationError
from .alerts import Alerter, NullAlerter
from .bridge import Bridge, BridgeState, Command, CommandResult, SymbolSpec, parse_ts
from .config import COPY_PLATFORM_MAGICS, AppConfig, MirrorConfig
from .notify import TradeNotifier, _fmt
from .store import Store

STALE_STATE_SEC = 5.0
MISSING_GRACE_SEC = 30          # destination position gone without a closing deal -> treated as closed after this
MAX_COMMAND_FAILURES = 3        # per position: after this many failed commands the mirror stops and asks for a hand check

OPENING, OPEN, CLOSED, FAILED = "OPENING", "OPEN", "CLOSED", "FAILED"

# Copy-trading platforms (IconFX/IconTech copy trading among them) stamp each copied position with a
# random 20-24 character base64 id (and, on IconTech, magic 2147483646: config.COPY_PLATFORM_MAGICS).
# A comment like that is never a hand-placed trade of the owner's.
_COPY_PLATFORM_ID = re.compile(r"^[A-Za-z0-9+/_-]{20,24}={0,2}$")
_MIRROR_COMMENT = re.compile(r"^m:[a-z0-9_]{1,8}:\d+$")


def exclusion_reason(comment: str, include_copy_trades: bool = False) -> str | None:
    """Why a source position is someone else's trade and must not be mirrored (None = it may be).
    include_copy_trades lets copy-trading platform positions through; the other exclusions always apply."""
    c = (comment or "").strip()
    if c.startswith("sig:"):
        return "signal_bot_trade"          # tg-signal-trader's own provider legs
    if c.startswith("copy:"):
        return "copier_trade"              # CopierDestination's copies
    if _MIRROR_COMMENT.match(c):
        return "mirror_trade"              # this mirror's own trades (stops A->B->A loops)
    if not include_copy_trades and _COPY_PLATFORM_ID.match(c) and re.search(r"\d", c) and re.search(r"[A-Za-z]", c):
        return "copy_platform_trade"
    return None


def mirror_comment(mirror: str, source_pos_id: int) -> str:
    return f"m:{mirror}:{source_pos_id}"


class SourceEvent(BaseModel):
    """One CopierSource line (mt5-copytrader Event.mqh)."""
    event_id: str
    ts: str = ""
    type: str
    source_pos_id: int
    symbol: str = ""
    direction: str = ""
    volume: float = 0.0
    remaining: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    source_balance: float = 0.0
    login: int = 0
    magic: int = 0
    comment: str = ""


class MirrorPosition(BaseModel):
    mirror: str
    source_pos_id: int
    symbol: str                     # source symbol
    dest_symbol: str
    side: str                       # BUY | SELL
    state: str = OPENING
    source_volume_open: float       # source lots when it opened
    source_volume: float            # source lots still open (0 after its CLOSE)
    source_open_price: float = 0.0
    dest_volume_open: float         # destination lots placed
    dest_volume: float = 0.0        # destination lots still open
    dest_ticket: int | None = None
    dest_open_price: float = 0.0
    sl: float = 0.0                 # SL/TP on the destination position
    tp: float = 0.0
    want_sl: float = 0.0            # SL/TP the destination should have (follows source MODIFYs in copy mode)
    want_tp: float = 0.0
    seq: int = 0
    inflight_cmd: str | None = None
    inflight_kind: str | None = None        # open | close | partial | sltp
    inflight_fields: dict = {}
    open_command: dict = {}         # the open_market command, re-sent unchanged if its OPEN event is replayed
    failures: int = 0
    attention: str = ""             # set when the mirror gave up on this position; no more automatic commands
    missing_since: str | None = None
    source_event_ts: str = ""       # source terminal's time of the event behind the inflight command (whole seconds)
    read_at: str | None = None      # when this service read that event
    sent_at: str | None = None      # when the inflight command was written to the destination bridge
    realised: float = 0.0           # destination P/L in account currency (estimate from tick value)
    reason: str = ""

    def next_cmd_id(self) -> str:
        self.seq += 1
        return f"{mirror_comment(self.mirror, self.source_pos_id)}:{self.seq}"


# ---- pure rules ------------------------------------------------------------------------------------

def _step_digits(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step)))) if step > 0 else 8


def round_to_step(volume: float, step: float) -> float:
    """Nearest multiple of the broker's volume step (halves round up), like CopierDestination."""
    if step <= 0:
        return volume
    return round(math.floor(volume / step + 0.5 + 1e-9) * step, _step_digits(step))


def floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    return round(math.floor(volume / step + 1e-9) * step, _step_digits(step))


def mirror_volume(m: MirrorConfig, source_volume: float, source_balance: float, dest_balance: float,
                  spec: SymbolSpec) -> tuple[float | None, str | None]:
    """Destination lots for a source position: (lots, None) or (None, reason).
    balance_ratio = source lots x destination balance / source balance x multiplier; source_lots = source
    lots x multiplier (same size at 1.0); fixed_lots = lots.
    Rounded to the nearest volume step, raised to the broker minimum, capped at max_lots and the broker maximum."""
    if m.sizing.mode == "fixed_lots":
        raw = m.sizing.lots
    elif m.sizing.mode == "source_lots":
        raw = source_volume * m.sizing.multiplier
    else:
        if source_balance <= 0:
            return None, "source_balance_missing"
        if dest_balance <= 0:
            return None, "destination_balance_missing"
        raw = source_volume * dest_balance / source_balance * m.sizing.multiplier
    cap = min(floor_to_step(m.max_lots, spec.volume_step), spec.volume_max)
    if cap < spec.volume_min - 1e-9:
        return None, "max_lots_below_volume_min"
    lots = max(round_to_step(raw, spec.volume_step), spec.volume_min)
    return round(min(lots, cap), _step_digits(spec.volume_step)), None


def pip_size(m: MirrorConfig, dest_symbol: str, spec: SymbolSpec) -> float | None:
    """Configured pip_size, else 10 points on a 3/5-digit symbol (FX); None when it cannot be known."""
    if dest_symbol in m.pip_size:
        return m.pip_size[dest_symbol]
    return 10 * spec.point if spec.digits in (3, 5) else None


def mirror_stops(m: MirrorConfig, side: str, source_sl: float, source_tp: float, dest_symbol: str,
                 spec: SymbolSpec) -> tuple[float, float, str | None]:
    """(sl, tp, None) for a new destination trade, or (0, 0, reason). 0 = no stop."""
    ref = spec.ask if side == "BUY" else spec.bid
    sign = 1 if side == "BUY" else -1
    out = []
    for mode, source_value, direction in ((m.stop_loss, source_sl, -1), (m.take_profit, source_tp, 1)):
        if mode == "copy":
            out.append(round(source_value, spec.digits) if source_value > 0 else 0.0)
        elif mode == "none":
            out.append(0.0)
        else:
            pip = pip_size(m, dest_symbol, spec)
            if pip is None:
                return 0.0, 0.0, f"pip_size_unknown:{dest_symbol}"
            out.append(round(ref + sign * direction * mode.fixed_pips * pip, spec.digits))
    return out[0], out[1], None


def target_dest_volume(mp: MirrorPosition, spec: SymbolSpec | None) -> float:
    """Destination lots that should stay open: the source's remaining fraction of the placed lots,
    rounded to the step; 0 once the source closed or when the rest would be under the broker minimum."""
    if mp.source_volume <= 1e-9 or mp.source_volume_open <= 0:
        return 0.0
    if mp.source_volume >= mp.source_volume_open - 1e-9:
        return mp.dest_volume_open
    step = spec.volume_step if spec else 0.01
    target = round_to_step(mp.dest_volume_open * mp.source_volume / mp.source_volume_open, step)
    return 0.0 if spec and target < spec.volume_min - 1e-9 else target


def _pl(mp: MirrorPosition, close_price: float, volume: float, spec: SymbolSpec | None) -> float | None:
    if spec is None or spec.tick_size <= 0 or not mp.dest_open_price or not close_price:
        return None
    sign = 1 if mp.side == "BUY" else -1
    return round(sign * (close_price - mp.dest_open_price) / spec.tick_size * spec.tick_value * volume, 2)


# ---- alerts ------------------------------------------------------------------------------------------

MIRROR_REASONS = {
    "stale": "source trade is older than max_open_age_sec",
    "no_quote": "destination has no price for this symbol (add it to SignalBridge InpSymbols)",
    "trade_not_allowed": "broker has trading disabled for this symbol",
    "source_balance_missing": "source event carries no balance",
    "destination_balance_missing": "destination balance is 0",
    "max_lots_below_volume_min": "max_lots is below the broker's minimum lot",
    "login_mismatch": "destination MT5 logged into the wrong account",
    "real_account_not_armed": "destination is a REAL account but live: false",
}


def _reason(code: str) -> str:
    if code.startswith("pip_size_unknown:"):
        return f"fixed_pips needs pip_size for {code.split(':', 1)[1]} in the mirror config"
    return MIRROR_REASONS.get(code.split(":")[0], code)


def _stops(sl: float, tp: float) -> str:
    return f"SL {_fmt(sl) if sl else 'none'} · TP {_fmt(tp) if tp else 'none'}"


def _timing(d: dict) -> str:
    """'⏱ 1.4 s after the source (read 0.31 · send 0.01 · fill 1.08)' from a latency dict; '' without one."""
    lat = d.get("latency") or {}
    if "source_to_result_s" not in lat:
        return ""
    parts = [f"{name} {lat[key]:.2f}" for name, key in (("read", "source_to_read_s"), ("send", "read_to_send_s"), ("fill", "send_to_result_s"))
             if key in lat]
    return f"\n⏱ {lat['source_to_result_s']:.2f} s after the source" + (f" ({' · '.join(parts)})" if parts else "")


def _gap(d: dict) -> str:
    """Destination fill vs the source's price, in pips (10 points), signed so + is worse for the trade."""
    if d.get("gap_pips") is None:
        return ""
    g = d["gap_pips"]
    return f" · {abs(g):.1f} pips {'worse' if g > 0 else 'better'} than source" if abs(g) >= 0.05 else " · same price as source"


def gap_pips(side: str, source_price: float, fill_price: float, spec: SymbolSpec | None) -> float | None:
    if spec is None or spec.point <= 0 or not source_price or not fill_price:
        return None
    sign = 1 if side == "BUY" else -1
    return round(sign * (fill_price - source_price) / (10 * spec.point), 1)


def mirror_event_line(kind: str, d: dict) -> str | None:
    """One message line for a mirror journal event, or None for events that are journal-only."""
    if kind == "mirror_source_open":
        return f"📥 Source opened {d['side']} {_fmt(d['volume'])} {d['symbol']} @ {_fmt(d['price'])} ({_stops(d['sl'], d['tp'])})"
    if kind == "mirror_open_sent":
        return f"➡️ Placing {_fmt(d['volume'])} lots {d['dest_symbol']} on {d['destination']} ({_stops(d['sl'], d['tp'])})"
    if kind == "mirror_placed":
        return f"✅ Placed {_fmt(d['volume'])} lots @ {_fmt(d['price'])} on {d['destination']} (#{d['ticket']}){_gap(d)}{_timing(d)}"
    if kind == "mirror_rejected":
        return f"⛔ Not mirrored: {_reason(d['reason'])}"
    if kind == "mirror_open_failed":
        return f"❗ Destination refused the trade: {d.get('text')}"
    if kind == "mirror_source_close":
        if d.get("remaining"):
            return f"📤 Source closed {_fmt(d['volume'])} @ {_fmt(d['price'])} ({_fmt(d['remaining'])} still open)"
        return f"📤 Source closed @ {_fmt(d['price'])}"
    if kind == "mirror_close_too_small":
        return f"↔️ {_fmt(d['dest_volume'])} lots can't be split in that proportion: the rest closes with the source's final close"
    if kind == "mirror_closed":
        pl = f" · P/L ≈ {d['pl']:+.2f}" if d.get("pl") is not None else ""
        if d.get("left"):
            return f"✂️ Closed {_fmt(d['volume'])} lots @ {_fmt(d['price'])} ({_fmt(d['left'])} left){pl}{_timing(d)}"
        total = f" · trade P/L ≈ {d['total']:+.2f}" if d.get("total") is not None and d.get("partial_before") else ""
        return f"✋ Closed {_fmt(d['volume'])} lots @ {_fmt(d['price'])}{pl}{total}{_timing(d)}"
    if kind == "mirror_dest_closed":
        how = {"SL": "🛑 Destination hit its SL", "TP": "🎯 Destination hit its TP"}.get(d.get("reason"), "✋ Destination position closed outside the mirror")
        price = f" @ {_fmt(d['price'])}" if d.get("price") else ""
        pl = f" · P/L ≈ {d['pl']:+.2f}" if d.get("pl") is not None else ""
        return how + price + pl
    if kind == "mirror_sltp_set":
        return f"🔒 Destination {_stops(d['sl'], d['tp'])}"
    if kind == "mirror_command_failed":
        return f"❗ {d.get('type')} failed on the destination: {d.get('text')}"
    if kind == "mirror_attention":
        return f"⚠️ Gave up after {d.get('failures')} failed commands: check {d.get('destination')} by hand"
    if kind == "mirror_source_login":
        return f"⚠️ Events file written by account {d.get('login') or 'unknown (old CopierSource build)'}, mirror expects {d.get('expected')}: ignoring it"
    if kind == "mirror_waiting":
        return f"⏳ Destination {d.get('destination')} terminal not responding ({d.get('why')}): source events wait"
    if kind == "mirror_resumed":
        return f"✅ Destination {d.get('destination')} back: mirroring resumed"
    return None


# ---- engine ------------------------------------------------------------------------------------------

_EVENTS_FILE = re.compile(r"_events_(\d{8})\.jsonl$")


class MirrorEngine:
    """Runs every configured mirror once per tick(). `bridges` maps each mirror destination name to its bridge."""

    def __init__(self, cfg: AppConfig, store: Store, bridges: dict[str, Bridge], alerter: Alerter | None = None,
                 now_local: Callable[[], datetime] | None = None):
        self.cfg, self.store, self.bridges = cfg, store, bridges
        self.alerter = alerter or NullAlerter()
        self.now_local = now_local or datetime.now
        self.notifier = TradeNotifier(lambda text: self.alerter.send(text), lambda _id: None, line=mirror_event_line)
        if cfg.notify_trades:
            store.listeners.append(self._on_journal)
        self._login_warned: set[tuple[str, int]] = set()
        self._waiting: dict[str, str] = {}             # mirror -> why its events are held
        self._waiting_since: dict[str, datetime] = {}  # mirror -> first tick its events were held
        self._blocked_notice: dict[str, str] = {}      # destination -> arming block already journaled

    def _on_journal(self, provider: str, kind: str, detail: dict, run_id: str | None) -> None:
        if provider.startswith("mirror:"):
            self.notifier.on_event(provider, kind, detail, run_id)

    # ---- persistence -----------------------------------------------------------------------------
    def get(self, mirror: str, source_pos_id: int) -> MirrorPosition | None:
        raw = self.store.get_mirror_position(mirror, source_pos_id)
        return MirrorPosition.model_validate_json(raw) if raw else None

    def positions(self, mirror: str, states: list[str] | None = None) -> list[MirrorPosition]:
        return [MirrorPosition.model_validate_json(r) for r in self.store.mirror_positions(mirror, states)]

    def _save(self, mp: MirrorPosition) -> None:
        self.store.save_mirror_position(mp.mirror, mp.source_pos_id, mp.state, mp.model_dump_json())

    def _journal(self, mirror: str, kind: str, detail: dict, mp: MirrorPosition | None = None,
                 event: SourceEvent | None = None) -> None:
        pos_id = mp.source_pos_id if mp else (event.source_pos_id if event else None)
        if pos_id is not None:
            symbol, side = (mp.symbol, mp.side) if mp else (event.symbol, event.direction)
            detail = {"header": f"[mirror {mirror}] {symbol} {side} (source #{pos_id})", **detail}
        self.store.journal(f"mirror:{mirror}", kind, detail, run_id=f"{mirror}:{pos_id}" if pos_id is not None else None)

    # ---- arming ------------------------------------------------------------------------------------
    def arming_block(self, mirror: str, state: BridgeState | None) -> str | None:
        """Same rule as the signal trader's: nothing goes to a destination on the wrong login, or to a REAL account unless live."""
        if state is None:
            return None
        dest = self.cfg.mirror_destination(mirror)
        if dest.expected_login and state.account.login != dest.expected_login:
            return f"login_mismatch: terminal is {state.account.login}, config expects {dest.expected_login}"
        if state.account.trade_mode == "REAL" and not dest.live:
            return "real_account_not_armed: set live: true and expected_login for this destination"
        return None

    def _unavailable(self, state: BridgeState | None) -> str | None:
        if state is None:
            return "no state.json"
        age = state.age_sec(self.now_local())
        return f"state.json not updated for {age:.0f}s" if age > STALE_STATE_SEC else None

    # ---- loop ----------------------------------------------------------------------------------------
    def tick(self) -> None:
        by_dest: dict[str, list[str]] = {}
        for name, m in self.cfg.mirrors.items():
            by_dest.setdefault(m.destination, []).append(name)
        for dest, names in by_dest.items():
            bridge = self.bridges[dest]
            state = bridge.read_state()
            results = bridge.read_results()
            for name in names:
                block = self.arming_block(name, state)
                if block and self._blocked_notice.get(name) != block:
                    self._blocked_notice[name] = block
                    self.store.journal(f"mirror:{name}", "arming_blocked", {"reason": block, "login": state.account.login, "trade_mode": state.account.trade_mode})
                elif not block:
                    self._blocked_notice.pop(name, None)
                self._apply_results(name, results, state)
                self._process_events(name, state, bridge, block)
                self._sync(name, state, bridge, block)
        self.notifier.flush()

    # ---- source events --------------------------------------------------------------------------
    def _cursor_key(self, mirror: str) -> str:
        return f"mirror_cursor:{mirror}"

    def _files(self, mirror: str) -> list[Path]:
        src = self.cfg.mirrors[mirror].source
        try:
            files = [p for p in Path(src.events_dir).glob(f"{src.events_base}_events_*.jsonl")
                     if _EVENTS_FILE.search(p.name) and p.name[:-len("_events_YYYYMMDD.jsonl")] == src.events_base]
        except OSError:
            return []
        return sorted(files, key=lambda p: p.name)

    def cursor(self, mirror: str) -> dict:
        """{"file": name or None, "offset": bytes}. First start: the end of the newest file, so trades
        that were already open (or closed) before the mirror existed are never replayed."""
        raw = self.store.kv_get(self._cursor_key(mirror))
        if raw:
            return json.loads(raw)
        files = self._files(mirror)
        cur = {"file": files[-1].name, "offset": files[-1].stat().st_size} if files else {"file": None, "offset": 0}
        self.store.kv_set(self._cursor_key(mirror), json.dumps(cur))
        self.store.journal(f"mirror:{mirror}", "mirror_started", {"cursor": cur, "events_dir": str(self.cfg.mirrors[mirror].source.events_dir)})
        return cur

    def _next_lines(self, mirror: str, cur: dict) -> tuple[list[tuple[str, dict]], dict]:
        """Complete lines after the cursor, each with the cursor just past it; moves on to the next
        day's file once the current one is drained. Returns (lines, cursor at the end)."""
        src_dir = Path(self.cfg.mirrors[mirror].source.events_dir)
        out: list[tuple[str, dict]] = []
        files = [p.name for p in self._files(mirror)]
        name, offset = cur["file"], cur["offset"]
        if name is None:
            if not files:
                return out, cur
            name, offset = files[0], 0
        while True:
            try:
                with open(src_dir / name, "rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                data = b""
            consumed = 0
            while True:
                nl = data.find(b"\n", consumed)
                if nl < 0:
                    break
                line = data[consumed:nl].decode("utf-8", errors="replace").strip()
                consumed = nl + 1
                out.append((line, {"file": name, "offset": offset + consumed}))
            offset += consumed
            later = [f for f in files if f > name]
            if consumed < len(data) or not later:     # a partial last line stays for the next tick
                return out, {"file": name, "offset": offset}
            name, offset = later[0], 0
            out.append(("", {"file": name, "offset": 0}))   # records the rotation even if the new file is empty

    def _process_events(self, mirror: str, state: BridgeState | None, bridge: Bridge, block: str | None) -> None:
        cur = self.cursor(mirror)
        lines, end = self._next_lines(mirror, cur)
        for line, after in lines:
            if line:
                try:
                    ev = SourceEvent.model_validate_json(line)
                except ValidationError:
                    self.store.journal(f"mirror:{mirror}", "mirror_bad_line", {"line": line[:300], "file": after["file"]})
                    ev = None
                if ev is not None and not self._handle(mirror, ev, state, bridge, block):
                    return          # destination unavailable: this event waits, cursor stays before it
            self.store.kv_set(self._cursor_key(mirror), json.dumps(after))
        if not lines and end != cur:
            self.store.kv_set(self._cursor_key(mirror), json.dumps(end))

    def _wait(self, mirror: str, why: str) -> bool:
        """Holds the event. Announced once the destination has been unavailable for terminal_alert_sec:
        a single unreadable state.json (Windows refuses the read while the EA replaces the file) is not news."""
        now = self.now_local()
        since = self._waiting_since.setdefault(mirror, now)
        if mirror not in self._waiting and (now - since).total_seconds() >= self.cfg.terminal_alert_sec:
            self._waiting[mirror] = why
            self.store.journal(f"mirror:{mirror}", "mirror_waiting", {"destination": self.cfg.mirrors[mirror].destination, "why": why})
        return False

    def _resume(self, mirror: str) -> None:
        self._waiting_since.pop(mirror, None)
        if self._waiting.pop(mirror, None) is not None:
            self.store.journal(f"mirror:{mirror}", "mirror_resumed", {"destination": self.cfg.mirrors[mirror].destination})

    def _handle(self, mirror: str, ev: SourceEvent, state: BridgeState | None, bridge: Bridge, block: str | None) -> bool:
        """Acts on one source event. False = the destination is unavailable; retry this event next tick."""
        m = self.cfg.mirrors[mirror]
        if ev.login != m.source.login:
            key = (mirror, ev.login)
            if key not in self._login_warned:
                self._login_warned.add(key)
                self.store.journal(f"mirror:{mirror}", "mirror_source_login", {"login": ev.login, "expected": m.source.login, "event_id": ev.event_id})
            return True
        if ev.type == "OPEN":
            return self._on_open(mirror, m, ev, state, bridge, block)
        mp = self.get(mirror, ev.source_pos_id)
        if mp is None or mp.state in (CLOSED, FAILED):
            self._journal(mirror, "mirror_event_ignored", {"type": ev.type, "event_id": ev.event_id,
                          "why": "not mirrored" if mp is None else f"mirror position {mp.state}"}, event=ev)
            return True
        if ev.type in ("CLOSE", "PARTIAL_CLOSE"):
            remaining = 0.0 if ev.type == "CLOSE" else ev.remaining
            if remaining >= mp.source_volume - 1e-9:
                self._journal(mirror, "mirror_event_ignored", {"type": ev.type, "event_id": ev.event_id, "why": "already applied"}, mp)
                return True
            mp.source_volume = round(remaining, 8)
            mp.source_event_ts, mp.read_at = ev.ts, self.now_local().isoformat()
            self._journal(mirror, "mirror_source_close", {"event_id": ev.event_id, "volume": ev.volume, "remaining": remaining, "price": ev.price}, mp)
            if remaining > 0:
                spec = state.symbols.get(mp.dest_symbol) if state else None
                target = target_dest_volume(mp, spec)
                if target >= mp.dest_volume - 1e-9 and mp.dest_volume > 0:
                    self._journal(mirror, "mirror_close_too_small", {"dest_volume": mp.dest_volume, "target": target}, mp)
        elif ev.type == "MODIFY":
            if m.stop_loss == "copy":
                mp.want_sl = ev.sl
            if m.take_profit == "copy":
                mp.want_tp = ev.tp
            mp.source_event_ts, mp.read_at = ev.ts, self.now_local().isoformat()
            self._journal(mirror, "mirror_source_modify", {"event_id": ev.event_id, "sl": ev.sl, "tp": ev.tp}, mp)
        else:
            self._journal(mirror, "mirror_event_ignored", {"type": ev.type, "event_id": ev.event_id, "why": "unknown type"}, mp)
            return True
        self._save(mp)
        return True          # the destination is brought in line by _sync

    def _filtered(self, m: MirrorConfig, ev: SourceEvent) -> str | None:
        copy_ok = m.source.include_copy_trades
        if ev.magic in COPY_PLATFORM_MAGICS:
            if not copy_ok:
                return "copy_platform_trade"
        elif ev.magic not in m.source.include_magics:
            return f"magic {ev.magic} not in include_magics"
        if m.source.symbols and ev.symbol not in m.source.symbols:
            return f"symbol {ev.symbol} not in symbols"
        return exclusion_reason(ev.comment, include_copy_trades=copy_ok)

    def _on_open(self, mirror: str, m: MirrorConfig, ev: SourceEvent, state: BridgeState | None, bridge: Bridge, block: str | None) -> bool:
        read_at = self.now_local()
        mp = self.get(mirror, ev.source_pos_id)
        if mp is not None:
            if mp.state == OPENING and mp.inflight_kind == "open" and mp.open_command:
                # Replayed OPEN (restart between saving and sending): send the same cmd_id again;
                # SignalBridge executes a cmd_id at most once.
                bridge.send(Command.model_validate(mp.open_command))
                self._journal(mirror, "mirror_open_resent", {"cmd_id": mp.inflight_cmd}, mp)
            else:
                self._journal(mirror, "mirror_event_ignored", {"type": "OPEN", "event_id": ev.event_id, "why": f"already mirrored ({mp.state})"}, mp)
            return True
        why = self._filtered(m, ev)
        if why:
            # Not the owner's trade (or not in scope): journal only, no message.
            self._journal(mirror, "mirror_filtered", {"event_id": ev.event_id, "why": why, "magic": ev.magic, "comment": ev.comment}, event=ev)
            return True
        dest_symbol = m.source.symbols.get(ev.symbol, ev.symbol)
        self._journal(mirror, "mirror_source_open", {"event_id": ev.event_id, "side": ev.direction, "volume": ev.volume, "symbol": ev.symbol,
                                                     "price": ev.price, "sl": ev.sl, "tp": ev.tp}, event=ev)
        try:
            age = (self.now_local() - parse_ts(ev.ts)).total_seconds() if ev.ts else 0.0
        except ValueError:
            age = 0.0
        if age > m.max_open_age_sec:
            return self._reject(mirror, ev, "stale", age_sec=round(age))
        unavailable = self._unavailable(state)
        if unavailable:
            return self._wait(mirror, unavailable)
        self._resume(mirror)
        if block:
            return self._reject(mirror, ev, block.split(":")[0], detail=block)
        spec = state.symbols.get(dest_symbol)
        if spec is None:
            return self._reject(mirror, ev, "no_quote", dest_symbol=dest_symbol)
        if not spec.trade_allowed:
            return self._reject(mirror, ev, "trade_not_allowed", dest_symbol=dest_symbol)
        lots, why = mirror_volume(m, ev.volume, ev.source_balance, state.account.balance, spec)
        if lots is None:
            return self._reject(mirror, ev, why)
        sl, tp, why = mirror_stops(m, ev.direction, ev.sl, ev.tp, dest_symbol, spec)
        if why:
            return self._reject(mirror, ev, why)
        mp = MirrorPosition(mirror=mirror, source_pos_id=ev.source_pos_id, symbol=ev.symbol, dest_symbol=dest_symbol, side=ev.direction,
                            source_volume_open=ev.volume, source_volume=ev.volume, source_open_price=ev.price,
                            dest_volume_open=lots, sl=sl, tp=tp, want_sl=sl, want_tp=tp)
        cmd = Command(cmd_id=mp.next_cmd_id(), type="open_market", symbol=dest_symbol, side=ev.direction, volume=lots,
                      sl=sl, tp=tp, comment=mirror_comment(mirror, ev.source_pos_id))
        mp.inflight_cmd, mp.inflight_kind, mp.open_command = cmd.cmd_id, "open", cmd.model_dump(exclude_none=True)
        mp.source_event_ts, mp.read_at, mp.sent_at = ev.ts, read_at.isoformat(), self.now_local().isoformat()
        self._save(mp)                    # before sending: a restart must find this position, never open it twice
        try:
            bridge.send(cmd)
        except OSError as e:
            mp.state, mp.reason, mp.inflight_cmd, mp.inflight_kind = FAILED, f"not sent: {e}", None, None
            self._save(mp)
            self._journal(mirror, "mirror_open_failed", {"cmd_id": cmd.cmd_id, "text": f"command not written: {e}"}, mp)
            return True
        self._journal(mirror, "mirror_open_sent", {"cmd_id": cmd.cmd_id, "volume": lots, "dest_symbol": dest_symbol, "destination": m.destination,
                                                   "sl": sl, "tp": tp}, mp)
        return True

    def _reject(self, mirror: str, ev: SourceEvent, reason: str, **detail) -> bool:
        self._journal(mirror, "mirror_rejected", {"event_id": ev.event_id, "reason": reason, **detail}, event=ev)
        return True

    # ---- destination ---------------------------------------------------------------------------------
    def _apply_results(self, mirror: str, results: list[CommandResult], state: BridgeState | None) -> None:
        prefix = f"m:{mirror}:"
        for r in results:
            if not r.cmd_id.startswith(prefix):
                continue
            try:
                pos_id = int(r.cmd_id.split(":")[2])
            except (IndexError, ValueError):
                continue
            mp = self.get(mirror, pos_id)
            if mp is None or mp.inflight_cmd != r.cmd_id:
                continue
            kind, fields = mp.inflight_kind, mp.inflight_fields
            mp.inflight_cmd, mp.inflight_kind, mp.inflight_fields = None, None, {}
            lat = self._latency(mp)
            self._journal(mirror, "mirror_latency", {"cmd_id": r.cmd_id, "type": kind, "ok": r.ok, **lat}, mp)
            spec = state.symbols.get(mp.dest_symbol) if state else None
            dest = self.cfg.mirrors[mirror].destination
            if kind == "open":
                if r.ok:
                    mp.state, mp.dest_ticket = OPEN, r.position or r.order
                    mp.dest_volume, mp.dest_open_price = mp.dest_volume_open, r.fill_price
                    if r.retcode_text == "dry-run":
                        mp.reason = "dry-run"     # no real destination position to watch
                    self._journal(mirror, "mirror_placed", {"cmd_id": r.cmd_id, "volume": mp.dest_volume, "price": r.fill_price,
                                                            "ticket": mp.dest_ticket, "destination": dest, "latency": lat,
                                                            "gap_pips": gap_pips(mp.side, mp.source_open_price, r.fill_price, spec)}, mp)
                else:
                    mp.state, mp.reason = FAILED, r.retcode_text
                    self._journal(mirror, "mirror_open_failed", {"cmd_id": r.cmd_id, "retcode": r.retcode, "text": r.retcode_text}, mp)
            elif r.ok and kind in ("close", "partial"):
                closed = mp.dest_volume if kind == "close" else fields["volume"]
                price = r.fill_price or self._last_out_price(mp, state)
                pl = _pl(mp, price, closed, spec)
                partial_before = mp.dest_volume < mp.dest_volume_open - 1e-9
                mp.realised = round(mp.realised + (pl or 0.0), 2)
                mp.dest_volume = 0.0 if kind == "close" else round(mp.dest_volume - closed, 8)
                if mp.dest_volume <= 1e-9:
                    mp.state, mp.dest_volume = CLOSED, 0.0
                self._journal(mirror, "mirror_closed", {"cmd_id": r.cmd_id, "volume": closed, "price": price, "pl": pl, "left": mp.dest_volume,
                                                        "total": mp.realised, "partial_before": partial_before, "latency": lat}, mp)
            elif r.ok and kind == "sltp":
                mp.sl, mp.tp = fields["sl"], fields["tp"]
                self._journal(mirror, "mirror_sltp_set", {"cmd_id": r.cmd_id, "sl": mp.sl, "tp": mp.tp}, mp)
            else:
                mp.failures += 1
                self._journal(mirror, "mirror_command_failed", {"cmd_id": r.cmd_id, "type": kind, "retcode": r.retcode, "text": r.retcode_text}, mp)
                if mp.failures >= MAX_COMMAND_FAILURES and not mp.attention:
                    mp.attention = f"{kind} failed: {r.retcode_text}"
                    self._journal(mirror, "mirror_attention", {"failures": mp.failures, "destination": dest, "last": r.retcode_text}, mp)
            self._save(mp)

    def _latency(self, mp: MirrorPosition) -> dict:
        """Seconds from the source event to this service reading it (source timestamps are whole
        seconds, so +-1 s), from reading it to sending the command, and from sending to the result."""
        now = self.now_local()
        out: dict = {}
        read = datetime.fromisoformat(mp.read_at) if mp.read_at else None
        sent = datetime.fromisoformat(mp.sent_at) if mp.sent_at else None
        try:
            src = parse_ts(mp.source_event_ts) if mp.source_event_ts else None
        except ValueError:
            src = None
        if src and read:
            out["source_to_read_s"] = round((read - src).total_seconds(), 3)
        if read and sent:
            out["read_to_send_s"] = round((sent - read).total_seconds(), 3)
        if sent:
            out["send_to_result_s"] = round((now - sent).total_seconds(), 3)
        if src:
            out["source_to_result_s"] = round((now - src).total_seconds(), 3)
        return out

    def _last_out_price(self, mp: MirrorPosition, state: BridgeState | None) -> float:
        if state is None or mp.dest_ticket is None:
            return 0.0
        deals = state.out_deals_for(mp.dest_ticket)
        return deals[-1].price if deals else 0.0

    def _send(self, mirror: str, mp: MirrorPosition, bridge: Bridge, kind: str, cmd_type: str, **fields) -> None:
        cmd = Command(cmd_id=mp.next_cmd_id(), type=cmd_type, **fields)
        mp.inflight_cmd, mp.inflight_kind, mp.inflight_fields = cmd.cmd_id, kind, {k: v for k, v in fields.items() if v is not None}
        mp.sent_at = self.now_local().isoformat()
        self._save(mp)
        try:
            bridge.send(cmd)
        except OSError as e:
            mp.inflight_cmd, mp.inflight_kind, mp.inflight_fields = None, None, {}
            mp.failures += 1
            self._save(mp)
            self._journal(mirror, "mirror_command_failed", {"cmd_id": cmd.cmd_id, "type": kind, "text": f"command not written: {e}"}, mp)
            return
        self._journal(mirror, "mirror_command_sent", {"cmd_id": cmd.cmd_id, "type": kind, "fields": mp.inflight_fields}, mp)

    def _sync(self, mirror: str, state: BridgeState | None, bridge: Bridge, block: str | None) -> None:
        """Brings each mirrored destination position in line with its source: adopts an open whose
        result was lost, closes the source's closed fraction, applies SL/TP, and notices a destination
        position that closed by itself (its own SL/TP, or by hand)."""
        if state is None:
            return
        armed = block is None and self._unavailable(state) is None
        for mp in self.positions(mirror, [OPENING, OPEN]):
            changed = False
            pos = next((p for p in state.positions if p.comment == mirror_comment(mirror, mp.source_pos_id)), None)
            if mp.state == OPENING and pos is not None:
                # The open filled but its result was never read (restart, lost results offset): adopt it.
                mp.state, mp.dest_ticket, mp.dest_volume, mp.dest_open_price = OPEN, pos.ticket, pos.volume, pos.price_open
                mp.sl, mp.tp = pos.sl, pos.tp
                mp.inflight_cmd = mp.inflight_kind = None
                mp.inflight_fields = {}
                self._journal(mirror, "mirror_adopted", {"ticket": pos.ticket, "volume": pos.volume, "price": pos.price_open}, mp)
                changed = True
            if mp.state != OPEN:
                if changed:
                    self._save(mp)
                continue
            if pos is None and mp.dest_ticket is not None:
                pos = next((p for p in state.positions if p.ticket == mp.dest_ticket), None)
            if pos is None and mp.inflight_kind != "close" and mp.reason != "dry-run":
                deals = state.out_deals_for(mp.dest_ticket) if mp.dest_ticket else []
                now = self.now_local()
                if not deals and mp.missing_since is None:
                    mp.missing_since = now.isoformat()
                    self._save(mp)
                    continue
                if not deals and (now - datetime.fromisoformat(mp.missing_since)).total_seconds() < MISSING_GRACE_SEC:
                    continue
                spec = state.symbols.get(mp.dest_symbol)
                price = deals[-1].price if deals else 0.0
                pl = _pl(mp, price, mp.dest_volume, spec) if deals else None
                mp.state, mp.dest_volume, mp.inflight_cmd, mp.inflight_kind, mp.inflight_fields = CLOSED, 0.0, None, None, {}
                mp.reason = f"closed on the destination ({deals[-1].reason if deals else 'no closing deal seen'})"
                self._journal(mirror, "mirror_dest_closed", {"reason": deals[-1].reason if deals else "", "price": price, "pl": pl}, mp)
                self._save(mp)
                continue
            if pos is not None and mp.missing_since is not None:
                mp.missing_since, changed = None, True
            if mp.inflight_cmd or mp.attention or not armed:
                if changed:
                    self._save(mp)
                continue
            spec = state.symbols.get(mp.dest_symbol)
            target = target_dest_volume(mp, spec)
            step = spec.volume_step if spec else 0.01
            if target <= 1e-9:
                self._send(mirror, mp, bridge, "close", "close", position=mp.dest_ticket)
            elif mp.dest_volume - target >= step - 1e-9:
                self._send(mirror, mp, bridge, "partial", "close", position=mp.dest_ticket,
                           volume=round(mp.dest_volume - target, _step_digits(step)))
            elif abs(mp.want_sl - mp.sl) > 1e-9 or abs(mp.want_tp - mp.tp) > 1e-9:
                self._send(mirror, mp, bridge, "sltp", "modify_sltp", position=mp.dest_ticket, sl=mp.want_sl, tp=mp.want_tp)
            elif changed:
                self._save(mp)


def mirror_status_lines(cfg: AppConfig, store: Store, bridges: dict[str, Bridge]) -> list[str]:
    lines: list[str] = []
    engine = MirrorEngine(cfg.model_copy(update={"notify_trades": False}), store, bridges)
    for name, m in cfg.mirrors.items():
        dest = cfg.mirror_destination(name)
        st = bridges[m.destination].read_state() if m.destination in bridges else None
        block = engine.arming_block(name, st)
        armed = "NO STATE" if st is None else (f"NOT ARMED ({block.split(':')[0]})" if block else ("ARMED" if dest.live else "armed (demo)"))
        raw = store.kv_get(f"mirror_cursor:{name}")
        cur = json.loads(raw) if raw else None
        lines.append(f"[mirror {name}] source {m.source.login} ({m.source.events_base} in {m.source.events_dir}) -> {m.destination} | {armed}"
                     + (" | INCLUDES COPY-PLATFORM TRADES (demo/learning only)" if m.source.include_copy_trades else "")
                     + (f" | reading {cur['file']}@{cur['offset']}" if cur and cur.get("file") else " | not started"))
        for mp in engine.positions(name, [OPENING, OPEN]):
            flag = f" | NEEDS A HAND CHECK: {mp.attention}" if mp.attention else ""
            lines.append(f"    source #{mp.source_pos_id} {mp.symbol} {mp.side} {mp.source_volume:g}/{mp.source_volume_open:g} -> "
                         f"#{mp.dest_ticket or '?'} {mp.dest_symbol} {mp.dest_volume:g}/{mp.dest_volume_open:g} {mp.state}{flag}")
    return lines
