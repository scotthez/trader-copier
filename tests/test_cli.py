from datetime import datetime, timezone
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.store import Store
from tg_signal_trader.bridge import FakeBridge
from tg_signal_trader.cli import bridge_ping, status_lines, bridge_test

LOCAL0 = datetime(2026, 9, 18, 13, 0, 0)


def fb():
    b = FakeBridge(now_local=LOCAL0, balance=12_345)
    b.set_quote("XAUUSD", 4346.8, 4347.0)
    return b


def test_bridge_ping_round_trip():
    b = fb()
    assert bridge_ping(b, timeout_sec=1, now_local=lambda: LOCAL0, sleep=lambda s: None) is not None
    assert b.sent[-1].type == "ping"


def test_bridge_ping_times_out_when_no_ea():
    class Silent(FakeBridge):
        def send(self, cmd): self.sent.append(cmd)   # never answers
    b = Silent(now_local=LOCAL0)
    assert bridge_ping(b, timeout_sec=0.01, now_local=lambda: LOCAL0, sleep=lambda s: None) is None


def test_status_lines_report_age_balance_symbols():
    cfg = AppConfig(providers={"wolves": ProviderConfig(telegram_chat=1, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})})
    lines = status_lines(cfg, Store(":memory:"), {"wolves": fb()}, now_local=lambda: LOCAL0)
    text = "\n".join(lines)
    assert "wolves" in text and "12345" in text and "XAUUSD" in text and "age 0.0s" in text and "OK" in text


def test_bridge_test_opens_modifies_closes():
    b = fb()
    lines = bridge_test(b, "XAUUSD", now_local=lambda: LOCAL0, sleep=lambda s: None)
    assert [c.type for c in b.sent] == ["open_market", "modify_sl", "close"] and b.read_state().positions == []
    assert b.sent[0].volume == 0.01 and all("OK" in l for l in lines)
