"""WolvesVIP templates (XAUUSD-only channel, several posting styles over time)."""
from __future__ import annotations
import re
from ..models import InboxMessage, Signal, Side, EntryType, complete_tps, make_signal_id
from ..normalize import normalize_text, canonical_symbol, parse_price
from . import ParseResult

_NUM = r"\d+(?:\.\d+)?"
_SL = re.compile(r"SL:\s*(" + _NUM + ")")
_TPN = re.compile(r"TP([1-4]):\s*(" + _NUM + "|OPEN)", re.I)
_TP_PLAIN = re.compile(r"^TP:\s*(" + _NUM + ")", re.I | re.M)
_AT = re.compile(r"^\s*(BUY|SELL)\s+(LIMIT\s+)?([A-Z0-9]+)\s*@\s*(" + _NUM + r")(?:\s+(" + _NUM + "))?", re.I | re.M)
_PAIR = re.compile(r"Pair:\s*([A-Z0-9]+)", re.I)
_SIDE = re.compile(r"Side:\s*(?:Long|Short)?\s*/?\s*(Buy|Sell)(\s+Limit)?", re.I)
_ENTRY = re.compile(r"Entry:\s*(" + _NUM + r")(?:\s+(" + _NUM + "))?", re.I)
_NOW = re.compile(r"^\s*([A-Z]+)\s+(?:re-entry\s+|continue\s+)?(buy|sell)\s+now(?:\s+again)?(?:\s+slowly)?\s*(" + _NUM + r")?\s*-?\s*(" + _NUM + ")?", re.I | re.M)


def _sl(norm: str) -> float | None:
    m = _SL.search(norm)
    return parse_price(m.group(1)) if m else None


def _numbered_tps(norm: str) -> list[float | None] | None:
    tps: dict[int, float | None] = {}
    for m in _TPN.finditer(norm):
        tps[int(m.group(1))] = None if m.group(2).upper() == "OPEN" else parse_price(m.group(2))
    if not all(k in tps for k in (1, 2, 3)):
        return None
    result: list[float | None] = [tps[1], tps[2], tps[3]]
    if 4 in tps:
        result.append(tps[4])
    return result


def _plain_tps(norm: str) -> list[float | None] | None:
    vals = [parse_price(m.group(1)) for m in _TP_PLAIN.finditer(norm)]
    if len(vals) < 3:
        return None
    return vals[:3] + ([vals[3]] if len(vals) > 3 else [])


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
            zone = ([parse_price(e.group(1))] + ([parse_price(e.group(2))] if e.group(2) else [])) if e else []
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
