from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from tg_signal_trader.models import InboxMessage, Side, EntryType
from tg_signal_trader.normalize import normalize_text, canonical_symbol
from tg_signal_trader.parsers import get_parser, looks_like_entry
from tg_signal_trader.export import read_fixture

T0 = datetime(2026, 9, 17, 7, 45, 4, tzinfo=timezone.utc)


def msg(text, mid=1, ts=T0, reply_to=None):
    return InboxMessage(msg_id=mid, chat_id=-1, provider="lewis", text=text, ts=ts, reply_to=reply_to)


def test_normalize_text():
    assert normalize_text("🟡  SELL XAUUSD NOW! 🔽\nStop Loss: 4377.26\nTake Profit Targets:\n🎯 TP1: 4369.94") == "SELL XAUUSD NOW!\nSL: 4377.26\nTP1: 4369.94"
    assert normalize_text("Entry Zone: 5164 – 5160") == "Entry Zone: 5164 - 5160"
    assert canonical_symbol("Gold") == "XAUUSD" and canonical_symbol("US100") == "NAS100" and canonical_symbol("DE40") == "GER40"
    assert canonical_symbol("BANANA") is None


def test_trade_setup_template():
    text = "TRADE SETUP:\n🔵  BUY NAS100\n\nStop Loss: 29088.91\n\nTake Profit Targets:\n🎯 TP1: 29212.15\n🎯 TP2: 29240.59\n🎯 TP3: 29306.95\n🎯 TP4: 29373.31"
    r = get_parser("lewis").parse(msg(text, 3701), [])
    s = r.signal
    assert r.template == "lewis.trade_setup" and s is not None
    assert (s.symbol, s.side, s.entry_type) == ("NAS100", Side.BUY, EntryType.MARKET)
    assert s.sl == 29088.91 and s.tps == [29212.15, 29240.59, 29306.95, 29373.31]
    assert s.id == "lewis:3701" and s.telegram_msg_id == 3701 and s.parsed_by == "lewis.trade_setup"


def test_trade_idea_with_open_tp4():
    text = "TRADE IDEA:\n🟡  SELL XAUUSD\nStop Loss: 4686.64\nTake Profit Targets:\n🎯 TP1: 4669.85\n🎯 TP2: 4667.17\n🎯 TP3: 4661.79\n🎯 TP4: OPEN"
    s = get_parser("lewis").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.tps == pytest.approx([4669.85, 4667.17, 4661.79, 4656.41])


def test_now_template_single_message():
    text = "🔵 BUY NAS100 NOW\n\nStop Loss: 24389\n\nTake Profit Targets:\n🎯 TP1: 24545\n🎯 TP2: 24620\n🎯 TP3: 24729\n🎯 TP4: 24949"
    r = get_parser("lewis").parse(msg(text, 8), [])
    assert r.template == "lewis.now" and r.signal.entry_type == EntryType.MARKET and r.signal.sl == 24389


def test_now_split_across_two_messages():
    head = msg("🔵  SELL NAS100 NOW!", 599, T0)
    body = msg("Stop Loss: 26249.79\nTake Profit Targets:\n🎯 TP1: 26123.85\n🎯 TP2: 26103.70\n🎯 TP3: 26063.40\n🎯 TP4: 25962.65", 600, T0 + timedelta(seconds=4))
    r = get_parser("lewis").parse(body, [head])
    assert r.template == "lewis.now_split" and r.signal.side == Side.SELL and r.signal.symbol == "NAS100" and r.signal.id == "lewis:600"
    # too old a header → not combined
    stale = msg("🔵  SELL NAS100 NOW!", 599, T0 - timedelta(seconds=120))
    assert get_parser("lewis").parse(body, [stale]).signal is None


def test_limit_setup_template():
    text = "🟡 XAUUSD — BUY LIMIT SETUP\n\nEntry Zone: 5164 – 5160\n\nStop Loss: 5154\n\nTake Profit Targets:\n🎯 TP1: 5167\n🎯 TP2: 5174\n🎯 TP3: 5184\n🎯 TP4: 5204"
    r = get_parser("lewis").parse(msg(text, 16), [])
    s = r.signal
    assert r.template == "lewis.limit_setup" and s.entry_type == EntryType.LIMIT and s.entry_zone == [5164.0, 5160.0]
    assert s.side == Side.BUY and s.sl == 5154 and s.tps == [5167, 5174, 5184, 5204]


def test_non_entry_messages_do_not_parse():
    p = get_parser("lewis")
    for t in ["TP1 HIT! ✔️", "You can put your stop-loss to break-even if you wish", "Get ready for NAS100! 🚨", "Cancel this order!"]:
        assert p.parse(msg(t), []).signal is None
    assert looks_like_entry("Stop Loss: 1 TP1: 2") and not looks_like_entry("TP1 HIT!")


def test_corpus_counts_and_pinned_samples():
    p = get_parser("lewis")
    msgs = read_fixture(Path("fixtures/lewis_messages.jsonl"))
    by_template: dict[str, int] = {}
    parsed = {}
    for i, m in enumerate(msgs):
        r = p.parse(m, msgs[max(0, i - 3):i])
        if r.signal:
            by_template[r.template] = by_template.get(r.template, 0) + 1
            parsed[m.msg_id] = r.signal
    total = sum(by_template.values())
    # ~389 entry-like messages exist in the export; templates must cover the large majority.
    assert total >= 360, by_template
    assert by_template.get("lewis.trade_setup", 0) >= 250
    assert by_template.get("lewis.limit_setup", 0) >= 15
    # Only 5 genuine split posts exist (April 2026). The other "Stop Loss:"-first bodies follow a
    # header-only RE-POST of a signal parsed seconds earlier ("for the people who can't see the
    # trade") — combining those would double-trade, so they must stay unparsed.
    assert by_template.get("lewis.now_split", 0) == 5
    # pinned samples (message ids from the export)
    assert parsed[3743].symbol == "XAUUSD" and parsed[3743].side == Side.SELL and parsed[3743].sl == 4377.26 and parsed[3743].tps == [4369.94, 4368.76, 4366.42, 4360.56]
    assert parsed[3735].symbol == "NAS100" and parsed[3735].tps[0] == 29657.33
    assert parsed[16].entry_type == EntryType.LIMIT and parsed[16].entry_zone == [5164.0, 5160.0]
    assert parsed[600].side == Side.SELL and parsed[600].symbol == "NAS100"
