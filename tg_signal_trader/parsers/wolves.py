"""WolvesVIP templates (XAUUSD-only channel, several posting styles over time)."""
from __future__ import annotations
import re
from ..models import InboxMessage, Signal, Side, EntryType, complete_tps, make_signal_id
from ..normalize import normalize_text, canonical_symbol, parse_price
from . import ParseResult

_NUM = r"\d+(?:\.\d+)?"
_SL = re.compile(r"SL:\s*(" + _NUM + ")")
_TPN = re.compile(r"TP([1-4]):\s*(" + _NUM + r"|OPEN)(?:\s*(\d+)\s*pips)?", re.I)
PIP = 0.1   # Wolves is XAUUSD-only; "50pips" = 5.0 in price
_TP_PLAIN = re.compile(r"^TP:\s*(" + _NUM + ")", re.I | re.M)
_AT = re.compile(r"^\s*(BUY|SELL)\s+((?:LIMIT|STOP)\s+)?([A-Z0-9]+)\s*@\s*(" + _NUM + r")(?:\s+(" + _NUM + "))?", re.I | re.M)
_PAIR = re.compile(r"Pair:\s*([A-Z0-9]+)", re.I)
_SIDE = re.compile(r"Side:\s*(?:Long|Short)?\s*/?\s*(Buy|Sell)(\s+(?:Limit|Stop))?", re.I)
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


_GLUED = re.compile(r"(TP[1-4]:\s*)(\d+)(pips)", re.I)


def _unglue_pips(norm: str, sl: float) -> str:
    """Splits a TP price that runs straight into its pips note (live, 2026-09-28: "TP2 4133100pips" was
    read as 4,133,100). Only when the number has more whole digits than the SL, split at the SL's digit
    count, and only if what is left is a real pips figure."""
    digits = len(str(int(sl)))

    def fix(m: re.Match) -> str:
        num = m.group(2)
        if len(num) <= digits or int(num[digits:]) == 0 or num[digits] == "0":
            return m.group(0)
        return f"{m.group(1)}{num[:digits]} {num[digits:]}{m.group(3)}"
    return _GLUED.sub(fix, norm)


def _pips(norm: str) -> dict[int, int]:
    """TP number → the pips figure Wolves writes beside it ("TP1 4275 50pips")."""
    return {int(m.group(1)): int(m.group(3)) for m in _TPN.finditer(norm) if m.group(3)}


def _in_order(tps: list[float], buy: bool, near: float) -> bool:
    vals = [t for t in tps if t is not None]
    beyond = all(t > near if buy else t < near for t in vals)
    return beyond and all((b > a) if buy else (b < a) for a, b in zip(vals, vals[1:]))


def fix_tp_typo(tps: list[float | None], buy: bool, near: float, pips: dict[int, int]) -> tuple[list[float | None], dict[int, float]]:
    """Repairs one mistyped TP price from its pips note (live, 2026-09-25: "TP1 4275 50pips / TP2 4260
    100pips / TP3 4265 150pips" from 4280, where TP2 was meant to be 4270). Deliberately narrow, since
    in Wolves' history the pips note is itself sometimes the typo or measured from another price:
    only a signal whose TPs are out of order or on the wrong side is touched; exactly one TP may
    disagree with its note while at least two others match theirs exactly; and the repaired ladder
    must then be in order. Anything else is returned unchanged (and validation rejects it as before).
    Returns (tps, {index: value as written})."""
    if _in_order(tps, buy, near):
        return tps, {}
    expected = {n - 1: round(near + (p * PIP if buy else -p * PIP), 5)
                for n, p in pips.items() if n - 1 < len(tps) and tps[n - 1] is not None}
    off = [i for i, e in expected.items() if abs(tps[i] - e) > 1e-6]
    agree = [i for i, e in expected.items() if abs(tps[i] - e) <= 1e-6]
    if len(off) != 1 or len(agree) < 2:
        return tps, {}
    i = off[0]
    fixed = list(tps)
    fixed[i] = expected[i]
    if not _in_order(fixed, buy, near):
        return tps, {}
    return fixed, {i: tps[i]}


def _plain_tps(norm: str) -> list[float | None] | None:
    vals = [parse_price(m.group(1)) for m in _TP_PLAIN.finditer(norm)]
    if len(vals) < 3:
        return None
    return vals[:3] + ([vals[3]] if len(vals) > 3 else [])


def _entry_type(suffix: str | None) -> EntryType:
    """"Limit" / "Stop" after the side; none is a market order. A stop was once read as a market order
    (live, 2026-10-13: #30415 "Sell Stop 4186" would have sold at 4192 straight away)."""
    word = (suffix or "").strip().upper()
    return EntryType.STOP if word == "STOP" else EntryType.LIMIT if word == "LIMIT" else EntryType.MARKET


def _build(msg: InboxMessage, template: str, symbol: str, side: str, entry_type: EntryType,
           zone: list[float], sl: float, tps: list[float | None], pips: dict[int, int] | None = None) -> ParseResult:
    sym = canonical_symbol(symbol)
    if sym is None:
        return ParseResult(template=template, rejected_reason=f"unknown symbol {symbol!r}")
    corrections: dict[int, float] = {}
    if zone and pips:
        tps, corrections = fix_tp_typo(tps, side.upper() == "BUY", zone[0], pips)
    try:
        full = complete_tps(tps)
    except ValueError as e:
        return ParseResult(template=template, rejected_reason=str(e))
    sig = Signal(id=make_signal_id(msg.provider, msg.msg_id), provider=msg.provider, symbol=sym,
                 side=Side(side.upper()), entry_type=entry_type, entry_zone=zone, sl=sl, tps=full,
                 received_at=msg.ts, raw_text=msg.text, telegram_msg_id=msg.msg_id, parsed_by=template,
                 tp_corrections=corrections)
    return ParseResult(signal=sig, template=template)


class WolvesParser:
    def parse(self, msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult:
        norm = normalize_text(msg.text)
        sl = _sl(norm)
        if sl is None:
            return ParseResult()
        norm = _unglue_pips(norm, sl)
        m = _AT.search(norm)
        if m:
            tps = _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.at", rejected_reason="TP1..TP3 missing")
            zone = [parse_price(m.group(4))] + ([parse_price(m.group(5))] if m.group(5) else [])
            et = _entry_type(m.group(2))
            return _build(msg, "wolves.at", m.group(3), m.group(1), et, zone if et.pending else [], sl, tps, _pips(norm))
        pair, side = _PAIR.search(norm), _SIDE.search(norm)
        if pair and side:
            tps = _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.block", rejected_reason="TP1..TP3 missing")
            e = _ENTRY.search(norm)
            zone = ([parse_price(e.group(1))] + ([parse_price(e.group(2))] if e.group(2) else [])) if e else []
            et = _entry_type(side.group(2))
            return _build(msg, "wolves.block", pair.group(1), side.group(1), et, zone if et.pending else [], sl, tps, _pips(norm))
        m = _NOW.search(norm)
        if m:
            tps = _plain_tps(norm) or _numbered_tps(norm)
            if tps is None:
                return ParseResult(template="wolves.now", rejected_reason="fewer than 3 TPs")
            return _build(msg, "wolves.now", m.group(1), m.group(2), EntryType.MARKET, [], sl, tps)
        return ParseResult(rejected_reason="SL found but no recognised header")
