import os, textwrap
import pytest
from tg_signal_trader.config import load_config, Secrets


def test_load_config_parses_providers(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
      providers:
        lewis:
          telegram_chat: -100123
          bridge_dir: /tmp/lewis
          risk_pct_per_leg: 1.0
          symbols: {NAS100: NAS100, XAUUSD: XAUUSD}
          sl_range: {NAS100: [5, 500], XAUUSD: [1, 60]}
        wolves:
          telegram_chat: -100456
          bridge_dir: /tmp/wolves
          symbols: {XAUUSD: GOLD}
          sl_range: {XAUUSD: [1, 60]}
      llm: {model: claude-opus-5}
      db_path: /tmp/x.sqlite
    """))
    cfg = load_config(p)
    assert set(cfg.providers) == {"lewis", "wolves"}
    lw = cfg.providers["lewis"]
    assert lw.risk_pct_per_leg == 1.0 and lw.max_signal_age_sec == 120 and lw.pending_ttl_hours == 24
    assert lw.management_actions == ["close_all", "cancel_pending", "move_sl", "break_even"]
    assert lw.max_open_signals == 2 and lw.max_legs_open == 8 and lw.daily_loss_stop_pct == 5.0
    assert cfg.providers["wolves"].symbols["XAUUSD"] == "GOLD"
    assert cfg.llm.model == "claude-opus-5" and cfg.llm.confidence_threshold == 0.8 and cfg.llm.timeout_sec == 8


def test_load_config_rejects_unknown_management_action(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("providers:\n  lewis:\n    telegram_chat: 1\n    bridge_dir: /tmp/x\n    symbols: {XAUUSD: XAUUSD}\n    sl_range: {XAUUSD: [1, 60]}\n    management_actions: [close_all, secure_half]\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Secrets.from_env()
    assert s.telegram_api_id == 12345 and s.telegram_api_hash == "abc" and s.anthropic_api_key is None
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    s = Secrets.from_env()
    assert s.telegram_api_id is None
    with pytest.raises(SystemExit):
        s.require_telegram()
