from datetime import datetime, timedelta, timezone
from tg_signal_trader.models import Signal, Side, EntryType
from tg_signal_trader.config import ProviderConfig
from tg_signal_trader.validation import validate_signal, Quote

NOW = datetime(2026, 9, 18, 13, 20, 30, tzinfo=timezone.utc)
CFG = ProviderConfig(telegram_chat=1, bridge_dir="/tmp/x", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})


def sig(**kw):
    base = dict(id="w:1", provider="wolves", symbol="XAUUSD", side=Side.SELL, entry_type=EntryType.MARKET, entry_zone=[],
                sl=4377.26, tps=[4369.94, 4368.76, 4366.42, 4360.56], received_at=NOW - timedelta(seconds=20), raw_text="", telegram_msg_id=1)
    base.update(kw)
    return Signal(**base)


Q = Quote(bid=4372.0, ask=4372.4)


def test_valid_sell_market():
    assert validate_signal(sig(), CFG, Q, NOW) == []


def test_sl_and_tp_sides():
    assert "sl_wrong_side" in validate_signal(sig(sl=4360), CFG, Q, NOW)
    assert "tp_wrong_side" in validate_signal(sig(tps=[4380, 4368.76, 4366.42, 4360.56]), CFG, Q, NOW)
    assert "tp_not_monotonic" in validate_signal(sig(tps=[4369.94, 4370.5, 4366.42, 4360.56]), CFG, Q, NOW)
    buy = sig(side=Side.BUY, sl=4365, tps=[4375, 4377, 4380, 4385])
    assert validate_signal(buy, CFG, Q, NOW) == []
    assert "sl_wrong_side" in validate_signal(sig(side=Side.BUY, sl=4380, tps=[4375, 4377, 4380, 4385]), CFG, Q, NOW)


def test_sl_distance_range():
    assert "sl_distance_out_of_range" in validate_signal(sig(sl=4500), CFG, Q, NOW)          # 128 pts > 60
    assert "sl_distance_out_of_range" in validate_signal(sig(sl=4372.6), CFG, Q, NOW)        # 0.2 pt < 1


def test_stale_and_distance():
    assert "stale" in validate_signal(sig(received_at=NOW - timedelta(seconds=200)), CFG, Q, NOW)
    lim = sig(entry_type=EntryType.LIMIT, entry_zone=[4390, 4395], sl=4402, tps=[4385, 4380, 4375, 4370])
    assert validate_signal(lim, CFG, Q, NOW) == []
    far = sig(entry_type=EntryType.LIMIT, entry_zone=[4450, 4455], sl=4462, tps=[4445, 4440, 4435, 4430])
    assert "limit_too_far" in validate_signal(far, CFG, Q, NOW)
    mk = sig(entry_zone=[4347], side=Side.BUY, sl=4341, tps=[4353, 4357, 4362, 4367])
    assert "market_too_far" in validate_signal(mk, CFG, Q, NOW)   # 4347 vs 4372 is 0.57% > 0.3%


def test_unmapped_symbol_and_no_quote():
    assert "symbol_unmapped" in validate_signal(sig(symbol="NAS100"), CFG, Q, NOW)
    assert "no_quote" in validate_signal(sig(), CFG, None, NOW)
