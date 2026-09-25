from datetime import datetime, timedelta, timezone
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.models import InboxMessage, LegState, RunState, Signal, Side, EntryType
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.store import Store
from tg_signal_trader.classifier import MockClassifier, Classification, EntryExtraction
from tg_signal_trader.trader import Trader, DryRunBridge

UTC0 = datetime(2026, 9, 18, 13, 0, 0, tzinfo=timezone.utc)
LOCAL0 = datetime(2026, 9, 18, 13, 0, 0)
ENTRY = "BUY XAUUSD @4347\n\nSL 4341\nTP1 4353\nTP2 4357\nTP3 4362\nTP4 Open"


def make(management=None, entries=None, precheck_market=False, **provider_overrides):
    pc = dict(telegram_chat=-2, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})
    pc.update(provider_overrides)
    cfg = AppConfig(providers={"wolves": ProviderConfig(**pc)}, db_path=":memory:")
    store = Store(":memory:")
    fb = FakeBridge(now_local=LOCAL0, balance=10_000)
    fb.set_quote("XAUUSD", 4346.8, 4347.0)
    clock = {"utc": UTC0, "local": LOCAL0}
    cfg.llm.market_crosscheck_after = not precheck_market
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
    tr, store, fb, _ = make(entries={ENTRY: _ex(sl=4314)}, precheck_market=True)   # model read the SL differently
    inbox(store, ENTRY, 101); tr.tick()
    run = store.get_run("wolves:101")
    assert run.state == RunState.REJECTED and fb.sent == []
    ev = next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")
    assert "crosscheck:sl" in ev["detail"]["reasons"] and ev["detail"]["crosscheck"]["model"]["sl"] == 4314


def test_crosscheck_low_confidence_or_wrong_side_rejects():
    tr, store, fb, _ = make(entries={ENTRY: _ex(side="SELL")}, precheck_market=True)
    inbox(store, ENTRY, 102); tr.tick()
    assert "crosscheck:side" in next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")["detail"]["reasons"]
    tr2, store2, fb2, _ = make(entries={ENTRY: _ex(confidence=0.3)}, precheck_market=True)
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


def test_deferred_market_signal_is_not_revalidated_against_a_moved_quote():
    # Reproduces a real live miss (2026-09-22, Lewis XAUUSD): a bare MARKET signal (no entry_zone,
    # so validate_signal's sl/tp-side checks reference the LIVE quote) was deferred once for
    # terminal_stale, then re-validated on each retry against whatever the quote had become by then.
    # Gold spiked hard during the ~35s the retries spanned, running straight through the tight TP
    # ladder — so by the final retry, TP1-TP3 were now BELOW the live quote for a BUY, and the
    # perfectly good signal was permanently rejected as "tp_wrong_side". validate_signal (and the
    # entry crosscheck) must run once per signal and be cached, not re-litigated against a moving
    # target every time a transient guard forces a retry.
    tr, store, fb, clock = make(entries={"TRADE SETUP: BUY XAUUSD": _ex()})   # defaults already match sig below
    sig = Signal(id="wolves:200", provider="wolves", symbol="XAUUSD", side=Side.BUY, entry_type=EntryType.MARKET,
                 entry_zone=[], sl=4341.0, tps=[4353.0, 4357.0, 4362.0, 4367.0], received_at=UTC0,
                 raw_text="TRADE SETUP: BUY XAUUSD", telegram_msg_id=200)
    clock["local"] = LOCAL0 + timedelta(seconds=10)             # terminal reads stale: first attempt defers
    assert tr._handle_signal("wolves", sig, fb.read_state()) is False
    assert store.get_run("wolves:200") is None
    fb.set_quote("XAUUSD", 4364.8, 4365.0)                      # price has since rallied straight through every TP
    fb.now = clock["local"]                                     # terminal catches up: no longer stale
    assert tr._handle_signal("wolves", sig, fb.read_state()) is True
    tr.sync("wolves", fb.read_state())
    assert store.get_run("wolves:200").state == RunState.ACTIVE  # validated once, against the original quote
    assert tr.classifier.calls.count(("entry", "TRADE SETUP: BUY XAUUSD")) == 1  # crosscheck not repeated on retry


