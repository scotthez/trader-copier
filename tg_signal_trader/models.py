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
    """Return 3 or 4 TPs, matching how many the source actually gave. A TP4 present in the source
    but written as "Open" (passed here as a 4th element that is None) is synthesised as
    tp3 + (tp3 - tp2). A signal that never mentions TP4 at all (a 3-element list) stays a 3-leg
    run — no leg 4 opens."""
    if len(tps) not in (3, 4) or any(t is None for t in tps[:3]):
        raise ValueError(f"need at least TP1..TP3, got {tps}")
    tp1, tp2, tp3 = (float(t) for t in tps[:3])
    if len(tps) == 3:
        return [tp1, tp2, tp3]
    tp4 = tps[3]
    return [tp1, tp2, tp3, float(tp4) if tp4 is not None else round(tp3 + (tp3 - tp2), 5)]


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
    inflight_cmd: str | None = None
    inflight_kind: str | None = None
    inflight_sl: float | None = None
    missing_since: datetime | None = None

    def comment(self, signal_id: str) -> str:
        return leg_comment(signal_id, self.n)


class SignalRun(BaseModel):
    signal: Signal
    legs: list[Leg]
    state: RunState = RunState.NEW
    be_applied: bool = False       # ladder: L2 TP → L3/L4 to break-even done
    tp1_applied: bool = False      # ladder: L3 TP → L4 to TP1 done
    pendings_cancelled: bool = False
    close_requested: bool = False  # entry crosscheck disagreed after placement → close/cancel every leg
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
