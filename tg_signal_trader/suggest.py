"""Best-guess repair of a signal whose TPs contradict each other, for the user to review. Never traded automatically."""
from __future__ import annotations
import re
from .normalize import normalize_text

_TP_PIPS = re.compile(r"TP([1-4]):\s*(\d+(?:\.\d+)?)\s*(\d+)\s*pips", re.I)
PIP_SIZE = {"XAUUSD": 0.1}


def _in_order(tps: list[float], buy: bool, ref: float | None) -> bool:
    beyond = ref is None or all(t > ref if buy else t < ref for t in tps)
    return beyond and all((b > a) if buy else (b < a) for a, b in zip(tps, tps[1:]))


def _one_digit_apart(a: float, b: float) -> bool:
    sa, sb = f"{a:g}", f"{b:g}"
    return len(sa) == len(sb) and sum(x != y for x, y in zip(sa, sb)) == 1


def suggest_tps(symbol: str, side: str, tps: list[float], ref: float | None, raw_text: str) -> dict | None:
    """A corrected TP ladder when the written one is out of order, or None if no confident guess.
    Returns {"tps": [...], "changed": {"TP2": [written, suggested]}, "basis": "..."}."""
    buy = side == "BUY"
    if _in_order(tps, buy, ref):
        return None
    pip = PIP_SIZE.get(symbol)
    if pip and ref is not None:
        notes = {int(n) - 1: int(p) for n, _, p in _TP_PIPS.findall(normalize_text(raw_text))}
        if notes and all(i < len(tps) for i in notes):
            guess = list(tps)
            for i, p in notes.items():
                guess[i] = round(ref + (p * pip if buy else -p * pip), 5)
            if _in_order(guess, buy, ref):
                return _result(tps, guess, "pips notes")
    candidates: list[tuple[tuple, list[float]]] = []
    n = len(tps)
    for i in range(n):
        if n < 3:
            break
        if 0 < i < n - 1:
            val = (tps[i - 1] + tps[i + 1]) / 2
        elif i == 0:
            val = tps[1] - (tps[2] - tps[1])
        else:
            val = tps[-2] + (tps[-2] - tps[-3])
        guess = list(tps)
        guess[i] = round(val, 5)
        if _in_order(guess, buy, ref):
            candidates.append(((0 if _one_digit_apart(tps[i], guess[i]) else 1, abs(tps[i] - guess[i])), guess))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return _result(tps, candidates[0][1], "even spacing")


def _result(written: list[float], guess: list[float], basis: str) -> dict:
    changed = {f"TP{i + 1}": [w, g] for i, (w, g) in enumerate(zip(written, guess)) if abs(w - g) > 1e-9}
    return {"tps": guess, "changed": changed, "basis": basis}