def test_slow_crosscheck_does_not_falsely_report_terminal_stale():
    # Real-world miss (2026-09-22, Wolves XAUUSD, all afternoon): the entry crosscheck is a real LLM
    # API call and routinely takes 8-14s. _handle_signal reads `state` once at the top of the tick,
    # then blocks inside that crosscheck call BEFORE ever checking guard_reasons() against it — so by
    # the time the terminal-health check runs, that same snapshot looks "stale" purely because of how
    # long the crosscheck took, even though the EA kept writing state.json every 500ms the entire time
    # and the bridge was perfectly healthy. Every signal needing a crosscheck was doomed to defer,
    # over and over, until it aged out — regardless of the terminal's real health.
    tr, store, fb, clock = make(entries={ENTRY: _ex()})
    real_crosscheck = tr._crosscheck

    def slow_crosscheck(provider, sig):
        clock["local"] += timedelta(seconds=8)   # simulate an 8s LLM round trip...
        fb.now += timedelta(seconds=8)           # ...during which the EA kept writing state.json
        return real_crosscheck(provider, sig)

    tr._crosscheck = slow_crosscheck
    inbox(store, ENTRY, 300)
    tr.tick(); tr.tick()
    assert store.get_run("wolves:300").state == RunState.ACTIVE
    assert not any(e["kind"] in ("guard_blocked", "signal_deferred") for e in store.journal_tail())


def test_ignore_patterns_short_circuit_boilerplate():
    boiler = "You can put your stop-loss to break-even if you wish, or keep it running if you want to maximise the profit potential! ✔️"
    tr, store, fb, _ = make(management={boiler: Classification(action="break_even", confidence=0.95)},
                            ignore_patterns=[r"(?i)you can put your stop-?loss to break-?even if you wish"])
    inbox(store, ENTRY, 110); tr.tick(); tr.tick()
    inbox(store, boiler, 111, reply_to=110); tr.tick(); tr.tick()
    assert ("management", boiler) not in tr.classifier.calls                       # never sent to the model
    assert all(l.sl_current == 4341.0 for l in store.get_run("wolves:110").legs)    # no BE applied
    assert any(e["kind"] == "management_ignored" for e in store.journal_tail())


def test_crosscheck_that_overruns_its_deadline_falls_back_to_template():
    # Live, Lewis NAS100 2026-09-24: a MARKET entry went out 61s after the post. The SDK timeout is per
    # network read, so a slow model reply could hold the order well past llm.timeout_sec.
    import time
    tr, store, fb, _ = make(entries={ENTRY: _ex()}, precheck_market=True)
    tr.cfg.llm.timeout_sec = 0.2
    real = tr.classifier.extract_entry
    tr.classifier.extract_entry = lambda text, provider: (time.sleep(1.0), real(text, provider))[1]
    inbox(store, ENTRY, 110)
    t0 = time.monotonic(); tr.tick()
    assert time.monotonic() - t0 < 0.8
    assert store.get_run("wolves:110").state in (RunState.PLACING, RunState.ACTIVE) and fb.sent
    ev = next(e for e in store.journal_tail() if e["kind"] == "entry_crosscheck_unavailable")
    assert "0.2s" in ev["detail"]["reason"] and ev["detail"]["crosscheck_sec"] < 0.8


def test_decisions_journal_signal_age_and_crosscheck_time():
    tr, store, fb, clock = make(entries={ENTRY: _ex()}, precheck_market=True)
    inbox(store, ENTRY, 111, ts=UTC0 - timedelta(seconds=7))
    tr.tick()
    j = {e["kind"]: e["detail"] for e in store.journal_tail()}
    assert j["signal_accepted"]["age_sec"] == 7.0
    assert isinstance(j["entry_crosscheck_ok"]["crosscheck_sec"], float)


