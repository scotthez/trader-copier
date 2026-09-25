from datetime import datetime, timezone
import pytest
from tg_signal_trader.models import Signal, SignalRun, Side, EntryType, LegState, RunState
from tg_signal_trader.config import ProviderConfig
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.ladder import place_run, apply_results, sync_run, apply_action

NOW = datetime(2026, 9, 18, 13, 0, 0)
CFG = ProviderConfig(telegram_chat=1, bridge_dir="/tmp/x", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)}, pending_ttl_hours=24)


def mk(side=Side.BUY, entry_type=EntryType.MARKET, zone=None, sl=4341.0, tps=(4353.0, 4357.0, 4362.0, 4367.0), mid=1):
    sig = Signal(id=f"wolves:{mid}", provider="wolves", symbol="XAUUSD", side=side, entry_type=entry_type, entry_zone=zone or [],
                 sl=sl, tps=list(tps), received_at=datetime(2026, 9, 18, 12, 59, 50, tzinfo=timezone.utc), raw_text="", telegram_msg_id=mid)
    return SignalRun.from_signal(sig)


def bridge():
    fb = FakeBridge(now_local=NOW, balance=10_000)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    return fb


def pump(run, fb):
    """Deliver results, then sync against state — one trader tick."""
    ev = apply_results(run, fb.read_results())
    ev += sync_run(run, CFG, fb.read_state(), fb, fb.now)
    return ev


def test_market_placement_sizes_and_opens_four_legs():
    fb, run = bridge(), mk()
    ev = place_run(run, CFG, fb.read_state(), fb, fb.now)
    assert run.state == RunState.PLACING and len(fb.sent) == 4 and all(c.type == "open_market" for c in fb.sent)
    # $100 risk / (6.0 / 0.01 * 1.0 = $600 per lot) = 0.1666 → 0.16 lots per leg
    assert [c.volume for c in fb.sent] == [0.16] * 4 and [c.tp for c in fb.sent] == [4353, 4357, 4362, 4367] and fb.sent[0].sl == 4341
    assert fb.sent[2].comment == "sig:wolves:1:L3" and fb.sent[0].cmd_id == "wolves:1:L1:1"
    pump(run, fb)
    assert run.state == RunState.ACTIVE and [l.state for l in run.legs] == [LegState.OPEN] * 4
    assert all(l.position_ticket and l.entry_price == 4347.0 for l in run.legs)
    assert [e["kind"] for e in ev][:1] == ["command_sent"]


