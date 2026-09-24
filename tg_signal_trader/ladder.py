"""SignalRun state machine: placement, fill/close detection, the TP ladder, management actions."""
from __future__ import annotations
from datetime import datetime, timedelta
from .bridge import Bridge, BridgeState, Command, CommandResult, fmt_ts
from .config import ProviderConfig
from .models import Leg, LegState, RunState, Side, EntryType, SignalRun
from .sizing import leg_volume, sl_improves, sl_valid_vs_market

MISSING_GRACE_SEC = 30      # position absent from state without a deal → CLOSED_MANUAL after this


def _ev(kind: str, run: SignalRun, **detail) -> dict:
    return {"kind": kind, "run_id": run.id, **detail}


def _send(run: SignalRun, leg: Leg, bridge: Bridge, cmd_type: str, **fields) -> dict:
    cmd = Command(cmd_id=run.next_cmd_id(leg.n), type=cmd_type, **fields)
    leg.inflight_cmd, leg.inflight_kind = cmd.cmd_id, cmd_type
    leg.inflight_sl = fields.get("sl") if cmd_type == "modify_sl" else None
    try:
        bridge.send(cmd)
    except OSError as e:
        # The command never reached the EA (live miss, 2026-09-24: commands.jsonl not writable by the
        # service user). Raising here crash-looped the whole trader for every provider, retrying the
        # same signal until it went stale. Never wait for a result that cannot come: an open leg is
        # cancelled; any other command is journaled as failed and the leg keeps its current state.
        leg.inflight_cmd = leg.inflight_kind = leg.inflight_sl = None
        if cmd_type in ("open_market", "open_pending"):
            leg.state, leg.reason = LegState.CANCELLED, f"{cmd_type} not sent: {e}"
        return _ev("command_send_failed", run, cmd_id=cmd.cmd_id, type=cmd_type, leg=leg.n, error=str(e))
    return _ev("command_sent", run, cmd_id=cmd.cmd_id, type=cmd_type, leg=leg.n, fields={k: v for k, v in fields.items() if v is not None})


def _recompute_state(run: SignalRun) -> None:
    if run.state == RunState.REJECTED:
        return
    if run.is_finished():
        run.state = RunState.DONE
    elif run.placing_legs() and not run.open_legs() and not run.pending_legs():
        run.state = RunState.PLACING
    else:
        run.state = RunState.ACTIVE


def _local_naive(t: datetime) -> datetime:
    return t.astimezone().replace(tzinfo=None)


def place_run(run: SignalRun, cfg: ProviderConfig, state: BridgeState, bridge: Bridge, now_local: datetime) -> list[dict]:
    sig = run.signal
    broker_symbol = cfg.symbols[sig.symbol]
    spec = state.symbols[broker_symbol]
    is_buy = sig.side == Side.BUY
    if sig.entry_type == EntryType.LIMIT:
        entry_ref = sig.entry_zone[0]
    else:
        entry_ref = spec.ask if is_buy else spec.bid
    events: list[dict] = []
    expires = fmt_ts(_local_naive(sig.received_at) + timedelta(hours=cfg.pending_ttl_hours)) if sig.entry_type == EntryType.LIMIT else None
    for leg in run.legs:
        try:
            floor = cfg.min_volume.get(sig.symbol, 0.0)
            leg.volume = leg_volume(state.account.balance, cfg.risk_pct_per_leg, entry_ref, sig.sl, spec.sizing(), floor)
            if floor > 0:
                by_risk = leg_volume(state.account.balance, cfg.risk_pct_per_leg, entry_ref, sig.sl, spec.sizing())
                if leg.volume > by_risk:
                    events.append(_ev("leg_volume_raised", run, leg=leg.n, by_risk=by_risk, volume=leg.volume, min_volume=floor))
        except ValueError as e:
            leg.state, leg.reason = LegState.CANCELLED, f"sizing: {e}"
            events.append(_ev("leg_cancelled", run, leg=leg.n, reason=leg.reason))
            continue
        comment = leg.comment(sig.id)
        if sig.entry_type == EntryType.MARKET:
            events.append(_send(run, leg, bridge, "open_market", symbol=broker_symbol, side=sig.side.value, volume=leg.volume,
                                sl=sig.sl, tp=leg.tp, comment=comment))
        else:
            events.append(_send(run, leg, bridge, "open_pending", symbol=broker_symbol, side=sig.side.value, volume=leg.volume,
                                price=entry_ref, sl=sig.sl, tp=leg.tp, comment=comment, expires_at=expires))
    run.state = RunState.PLACING
    if not run.placing_legs():
        _recompute_state(run)
    return events


