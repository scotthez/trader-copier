"""Lewis Inner Circle templates."""
from __future__ import annotations
import re
from datetime import timedelta
from ..models import InboxMessage, Signal, Side, EntryType, complete_tps, make_signal_id
from ..normalize import normalize_text, canonical_symbol, parse_price
from . import ParseResult

_NUM = r"\d+(?:\.\d+)?"
_SL = re.compile(r"SL:\s*(" + _NUM + ")")
_TP = re.compile(r"TP([1-4]):\s*(" + _NUM + "|OPEN)", re.I)
_HEAD_SETUP = re.compile(r"^(?:TRADE SETUP|TRADE IDEA):\s*\n\s*(BUY|SELL)\s+([A-Z0-9]+)\b", re.I)
_HEAD_NOW = re.compile(r"^\s*(BUY|SELL)\s+([A-Z0-9]+)\s+NOW\b|^\s*([A-Z0-9]+)\s*-\s*(BUY|SELL)\s+NOW\b", re.I)
_HEAD_LIMIT = re.compile(r"^\s*([A-Z0-9]+)\s*-\s*(BUY|SELL)\s+LIMIT\s+SETUP", re.I)
_ZONE = re.compile(r"Entry Zone:\s*(" + _NUM + r")\s*-\s*(" + _NUM + ")", re.I)
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