# ---- MARKET entries: place first, crosscheck straight after (llm.market_crosscheck_after) ----------

LIMIT_ENTRY = "BUY LIMIT XAUUSD @4345 4343\n\nSL 4335\nTP1 4353\nTP2 4357\nTP3 4362"


def _settle(tr, n=20):
    """Ticks until the background crosscheck has been settled (MockClassifier answers at once)."""
    import time
    for _ in range(n):
        tr.tick()
        if not tr._postchecks:
            return
        time.sleep(0.01)
    raise AssertionError("postcheck never settled")


def test_market_entry_is_sent_before_the_model_answers():
    import threading
    gate = threading.Event()
    tr, store, fb, _ = make(entries={ENTRY: _ex()})
    real = tr.classifier.extract_entry
    tr.classifier.extract_entry = lambda text, provider: (gate.wait(5), real(text, provider))[1]
    inbox(store, ENTRY, 120); tr.tick()
    assert len([c for c in fb.sent if c.type == "open_market"]) == 4      # orders out, model still thinking
    assert not any(e["kind"].startswith("entry_crosscheck") for e in store.journal_tail())
    gate.set(); _settle(tr)
    ok = next(e for e in store.journal_tail() if e["kind"] == "entry_crosscheck_ok")
    assert ok["detail"]["after_placement"] is True
    assert store.get_run("wolves:120").state == RunState.ACTIVE and not store.get_run("wolves:120").close_requested


def test_market_entry_is_closed_when_the_model_disagrees():
    tr, store, fb, _ = make(entries={ENTRY: _ex(sl=4314)})
    inbox(store, ENTRY, 121); tr.tick(); _settle(tr); tr.tick()
    ev = next(e for e in store.journal_tail() if e["kind"] == "crosscheck_failed_closing")
    assert "crosscheck:sl" in ev["detail"]["reasons"] and ev["detail"]["crosscheck"]["model"]["sl"] == 4314
    tr.tick()
    run = store.get_run("wolves:121")
    assert [c.type for c in fb.sent].count("close") == 4 and fb.positions == []
    assert run.state == RunState.DONE and all(l.state == LegState.CLOSED_MANUAL for l in run.legs)


def test_disagreement_closes_legs_whose_fill_arrives_later():
    tr, store, fb, _ = make(entries={ENTRY: _ex(side="SELL")})
    fb_results = fb.read_results
    held: list = []
    fb.read_results = lambda: (held.extend(fb_results()), [])[1]            # fills not reported yet
    inbox(store, ENTRY, 122); tr.tick(); _settle(tr)
    assert store.get_run("wolves:122").close_requested and all(l.state == LegState.PLACING for l in store.get_run("wolves:122").legs)
    fb.read_results = lambda: (held + fb_results(), held.clear())[0]       # fills land now
    tr.tick(); tr.tick()
    assert fb.positions == [] and store.get_run("wolves:122").state == RunState.DONE


def test_unavailable_model_after_placement_leaves_the_trade_standing():
    tr, store, fb, _ = make()                                                # MockClassifier returns None
    inbox(store, ENTRY, 123); tr.tick(); _settle(tr); tr.tick()
    ev = next(e for e in store.journal_tail() if e["kind"] == "entry_crosscheck_unavailable")
    assert ev["detail"]["after_placement"] is True and store.get_run("wolves:123").state == RunState.ACTIVE


