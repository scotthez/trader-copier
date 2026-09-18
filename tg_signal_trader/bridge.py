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
