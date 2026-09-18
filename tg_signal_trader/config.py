"""config.yaml (no secrets) and environment secrets."""
from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml

ALLOWED_ACTIONS = ["close_all", "cancel_pending", "move_sl", "break_even"]


class ProviderConfig(BaseModel):
    telegram_chat: int
    bridge_dir: Path
    risk_pct_per_leg: float = 1.0
    symbols: dict[str, str]                         # canonical -> broker symbol
    sl_range: dict[str, tuple[float, float]]        # canonical -> (min, max) SL distance in price units
    market_entry_tolerance_pct: float = 0.3
    limit_max_distance_pct: float = 1.0
    max_signal_age_sec: int = 120
    pending_ttl_hours: int = 24
    management_actions: list[str] = Field(default_factory=lambda: list(ALLOWED_ACTIONS))
    max_open_signals: int = 2
    max_legs_open: int = 8
    daily_loss_stop_pct: float = 5.0

    @field_validator("management_actions")
    @classmethod
    def _known_actions(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ALLOWED_ACTIONS]
        if bad:
            raise ValueError(f"unknown management_actions {bad}; allowed: {ALLOWED_ACTIONS}")
        return v


class LlmConfig(BaseModel):
    provider: str = "anthropic"          # anthropic | openrouter
    model: str = "claude-opus-5"
    confidence_threshold: float = 0.8
    timeout_sec: float = 8.0

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in ("anthropic", "openrouter"):
            raise ValueError("llm.provider must be 'anthropic' or 'openrouter'")
        return v


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    llm: LlmConfig = Field(default_factory=LlmConfig)
    db_path: Path = Path("tg_signal_trader.sqlite")
    session_path: Path = Path("tg_listener.session")
    poll_interval_sec: float = 0.5


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


class Secrets(BaseModel):
    telegram_api_id: int | None = None       # required only by listen / resolve-chats
    telegram_api_hash: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None      # overrides llm.model when llm.provider is openrouter

    @classmethod
    def from_env(cls) -> "Secrets":
        api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        return cls(telegram_api_id=int(api_id) if api_id else None,
                   telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", "").strip() or None,
                   anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
                   openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
                   openrouter_model=os.environ.get("OPENROUTER_MODEL") or None)

    def require_telegram(self) -> "Secrets":
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set (my.telegram.org → API development tools)")
        return self
