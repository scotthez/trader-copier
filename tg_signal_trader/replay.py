"""Replay a Telegram export through the full pipeline against a FakeBridge (or a real one in dry-run)."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from .bridge import Bridge, FakeBridge
from .classifier import Classifier
from .config import AppConfig
from .export import read_export
from .models import RunState
from .parsers import get_parser
from .store import Store
from .trader import Trader


def replay(cfg: AppConfig, store: Store, classifier: Classifier, provider: str, export_dir: Path, bridge: Bridge | None = None) -> dict:
    pcfg = cfg.providers[provider].model_copy(update={"max_signal_age_sec": 10**9, "max_open_signals": 10**6, "max_legs_open": 10**6, "daily_loss_stop_pct": 10**6})
    cfg = cfg.model_copy(update={"providers": {provider: pcfg}})
    msgs = read_export(export_dir, provider, chat_id=pcfg.telegram_chat)
    parser = get_parser(provider)
    fb = FakeBridge(now_local=msgs[0].ts.replace(tzinfo=None) if msgs else datetime.now())
    for sym in pcfg.symbols.values():
        fb.set_quote(sym, 1.0, 1.0, tick_value=1.0, tick_size=0.01)
    clock = {"utc": msgs[0].ts if msgs else datetime.now()}
    trader = Trader(cfg, store, {provider: bridge or fb}, classifier,
                    now_utc=lambda: clock["utc"], now_local=lambda: clock["utc"].replace(tzinfo=None))
    counts = {"messages": len(msgs), "signals_accepted": 0, "signals_rejected": 0, "management_applied": 0, "management_skipped": 0, "unparsed": 0}
    for i, m in enumerate(msgs):
        clock["utc"] = m.ts + timedelta(seconds=1)
        fb.now = clock["utc"].replace(tzinfo=None)
        r = parser.parse(m, msgs[max(0, i - 3):i])
        if r.signal and bridge is None:
            # Seed the fake quote around the signal's own price so validation is exercised meaningfully.
            ref = r.signal.entry_zone[0] if r.signal.entry_zone else (r.signal.tps[0] + r.signal.sl) / 2
            fb.set_quote(pcfg.symbols.get(r.signal.symbol, "?"), ref - 0.1, ref + 0.1, tick_value=1.0, tick_size=0.01)
            # Retire the previous signal's positions (as if every TP hit) so only the newest run stays
            # ACTIVE: keeps the replay linear and lets management messages resolve to that run.
            for pos in list(fb.positions):
                fb.hit_tp(pos.ticket)
            fb.orders.clear()
        store.add_inbox(m)          # one message per tick, so each is judged against its own quote/clock
        trader.tick()
    for e in store.journal_tail(10**6):
        if e["kind"] in counts:
            counts[e["kind"]] += 1
    counts["signals_rejected"] = sum(1 for r in store.runs(provider) if r.state == RunState.REJECTED)
    counts["signals_accepted"] = sum(1 for r in store.runs(provider) if r.state != RunState.REJECTED)
    counts["unparsed"] = sum(1 for e in store.journal_tail(10**6) if e["kind"] == "signal_unparsed")
    return counts
