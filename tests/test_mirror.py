import json
from datetime import datetime, timedelta
from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError
from tg_signal_trader.bridge import FakeBridge, SymbolSpec, fmt_ts
from tg_signal_trader.config import AppConfig, MirrorConfig, load_config
from tg_signal_trader.mirror import (MirrorEngine, exclusion_reason, mirror_stops, mirror_volume, mirror_status_lines,
                                     OPEN, OPENING, CLOSED, FAILED)
from tg_signal_trader.store import Store

LOCAL0 = datetime(2026, 10, 27, 10, 0, 0)
SRC, DST = 4100074, 1514988624


class Alerts:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def mirror_cfg(events_dir, **over) -> dict:
    m = {"source": {"login": SRC, "events_dir": str(events_dir)}, "destination": "demo", "max_lots": 5.0}
    for k, v in over.items():
        if k == "source":
            m["source"].update(v)
        else:
            m[k] = v
    return m


def make(tmp_path, notify=True, dest_login=DST, trade_mode="DEMO", **over):
    events = tmp_path / "Common" / "Files"
    events.mkdir(parents=True, exist_ok=True)
    (events / "copytrader_events_20261027.jsonl").write_text("")
    cfg = AppConfig(accounts={"demo": {"bridge_dir": str(tmp_path / "dest"), "expected_login": DST}},
                    mirrors={"own": mirror_cfg(events, **over)}, notify_trades=notify, db_path=":memory:")
    store = Store(":memory:")
    fb = FakeBridge(now_local=LOCAL0, balance=50_000, login=dest_login)
    fb.account.trade_mode = trade_mode
    fb.set_quote("XAUUSD", 4000.0, 4000.2)
    fb.set_quote("EURUSD", 1.08000, 1.08002, digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0)
    clock = {"now": LOCAL0}
    alerts = Alerts()
    eng = MirrorEngine(cfg, store, {"demo": fb}, alerter=alerts, now_local=lambda: clock["now"])
    eng.tick()      # first start: cursor at the end of today's (empty) file
    return eng, store, fb, clock, alerts, events / "copytrader_events_20261027.jsonl"


_seq = [0]


def ev(kind, pos, ts=LOCAL0, **kw) -> dict:
    _seq[0] += 1
    d = {"event_id": f"20261027-100000-{_seq[0]:06d}", "ts": fmt_ts(ts), "type": kind, "source_pos_id": pos, "symbol": "XAUUSD",
         "direction": "BUY", "volume": 0.0, "remaining": 0, "price": 4000.1, "sl": 0, "tp": 0, "source_balance": 100_000,
         "login": SRC, "magic": 0, "comment": ""}
    d.update(kw)
    return d


def write(path: Path, *events: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def step(eng, fb, clock, n=2, sec=0.2):
    for _ in range(n):
        clock["now"] += timedelta(seconds=sec)
        fb.now = clock["now"]
        eng.tick()


def opened(**kw):
    return ev("OPEN", 501, volume=1.0, sl=3990.0, tp=4020.0, **kw)


# ---- config -------------------------------------------------------------------------------------

def test_config_example_parses_with_defaults(tmp_path):
    raw = {"accounts": {"ftmo_demo": {"bridge_dir": "/x/signalbridge", "expected_login": DST}},
           "mirrors": {"icon": {"source": {"login": SRC, "events_dir": "/x/Common/Files"}, "destination": "ftmo_demo",
                                "sizing": {"mode": "source_lots"}, "max_lots": 1.0}},
           "notify_trades": False}
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump(raw))
    cfg = load_config(p)
    m = cfg.mirrors["icon"]
    assert cfg.providers == {} and m.source.include_magics == [0] and m.source.events_base == "copytrader"
    assert m.stop_loss == "copy" and m.take_profit == "copy" and m.mirror_closes == "proportional" and m.sizing.multiplier == 1.0
    assert cfg.mirror_destination("icon").expected_login == DST and cfg.mirror_destination("icon").live is False


def test_config_fixed_pips_and_symbol_list():
    m = MirrorConfig.model_validate({"source": {"login": 1, "events_dir": "/x", "symbols": ["XAUUSD", "EURUSD"]}, "destination": "d",
                                     "max_lots": 1, "stop_loss": {"fixed_pips": 20}, "take_profit": "none"})
    assert m.source.symbols == {"XAUUSD": "XAUUSD", "EURUSD": "EURUSD"} and m.stop_loss.fixed_pips == 20 and m.take_profit == "none"