def apply_results(run: SignalRun, results: list[CommandResult]) -> list[dict]:
    events: list[dict] = []
    by_id = {r.cmd_id: r for r in results}
    for leg in run.legs:
        r = by_id.get(leg.inflight_cmd or "")
        if r is None:
            continue
        kind = leg.inflight_kind
        leg.inflight_cmd = leg.inflight_kind = None
        if not r.ok:
            events.append(_ev("command_failed", run, cmd_id=r.cmd_id, type=kind, leg=leg.n, retcode=r.retcode, text=r.retcode_text))
            if kind in ("open_market", "open_pending"):
                leg.state, leg.reason = LegState.CANCELLED, f"{kind} failed: {r.retcode} {r.retcode_text}"
            leg.inflight_sl = None
            continue
        if kind == "open_market":
            leg.state, leg.position_ticket, leg.entry_price = LegState.OPEN, r.position or r.order, r.fill_price or None
        elif kind == "open_pending":
            leg.state, leg.order_ticket = LegState.PENDING_ORDER, r.order
        elif kind == "modify_sl" and leg.inflight_sl is not None:
            leg.sl_current = leg.inflight_sl
        elif kind == "close":
            leg.state, leg.reason = LegState.CLOSED_MANUAL, "closed by command"
        elif kind == "cancel":
            leg.state, leg.reason = LegState.CANCELLED, "cancelled by command"
        leg.inflight_sl = None
    _recompute_state(run)
    return events


def _detect(run: SignalRun, state: BridgeState, now_local: datetime) -> list[dict]:
    events: list[dict] = []
    for leg in run.legs:
        comment = leg.comment(run.signal.id)
        if leg.state == LegState.PENDING_ORDER and leg.inflight_cmd is None:
            order = next((o for o in state.orders if o.ticket == leg.order_ticket), None)
            if order is None:
                pos = state.position_by_comment(comment)
                if pos is not None:
                    leg.state, leg.position_ticket, leg.entry_price = LegState.OPEN, pos.ticket, pos.price_open
                    events.append(_ev("leg_filled", run, leg=leg.n, price=pos.price_open))
                else:
                    leg.state, leg.reason = LegState.CANCELLED, "pending order gone (expired or cancelled at broker)"
                    events.append(_ev("leg_cancelled", run, leg=leg.n, reason=leg.reason))
        elif leg.state == LegState.OPEN and leg.inflight_cmd is None:
            pos = next((p for p in state.positions if p.ticket == leg.position_ticket), None)
            if pos is not None:
                leg.missing_since = None
                if leg.entry_price is None:
                    leg.entry_price = pos.price_open
                continue
            deals = state.out_deals_for(leg.position_ticket or -1)
            if deals:
                reason = deals[-1].reason
                leg.state = LegState.CLOSED_TP if reason == "TP" else LegState.CLOSED_SL if reason == "SL" else LegState.CLOSED_MANUAL
                leg.reason = f"deal reason {reason}"
                events.append(_ev("leg_closed", run, leg=leg.n, state=leg.state.value, price=deals[-1].price))
            elif leg.missing_since is None:
                leg.missing_since = now_local
            elif (now_local - leg.missing_since).total_seconds() > MISSING_GRACE_SEC:
                leg.state, leg.reason = LegState.CLOSED_MANUAL, "position vanished without a deal"
                events.append(_ev("leg_closed", run, leg=leg.n, state=leg.state.value))
    return events


