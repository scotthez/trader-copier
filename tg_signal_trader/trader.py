"""The trader loop: inbox → signals/management → runs → bridge commands."""
from __future__ import annotations
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from .bridge import Bridge, BridgeState, Command, CommandResult
from .classifier import Classifier, RunContext
from .config import AppConfig
from .ladder import place_run, apply_results, sync_run, apply_action, _recompute_state
from .models import InboxMessage, LegState, RunState, Signal, SignalRun, Side, EntryType, complete_tps, make_signal_id
from .normalize import canonical_symbol
from .parsers import get_parser, looks_like_entry
from .store import Store
from .validation import validate_signal, Quote

STALE_STATE_SEC = 5.0

# Guards that are purely a moment's bridge/connectivity hiccup and self-resolve within a poll cycle
# or two. A signal blocked ONLY by one of these is deferred (left unprocessed, retried next tick)
# rather than permanently rejected — the terminal genuinely recovering a second later must not
# throw away an otherwise-good signal. Every other guard (wrong account, disarmed, risk limits) is a
# real reason not to trade and stays a permanent rejection. The natural bound against retrying
# forever is validate_signal's own 'stale' check (the signal's own age vs max_signal_age_sec) — if
# the terminal never recovers, the signal eventually ages out there and is rejected for real.
TRANSIENT_GUARDS = frozenset({"terminal_stale"})


class _Background:
    """fn() running on a daemon thread. A call that overruns its deadline is abandoned there and its
    result discarded."""

    def __init__(self, fn: Callable):
        self.started = time.monotonic()
        self._box: dict = {}
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable) -> None:
        try:
            self._box["value"] = fn()
        except BaseException as e:   # re-raised on the caller's thread by result()
            self._box["error"] = e

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def done(self) -> bool:
        return not self._thread.is_alive()

    def wait(self, sec: float) -> None:
        self._thread.join(max(0.0, sec))

    def result(self):
        if "error" in self._box:
            raise self._box["error"]
        return self._box.get("value")


def _call_with_deadline(fn: Callable, deadline_sec: float):
    """Runs fn() and returns its result, or raises TimeoutError once deadline_sec of wall-clock time
    has passed. The SDKs' own timeout is per network read, not per call: a response that keeps
    trickling bytes (OpenRouter pads slow non-streaming replies with whitespace), plus max_retries,
    can hold a MARKET entry far past llm.timeout_sec."""
    job = _Background(fn)
    job.wait(deadline_sec)
    if not job.done():
        raise TimeoutError(f"no answer within {deadline_sec:g}s")
    return job.result()


class DryRunBridge:
    """Journals commands instead of sending them; fabricates ok results so state machines advance."""

    def __init__(self, inner: Bridge, store: Store, provider: str):
        self.inner, self.store, self.provider = inner, store, provider
        self._results: list[CommandResult] = []
        self._ticket = 900_000

    def send(self, cmd: Command) -> None:
        self.store.journal(self.provider, "dry_run_command", cmd.model_dump(exclude_none=True))
        self._ticket += 1
        self._results.append(CommandResult(cmd_id=cmd.cmd_id, ok=True, retcode=0, retcode_text="dry-run",
                                           position=self._ticket, order=self._ticket, fill_price=cmd.price or 0.0))

    def read_results(self) -> list[CommandResult]:
        out, self._results = self._results, []
        return out

    def read_state(self) -> BridgeState | None:
        return self.inner.read_state()