@pytest.mark.parametrize("change, msg", [
    ({"destination": "nope"}, "not under accounts"),
    ({"name": "Way-Too-Long"}, "1-8 characters"),
    ({"source": {"login": DST}}, "is the source account"),
    ({"sizing": {"mode": "fixed_lots"}}, "needs lots"),
    ({"max_lots": 0}, "greater than 0"),
    ({"source": {"include_magics": []}}, "at least one magic"),
    ({"stop_loss": "tight"}, "stop_loss"),
])
def test_config_rejects(change, msg):
    m = {"source": {"login": SRC, "events_dir": "/x"}, "destination": "demo", "max_lots": 1}
    name = change.pop("name", "own")
    for k, v in change.items():
        m[k] = {**m[k], **v} if isinstance(v, dict) and isinstance(m.get(k), dict) else v
    with pytest.raises(ValidationError, match=msg):
        AppConfig(accounts={"demo": {"bridge_dir": "/d", "expected_login": DST}}, mirrors={name: m})


def test_config_rejects_duplicate_mirror_and_name_clash():
    m = {"source": {"login": SRC, "events_dir": "/x"}, "destination": "demo", "max_lots": 1}
    with pytest.raises(ValidationError, match="open twice"):
        AppConfig(accounts={"demo": {"bridge_dir": "/d"}}, mirrors={"a": m, "b": m})
    with pytest.raises(ValidationError, match="live: true requires expected_login"):
        AppConfig(accounts={"demo": {"bridge_dir": "/d", "live": True}}, mirrors={"a": m})


def test_swapping_source_and_destination_is_config_only(tmp_path):
    """The same two accounts either way round: only the mirror block changes."""
    accounts = {"a": {"bridge_dir": "/a", "expected_login": 111}, "b": {"bridge_dir": "/b", "expected_login": 222}}
    ab = AppConfig(accounts=accounts, mirrors={"m": {"source": {"login": 111, "events_dir": "/ca"}, "destination": "b", "max_lots": 1}})
    ba = AppConfig(accounts=accounts, mirrors={"m": {"source": {"login": 222, "events_dir": "/cb"}, "destination": "a", "max_lots": 1}})
    assert ab.mirror_destination("m").expected_login == 222 and ba.mirror_destination("m").expected_login == 111
    # and the engine really trades the configured destination: events from 222 go to bridge "a"
    (tmp_path / "cb").mkdir(); f = tmp_path / "cb" / "copytrader_events_20261027.jsonl"; f.write_text("")
    ba = AppConfig(accounts={"a": {"bridge_dir": "/a", "expected_login": 111}, "b": {"bridge_dir": "/b", "expected_login": 222}},
                   mirrors={"m": {"source": {"login": 222, "events_dir": str(tmp_path / "cb")}, "destination": "a", "max_lots": 1}}, notify_trades=False)
    fa, fbb = FakeBridge(LOCAL0, login=111), FakeBridge(LOCAL0, login=222)
    for b in (fa, fbb):
        b.set_quote("XAUUSD", 4000.0, 4000.2)
    eng = MirrorEngine(ba, Store(":memory:"), {"a": fa, "b": fbb}, now_local=lambda: LOCAL0)
    eng.tick()
    write(f, ev("OPEN", 9, volume=0.1, login=222))
    eng.tick()
    assert [c.type for c in fa.sent] == ["open_market"] and fbb.sent == []


def test_destination_can_be_a_provider_terminal():
    cfg = AppConfig(providers={"wolves": {"telegram_chat": 1, "bridge_dir": "/w", "symbols": {"XAUUSD": "XAUUSD"}, "sl_range": {"XAUUSD": [1, 60]},
                                          "expected_login": 77}},
                    mirrors={"m": {"source": {"login": SRC, "events_dir": "/x"}, "destination": "wolves", "max_lots": 1}})
    assert cfg.mirror_destination("m").bridge_dir == Path("/w") and cfg.mirror_destination("m").expected_login == 77


# ---- filters ------------------------------------------------------------------------------------

