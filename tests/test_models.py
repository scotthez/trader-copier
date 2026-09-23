from datetime import datetime, timezone
import pytest
from tg_signal_trader.models import (Side, EntryType, LegState, RunState, Signal, Leg, SignalRun,
                                     complete_tps, make_signal_id, leg_comment)


def test_complete_tps_fills_open_tp4_by_last_increment():
    # A TP4 explicitly present but written as "Open" (a 4th element that is None) is synthesised.
    assert complete_tps([4381, 4386, 4391, None]) == [4381, 4386, 4391, 4396]
    assert complete_tps([4356.27, 4357.42, 4359.73, None]) == pytest.approx([4356.27, 4357.42, 4359.73, 4362.04])


def test_complete_tps_keeps_explicit_tp4():
    assert complete_tps([1, 2, 3, 10]) == [1, 2, 3, 10]


def test_complete_tps_stays_three_legs_when_tp4_never_mentioned():
    # No TP4 at all in the source (a 3-element list) stays a 3-leg run — no synthesized leg 4.
    assert complete_tps([4381, 4386, 4391]) == [4381, 4386, 4391]


def test_complete_tps_rejects_fewer_than_three():
    with pytest.raises(ValueError):
        complete_tps([1, 2])


def test_signal_id_and_comment():
    assert make_signal_id("wolves", 29501) == "wolves:29501"
    assert leg_comment("wolves:29501", 3) == "sig:wolves:29501:L3"


def test_signal_run_builds_four_legs():
    sig = Signal(id="lewis:1", provider="lewis", symbol="NAS100", side=Side.BUY, entry_type=EntryType.MARKET,
                 entry_zone=[], sl=29088.91, tps=[29212.15, 29240.59, 29306.95, 29373.31],
                 received_at=datetime(2026, 9, 17, 7, 45, 4, tzinfo=timezone.utc), raw_text="x", telegram_msg_id=1)
    run = SignalRun.from_signal(sig)
    assert run.state == RunState.NEW
    assert [l.n for l in run.legs] == [1, 2, 3, 4]
    assert [l.tp for l in run.legs] == sig.tps
    assert all(l.state == LegState.PLACING for l in run.legs)
    assert all(l.sl_current == 29088.91 for l in run.legs)
    assert run.open_legs() == [] and run.pending_legs() == []
    assert run.is_finished() is False
