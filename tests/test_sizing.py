import pytest
from tg_signal_trader.models import Side
from tg_signal_trader.sizing import SizingSpec, leg_volume, sl_improves, sl_valid_vs_market
from tg_signal_trader.validation import Quote

GOLD = SizingSpec(volume_step=0.01, volume_min=0.01, volume_max=50, tick_value=1.0, tick_size=0.01)   # $1 per 0.01 per lot
NAS = SizingSpec(volume_step=0.1, volume_min=0.1, volume_max=100, tick_value=0.1, tick_size=0.1)     # $1 per point per lot


def test_leg_volume_gold():
    # risk 1% of 10,000 = $100; SL 6.0 → 600 ticks × $1 = $600 per lot → 0.1666 → floor to 0.16
    assert leg_volume(10_000, 1.0, 4347, 4341, GOLD) == pytest.approx(0.16)


def test_leg_volume_nas():
    # $100 risk; SL 123.24 points → $123.24 per lot → 0.81 → floor to step 0.1 → 0.8
    assert leg_volume(10_000, 1.0, 29212.15, 29088.91, NAS) == pytest.approx(0.8)


def test_leg_volume_clamps():
    assert leg_volume(100, 1.0, 4347, 4341, GOLD) == pytest.approx(0.01)       # below min → min
    assert leg_volume(10_000_000, 1.0, 4347, 4346.9, GOLD) == pytest.approx(50)  # above max → max


def test_leg_volume_rejects_zero_distance():
    with pytest.raises(ValueError):
        leg_volume(10_000, 1.0, 4347, 4347, GOLD)


def test_sl_one_way():
    assert sl_improves(Side.BUY, 4350, 4341) and not sl_improves(Side.BUY, 4340, 4341) and sl_improves(Side.BUY, 4340, 0)
    assert sl_improves(Side.SELL, 4370, 4377) and not sl_improves(Side.SELL, 4378, 4377)


def test_sl_vs_market():
    q = Quote(bid=4372.0, ask=4372.4)
    assert sl_valid_vs_market(Side.BUY, 4371, q) and not sl_valid_vs_market(Side.BUY, 4373, q)
    assert sl_valid_vs_market(Side.SELL, 4373, q) and not sl_valid_vs_market(Side.SELL, 4372, q)


def test_min_volume_floor_raises_small_risk_sizes():
    # live 2026-09-24: £630 account, 1% per leg, gold SL 4.75 away → 0.0176 → 0.01; user wants ≥ 0.02
    spec = SizingSpec(volume_step=0.01, volume_min=0.01, volume_max=50, tick_value=0.7555, tick_size=0.01)
    assert leg_volume(630, 1.0, 4268.30, 4263.55, spec) == pytest.approx(0.01)
    assert leg_volume(630, 1.0, 4268.30, 4263.55, spec, floor=0.02) == pytest.approx(0.02)
    assert leg_volume(10_000, 1.0, 4347, 4341, GOLD, floor=0.02) == pytest.approx(0.16)   # risk size already above the floor
    assert leg_volume(630, 1.0, 4268.30, 4263.55, spec, floor=0.015) == pytest.approx(0.02)  # floor rounds up to the step
    tiny_max = GOLD.model_copy(update={"volume_max": 0.01})
    assert leg_volume(100, 1.0, 4347, 4341, tiny_max, floor=0.02) == pytest.approx(0.01)   # broker max still wins