@pytest.mark.parametrize("comment", ["kQ3J8sX0aB1cD2eF3gH4iQ==", "Zm9vYmFyMTIzNDU2Nzg5MGFi", "aB3dE5gH7jK9mN1pQ3sT5v", "sig:wolves:812:L1",
                                     "copy:123456", "m:own:501"])
def test_copy_platform_and_bot_comments_are_excluded(comment):
    assert exclusion_reason(comment) is not None


@pytest.mark.parametrize("comment", ["", "breakout", "my gold swing", "my_long_term_gold_hold", "TP at 4020", "#123456"])
def test_own_comments_are_allowed(comment):
    assert exclusion_reason(comment) is None


def test_copy_platform_trade_is_not_mirrored_and_not_alerted(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(comment="kQ3J8sX0aB1cD2eF3gH4iQ=="))
    step(eng, fb, clock)
    assert fb.sent == [] and alerts.sent == [] and eng.get("own", 501) is None
    assert any(e["kind"] == "mirror_filtered" and e["detail"]["why"] == "copy_platform_trade" for e in store.journal_tail())


def test_copy_platform_magic_is_never_mirrored_and_cannot_be_included(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(magic=2147483646, comment=""))
    step(eng, fb, clock)
    assert fb.sent == [] and alerts.sent == []
    with pytest.raises(ValidationError, match="copy-trading platform"):
        mirror_cfg_model = {"source": {"login": SRC, "events_dir": "/x", "include_magics": [0, 2147483646]}, "destination": "d", "max_lots": 1}
        MirrorConfig.model_validate(mirror_cfg_model)


def test_magic_filter(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, source={"include_magics": [0, 555]})
    write(f, ev("OPEN", 1, volume=0.1, magic=903001), ev("OPEN", 2, volume=0.1, magic=555), ev("OPEN", 3, volume=0.1, magic=0))
    step(eng, fb, clock)
    assert sorted(int(c.comment.split(":")[2]) for c in fb.sent) == [2, 3]


def test_symbol_allow_list_and_mapping(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, source={"symbols": {"GOLD": "XAUUSD"}})
    write(f, ev("OPEN", 1, volume=0.1, symbol="GOLD"), ev("OPEN", 2, volume=0.1, symbol="EURUSD"))
    step(eng, fb, clock)
    assert [(c.symbol, c.comment) for c in fb.sent] == [("XAUUSD", "m:own:1")]


# ---- sizing ---------------------------------------------------------------------------------------

SPEC = SymbolSpec(bid=4000, ask=4000.2, digits=2, point=0.01, volume_step=0.01, volume_min=0.01, volume_max=50,
                  tick_value=1.0, tick_size=0.01, trade_allowed=True)


def _m(**kw) -> MirrorConfig:
    return MirrorConfig.model_validate({"source": {"login": 1, "events_dir": "/x"}, "destination": "d", "max_lots": 5, **kw})


def test_balance_ratio_rounds_to_nearest_step():
    assert mirror_volume(_m(), 1.0, 100_000, 50_000, SPEC) == (0.5, None)
    assert mirror_volume(_m(), 0.33, 100_000, 50_000, SPEC) == (0.17, None)     # 0.165 -> 0.17 (halves up)
    assert mirror_volume(_m(sizing={"multiplier": 2}), 0.33, 100_000, 50_000, SPEC) == (0.33, None)


def test_sizing_floors_at_broker_min_and_caps_at_max_lots():
    assert mirror_volume(_m(), 0.01, 100_000, 10_000, SPEC) == (0.01, None)      # 0.001 -> broker minimum
    assert mirror_volume(_m(max_lots=0.25), 1.0, 10_000, 50_000, SPEC) == (0.25, None)
    assert mirror_volume(_m(max_lots=100), 30, 10_000, 50_000, SPEC) == (50, None)    # broker volume_max
    assert mirror_volume(_m(max_lots=0.005), 1.0, 1, 1, SPEC) == (None, "max_lots_below_volume_min")


def test_source_lots_and_fixed_lots():
    assert mirror_volume(_m(sizing={"mode": "source_lots"}), 0.37, 0, 0, SPEC) == (0.37, None)    # balances not needed
    assert mirror_volume(_m(sizing={"mode": "source_lots", "multiplier": 0.5}), 0.37, 0, 0, SPEC) == (0.19, None)
    assert mirror_volume(_m(sizing={"mode": "fixed_lots", "lots": 0.2}), 3.0, 0, 0, SPEC) == (0.2, None)
    assert mirror_volume(_m(), 1.0, 0, 50_000, SPEC) == (None, "source_balance_missing")