def _modify(run: SignalRun, leg: Leg, bridge: Bridge, new_sl: float, why: str) -> dict:
    if not sl_improves(run.signal.side, new_sl, leg.sl_current):
        return _ev("sl_move_skipped", run, leg=leg.n, proposed=new_sl, current=leg.sl_current, why=why)
    return _send(run, leg, bridge, "modify_sl", position=leg.position_ticket, sl=new_sl)


def _ladder(run: SignalRun, bridge: Bridge) -> list[dict]:
    events: list[dict] = []
    legs = run.legs
    if len(legs) >= 2 and legs[1].state == LegState.CLOSED_TP and not run.be_applied:
        for leg in legs[2:]:
            if leg.state == LegState.OPEN and leg.entry_price:
                events.append(_modify(run, leg, bridge, leg.entry_price, "break-even after TP2"))
        run.be_applied = True
    if len(legs) >= 3 and legs[2].state == LegState.CLOSED_TP and not run.tp1_applied:
        for leg in legs[3:]:
            if leg.state == LegState.OPEN:
                events.append(_modify(run, leg, bridge, run.signal.tps[0], "SL to TP1 after TP3"))
        run.tp1_applied = True
    if any(l.state == LegState.CLOSED_SL for l in legs) and run.pending_legs() and not run.pendings_cancelled:
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    return events


def _expiry_backstop(run: SignalRun, cfg: ProviderConfig, bridge: Bridge, now_local: datetime) -> list[dict]:
    if run.signal.entry_type != EntryType.LIMIT:
        return []
    deadline = _local_naive(run.signal.received_at) + timedelta(hours=cfg.pending_ttl_hours, minutes=1)
    if now_local < deadline:
        return []
    return [_send(run, leg, bridge, "cancel", order=leg.order_ticket) for leg in run.pending_legs() if leg.inflight_cmd is None]


def _close_everything(run: SignalRun, bridge: Bridge) -> list[dict]:
    """Closes open legs and cancels pending ones, re-applied every tick so a leg still PLACING when
    the close was requested is closed as soon as its fill is known."""
    events: list[dict] = []
    for leg in run.legs:
        if leg.inflight_cmd is not None:
            continue
        if leg.state == LegState.OPEN:
            events.append(_send(run, leg, bridge, "close", position=leg.position_ticket))
        elif leg.state == LegState.PENDING_ORDER:
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
    return events


def sync_run(run: SignalRun, cfg: ProviderConfig, state: BridgeState, bridge: Bridge, now_local: datetime) -> list[dict]:
    events = _detect(run, state, now_local)
    if run.close_requested:
        events += _close_everything(run, bridge)
    else:
        events += _ladder(run, bridge)
        events += _expiry_backstop(run, cfg, bridge, now_local)
    _recompute_state(run)
    return events


def apply_action(run: SignalRun, action: str, price: float | None, state: BridgeState, bridge: Bridge, cfg: ProviderConfig) -> list[dict]:
    events: list[dict] = []
    if action == "close_all":
        for leg in run.open_legs():
            events.append(_send(run, leg, bridge, "close", position=leg.position_ticket))
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    elif action == "cancel_pending":
        for leg in run.pending_legs():
            events.append(_send(run, leg, bridge, "cancel", order=leg.order_ticket))
        run.pendings_cancelled = True
    elif action == "break_even":
        for leg in run.open_legs():
            if leg.entry_price:
                events.append(_modify(run, leg, bridge, leg.entry_price, "break-even by provider"))
    elif action == "move_sl":
        if price is None:
            return [_ev("action_rejected", run, action=action, why="no price")]
        quote = state.symbols[cfg.symbols[run.signal.symbol]].quote()
        if not sl_valid_vs_market(run.signal.side, price, quote):
            return [_ev("action_rejected", run, action=action, why=f"SL {price} on the wrong side of market {quote.bid}/{quote.ask}")]
        if not any(sl_improves(run.signal.side, price, l.sl_current) for l in run.open_legs()):
            return [_ev("action_rejected", run, action=action, why=f"SL {price} does not improve any leg")]
        for leg in run.open_legs():
            events.append(_modify(run, leg, bridge, price, "provider move_sl"))
    else:
        return [_ev("action_rejected", run, action=action, why="unknown action")]
    return events
