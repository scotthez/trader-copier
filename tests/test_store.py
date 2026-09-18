from datetime import datetime, timezone
from tg_signal_trader.models import InboxMessage, Signal, SignalRun, Side, EntryType, RunState
from tg_signal_trader.store import Store

T = datetime(2026, 9, 18, 13, 20, 7, tzinfo=timezone.utc)


def sig(mid=1, provider="wolves"):
    return Signal(id=f"{provider}:{mid}", provider=provider, symbol="XAUUSD", side=Side.BUY, entry_type=EntryType.MARKET,
                  entry_zone=[], sl=4341, tps=[4353, 4357, 4362, 4367], received_at=T, raw_text="x", telegram_msg_id=mid)


def test_inbox_dedupe_order_and_status():
    s = Store(":memory:")
    assert s.add_inbox(InboxMessage(msg_id=5, chat_id=-1, provider="wolves", text="b", ts=T))
    assert s.add_inbox(InboxMessage(msg_id=3, chat_id=-1, provider="wolves", text="a", ts=T))
    assert not s.add_inbox(InboxMessage(msg_id=5, chat_id=-1, provider="wolves", text="dup", ts=T))
    assert [m.msg_id for m in s.new_inbox("wolves")] == [3, 5]
    s.set_inbox_status("wolves", 3, "processed")
    assert [m.msg_id for m in s.new_inbox("wolves")] == [5]
    assert [m.msg_id for m in s.recent_inbox("wolves", before_msg_id=5, n=3)] == [3]
    assert s.new_inbox("lewis") == []


def test_runs_round_trip_and_queries():
    s = Store(":memory:")
    r1 = SignalRun.from_signal(sig(1)); r2 = SignalRun.from_signal(sig(2))
    r2.state = RunState.ACTIVE
    s.save_run(r1); s.save_run(r2)
    back = s.get_run("wolves:1")
    assert back == r1 and s.get_run("nope") is None
    assert [r.id for r in s.runs("wolves")] == ["wolves:2", "wolves:1"]
    assert [r.id for r in s.runs("wolves", [RunState.ACTIVE])] == ["wolves:2"]
    assert s.run_by_msg_id("wolves", 2).id == "wolves:2" and s.run_by_msg_id("wolves", 9) is None
    r1.state = RunState.DONE; s.save_run(r1)
    assert s.get_run("wolves:1").state == RunState.DONE


def test_journal_classifications_kv():
    s = Store(":memory:")
    s.journal("wolves", "signal_rejected", {"reasons": ["stale"]}, run_id="wolves:1")
    s.journal("lewis", "command_sent", {"cmd_id": "x"})
    tail = s.journal_tail(10)
    assert [e["kind"] for e in tail] == ["command_sent", "signal_rejected"] and tail[1]["detail"]["reasons"] == ["stale"]
    s.save_classification("wolves", 7, "Delete this", "close_all", None, 0.95, "provider says delete", True)
    assert s.kv_get("k") is None
    s.kv_set("k", "v"); s.kv_set("k", "w")
    assert s.kv_get("k") == "w"