# ---- SL/TP ----------------------------------------------------------------------------------------

FX = SymbolSpec(bid=1.08000, ask=1.08002, digits=5, point=0.00001, volume_step=0.01, volume_min=0.01, volume_max=50,
                tick_value=1.0, tick_size=0.00001, trade_allowed=True)


def test_stop_modes():
    assert mirror_stops(_m(), "BUY", 3990, 4020, "XAUUSD", SPEC) == (3990, 4020, None)
    assert mirror_stops(_m(), "BUY", 0, 0, "XAUUSD", SPEC) == (0, 0, None)
    assert mirror_stops(_m(stop_loss="none", take_profit="none"), "BUY", 3990, 4020, "XAUUSD", SPEC) == (0, 0, None)
    # 20 pips on 5-digit FX = 0.0020, from the ask for a buy and the bid for a sell
    assert mirror_stops(_m(stop_loss={"fixed_pips": 20}, take_profit={"fixed_pips": 40}), "BUY", 0, 0, "EURUSD", FX) == (1.07802, 1.08402, None)
    assert mirror_stops(_m(stop_loss={"fixed_pips": 20}, take_profit="copy"), "SELL", 0, 1.07, "EURUSD", FX) == (1.082, 1.07, None)


def test_fixed_pips_needs_pip_size_off_fx():
    sl, tp, why = mirror_stops(_m(stop_loss={"fixed_pips": 50}), "BUY", 0, 4020, "XAUUSD", SPEC)
    assert why == "pip_size_unknown:XAUUSD"
    assert mirror_stops(_m(stop_loss={"fixed_pips": 50}, pip_size={"XAUUSD": 0.1}), "SELL", 0, 0, "XAUUSD", SPEC) == (4005.0, 0, None)


# ---- open / close path ------------------------------------------------------------------------------

