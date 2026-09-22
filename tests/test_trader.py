from datetime import datetime, timedelta, timezone
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.models import InboxMessage, LegState, RunState
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.store import Store
from tg_signal_trader.classifier import MockClassifier, Classification, EntryExtraction
from tg_signal_trader.trader import Trader, DryRunBridge

UTC0 = datetime(2026, 9, 18, 13, 0, 0, tzinfo=timezone.utc)
LOCAL0 = datetime(2026, 9, 18, 13, 0, 0)
ENTRY = "BUY XAUUSD @4347\n\nSL 4341\nTP1 4353\nTP2 4357\nTP3 4362\nTP4 Open"


def make(management=None, entries=None, **provider_overrides):
    pc = dict(telegram_chat=-2, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})
    pc.update(provider_overrides)
    cfg = AppConfig(providers={"wolves": ProviderConfig(**pc)}, db_path=":memory:")
    store = Store(":memory:")
    fb = FakeBridge(now_local=LOCAL0, balance=10_000)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    clock = {"utc": UTC0, "local": LOCAL0}
    tr = Trader(cfg, store, {"wolves": fb}, MockClassifier(management, entries), now_utc=lambda: clock["utc"], now_local=lambda: clock["local"])
    return tr, store, fb, clock


def inbox(store, text, mid, ts=None, reply_to=None):
    store.add_inbox(InboxMessage(msg_id=mid, chat_id=-2, provider="wolves", text=text, ts=ts or UTC0 - timedelta(seconds=5), reply_to=reply_to))


def test_entry_message_becomes_active_run():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 10)
    tr.tick(); tr.tick()
    run = store.get_run("wolves:10")
    assert run.state == RunState.ACTIVE and [l.state for l in run.legs] == [LegState.OPEN] * 4
    assert store.new_inbox("wolves") == [] and any(e["kind"] == "signal_accepted" for e in store.journal_tail())


def test_stale_entry_is_not_traded():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 11, ts=UTC0 - timedelta(seconds=500))
    tr.tick()
    assert store.get_run("wolves:11").state == RunState.REJECTED and fb.sent == []
    assert any(e["kind"] == "signal_rejected" and "stale" in e["detail"]["reasons"] for e in store.journal_tail())


def test_guards_block_placement():
    tr, store, fb, clock = make(max_open_signals=1)
    inbox(store, ENTRY, 12); tr.tick(); tr.tick()
    inbox(store, ENTRY.replace("4347", "4348"), 13); tr.tick()
    assert store.get_run("wolves:13").state == RunState.REJECTED
    assert any(e["kind"] == "guard_blocked" and "max_open_signals" in e["detail"]["reasons"] for e in store.journal_tail())
    # stale terminal
    clock["local"] = LOCAL0 + timedelta(seconds=30)
    inbox(store, ENTRY, 14, ts=clock["utc"]); tr.tick()
    assert "terminal_stale" in tr.guard_reasons("wolves", fb.read_state())


def test_daily_loss_stop():
    tr, store, fb, _ = make(daily_loss_stop_pct=5.0)
    tr.tick()                                   # records start-of-day equity 10,000
    fb.account.equity = 9_400
    inbox(store, ENTRY, 15); tr.tick()
    assert store.get_run("wolves:15").state == RunState.REJECTED and fb.sent == []


def test_management_reply_applies_to_referenced_run():
    tr, store, fb, _ = make(management={"Delete this": Classification(action="close_all", confidence=0.95, reason="delete")})
    inbox(store, ENTRY, 20); tr.tick(); tr.tick()
    inbox(store, "Delete this", 21, reply_to=20); tr.tick(); tr.tick()
    assert store.get_run("wolves:20").state == RunState.DONE and fb.read_state().positions == []


def test_management_without_reply_uses_single_active_run_only():
    tr, store, fb, _ = make(management={"im be now": Classification(action="break_even", confidence=0.9)}, max_open_signals=3)
    inbox(store, ENTRY, 30); tr.tick(); tr.tick()
    inbox(store, "im be now", 31); tr.tick(); tr.tick()
    assert all(l.sl_current == 4347.0 for l in store.get_run("wolves:30").legs)
    inbox(store, ENTRY.replace("4347", "4348"), 32); tr.tick(); tr.tick()
    inbox(store, "im be now", 33); tr.tick()
    assert any(e["kind"] == "management_ambiguous" for e in store.journal_tail())


def test_low_confidence_and_disallowed_actions_are_journal_only():
    tr, store, fb, _ = make(management={"maybe close": Classification(action="close_all", confidence=0.5)}, management_actions=["move_sl"])
    inbox(store, ENTRY, 40); tr.tick(); tr.tick()
    inbox(store, "maybe close", 41, reply_to=40); tr.tick()
    assert store.get_run("wolves:40").state == RunState.ACTIVE
    kinds = [e["kind"] for e in store.journal_tail()]
    assert "management_skipped" in kinds


