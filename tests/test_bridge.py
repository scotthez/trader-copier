import json
from datetime import datetime
from pathlib import Path
from tg_signal_trader.bridge import (FileBridge, FakeBridge, BridgeState, Command, CommandResult, TS_FMT)

STATE = {"ts": "2026.09.18 13:20:07", "account": {"login": 1, "balance": 10000, "equity": 10050, "margin_free": 9000, "hedging": True},
         "symbols": {"XAUUSD": {"bid": 4372.0, "ask": 4372.4, "digits": 2, "point": 0.01, "volume_step": 0.01, "volume_min": 0.01,
                                "volume_max": 50, "tick_value": 1.0, "tick_size": 0.01, "trade_allowed": True}},
         "positions": [{"ticket": 11, "symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "price_open": 4370, "sl": 4360, "tp": 4380, "comment": "sig:w:1:L1", "magic": 903001}],
         "orders": [{"ticket": 12, "symbol": "XAUUSD", "type": "BUY_LIMIT", "volume": 0.1, "price": 4360, "sl": 4350, "tp": 4380, "comment": "sig:w:1:L2", "expiration": "2026.09.19 13:20:07"}],
         "deals_recent": [{"ticket": 5, "position_id": 11, "entry": "IN", "reason": "EXPERT", "price": 4370, "volume": 0.1, "time": "2026.09.18 13:10:00"}]}


def test_file_bridge_round_trip(tmp_path: Path):
    b = FileBridge(tmp_path)
    assert b.read_state() is None
    (tmp_path / "state.json").write_text(json.dumps(STATE))
    st = b.read_state()
    assert st.account.balance == 10000 and st.symbols["XAUUSD"].quote().mid == 4372.2
    assert st.ts_local() == datetime(2026, 9, 18, 13, 20, 7)
    assert st.age_sec(datetime(2026, 9, 18, 13, 20, 9)) == 2.0
    assert st.position_by_comment("sig:w:1:L1").ticket == 11 and st.order_by_comment("sig:w:1:L2").ticket == 12
    assert st.position_by_comment("nope") is None and st.out_deals_for(11) == []
    b.send(Command(cmd_id="w:1:L1:1", type="open_market", symbol="XAUUSD", side="BUY", volume=0.1, sl=4341, tp=4353, comment="sig:w:1:L1"))
    line = (tmp_path / "commands.jsonl").read_text()
    assert json.loads(line) == {"cmd_id": "w:1:L1:1", "type": "open_market", "symbol": "XAUUSD", "side": "BUY", "volume": 0.1, "sl": 4341, "tp": 4353, "comment": "sig:w:1:L1"}
    (tmp_path / "results.jsonl").write_text('{"cmd_id":"w:1:L1:1","ok":true,"retcode":10009,"retcode_text":"done","position":11,"order":11,"fill_price":4372.4,"attempts":1}\n{"cmd_id":"x","ok":fal')
    rs = b.read_results()
    assert len(rs) == 1 and rs[0].position == 11 and rs[0].ok and b.results_offset > 0
    assert b.read_results() == []                       # partial line not consumed
    (tmp_path / "results.jsonl").open("a").write('sy}\n')
    assert b.read_results() == []                       # completed but unparseable → skipped, not raised
    assert b.read_state().age_sec(datetime(2026, 9, 18, 13, 20, 7)) == 0


def test_fake_bridge_simulates_lifecycle():
    fb = FakeBridge(now_local=datetime(2026, 9, 18, 13, 0, 0))
    fb.set_quote("XAUUSD", 4372.0, 4372.4)
    fb.send(Command(cmd_id="c1", type="ping"))
    fb.send(Command(cmd_id="c2", type="open_market", symbol="XAUUSD", side="BUY", volume=0.1, sl=4341, tp=4353, comment="sig:w:1:L1"))
    fb.send(Command(cmd_id="c3", type="open_pending", symbol="XAUUSD", side="SELL", volume=0.1, price=4390, sl=4402, tp=4385, comment="sig:w:2:L1", expires_at="2026.09.19 13:00:00"))
    rs = {r.cmd_id: r for r in fb.read_results()}
    assert rs["c1"].ok and rs["c2"].ok and rs["c2"].position > 0 and rs["c2"].fill_price == 4372.4 and rs["c3"].order > 0
    st = fb.read_state()
    assert len(st.positions) == 1 and len(st.orders) == 1 and st.position_by_comment("sig:w:1:L1").sl == 4341
    fb.send(Command(cmd_id="c4", type="modify_sl", position=rs["c2"].position, sl=4350))
    assert fb.read_results()[0].ok and fb.read_state().positions[0].sl == 4350
    fb.hit_tp(rs["c2"].position)
    st = fb.read_state()
    assert st.positions == [] and st.out_deals_for(rs["c2"].position)[0].reason == "TP"
    fb.fill_pending(rs["c3"].order)
    st = fb.read_state()
    assert st.orders == [] and st.position_by_comment("sig:w:2:L1").type == "SELL" and st.position_by_comment("sig:w:2:L1").price_open == 4390
    fb.fail_next("close", 10018, "Market closed")
    fb.send(Command(cmd_id="c5", type="close", position=st.position_by_comment("sig:w:2:L1").ticket))
    r = fb.read_results()[0]
    assert not r.ok and r.retcode == 10018
    fb.send(Command(cmd_id="c6", type="close", position=st.position_by_comment("sig:w:2:L1").ticket))
    assert fb.read_results()[0].ok and fb.read_state().positions == []
    fb.advance(3600)
    assert fb.read_state().ts_local() == datetime(2026, 9, 18, 14, 0, 0)
    assert [c.cmd_id for c in fb.sent] == ["c1", "c2", "c3", "c4", "c5", "c6"]