def test_slow_postcheck_times_out_and_the_trade_stands():
    import threading, time
    gate = threading.Event()
    tr, store, fb, _ = make(entries={ENTRY: _ex(sl=4314)})                  # would disagree, but answers too late
    tr.cfg.llm.timeout_sec = 0.1
    real = tr.classifier.extract_entry
    tr.classifier.extract_entry = lambda text, provider: (gate.wait(5), real(text, provider))[1]
    inbox(store, ENTRY, 124); tr.tick(); time.sleep(0.15); tr.tick()
    ev = next(e for e in store.journal_tail() if e["kind"] == "entry_crosscheck_unavailable")
    assert "0.1s" in ev["detail"]["reason"] and not tr._postchecks
    gate.set()
    assert not store.get_run("wolves:124").close_requested


def test_limit_entry_is_still_checked_before_placing():
    tr, store, fb, _ = make(entries={LIMIT_ENTRY: _ex(entry_type="LIMIT", entry_zone=[4345, 4343], sl=4330, tps=[4353, 4357, 4362])})
    inbox(store, LIMIT_ENTRY, 125); tr.tick()
    assert store.get_run("wolves:125").state == RunState.REJECTED and fb.sent == []
    assert "crosscheck:sl" in next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")["detail"]["reasons"]


def test_daily_loss_stop_zero_means_off():
    tr, store, fb, clock = make(daily_loss_stop_pct=0)
    fb.account.equity = 10_000
    tr.tick()                                                # start-of-day equity recorded
    fb.account.equity = 10_000                               # flat day: 0% down must not count as "reached 0%"
    inbox(store, ENTRY, 130); tr.tick()
    assert "daily_loss_stop" not in tr.guard_reasons("wolves", fb.read_state())
    fb.account.equity = 5_000                                # even -50% does not block when off
    assert "daily_loss_stop" not in tr.guard_reasons("wolves", fb.read_state())


def test_crosscheck_ignores_an_empty_tp_slot_from_tp_open():
    # live 2026-09-24, Wolves #29904: the model read "TP: Open" as an empty TP1 and shifted the real TPs
    tr, store, fb, _ = make(entries={LIMIT_ENTRY: _ex(entry_type="LIMIT", entry_zone=[4345, 4343], sl=4335, tps=[None, 4353, 4357, 4362])})
    inbox(store, LIMIT_ENTRY, 131); tr.tick()
    assert store.get_run("wolves:131").state != RunState.REJECTED and fb.sent


def test_crosscheck_still_rejects_a_misread_tp_number():
    tr, store, fb, _ = make(entries={LIMIT_ENTRY: _ex(entry_type="LIMIT", entry_zone=[4345, 4343], sl=4335, tps=[None, 4353.5, 4357, 4362])})
    inbox(store, LIMIT_ENTRY, 132); tr.tick()
    assert store.get_run("wolves:132").state == RunState.REJECTED and fb.sent == []
    assert "crosscheck:tps" in next(e for e in store.journal_tail() if e["kind"] == "signal_rejected")["detail"]["reasons"]


def test_repaired_tp_is_crosschecked_against_the_value_as_written():
    # the model reads "TP2 4260" as written; that must not count as a disagreement with the repaired 4270
    LIVE_29936 = ("Gold 🏆\nPair: XAUUSD 📊\nSide: Short / Sell Limit\nEntry: 4280 4283\nTP: Open\nSL: 4288\n\n"
                  "TP1 4275 50pips ✅\nTP2 4260 100pips ✅\nTP3 4265 150pips ✅")
    tr, store, fb, _ = make(entries={LIVE_29936: _ex(side="SELL", entry_type="LIMIT", entry_zone=[4280, 4283], sl=4288, tps=[None, 4275, 4260, 4265])},
                            sl_range={"XAUUSD": (1, 60)}, limit_max_distance_pct=5.0)
    fb.set_quote("XAUUSD", 4276.0, 4276.2)
    inbox(store, LIVE_29936, 29936); tr.tick()
    kinds = [e["kind"] for e in store.journal_tail()]
    assert "tp_corrected" in kinds and "signal_accepted" in kinds, kinds
    tps = sorted({c.tp for c in fb.sent if c.type == "open_pending"}, reverse=True)
    assert tps == [4275.0, 4270.0, 4265.0]
