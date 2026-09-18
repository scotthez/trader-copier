"""Arithmetic validation of a parsed signal. Returns failed rule names; empty list = tradeable."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel
from .config import ProviderConfig
from .models import Signal, Side, EntryType


class Quote(BaseModel):
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def validate_signal(sig: Signal, cfg: ProviderConfig, quote: Quote | None, now: datetime) -> list[str]:
    fails: list[str] = []
    if sig.symbol not in cfg.symbols:
        fails.append("symbol_unmapped")
    buy = sig.side == Side.BUY
    ref = sig.entry_zone[0] if sig.entry_zone else (quote.mid if quote else None)
    if ref is not None:
        if (buy and sig.sl >= ref) or (not buy and sig.sl <= ref):
            fails.append("sl_wrong_side")
        if any((buy and tp <= ref) or (not buy and tp >= ref) for tp in sig.tps):
            fails.append("tp_wrong_side")
        dist = abs(ref - sig.sl)
        lo, hi = cfg.sl_range.get(sig.symbol, (0.0, float("inf")))
        if not (lo <= dist <= hi):
            fails.append("sl_distance_out_of_range")
    steps = [b - a for a, b in zip(sig.tps, sig.tps[1:])]
    if any((s <= 0) if buy else (s >= 0) for s in steps):
        fails.append("tp_not_monotonic")
    if (now - sig.received_at).total_seconds() > cfg.max_signal_age_sec:
        fails.append("stale")
    if quote is None:
        fails.append("no_quote")
    else:
        if sig.entry_type == EntryType.MARKET and sig.entry_zone:
            if abs(sig.entry_zone[0] - quote.mid) / quote.mid * 100 > cfg.market_entry_tolerance_pct:
                fails.append("market_too_far")
        if sig.entry_type == EntryType.LIMIT:
            near = sig.entry_zone[0] if sig.entry_zone else quote.mid
            if abs(near - quote.mid) / quote.mid * 100 > cfg.limit_max_distance_pct:
                fails.append("limit_too_far")
    return fails
