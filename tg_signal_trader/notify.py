"""Turns journal events into plain-English trade notifications, one message per trade per tick."""
from __future__ import annotations
from typing import Callable
from .models import SignalRun

REASONS = {
    "stale": "arrived too late (older than max_signal_age_sec)",
    "no_state": "MT5 terminal not responding",
    "no_quote": "no price from MT5 for this symbol",
    "symbol_unmapped": "symbol not in config",
    "sl_wrong_side": "price already past the SL",
    "tp_wrong_side": "price already past a TP",
    "sl_distance_out_of_range": "SL distance outside the allowed sl_range",
    "tp_not_monotonic": "TPs out of order",
    "market_too_far": "price moved too far from the signal's entry",
    "limit_too_far": "limit entry too far from current price",
    "trade_not_allowed": "broker has trading disabled for this symbol",
    "max_open_signals": "already at max open signals",
    "max_legs_open": "already at max open legs",
    "daily_loss_stop": "daily loss stop reached",
    "terminal_stale": "MT5 terminal not updating",
    "login_mismatch": "MT5 logged into the wrong account",
    "real_account_not_armed": "REAL account but live: false",
}


def _reasons(codes: list[str]) -> str:
    out = []
    for c in codes:
        if c.startswith("crosscheck:"):
            out.append(f"AI read the {c.split(':', 1)[1]} differently")
        else:
            out.append(REASONS.get(c, c))
    return "; ".join(out)


def _fmt(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def describe_signal(sig: dict) -> str:
    """'SELL LIMIT XAUUSD @ 4285 · SL 4302 · TP 4280 / 4275 / 4270'"""
    entry = f"LIMIT {sig['symbol']} @ {_fmt(sig['entry_zone'][0])}" if sig["entry_type"] == "LIMIT" else f"{sig['symbol']} at market"
    return f"{sig['side']} {entry} · SL {_fmt(sig['sl'])} · TP {' / '.join(_fmt(t) for t in sig['tps'])}"


def event_line(kind: str, d: dict) -> str | None:
    """One line for a journal event, or None for events that are not worth a message."""
    if kind == "signal_accepted":
        vols = d.get("volumes") or []
        lots = f"{len(vols)} × {_fmt(vols[0])} lots" if vols and len(set(vols)) == 1 else " / ".join(_fmt(v) for v in vols) + " lots"
        return f"✅ Placed: {describe_signal(d['signal'])} · {lots}"
    if kind == "signal_rejected":
        line = f"⛔ Not placed: {_reasons(d.get('reasons', []))}\n   {describe_signal(d['signal'])}"
        s = d.get("suggestion")
        if s:
            fixes = ", ".join(f"{k} {_fmt(w)} → {_fmt(g)}" for k, (w, g) in s["changed"].items())
            fixed = dict(d["signal"], tps=s["tps"])
            line += (f"\n💡 Looks like a typo. Best guess ({s['basis']}): {fixes}"
                     f"\n   {describe_signal(fixed)}\n   Check it against the channel before placing it yourself.")
        return line
    if kind == "guard_blocked":
        return f"⛔ Not placed: {_reasons(d.get('reasons', []))}\n   {describe_signal(d['signal'])}"
    if kind == "signal_deferred":
        return f"⏳ Signal waiting to be placed: {_reasons(d.get('reasons', []))}"
    if kind == "signal_unparsed":
        return f"❓ Looked like a trade but couldn't be read ({d.get('reason')}):\n   {d.get('text', '')[:160]}"
    if kind == "tp_corrected":
        fixes = ", ".join(f"{k} {_fmt(v)} → {_fmt(d['used'][k])}" for k, v in d.get("as_written", {}).items())
        return f"✏️ Fixed a TP typo from its pips note: {fixes}"
    if kind == "leg_filled":
        return f"▶️ Leg {d['leg']} filled @ {_fmt(d['price'])}"
    if kind == "leg_closed":
        state = d.get("state")
        label = {"CLOSED_TP": "🎯 Leg {n} hit TP", "CLOSED_SL": "🛑 Leg {n} hit SL"}.get(state, "✋ Leg {n} closed")
        price = f" @ {_fmt(d['price'])}" if d.get("price") is not None else ""
        return label.format(n=d["leg"]) + price
    if kind == "command_sent" and d.get("type") == "modify_sl":
        return f"🔒 Leg {d['leg']} SL moved to {_fmt(d['fields']['sl'])}"
    if kind == "command_sent" and d.get("type") == "cancel":
        return f"🗑 Cancelling pending leg {d['leg']}"
    if kind == "command_sent" and d.get("type") == "close":
        return f"✋ Closing leg {d['leg']}"
    if kind == "leg_cancelled":
        return f"🗑 Leg {d['leg']} cancelled ({d.get('reason')})"
    if kind in ("command_failed", "command_send_failed"):
        return f"❗ Leg {d.get('leg')} {d.get('type')} failed: {d.get('text') or d.get('error')}"
    if kind == "management_applied":
        c = d.get("classification", {})
        price = f" {_fmt(c['price'])}" if c.get("price") is not None else ""
        return f"💬 Provider said \"{d.get('text', '')[:120]}\" → {c.get('action')}{price}"
    if kind == "management_skipped" and d.get("classification", {}).get("action") not in (None, "none"):
        c = d["classification"]
        return f"💬 Provider said \"{d.get('text', '')[:120]}\" → {c['action']} NOT applied (not enabled, low confidence or not armed)"
    if kind == "crosscheck_failed_closing":
        return f"⚠️ AI disagreed after placing ({_reasons(d.get('reasons', []))}) - closing the whole trade"
    if kind == "sl_move_skipped":
        return None
    return None


class TradeNotifier:
    """Collects event lines per (provider, trade) during a tick and sends one message per trade at flush()."""

    def __init__(self, send: Callable[[str], None], get_run: Callable[[str], SignalRun | None]):
        self.send, self.get_run = send, get_run
        self._pending: dict[tuple[str, str | None], list[str]] = {}
        self._headers: dict[tuple[str, str | None], str] = {}

    def on_event(self, provider: str, kind: str, detail: dict, run_id: str | None) -> None:
        line = event_line(kind, detail)
        if line is None:
            return
        key = (provider, run_id)
        self._pending.setdefault(key, []).append(line)
        if key not in self._headers and "signal" in detail and isinstance(detail["signal"], dict):
            s = detail["signal"]
            self._headers[key] = self._header(provider, s["symbol"], s["side"], s["entry_type"], s["telegram_msg_id"])

    def _header(self, provider: str, symbol: str, side: str, entry_type: str, msg_id) -> str:
        kind = " LIMIT" if entry_type == "LIMIT" else ""
        return f"[{provider}] {symbol} {side}{kind} (msg #{msg_id})"

    def flush(self) -> None:
        for key, lines in self._pending.items():
            provider, run_id = key
            header = self._headers.get(key)
            if header is None and run_id:
                run = self.get_run(run_id)
                if run is not None:
                    s = run.signal
                    header = self._header(provider, s.symbol, s.side.value, s.entry_type.value, s.telegram_msg_id)
            self.send((header or f"[{provider}]") + "\n" + "\n".join(lines))
        self._pending.clear()
        self._headers.clear()
