"""Risk-based leg sizing and stop-loss rules."""
from __future__ import annotations
import math
from pydantic import BaseModel
from .models import Side
from .validation import Quote


class SizingSpec(BaseModel):
    volume_step: float
    volume_min: float
    volume_max: float
    tick_value: float      # account-currency value of one tick for one lot
    tick_size: float


def leg_volume(balance: float, risk_pct: float, entry: float, sl: float, spec: SizingSpec) -> float:
    dist = abs(entry - sl)
    if dist <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        raise ValueError("SL distance, tick_size and tick_value must be positive")
    risk_amount = balance * risk_pct / 100.0
    loss_per_lot = dist / spec.tick_size * spec.tick_value
    raw = risk_amount / loss_per_lot
    stepped = math.floor(raw / spec.volume_step + 1e-9) * spec.volume_step
    clamped = min(max(stepped, spec.volume_min), spec.volume_max)
    digits = max(0, -int(math.floor(math.log10(spec.volume_step))))
    return round(clamped, digits)


def sl_improves(side: Side, new_sl: float, current_sl: float) -> bool:
    if current_sl <= 0:
        return True
    return new_sl > current_sl if side == Side.BUY else new_sl < current_sl


def sl_valid_vs_market(side: Side, new_sl: float, quote: Quote) -> bool:
    return new_sl < quote.bid if side == Side.BUY else new_sl > quote.ask