def test_llm_entry_fallback_goes_through_validation():
    ex = EntryExtraction(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.9)
    tr, store, fb, _ = make(entries={"gold long sl 4341 tp: 4353 tp: 4357 tp: 4362": ex})
    inbox(store, "gold long sl 4341 tp: 4353 tp: 4357 tp: 4362", 50); tr.tick(); tr.tick()
    run = store.get_run("wolves:50")
    assert run is not None and run.signal.parsed_by == "llm" and run.state == RunState.ACTIVE


def test_startup_reconcile_adopts_by_comment():
    tr, store, fb, _ = make()
    inbox(store, ENTRY, 60); tr.tick()
    run = store.get_run("wolves:60")
    # Simulate a crash after the commands were sent but before their results were read:
    # the positions exist on the account, the run still thinks it is placing.
    for leg in run.legs:
        leg.state, leg.position_ticket, leg.entry_price, leg.inflight_cmd, leg.inflight_kind = LegState.PLACING, None, None, None, None
    run.state = RunState.PLACING
    store.save_run(run)
    tr2 = Trader(tr.cfg, store, {"wolves": fb}, MockClassifier(), now_utc=tr.now_utc, now_local=tr.now_local)
    tr2.startup_reconcile()
    run = store.get_run("wolves:60")
    assert run.state == RunState.ACTIVE and all(l.state == LegState.OPEN and l.position_ticket for l in run.legs)


def test_dry_run_bridge_never_sends():
    tr, store, fb, _ = make()
    tr.bridges["wolves"] = DryRunBridge(fb, store, "wolves")
    inbox(store, ENTRY, 70); tr.tick(); tr.tick()
    assert fb.sent == [] and sum(1 for e in store.journal_tail() if e["kind"] == "dry_run_command") == 4


def test_management_passes_quoted_text_to_classifier():
    tr, store, fb, _ = make(management={"Delete this": Classification(action="close_all", confidence=0.95)})
    inbox(store, ENTRY, 80); tr.tick(); tr.tick()
    inbox(store, "Delete this", 81, reply_to=80); tr.tick()
    # MockClassifier records calls; extend it to capture kwargs via a small subclass check
    assert tr.classifier.calls[-1] == ("management", "Delete this")


def test_arming_blocks_real_account_until_live_true():
    tr, store, fb, _ = make()
    fb.account.trade_mode = "REAL"
    inbox(store, ENTRY, 90); tr.tick(); tr.tick()
    assert fb.sent == [] and store.get_run("wolves:90").state == RunState.REJECTED
    assert any(e["kind"] == "guard_blocked" and "real_account_not_armed" in e["detail"]["reasons"] for e in store.journal_tail())
    assert any(e["kind"] == "arming_blocked" for e in store.journal_tail())


def test_arming_blocks_login_mismatch_everywhere():
    tr, store, fb, _ = make(management={"Delete this": Classification(action="close_all", confidence=0.95)})
    inbox(store, ENTRY, 91); tr.tick(); tr.tick()                       # placed while login matches (0 = any)
    assert len(fb.sent) == 4
    tr.cfg.providers["wolves"].expected_login = 999                      # now the terminal is the wrong account
    inbox(store, "Delete this", 92, reply_to=91); tr.tick(); tr.tick()
    assert len(fb.sent) == 4 and fb.read_state().positions != []       # no close was sent
    assert any(e["kind"] == "management_skipped" and "login_mismatch" in str(e["detail"]) for e in store.journal_tail())
    fb.hit_tp(fb.read_state().positions[1].ticket); fb.hit_tp(fb.read_state().positions[0].ticket); tr.tick(); tr.tick()
    assert not any(c.type == "modify_sl" for c in fb.sent)             # ladder moves are not sent either


def test_armed_live_account_trades():
    tr, store, fb, _ = make(live=True, expected_login=1)
    fb.account.trade_mode = "REAL"
    inbox(store, ENTRY, 93); tr.tick(); tr.tick()
    assert len(fb.sent) == 4 and store.get_run("wolves:93").state == RunState.ACTIVE


def _ex(**kw):
    base = dict(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.95)
    base.update(kw)
    return EntryExtraction(**base)


def test_crosscheck_agreement_trades():
    tr, store, fb, _ = make(entries={ENTRY: _ex()})
    inbox(store, ENTRY, 100); tr.tick(); tr.tick()
    assert store.get_run("wolves:100").state == RunState.ACTIVE and ("entry", ENTRY) in tr.classifier.calls
    assert any(e["kind"] == "entry_crosscheck_ok" for e in store.journal_tail())


def test_crosscheck_mismatch_rejects():
    tr, store, fb, _ = make(entries={ENTRY: _ex(sl=4314)})           # model read the SL differently
    inbox(store, ENTRY, 101); tr.tick()
    run = store.get_run("wolves:101")
    assert run.state == RunState.REJECTED and fb.sent == []
    ev = next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")
    assert "crosscheck:sl" in ev["detail"]["reasons"] and ev["detail"]["crosscheck"]["model"]["sl"] == 4314