def test_ladder_moves_be_after_tp2_and_tp1_after_tp3():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.hit_tp(run.legs[0].position_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.CLOSED_TP and [l.sl_current for l in run.legs[1:]] == [4341] * 3 and not run.be_applied
    fb.hit_tp(run.legs[1].position_ticket); pump(run, fb)
    assert run.be_applied and [c.type for c in fb.sent[4:]] == ["modify_sl", "modify_sl"]
    pump(run, fb)                              # deliver modify results
    assert run.legs[2].sl_current == 4347.0 and run.legs[3].sl_current == 4347.0   # own entry (break-even)
    st = fb.read_state()
    assert st.position_by_comment("sig:wolves:1:L3").sl == 4347.0
    fb.hit_tp(run.legs[2].position_ticket); pump(run, fb); pump(run, fb)
    assert run.tp1_applied and run.legs[3].sl_current == 4353.0                    # TP1 price
    fb.hit_tp(run.legs[3].position_ticket); pump(run, fb)
    assert run.state == RunState.DONE and run.is_finished()


def test_sl_hit_and_manual_close_are_classified():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.hit_sl(run.legs[0].position_ticket); fb.close_manually(run.legs[1].position_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.CLOSED_SL and run.legs[1].state == LegState.CLOSED_MANUAL


def test_sl_moves_are_one_way():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    # Simulate the broker already having a better stop on L3 (e.g. moved by hand) than break-even
    run.legs[2].sl_current = 4350.0
    fb.hit_tp(run.legs[0].position_ticket); fb.hit_tp(run.legs[1].position_ticket); pump(run, fb)
    sent = [c for c in fb.sent if c.type == "modify_sl"]
    assert len(sent) == 1 and sent[0].comment is None and sent[0].position == run.legs[3].position_ticket   # L3 not worsened


def test_pending_placement_fill_expiry_and_cancel_on_sl():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0, 4395.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=2)
    place_run(run, CFG, fb.read_state(), fb, fb.now)
    assert all(c.type == "open_pending" and c.price == 4390.0 and c.expires_at == "2026.09.19 12:59:50" for c in fb.sent)
    # sizing uses the zone price: $100 / (12.0/0.01*1) = 0.0833 → 0.08
    assert fb.sent[0].volume == pytest.approx(0.08)
    pump(run, fb)
    assert [l.state for l in run.legs] == [LegState.PENDING_ORDER] * 4 and run.state == RunState.ACTIVE
    fb.fill_pending(run.legs[0].order_ticket); fb.fill_pending(run.legs[1].order_ticket); pump(run, fb)
    assert run.legs[0].state == LegState.OPEN and run.legs[0].entry_price == 4390.0 and run.legs[0].position_ticket == run.legs[0].order_ticket
    fb.expire_order(run.legs[2].order_ticket); pump(run, fb)
    assert run.legs[2].state == LegState.PENDING_ORDER                   # within the grace: could be a lagging snapshot
    fb.advance(31); pump(run, fb)
    assert run.legs[2].state == LegState.CANCELLED and "gone" in run.legs[2].reason
    fb.hit_sl(run.legs[0].position_ticket); pump(run, fb)
    assert run.pendings_cancelled and any(c.type == "cancel" and c.order == run.legs[3].order_ticket for c in fb.sent)
    pump(run, fb)
    assert run.legs[3].state == LegState.CANCELLED


def test_pending_ttl_backstop_cancels_after_expiry():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=3)
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.advance(24 * 3600 + 120); pump(run, fb)
    assert sum(1 for c in fb.sent if c.type == "cancel") == 4


def test_failed_leg_does_not_block_others():
    fb, run = bridge(), mk()
    fb.fail_next("open_market", 10019, "No money")
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    assert run.legs[0].state == LegState.CANCELLED and "No money" in run.legs[0].reason
    assert [l.state for l in run.legs[1:]] == [LegState.OPEN] * 3 and run.state == RunState.ACTIVE


