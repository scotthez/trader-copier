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


def test_make_classifier_selects_provider():
    from tg_signal_trader.cli import make_classifier
    from tg_signal_trader.config import Secrets, LlmConfig
    from tg_signal_trader.classifier import ClaudeClassifier
    from tg_signal_trader.classifier_openrouter import OpenRouterClassifier
    sec = Secrets(telegram_api_id=1, telegram_api_hash="h", anthropic_api_key="a", openrouter_api_key="o", openrouter_model="openai/gpt-5-mini")
    pc = ProviderConfig(telegram_chat=1, bridge_dir="/tmp/w", symbols={"XAUUSD": "XAUUSD"}, sl_range={"XAUUSD": (1, 60)})
    assert isinstance(make_classifier(AppConfig(providers={"w": pc}), sec), ClaudeClassifier)
    c = make_classifier(AppConfig(providers={"w": pc}, llm=LlmConfig(provider="openrouter", model="x")), sec)
    assert isinstance(c, OpenRouterClassifier) and c.model == "openai/gpt-5-mini"   # env model overrides config


def test_classify_eval_runs_classifier_over_non_entry_messages(tmp_path):
    from pathlib import Path
    from tg_signal_trader.cli import classify_eval
    from tg_signal_trader.classifier import MockClassifier, Classification
    (tmp_path / "messages.html").write_text((Path(__file__).parent / "data" / "export_sample.html").read_text())
    mc = MockClassifier(management={"TP1 HIT! ✔️": Classification(action="none", confidence=0.99, reason="tp hit")})
    lines = []
    counts = classify_eval(mc, "lewis", tmp_path, n=10, echo=lines.append)
    assert counts == {"none": 1} and [t for k, t in mc.calls] == ["TP1 HIT! ✔️"]