def test_open_places_matching_trade_and_alerts(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    c = fb.sent[0]
    assert (c.type, c.symbol, c.side, c.volume, c.sl, c.tp, c.comment) == ("open_market", "XAUUSD", "BUY", 0.5, 3990.0, 4020.0, "m:own:501")
    mp = eng.get("own", 501)
    assert mp.state == OPEN and mp.dest_ticket == fb.positions[0].ticket and mp.dest_open_price == 4000.2
    text = "\n".join(alerts.sent)
    assert "[mirror own] XAUUSD BUY (source #501)" in text and "📥 Source opened BUY 1 XAUUSD @ 4000.1" in text
    assert "➡️ Placing 0.5 lots XAUUSD on demo (SL 3990 · TP 4020)" in text and "✅ Placed 0.5 lots @ 4000.2 on demo" in text


def test_notify_trades_off_sends_nothing(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, notify=False)
    write(f, opened())
    step(eng, fb, clock)
    assert len(fb.positions) == 1 and alerts.sent == []


def test_rejected_open_is_alerted_with_reason(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, ev("OPEN", 7, volume=0.1, symbol="NAS100"))
    step(eng, fb, clock)
    assert fb.sent == [] and any("⛔ Not mirrored: destination has no price for this symbol" in a for a in alerts.sent)


def test_destination_refusal_is_alerted(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    fb.fail_next("open_market", 10019, "no money")
    write(f, opened())
    step(eng, fb, clock)
    assert eng.get("own", 501).state == FAILED and any("❗ Destination refused the trade: no money" in a for a in alerts.sent)
    write(f, ev("CLOSE", 501, volume=1.0, price=4010))     # nothing to close on the destination
    step(eng, fb, clock)
    assert [c.type for c in fb.sent] == ["open_market"]


def test_proportional_partial_closes_then_final_remainder(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, sizing={"mode": "source_lots", "multiplier": 0.3})
    write(f, opened())
    step(eng, fb, clock)
    assert fb.positions[0].volume == 0.3
    write(f, ev("PARTIAL_CLOSE", 501, volume=0.6, remaining=0.4, price=4010.0))
    step(eng, fb, clock)
    assert [(c.type, c.volume) for c in fb.sent[1:]] == [("close", 0.18)]
    assert fb.positions[0].volume == 0.12 and eng.get("own", 501).dest_volume == 0.12
    assert any("✂️ Closed 0.18 lots @ 4000 (0.12 left) · P/L ≈ -3.60" in a for a in alerts.sent)
    write(f, ev("PARTIAL_CLOSE", 501, volume=0.1, remaining=0.3, price=4011.0))     # 0.3 x 0.3 = 0.09 -> close 0.03
    step(eng, fb, clock)
    assert fb.sent[-1].volume == 0.03 and fb.positions[0].volume == 0.09
    fb.set_quote("XAUUSD", 4010.0, 4010.2)
    write(f, ev("CLOSE", 501, volume=0.3, price=4012.0))
    step(eng, fb, clock)
    last = fb.sent[-1]
    assert last.type == "close" and last.volume is None and fb.positions == []
    mp = eng.get("own", 501)
    assert mp.state == CLOSED and mp.dest_volume == 0
    assert any("✋ Closed 0.09 lots @ 4010 · P/L ≈ +88.20 · trade P/L ≈ +84.00" in a for a in alerts.sent)


def test_partial_too_small_to_split_waits_for_final_close(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, sizing={"mode": "fixed_lots", "lots": 0.01})
    write(f, opened())
    step(eng, fb, clock)
    write(f, ev("PARTIAL_CLOSE", 501, volume=0.5, remaining=0.5))
    step(eng, fb, clock)
    assert [c.type for c in fb.sent] == ["open_market"] and fb.positions[0].volume == 0.01
    assert any("can't be split" in a for a in alerts.sent)
    write(f, ev("PARTIAL_CLOSE", 501, volume=0.4, remaining=0.1))    # 10 % of 0.01 is below the minimum: close it all
    step(eng, fb, clock)
    assert fb.positions == [] and eng.get("own", 501).state == CLOSED


def test_replayed_partial_close_is_not_applied_twice(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, sizing={"mode": "source_lots"})
    write(f, opened())
    step(eng, fb, clock)
    pc = ev("PARTIAL_CLOSE", 501, volume=0.5, remaining=0.5)
    write(f, pc)
    step(eng, fb, clock)
    write(f, pc)     # same event again (cursor lost / file re-read)
    step(eng, fb, clock)
    assert [(c.type, c.volume) for c in fb.sent] == [("open_market", 1.0), ("close", 0.5)] and fb.positions[0].volume == 0.5


def test_close_before_open_result_is_applied_after_fill(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(), ev("CLOSE", 501, volume=1.0))     # both read in the same tick: open result not back yet
    clock["now"] += timedelta(seconds=0.2); fb.now = clock["now"]
    eng.tick()
    assert eng.get("own", 501).state == OPENING and eng.get("own", 501).source_volume == 0
    step(eng, fb, clock)
    assert [c.type for c in fb.sent] == ["open_market", "close"] and fb.positions == [] and eng.get("own", 501).state == CLOSED


def test_modify_copies_sl_tp_in_copy_mode(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    write(f, ev("MODIFY", 501, sl=3995.0, tp=0))
    step(eng, fb, clock)
    c = fb.sent[-1]
    assert (c.type, c.sl, c.tp) == ("modify_sltp", 3995.0, 0) and (fb.positions[0].sl, fb.positions[0].tp) == (3995.0, 0)
    assert any("🔒 Destination SL 3995 · TP none" in a for a in alerts.sent)


def test_modify_ignored_for_fixed_or_none_modes(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, stop_loss={"fixed_pips": 30}, take_profit="none", pip_size={"XAUUSD": 0.1})
    write(f, opened())
    step(eng, fb, clock)
    assert (fb.sent[0].sl, fb.sent[0].tp) == (3997.2, 0)
    write(f, ev("MODIFY", 501, sl=3999.0, tp=4050.0))
    step(eng, fb, clock)
    assert [c.type for c in fb.sent] == ["open_market"]


def test_destination_sl_hit_is_reported_and_later_source_close_ignored(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    fb.hit_sl(fb.positions[0].ticket)
    step(eng, fb, clock)
    assert eng.get("own", 501).state == CLOSED and any("🛑 Destination hit its SL @ 3990 · P/L ≈ -510.00" in a for a in alerts.sent)
    write(f, ev("CLOSE", 501, volume=1.0))
    step(eng, fb, clock)
    assert [c.type for c in fb.sent] == ["open_market"]


def test_failed_close_retries_then_asks_for_a_hand_check(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    write(f, ev("CLOSE", 501, volume=1.0))
    for _ in range(8):                           # the market stays closed
        fb.fail_next("close", 10018, "market closed")
        step(eng, fb, clock, n=1)
    assert [c.type for c in fb.sent].count("close") == 3 and len(fb.positions) == 1
    assert eng.get("own", 501).attention and any("⚠️ Gave up after 3 failed commands" in a for a in alerts.sent)


# ---- safety -------------------------------------------------------------------------------------

def test_events_from_another_login_are_ignored_once_alerted(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(login=999), ev("OPEN", 502, volume=1.0, login=999), ev("OPEN", 503, volume=1.0, login=0))
    step(eng, fb, clock)
    assert fb.sent == []
    assert sum("Events file written by account 999" in a for a in alerts.sent) == 1
    assert any("unknown (old CopierSource build)" in a for a in alerts.sent)


def test_destination_arming_guard(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path, dest_login=12345)
    write(f, opened())
    step(eng, fb, clock)
    assert fb.sent == [] and any("wrong account" in a for a in alerts.sent)
    eng, store, fb, clock, alerts, f = make(tmp_path / "real", trade_mode="REAL")
    write(f, opened())
    step(eng, fb, clock)
    assert fb.sent == [] and any("REAL account but live: false" in a for a in alerts.sent)


def test_stale_open_not_mirrored(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(ts=LOCAL0 - timedelta(seconds=400)))
    step(eng, fb, clock)
    assert fb.sent == [] and any("older than max_open_age_sec" in a for a in alerts.sent)


def test_silent_destination_holds_events_until_back(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    frozen = fb.read_state()
    fb.read_state = lambda: frozen       # terminal stopped writing state.json
    write(f, opened(ts=LOCAL0 + timedelta(seconds=10)))
    clock["now"] = LOCAL0 + timedelta(seconds=10); eng.tick()
    assert fb.sent == [] and any("⏳ Destination demo terminal not responding" in a for a in alerts.sent)
    del fb.read_state                    # back
    fb.now = clock["now"] = LOCAL0 + timedelta(seconds=30)
    eng.tick(); eng.tick()
    assert [c.type for c in fb.sent] == ["open_market"] and any("mirroring resumed" in a for a in alerts.sent)


def test_never_writes_to_the_source(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    before = sorted(p.name for p in f.parent.iterdir())
    write(f, opened(), ev("CLOSE", 501, volume=1.0))
    size = f.stat().st_size
    step(eng, fb, clock, n=4)
    assert sorted(p.name for p in f.parent.iterdir()) == before and f.stat().st_size == size


# ---- restart / persistence ------------------------------------------------------------------------

def _restart(eng, store, fb, clock):
    return MirrorEngine(eng.cfg, store, {"demo": fb}, alerter=Alerts(), now_local=lambda: clock["now"])


def test_restart_never_double_opens(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    clock["now"] += timedelta(seconds=0.2); fb.now = clock["now"]
    eng.tick()                                   # open sent, result not read yet
    fb.read_results()                            # ...and the result is lost with the crash
    store.kv_set("mirror_cursor:own", json.dumps({"file": f.name, "offset": 0}))    # cursor not saved either
    eng2 = _restart(eng, store, fb, clock)
    step(eng2, fb, clock, n=3)
    assert len(fb.positions) == 1                                       # replayed OPEN re-sent the same cmd_id
    assert eng2.get("own", 501).state == OPEN and eng2.get("own", 501).dest_ticket == fb.positions[0].ticket   # adopted by comment
    write(f, ev("CLOSE", 501, volume=1.0))
    step(eng2, fb, clock)
    assert fb.positions == []


def test_restart_resends_an_open_that_never_reached_the_bridge(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    real_send = fb.send
    fb.send = lambda cmd: None                   # crash after saving the position, before the command was written
    clock["now"] += timedelta(seconds=0.2); fb.now = clock["now"]
    eng.tick()
    fb.send = real_send
    store.kv_set("mirror_cursor:own", json.dumps({"file": f.name, "offset": 0}))
    eng2 = _restart(eng, store, fb, clock)
    step(eng2, fb, clock)
    assert len(fb.positions) == 1 and eng2.get("own", 501).state == OPEN


def test_restart_with_open_position_still_mirrors_the_close(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    eng2 = _restart(eng, store, fb, clock)
    write(f, ev("CLOSE", 501, volume=1.0))
    step(eng2, fb, clock)
    assert fb.positions == [] and [c.type for c in fb.sent] == ["open_market", "close"]


def test_first_start_skips_history_and_rotates_to_next_day(tmp_path):
    events = tmp_path / "Common"; events.mkdir()
    old = events / "copytrader_events_20261026.jsonl"
    write(old, ev("OPEN", 1, volume=1.0))
    cfg = AppConfig(accounts={"demo": {"bridge_dir": "/d", "expected_login": DST}},
                    mirrors={"own": mirror_cfg(events)}, notify_trades=False)
    fb = FakeBridge(LOCAL0, login=DST); fb.set_quote("XAUUSD", 4000.0, 4000.2)
    clock = {"now": LOCAL0}
    eng = MirrorEngine(cfg, Store(":memory:"), {"demo": fb}, now_local=lambda: clock["now"])
    eng.tick()
    assert fb.sent == []                                     # trade from before the mirror existed
    write(events / "copytrader_events_20261027.jsonl", ev("OPEN", 2, volume=1.0))
    step(eng, fb, clock)
    assert [c.comment for c in fb.sent] == ["m:own:2"]
    assert json.loads(eng.store.kv_get("mirror_cursor:own"))["file"] == "copytrader_events_20261027.jsonl"


def test_latency_is_journaled(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened(ts=LOCAL0 - timedelta(seconds=1)))
    step(eng, fb, clock, sec=0.25)
    lat = next(e["detail"] for e in store.journal_tail() if e["kind"] == "mirror_latency")
    assert lat["type"] == "open" and lat["ok"] is True
    assert lat["source_to_read_s"] == 1.25 and lat["read_to_send_s"] == 0 and lat["send_to_result_s"] == 0.25
    from tg_signal_trader.cli import mirror_latency_lines
    lines = mirror_latency_lines(store)
    assert len(lines) == 2 and "own" in lines[1] and "1.250" in lines[1]


def test_status_lines(tmp_path):
    eng, store, fb, clock, alerts, f = make(tmp_path)
    write(f, opened())
    step(eng, fb, clock)
    lines = mirror_status_lines(eng.cfg, store, {"demo": fb})
    assert lines[0].startswith(f"[mirror own] source {SRC}") and "armed (demo)" in lines[0]
    assert "source #501 XAUUSD BUY 1/1 ->" in lines[1] and "0.5/0.5 OPEN" in lines[1]


def test_readme_mirror_example_parses():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme.split("## Mirror one of my accounts to another", 1)[1]
    block = section.split("```yaml\n", 1)[1].split("```", 1)[0]
    cfg = AppConfig.model_validate(yaml.safe_load(block))
    m = cfg.mirrors["icon"]
    assert m.source.login == 4100074 and cfg.mirror_destination("icon").expected_login == 1514988624
    assert m.sizing.mode == "source_lots" and m.source.symbols["XAUUSD.p"] == "XAUUSD" and cfg.notify_trades


def test_dry_run_journals_commands_and_does_not_report_a_vanished_position(tmp_path):
    from tg_signal_trader.trader import DryRunBridge
    eng, store, fb, clock, alerts, f = make(tmp_path)
    dry = DryRunBridge(fb, store, "mirror-dest:demo")
    eng.bridges = {"demo": dry}
    write(f, opened())
    step(eng, fb, clock, n=3)
    clock["now"] += timedelta(seconds=60); fb.now = clock["now"]
    eng.tick()
    write(f, ev("CLOSE", 501, volume=1.0))
    step(eng, fb, clock)
    assert fb.sent == [] and fb.positions == []
    assert [e["detail"]["type"] for e in reversed(store.journal_tail(200)) if e["kind"] == "dry_run_command"] == ["open_market", "close"]
    assert not any("outside the mirror" in a for a in alerts.sent)