def test_management_actions():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    st = fb.read_state()
    assert apply_action(run, "move_sl", 4400.0, st, fb, CFG)[0]["kind"] == "action_rejected"      # BUY: SL above bid is invalid
    assert apply_action(run, "move_sl", 4340.0, st, fb, CFG)[0]["kind"] == "action_rejected"      # worse than current 4341
    ev = apply_action(run, "move_sl", 4345.0, st, fb, CFG); pump(run, fb)
    assert all(l.sl_current == 4345.0 for l in run.legs) and len([e for e in ev if e["kind"] == "command_sent"]) == 4
    apply_action(run, "break_even", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert all(l.sl_current == 4347.0 for l in run.legs)
    apply_action(run, "close_all", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert run.state == RunState.DONE and all(l.state == LegState.CLOSED_MANUAL for l in run.legs) and fb.read_state().positions == []


def test_cancel_pending_action_keeps_open_legs():
    fb = bridge()
    run = mk(side=Side.SELL, entry_type=EntryType.LIMIT, zone=[4390.0], sl=4402.0, tps=(4385.0, 4380.0, 4375.0, 4370.0), mid=4)
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.fill_pending(run.legs[0].order_ticket); pump(run, fb)
    apply_action(run, "cancel_pending", None, fb.read_state(), fb, CFG); pump(run, fb)
    assert run.legs[0].state == LegState.OPEN and [l.state for l in run.legs[1:]] == [LegState.CANCELLED] * 3 and run.state == RunState.ACTIVE


def test_unwritable_bridge_cancels_legs_instead_of_crashing():
    fb, run = bridge(), mk()

    def denied(cmd):
        raise PermissionError(13, "Permission denied", "commands.jsonl")
    fb.send = denied
    events = place_run(run, CFG, fb.read_state(), fb, fb.now)
    assert [e["kind"] for e in events] == ["command_send_failed"] * 4
    assert all(l.state == LegState.CANCELLED and "not sent" in l.reason and l.inflight_cmd is None for l in run.legs)
    assert run.state == RunState.DONE


def test_unwritable_bridge_on_sl_move_is_journaled_not_raised():
    fb, run = bridge(), mk()
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    fb.send = lambda cmd: (_ for _ in ()).throw(PermissionError(13, "Permission denied"))
    fb.hit_tp(run.legs[1].position_ticket)
    events = pump(run, fb)
    assert [e["kind"] for e in events].count("command_send_failed") == 2
    assert all(l.inflight_cmd is None for l in run.legs)
    assert [l.state for l in run.legs[2:]] == [LegState.OPEN] * 2 and run.state == RunState.ACTIVE


def test_min_volume_floor_is_applied_and_journaled():
    fb = FakeBridge(now_local=NOW, balance=630)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    cfg = CFG.model_copy(update={"min_volume": {"XAUUSD": 0.02}})
    run = mk()
    events = place_run(run, cfg, fb.read_state(), fb, fb.now)
    assert [l.volume for l in run.legs] == [0.02] * 4
    raised = [e for e in events if e["kind"] == "leg_volume_raised"]
    assert len(raised) == 4 and raised[0]["by_risk"] == 0.01 and raised[0]["volume"] == 0.02


def test_pending_order_missing_from_a_lagging_snapshot_is_not_cancelled():
    # live 2026-09-25, Wolves #29931: results arrived before state.json listed the new orders, and
    # the legs were declared gone 0.5s after placement while the orders sat live at the broker.
    from datetime import timedelta
    from tg_signal_trader.ladder import MISSING_GRACE_SEC
    fb, run = bridge(), mk(entry_type=EntryType.LIMIT, zone=[4340.0, 4338.0], sl=4330.0, tps=(4345.0, 4350.0, 4355.0))
    stale = fb.read_state()                                   # snapshot taken before the orders exist
    place_run(run, CFG, stale, fb, fb.now)
    apply_results(run, fb.read_results())
    assert [l.state for l in run.legs] == [LegState.PENDING_ORDER] * 3
    ev = sync_run(run, CFG, stale, fb, fb.now)                # lagging snapshot: no orders listed
    assert not [e for e in ev if e["kind"] == "leg_cancelled"] and all(l.state == LegState.PENDING_ORDER for l in run.legs)
    ev = sync_run(run, CFG, fb.read_state(), fb, fb.now + timedelta(seconds=1))   # snapshot catches up
    assert all(l.state == LegState.PENDING_ORDER and l.missing_since is None for l in run.legs)
    # an order that really disappears is still cancelled once the grace has passed
    fb.expire_order(run.legs[0].order_ticket)
    t = fb.now + timedelta(seconds=2)
    sync_run(run, CFG, fb.read_state(), fb, t)
    sync_run(run, CFG, fb.read_state(), fb, t + timedelta(seconds=MISSING_GRACE_SEC + 1))
    assert run.legs[0].state == LegState.CANCELLED and run.legs[1].state == LegState.PENDING_ORDER


def test_second_cancel_message_does_not_resend_an_inflight_cancel():
    fb, run = bridge(), mk(entry_type=EntryType.LIMIT, zone=[4340.0, 4338.0], sl=4330.0, tps=(4345.0, 4350.0, 4355.0))
    place_run(run, CFG, fb.read_state(), fb, fb.now); pump(run, fb)
    first = apply_action(run, "cancel_pending", None, fb.read_state(), fb, CFG)
    second = apply_action(run, "cancel_pending", None, fb.read_state(), fb, CFG)
    assert len(first) == 3 and second == []
    pump(run, fb)
    assert all(l.state == LegState.CANCELLED and l.reason == "cancelled by command" for l in run.legs)
