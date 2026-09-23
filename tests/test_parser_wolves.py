from datetime import datetime, timezone
from pathlib import Path
import pytest
from tg_signal_trader.models import InboxMessage, Side, EntryType
from tg_signal_trader.parsers import get_parser
from tg_signal_trader.export import read_fixture

T0 = datetime(2026, 9, 11, 15, 35, 48, tzinfo=timezone.utc)


def msg(text, mid=1):
    return InboxMessage(msg_id=mid, chat_id=-2, provider="wolves", text=text, ts=T0)


def test_at_template_market():
    text = "BUY XAUUSD @4347\n\nSL 4341\nTP1 4353\nTP2 4357\nTP3 4362\nTP4 Open"
    r = get_parser("wolves").parse(msg(text, 10), [])
    s = r.signal
    assert r.template == "wolves.at" and s.symbol == "XAUUSD" and s.side == Side.BUY and s.entry_type == EntryType.MARKET
    assert s.sl == 4341 and s.tps == [4353, 4357, 4362, 4367]


def test_at_template_limit_zone():
    text = "SELL Limit XAUUSD @4290 4295\n\nSL 4302\nTP1 4285\nTP2 4280\nTP3 4275\nTP4 Open"
    s = get_parser("wolves").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.entry_type == EntryType.LIMIT and s.entry_zone == [4290.0, 4295.0]
    assert s.tps == [4285, 4280, 4275, 4270]


def test_block_template():
    # "TP: Open" here is a blanket disclaimer, not a TP4 line — no TP4 is ever mentioned, so this
    # stays a 3-leg signal (see test_block_template_with_explicit_tp4 for the 4-leg case).
    text = ("Gold 🏆\nPair: XAUUSD 📊\nSide: Long / Buy Limit 3rd entry\nEntry: 4417 4413\nTP: Open\nSL: 4400\n"
            "Note: past profits do not predict future profits\nRisk 0.5-1-2%\nTP1 4422 50pips ✅\nTP2 4427 100pips ✅\nTP3 4432 150pips ✅\nUse Proper Risk Management")
    r = get_parser("wolves").parse(msg(text, 29524), [])
    s = r.signal
    assert r.template == "wolves.block" and s.side == Side.BUY and s.entry_type == EntryType.LIMIT
    assert s.entry_zone == [4417.0, 4413.0] and s.sl == 4400 and s.tps == [4422, 4427, 4432]


def test_block_template_with_explicit_tp4():
    text = ("Gold 🏆\nPair: XAUUSD 📊\nSide: Long / Buy Limit\nEntry: 4417 4413\nSL: 4400\n"
            "TP1 4422 50pips ✅\nTP2 4427 100pips ✅\nTP3 4432 150pips ✅\nTP4 Open\nUse Proper Risk Management")
    s = get_parser("wolves").parse(msg(text, 29525), []).signal
    assert s.tps == [4422, 4427, 4432, 4437]


def test_block_template_market_short():
    text = "Gold 🏆\nPair: XAUUSD 📊\nSide: Short / Sell\nEntry: 4402\nTP: Open\nSL: 4415\nTP1 4397 50pips ✅\nTP2 4392 100pips ✅\nTP3 4387 150pips ✅"
    s = get_parser("wolves").parse(msg(text), []).signal
    assert s.side == Side.SELL and s.entry_type == EntryType.MARKET and s.entry_zone == [] and s.tps == [4397, 4392, 4387]


def test_now_template():
    text = "Gold sell now 2034 - 2037\n\nSL: 2040\n\nTP: 2032\nTP: 2030\nTP: 2028"
    r = get_parser("wolves").parse(msg(text), [])
    s = r.signal
    assert r.template == "wolves.now" and s.side == Side.SELL and s.entry_type == EntryType.MARKET
    assert s.sl == 2040 and s.tps == [2032, 2030, 2028]


def test_non_entry_messages():
    p = get_parser("wolves")
    for t in ["RUNNING 40PIPS 🤑🤑🤑", "Secure 50% and BE", "Move SL 4412", "Delete this", "I'm in 4364 enter now"]:
        assert p.parse(msg(t), []).signal is None


def test_corpus_counts_and_pinned_samples():
    p = get_parser("wolves")
    msgs = read_fixture(Path("fixtures/wolves_messages.jsonl"))
    by_template: dict[str, int] = {}
    parsed = {}
    for i, m in enumerate(msgs):
        r = p.parse(m, msgs[max(0, i - 3):i])
        if r.signal:
            by_template[r.template] = by_template.get(r.template, 0) + 1
            parsed[m.msg_id] = r.signal
    # ~2270 messages carry "SL:"; XAUUSD-only templates must cover most of them (crypto/FX ones are skipped by design).
    assert sum(by_template.values()) >= 1900, by_template
    assert by_template.get("wolves.block", 0) >= 1000 and by_template.get("wolves.now", 0) >= 600
    assert parsed[29603].side == Side.SELL and parsed[29603].entry_type == EntryType.LIMIT and parsed[29603].entry_zone == [4393.0, 4396.0] and parsed[29603].sl == 4408
    assert parsed[29546].side == Side.BUY and parsed[29546].entry_type == EntryType.MARKET and parsed[29546].tps == [4417, 4427, 4437]
    assert parsed[46].side == Side.SELL and parsed[46].sl == 2040 and parsed[46].tps[:3] == [2032, 2030, 2028]
