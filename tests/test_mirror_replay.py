"""Feeds a CopierSource events file (fixtures/mirror_events_20261027.jsonl: the EA's exact line format,
account 4100074 with a copy-platform trade, a Wolves bot leg and two hand-placed trades) line by line
through the whole mirror path to a fake destination bridge."""
import json
from datetime import timedelta
from pathlib import Path
from tg_signal_trader.bridge import FakeBridge, parse_ts
from tg_signal_trader.config import AppConfig
from tg_signal_trader.mirror import MirrorEngine, CLOSED
from tg_signal_trader.store import Store

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mirror_events_20261027.jsonl"


class Alerts:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def test_replay_events_file_to_fake_bridge(tmp_path):
    lines = FIXTURE.read_text().splitlines()
    events_dir = tmp_path / "Common" / "Files"; events_dir.mkdir(parents=True)
    f = events_dir / "copytrader_events_20261027.jsonl"; f.write_text("")
    cfg = AppConfig(accounts={"ftmo_demo": {"bridge_dir": str(tmp_path / "sb"), "expected_login": 1514988624}},
                    mirrors={"icon": {"source": {"login": 4100074, "events_dir": str(events_dir)}, "destination": "ftmo_demo",
                                      "sizing": {"mode": "source_lots"}, "max_lots": 1.0}})
    cfg.mirrors["icon"].source.symbols = {"XAUUSD.p": "XAUUSD", "EURUSD.p": "EURUSD", "USDJPY.p": "USDJPY"}
    first = parse_ts(json.loads(lines[0])["ts"])
    clock = {"now": first - timedelta(seconds=1)}
    fb = FakeBridge(now_local=clock["now"], balance=10_000, login=1514988624)
    fb.set_quote("XAUUSD", 4001.3, 4001.5)
    fb.set_quote("EURUSD", 1.08000, 1.08002, digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0)
    fb.set_quote("USDJPY", 152.30, 152.31, digits=3, point=0.001, tick_size=0.001, tick_value=0.66)
    store, alerts = Store(":memory:"), Alerts()
    eng = MirrorEngine(cfg, store, {"ftmo_demo": fb}, alerter=alerts, now_local=lambda: clock["now"])
    eng.tick()
    for line in lines:                         # one source event at a time, as CopierSource writes them
        clock["now"] = parse_ts(json.loads(line)["ts"]) + timedelta(seconds=0.3)
        fb.now = clock["now"]
        with open(f, "a") as out:
            out.write(line + "\n")
        for _ in range(3):
            clock["now"] += timedelta(seconds=0.5); fb.now = clock["now"]
            eng.tick()

    sent = [(c.type, c.symbol or c.position, c.side, c.volume, c.sl, c.tp) for c in fb.sent]
    gold, eur = fb.sent[0].cmd_id, fb.sent[2].cmd_id
    assert [c.type for c in fb.sent] == ["open_market", "modify_sltp", "open_market", "close", "close", "close"]
    assert sent[0] == ("open_market", "XAUUSD", "BUY", 0.1, 3991.35, 4021.35)              # same size, source SL/TP
    assert (fb.sent[1].sl, fb.sent[1].tp) == (3998.0, 4021.35)                             # MODIFY followed
    assert sent[2] == ("open_market", "EURUSD", "SELL", 0.2, 1.08301, 1.07501)
    assert (fb.sent[3].volume, fb.sent[4].volume, fb.sent[5].volume) == (0.04, None, None)  # partial, then the remainder, then EUR
    assert gold.startswith("m:icon:88001207:") and eur.startswith("m:icon:88001215:")
    # the copy-platform trade and the Wolves bot leg never reached the destination
    assert not any(c.symbol == "USDJPY" for c in fb.sent) and not any(c.side == "SELL" and c.symbol == "XAUUSD" for c in fb.sent)
    assert fb.positions == []
    assert [mp.state for mp in eng.positions("icon")] == [CLOSED, CLOSED]
    filtered = [e["detail"]["why"] for e in store.journal_tail(500) if e["kind"] == "mirror_filtered"]
    assert sorted(filtered) == ["copy_platform_trade", "magic 903001 not in include_magics"]
    # every mirrored action has a latency row; one message per source event / result moment
    assert sum(e["kind"] == "mirror_latency" for e in store.journal_tail(500)) == 6
    text = "\n".join(alerts.sent)
    assert "USDJPY" not in text
    assert "📥 Source opened BUY 0.1 XAUUSD.p @ 4001.35" in text and "✅ Placed 0.1 lots @ 4001.5 on ftmo_demo" in text
    assert "✂️ Closed 0.04 lots" in text and "✋ Closed 0.06 lots" in text and "✋ Closed 0.2 lots" in text
