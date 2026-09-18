import textwrap
import pytest
from tg_signal_trader.config import load_config, Secrets


def test_llm_provider_defaults_and_openrouter(tmp_path):
    p = tmp_path / "c.yaml"
    base = "providers:\n  lewis:\n    telegram_chat: 1\n    bridge_dir: /tmp/x\n    symbols: {XAUUSD: XAUUSD}\n    sl_range: {XAUUSD: [1, 60]}\n"
    p.write_text(base)
    assert load_config(p).llm.provider == "anthropic"
    p.write_text(base + "llm: {provider: openrouter, model: openai/gpt-5-mini}\n")
    cfg = load_config(p)
    assert cfg.llm.provider == "openrouter" and cfg.llm.model == "openai/gpt-5-mini"
    p.write_text(base + "llm: {provider: gemini}\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_secrets_read_openrouter(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1"); monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key"); monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-5-mini")
    s = Secrets.from_env()
    assert s.openrouter_api_key == "or-key" and s.openrouter_model == "openai/gpt-5-mini"