def test_crosscheck_low_confidence_or_wrong_side_rejects():
    tr, store, fb, _ = make(entries={ENTRY: _ex(side="SELL")})
    inbox(store, ENTRY, 102); tr.tick()
    assert "crosscheck:side" in next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")["detail"]["reasons"]
    tr2, store2, fb2, _ = make(entries={ENTRY: _ex(confidence=0.3)})
    inbox(store2, ENTRY, 103); tr2.tick()
    assert "crosscheck:confidence" in next(e for e in store2.journal_tail() if e["kind"] == "signal_rejected")["detail"]["reasons"]


def test_crosscheck_unavailable_falls_back_to_template():
    tr, store, fb, _ = make()                                            # MockClassifier returns None → unavailable
    inbox(store, ENTRY, 104); tr.tick(); tr.tick()
    assert store.get_run("wolves:104").state == RunState.ACTIVE
    assert any(e["kind"] == "entry_crosscheck_unavailable" for e in store.journal_tail())


def test_crosscheck_can_be_disabled():
    tr, store, fb, _ = make(entries={ENTRY: _ex(sl=4314)})
    tr.cfg.llm.entry_crosscheck = False
    inbox(store, ENTRY, 105); tr.tick(); tr.tick()
    assert store.get_run("wolves:105").state == RunState.ACTIVE and ("entry", ENTRY) not in tr.classifier.calls


def test_terminal_stale_defers_then_places_once_fresh():
    tr, store, fb, clock = make()
    clock["local"] = LOCAL0 + timedelta(seconds=10)          # terminal state (still stamped LOCAL0) now reads stale
    inbox(store, ENTRY, 120); tr.tick()
    assert store.get_run("wolves:120") is None               # deferred, not rejected: no run created yet
    assert store.new_inbox("wolves") != []                   # message left 'new' so it is retried
    assert any(e["kind"] == "signal_deferred" and "terminal_stale" in e["detail"]["reasons"] for e in store.journal_tail())
    fb.now = clock["local"]                                  # terminal catches up
    tr.tick(); tr.tick()
    assert store.get_run("wolves:120").state == RunState.ACTIVE


def test_terminal_stale_only_defers_once_per_message():
    tr, store, fb, clock = make()
    clock["local"] = LOCAL0 + timedelta(seconds=10)
    inbox(store, ENTRY, 121); tr.tick(); tr.tick(); tr.tick()
    assert sum(1 for e in store.journal_tail() if e["kind"] == "signal_deferred") == 1


def test_permanent_guard_still_rejects_immediately_even_when_terminal_is_stale():
    tr, store, fb, clock = make()
    tr.cfg.providers["wolves"].expected_login = 999           # wrong account: not a transient condition
    clock["local"] = LOCAL0 + timedelta(seconds=10)            # ALSO stale, mixed with a permanent guard
    inbox(store, ENTRY, 122); tr.tick()
    run = store.get_run("wolves:122")
    assert run is not None and run.state == RunState.REJECTED and fb.sent == []
    ev = next(e for e in store.journal_tail() if e["kind"] == "guard_blocked")
    assert "login_mismatch" in ev["detail"]["reasons"] and "terminal_stale" in ev["detail"]["reasons"]


def test_signal_that_never_recovers_eventually_rejected_as_stale():
    tr, store, fb, clock = make()
    clock["local"] = LOCAL0 + timedelta(seconds=10)            # terminal never catches up in this test
    inbox(store, ENTRY, 123); tr.tick()
    assert store.get_run("wolves:123") is None                # deferred first
    clock["utc"] = UTC0 + timedelta(seconds=200)               # now well past max_signal_age_sec (120s)
    clock["local"] = LOCAL0 + timedelta(seconds=210)           # terminal still stale too
    tr.tick()
    run = store.get_run("wolves:123")
    assert run.state == RunState.REJECTED
    ev = next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")
    assert "stale" in ev["detail"]["reasons"]


def test_ignore_patterns_short_circuit_boilerplate():
    boiler = "You can put your stop-loss to break-even if you wish, or keep it running if you want to maximise the profit potential! ✔️"
    tr, store, fb, _ = make(management={boiler: Classification(action="break_even", confidence=0.95)},
                            ignore_patterns=[r"(?i)you can put your stop-?loss to break-?even if you wish"])
    inbox(store, ENTRY, 110); tr.tick(); tr.tick()
    inbox(store, boiler, 111, reply_to=110); tr.tick(); tr.tick()
    assert ("management", boiler) not in tr.classifier.calls                       # never sent to the model
    assert all(l.sl_current == 4341.0 for l in store.get_run("wolves:110").legs)    # no BE applied
    assert any(e["kind"] == "management_ignored" for e in store.journal_tail())