class Trader:
    def __init__(self, cfg: AppConfig, store: Store, bridges: dict[str, Bridge], classifier: Classifier,
                 now_utc: Callable[[], datetime] | None = None, now_local: Callable[[], datetime] | None = None):
        self.cfg, self.store, self.bridges, self.classifier = cfg, store, bridges, classifier
        self.now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self.now_local = now_local or datetime.now
        self.parsers = {p: get_parser(p) for p in cfg.providers}
        self._blocked_notice: dict[str, str] = {}
        self._deferred_logged: set[tuple[str, int]] = set()
        self._tp_corrected_logged: set[str] = set()   # (provider, msg_id) already journaled as deferred
        # The quote and entry-crosscheck result seen the FIRST time a signal is evaluated, kept per
        # signal id across any TRANSIENT_GUARDS-triggered retries. validate_signal()'s sl/tp-side and
        # distance checks reference the live quote when entry_zone is empty (a bare MARKET order), so
        # re-running them fresh after a deferral re-litigates the signal against wherever price has
        # since moved — in a fast market that falsely rejects a signal that was perfectly sane when it
        # arrived (live miss, Lewis XAUUSD, 2026-09-22: rejected 35s later as "tp_wrong_side" after gold
        # spiked through the ladder during two terminal_stale retries). validate_signal() itself still
        # runs fresh on every attempt so its own time-based 'stale' check (and trade_not_allowed) keep
        # working as the real backstop; only the price reference and the (costly, deterministic) LLM
        # crosscheck are frozen. Entries cleared once the run is finalised either way.
        self._signal_quote_cache: dict[str, Quote] = {}
        self._signal_crosscheck_cache: dict[str, tuple[list[str], dict | None]] = {}
        # MARKET entries placed before their crosscheck (llm.market_crosscheck_after): signal id →
        # (provider, signal, background model call). Held in memory only; a trader restart before the
        # answer lands leaves the template reading standing, as when the model is unavailable.
        self._postchecks: dict[str, tuple[str, Signal, _Background]] = {}

    # ---- arming ---------------------------------------------------------
    def arming_block(self, provider: str, state: BridgeState | None) -> str | None:
        """Why no command may be sent to this provider's terminal right now (None = armed)."""
        if state is None:
            return None
        cfg = self.cfg.providers[provider]
        if cfg.expected_login and state.account.login != cfg.expected_login:
            return f"login_mismatch: terminal is {state.account.login}, config expects {cfg.expected_login}"
        if state.account.trade_mode == "REAL" and not cfg.live:
            return "real_account_not_armed: set live: true and expected_login for this provider"
        return None

    # ---- loop -----------------------------------------------------------
    def tick(self) -> None:
        for provider in self.cfg.providers:
            state = self.bridges[provider].read_state()
            if state is not None:
                self._track_start_of_day(provider, state)
            block = self.arming_block(provider, state)
            if block and self._blocked_notice.get(provider) != block:
                self._blocked_notice[provider] = block
                self.store.journal(provider, "arming_blocked", {"reason": block, "login": state.account.login, "trade_mode": state.account.trade_mode})
            elif not block:
                self._blocked_notice.pop(provider, None)
            self.process_inbox(provider, state)
            self._finish_postchecks(provider)
            self.sync(provider, state)

    def sync(self, provider: str, state: BridgeState | None) -> None:
        bridge, cfg = self.bridges[provider], self.cfg.providers[provider]
        results = bridge.read_results()
        armed = self.arming_block(provider, state) is None
        for run in self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE]):
            events = apply_results(run, results)
            if state is not None and armed:
                events += sync_run(run, cfg, state, bridge, self.now_local())
            for e in events:
                self.store.journal(provider, e.pop("kind"), e, run_id=run.id)
            self.store.save_run(run)

    # ---- inbox -----------------------------------------------------------
    def process_inbox(self, provider: str, state: BridgeState | None) -> None:
        parser = self.parsers[provider]
        for msg in self.store.new_inbox(provider):
            prev = self.store.recent_inbox(provider, msg.msg_id, 3)
            result = parser.parse(msg, prev)
            sig = result.signal
            if sig is None and looks_like_entry(msg.text):
                sig = self._llm_entry(msg, provider)
            if sig is not None:
                if sig.tp_corrections and sig.id not in self._tp_corrected_logged:
                    self._tp_corrected_logged.add(sig.id)
                    self.store.journal(provider, "tp_corrected", {"signal": sig.id, "as_written": {f"TP{i + 1}": v for i, v in sig.tp_corrections.items()},
                                                                  "used": {f"TP{i + 1}": sig.tps[i] for i in sig.tp_corrections}}, run_id=sig.id)
                if not self._handle_signal(provider, sig, state):
                    continue   # deferred: leave the inbox message 'new' so it is retried next tick
            elif looks_like_entry(msg.text):
                self.store.journal(provider, "signal_unparsed", {"msg_id": msg.msg_id, "reason": result.rejected_reason or "llm declined", "text": msg.text[:300]})
            elif msg.text.strip():
                if self._ignored(provider, msg.text):
                    self.store.journal(provider, "management_ignored", {"msg_id": msg.msg_id, "text": msg.text[:200]})
                else:
                    self._handle_management(provider, msg, state, prev)
            self.store.set_inbox_status(provider, msg.msg_id, "processed")

    def _ignored(self, provider: str, text: str) -> bool:
        import re
        return any(re.search(p, text) for p in self.cfg.providers[provider].ignore_patterns)

    def _llm_entry(self, msg: InboxMessage, provider: str) -> Signal | None:
        ex = self.classifier.extract_entry(msg.text, provider)
        if ex is None or ex.confidence < self.cfg.llm.confidence_threshold:
            self.store.journal(provider, "llm_entry_rejected", {"msg_id": msg.msg_id, "extraction": ex.model_dump() if ex else None})
            return None
        sym = canonical_symbol(ex.symbol)
        if sym is None:
            return None
        try:
            tps = complete_tps(ex.tps)
        except ValueError:
            return None
        return Signal(id=make_signal_id(provider, msg.msg_id), provider=provider, symbol=sym, side=Side(ex.side),
                      entry_type=EntryType(ex.entry_type), entry_zone=ex.entry_zone, sl=ex.sl, tps=tps,
                      received_at=msg.ts, raw_text=msg.text, telegram_msg_id=msg.msg_id, parsed_by="llm")

    # ---- signals --------------------------------------------------------
    def guard_reasons(self, provider: str, state: BridgeState | None) -> list[str]:
        cfg = self.cfg.providers[provider]
        reasons: list[str] = []
        if state is None:
            return ["no_state"]
        if state.age_sec(self.now_local()) > STALE_STATE_SEC:
            reasons.append("terminal_stale")
        block = self.arming_block(provider, state)
        if block:
            reasons.append(block.split(":")[0])
        live = self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE])
        if len(live) >= cfg.max_open_signals:
            reasons.append("max_open_signals")
        if sum(len(r.open_legs()) + len(r.pending_legs()) + len(r.placing_legs()) for r in live) >= cfg.max_legs_open:
            reasons.append("max_legs_open")
        sod = self.store.kv_get(self._sod_key(provider))
        if cfg.daily_loss_stop_pct > 0 and sod and float(sod) > 0 and (float(sod) - state.account.equity) / float(sod) * 100 >= cfg.daily_loss_stop_pct:
            reasons.append("daily_loss_stop")
        return reasons

    def _sod_key(self, provider: str) -> str:
        return f"sod_equity:{provider}:{self.now_local().date().isoformat()}"

    def _track_start_of_day(self, provider: str, state: BridgeState) -> None:
        key = self._sod_key(provider)
        if self.store.kv_get(key) is None:
            self.store.kv_set(key, str(state.account.equity))

    def _crosscheck(self, provider: str, sig: Signal) -> tuple[list[str], dict | None]:
        """Independent model reading of a template-parsed entry. Returns (mismatched fields, model dump)."""
        t0 = time.monotonic()
        try:
            ex = _call_with_deadline(lambda: self.classifier.extract_entry(sig.raw_text, provider), self.cfg.llm.timeout_sec)
            why = None if ex is not None else "no answer"
        except TimeoutError as e:
            ex, why = None, str(e)
        sec = round(time.monotonic() - t0, 2)
        if ex is None:
            self.store.journal(provider, "entry_crosscheck_unavailable", {"signal": sig.id, "reason": why, "crosscheck_sec": sec})
            return [], None
        return self._compare(sig, ex, sec)

    def _compare(self, sig: Signal, ex, sec: float) -> tuple[list[str], dict]:
        """Fields where the model's reading disagrees with the template's. Returns (mismatches, model dump)."""
        model = ex.model_dump()
        model["crosscheck_sec"] = sec
        bad: list[str] = []
        if ex.confidence < self.cfg.llm.confidence_threshold:
            bad.append("crosscheck:confidence")
        if canonical_symbol(ex.symbol) != sig.symbol:
            bad.append("crosscheck:symbol")
        if ex.side != sig.side.value:
            bad.append("crosscheck:side")
        if ex.entry_type != sig.entry_type.value:
            bad.append("crosscheck:entry_type")
        if abs(ex.sl - sig.sl) > 1e-6:
            bad.append("crosscheck:sl")
        # Compare the TP numbers the model found, in order, ignoring empty slots: the model may put a
        # "TP: Open" line in a slot of its own (live, Wolves 2026-09-24: [None, 4257, 4252, 4247] vs the
        # template's [4257, 4252, 4247]; every number agreed, yet the trade was rejected).
        # The model reads the message as written, so it is compared with the TPs as written; a TP the
        # parser repaired from its pips note (tp_corrections) is compared at its original value.
        written = [sig.tp_corrections.get(i, t) for i, t in enumerate(sig.tps)]
        model_tps = [t for t in ex.tps if t is not None][:3]
        if len(model_tps) < 3 or any(abs(a - b) > 1e-6 for a, b in zip(model_tps, written[:3])):
            bad.append("crosscheck:tps")
        if sig.entry_zone and ex.entry_zone and abs(ex.entry_zone[0] - sig.entry_zone[0]) > 1e-6:
            bad.append("crosscheck:entry_zone")
        return bad, model

    def _age(self, sig: Signal) -> float:
        """Seconds from the Telegram post to now — the delay the order pays for, journaled on every decision."""
        return round((self.now_utc() - sig.received_at).total_seconds(), 1)

    def _handle_signal(self, provider: str, sig: Signal, state: BridgeState | None) -> bool:
        """Returns True once the signal is handled for good (placed or permanently rejected), False
        if it was deferred and must be retried on a later tick (see TRANSIENT_GUARDS)."""
        cfg, bridge = self.cfg.providers[provider], self.bridges[provider]
        run = SignalRun.from_signal(sig)
        broker_symbol = cfg.symbols.get(sig.symbol)
        spec = state.symbols.get(broker_symbol) if (state and broker_symbol) else None
        live_quote = spec.quote() if spec else None
        if live_quote is not None:
            quote = self._signal_quote_cache.setdefault(sig.id, live_quote)
        else:
            quote = self._signal_quote_cache.get(sig.id)
        fails = validate_signal(sig, cfg, quote, self.now_utc())
        crosscheck: dict | None = None
        check = self.cfg.llm.entry_crosscheck and sig.parsed_by != "llm"
        postcheck = check and sig.entry_type == EntryType.MARKET and self.cfg.llm.market_crosscheck_after
        if check and not postcheck:
            cached = self._signal_crosscheck_cache.get(sig.id)
            if cached is None:
                cached = self._crosscheck(provider, sig)
                self._signal_crosscheck_cache[sig.id] = cached
            bad, model = cached
            fails.extend(bad)
            if model is not None:
                crosscheck = {"template": {"symbol": sig.symbol, "side": sig.side.value, "entry_type": sig.entry_type.value, "sl": sig.sl, "tps": sig.tps}, "model": model}
                if not bad:
                    self.store.journal(provider, "entry_crosscheck_ok", {"signal": sig.id, "crosscheck_sec": model.get("crosscheck_sec")}, run_id=run.id)
        if spec is not None and not spec.trade_allowed:
            fails.append("trade_not_allowed")
        if fails:
            run.state = RunState.REJECTED
            self.store.save_run(run)
            self.store.journal(provider, "signal_rejected", {"reasons": fails, "signal": sig.model_dump(mode="json"), "crosscheck": crosscheck,
                                                             "age_sec": self._age(sig)}, run_id=run.id)
            self._signal_quote_cache.pop(sig.id, None)
            self._signal_crosscheck_cache.pop(sig.id, None)
            return True
        # Re-read the bridge fresh right here rather than reusing `state` (captured at the top of this
        # tick, before the crosscheck call above): the crosscheck is a real network round trip to the
        # LLM and routinely takes several seconds, so by now `state` can look stale purely because of
        # how long that call took — not because the terminal ever stopped writing state.json. Checking
        # freshness against a just-taken read avoids blaming the terminal for our own processing time
        # (live miss, Wolves XAUUSD, 2026-09-22: every crosscheck-needing signal that afternoon was
        # deferred on a false terminal_stale and eventually timed out, never once for a real reason).
        state = self.bridges[provider].read_state()
        guards = self.guard_reasons(provider, state)
        if guards:
            if set(guards) <= TRANSIENT_GUARDS:
                key = (provider, sig.telegram_msg_id)
                if key not in self._deferred_logged:
                    self._deferred_logged.add(key)
                    self.store.journal(provider, "signal_deferred", {"reasons": guards, "signal_id": sig.id})
                return False
            run.state = RunState.REJECTED
            self.store.save_run(run)
            self.store.journal(provider, "guard_blocked", {"reasons": guards, "signal": sig.model_dump(mode="json")}, run_id=run.id)
            self._signal_quote_cache.pop(sig.id, None)
            self._signal_crosscheck_cache.pop(sig.id, None)
            return True
        events = place_run(run, cfg, state, bridge, self.now_local())
        self.store.save_run(run)
        self.store.journal(provider, "signal_accepted", {"signal": sig.model_dump(mode="json"), "volumes": [l.volume for l in run.legs],
                                                         "age_sec": self._age(sig)}, run_id=run.id)
        for e in events:
            self.store.journal(provider, e.pop("kind"), e, run_id=run.id)
        if postcheck:
            self._postchecks[sig.id] = (provider, sig, _Background(lambda: self.classifier.extract_entry(sig.raw_text, provider)))
        self._signal_quote_cache.pop(sig.id, None)
        self._signal_crosscheck_cache.pop(sig.id, None)
        return True

    def _finish_postchecks(self, provider: str) -> None:
        """Settles the crosschecks of MARKET entries that were placed first. Agreement or no answer →
        the trade stands. Disagreement → the run is flagged close_requested, and sync closes every leg
        (including legs whose fill arrives later) and cancels anything pending."""
        timeout = self.cfg.llm.timeout_sec
        for sig_id, (prov, sig, job) in list(self._postchecks.items()):
            if prov != provider or (not job.done() and job.elapsed() < timeout):
                continue
            del self._postchecks[sig_id]
            sec = round(job.elapsed(), 2)
            ex, why = None, "no answer"
            if not job.done():
                why = f"no answer within {timeout:g}s"
            else:
                try:
                    ex = job.result()
                except Exception as e:   # a classifier bug must not take the trader down
                    why = f"error: {type(e).__name__}"
            if ex is None:
                self.store.journal(provider, "entry_crosscheck_unavailable", {"signal": sig_id, "reason": why, "crosscheck_sec": sec, "after_placement": True}, run_id=sig_id)
                continue
            bad, model = self._compare(sig, ex, sec)
            if not bad:
                self.store.journal(provider, "entry_crosscheck_ok", {"signal": sig_id, "crosscheck_sec": sec, "after_placement": True}, run_id=sig_id)
                continue
            run = self.store.get_run(sig_id)
            if run is not None:
                run.close_requested = True
                self.store.save_run(run)
            template = {"symbol": sig.symbol, "side": sig.side.value, "entry_type": sig.entry_type.value, "sl": sig.sl, "tps": sig.tps}
            self.store.journal(provider, "crosscheck_failed_closing", {"reasons": bad, "crosscheck": {"template": template, "model": model}}, run_id=sig_id)

    # ---- management -----------------------------------------------------
    def resolve_reference(self, provider: str, msg: InboxMessage) -> SignalRun | None:
        if msg.reply_to is not None:
            run = self.store.run_by_msg_id(provider, msg.reply_to)
            if run is not None and run.state in (RunState.PLACING, RunState.ACTIVE):
                return run
        active = self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE])
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            self.store.journal(provider, "management_ambiguous", {"msg_id": msg.msg_id, "active_runs": [r.id for r in active], "text": msg.text[:300]})
        return None

    def _handle_management(self, provider: str, msg: InboxMessage, state: BridgeState | None, prev: list[InboxMessage]) -> None:
        cfg, bridge = self.cfg.providers[provider], self.bridges[provider]
        run = self.resolve_reference(provider, msg)
        if run is None:
            self.store.journal(provider, "management_no_reference", {"msg_id": msg.msg_id, "text": msg.text[:300]})
            return
        ctx = RunContext(symbol=run.signal.symbol, side=run.signal.side.value, entry_type=run.signal.entry_type.value,
                         entry_zone=run.signal.entry_zone, sl=run.signal.sl, open_legs=len(run.open_legs()),
                         pending_legs=len(run.pending_legs()), sl_current=max((l.sl_current for l in run.open_legs()), default=run.signal.sl))
        quoted = self.store.get_inbox(provider, msg.reply_to) if msg.reply_to is not None else None
        c = self.classifier.classify_management(msg.text, provider, ctx, [p.text[:200] for p in prev],
                                                reply_text=quoted.text[:600] if quoted else None)
        block = self.arming_block(provider, state)
        executable = (c.action != "none" and c.confidence >= self.cfg.llm.confidence_threshold
                      and c.action in cfg.management_actions and state is not None and block is None)
        self.store.save_classification(provider, msg.msg_id, msg.text, c.action, c.price, c.confidence, c.reason, executable)
        if not executable:
            self.store.journal(provider, "management_skipped", {"msg_id": msg.msg_id, "classification": c.model_dump(), "arming": block}, run_id=run.id)
            return
        events = apply_action(run, c.action, c.price, state, bridge, cfg)
        self.store.save_run(run)
        self.store.journal(provider, "management_applied", {"msg_id": msg.msg_id, "classification": c.model_dump()}, run_id=run.id)
        for e in events:
            self.store.journal(provider, e.pop("kind"), e, run_id=run.id)

    # ---- startup --------------------------------------------------------
    def startup_reconcile(self) -> None:
        for provider in self.cfg.providers:
            state = self.bridges[provider].read_state()
            if state is None:
                continue
            for run in self.store.runs(provider, [RunState.PLACING, RunState.ACTIVE]):
                changed = False
                for leg in run.legs:
                    if leg.state not in (LegState.PLACING, LegState.PENDING_ORDER, LegState.OPEN):
                        continue
                    comment = leg.comment(run.signal.id)
                    pos, order = state.position_by_comment(comment), state.order_by_comment(comment)
                    if pos is not None and leg.state != LegState.OPEN:
                        leg.state, leg.position_ticket, leg.entry_price, leg.inflight_cmd, leg.inflight_kind = LegState.OPEN, pos.ticket, pos.price_open, None, None
                        changed = True
                    elif order is not None and leg.state == LegState.PLACING:
                        leg.state, leg.order_ticket, leg.inflight_cmd, leg.inflight_kind = LegState.PENDING_ORDER, order.ticket, None, None
                        changed = True
                if changed:
                    _recompute_state(run)
                    self.store.save_run(run)
                    self.store.journal(provider, "reconciled", {"legs": [l.state.value for l in run.legs]}, run_id=run.id)
